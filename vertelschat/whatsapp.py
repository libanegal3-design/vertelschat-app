"""WhatsApp Business Platform (Cloud API) integration.

Endpoints used (Graph API, version configurable, default v25.0):
  POST /{phone-number-id}/messages   send text / interactive buttons / template / reaction / read receipt
  GET  /{media-id}                   media metadata (url valid ~5 minutes, mime_type, sha256, file_size)
  GET  <media url>                   binary download with the same bearer token
Webhooks: GET verification (hub.challenge) and POST notifications signed with X-Hub-Signature-256.

Identity: every inbound message carries a business-scoped user id (from_user_id / contacts[].user_id).
The phone number (from / wa_id) can be absent for users with a WhatsApp username, so identities are
matched on BSUID first and on a keyed hash of the phone number second. Phone numbers are never stored
in plain text: the raw payload is scrubbed before it is persisted."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .jobs import enqueue
from .models import WhatsAppIdentity, WhatsAppMessage
from .security import encrypt_phone, phone_hash, verify_meta_signature

log = logging.getLogger("vertelschat.whatsapp")
GRAPH_BASE = "https://graph.facebook.com"
MAX_INBOUND_MEDIA_BYTES = 100 * 1024 * 1024
# Throttling / temporary errors that are safe to retry (see Cloud API error codes reference).
RETRIABLE_CODES = {1, 2, 4, 17, 80007, 130429, 131000, 131016, 131048, 131056, 133004, 133005}


class WhatsAppError(Exception):
    def __init__(self, message: str, *, code=None, http_status: int | None = None, retriable: bool = False,
                 unknown_outcome: bool = False, details: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retriable = retriable
        self.unknown_outcome = unknown_outcome
        self.details = details


class SignatureError(Exception):
    pass


@dataclass
class Recipient:
    phone: str = ""  # digits, international format without '+'
    bsuid: str = ""

    def address(self) -> dict:
        if self.phone:
            return {"to": self.phone}
        if self.bsuid:
            # Sending to a business-scoped user id (for users without a shared phone number).
            return {"recipient": self.bsuid}
        raise WhatsAppError("geen WhatsApp-adres bekend", retriable=False)


def sanitize_param(text: str, limit: int = 900) -> str:
    """Template body parameters may not contain newlines, tabs or 4+ consecutive spaces."""
    flat = " ".join((text or "").replace("\t", " ").split())
    return flat[:limit]


def _base(r: Recipient) -> dict:
    return {"messaging_product": "whatsapp", "recipient_type": "individual", **r.address()}


def text_message(r: Recipient, body: str) -> dict:
    return {**_base(r), "type": "text", "text": {"body": body[:4096], "preview_url": False}}


def buttons_message(r: Recipient, body: str, buttons: list[tuple[str, str]], footer: str = "") -> dict:
    interactive: dict = {"type": "button", "body": {"text": body[:1024]},
                         "action": {"buttons": [{"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}}
                                                for bid, title in buttons[:3]]}}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    return {**_base(r), "type": "interactive", "interactive": interactive}


def template_message(r: Recipient, name: str, lang: str, params: list[str],
                     quick_reply_payloads: list[str] | None = None) -> dict:
    components = [{"type": "body", "parameters": [{"type": "text", "text": sanitize_param(p)} for p in params]}]
    for i, payload in enumerate(quick_reply_payloads or []):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                           "parameters": [{"type": "payload", "payload": payload}]})
    return {**_base(r), "type": "template",
            "template": {"name": name, "language": {"code": lang}, "components": components}}


def reaction_message(r: Recipient, wamid: str, emoji: str = "\u2764\ufe0f") -> dict:
    return {**_base(r), "type": "reaction", "reaction": {"message_id": wamid, "emoji": emoji}}


# --------------------------------------------------------------------------- backends
class CloudBackend:
    name = "cloud"

    def __init__(self) -> None:
        s = get_settings()
        self.token = s.whatsapp_token
        self.phone_number_id = s.whatsapp_phone_number_id
        self.version = s.whatsapp_api_version
        self.client = httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _require_config(self) -> None:
        if not self.token or not self.phone_number_id:
            raise WhatsAppError("WhatsApp is niet geconfigureerd (WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID)",
                                retriable=True, code="not_configured")

    @staticmethod
    def _error(r: httpx.Response) -> WhatsAppError:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code")
        retriable = r.status_code >= 500 or r.status_code == 429 or code in RETRIABLE_CODES
        return WhatsAppError(err.get("message") or f"HTTP {r.status_code}", code=code, http_status=r.status_code,
                             retriable=retriable, details=json.dumps(err)[:800])

    def send(self, payload: dict) -> str:
        self._require_config()
        url = f"{GRAPH_BASE}/{self.version}/{self.phone_number_id}/messages"
        try:
            r = self.client.post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # request never reached Meta: safe to retry
            raise WhatsAppError(f"verbinding mislukt: {exc}", retriable=True) from exc
        except httpx.HTTPError as exc:  # may or may not have been delivered: never auto-resend
            raise WhatsAppError(f"onbekende uitkomst: {exc}", unknown_outcome=True) from exc
        if r.status_code >= 400:
            raise self._error(r)
        return r.json()["messages"][0]["id"]

    def mark_read(self, wamid: str) -> None:
        try:
            self.send({"messaging_product": "whatsapp", "status": "read", "message_id": wamid})
        except (WhatsAppError, KeyError):
            pass  # best effort; the blue ticks are a courtesy, not a guarantee

    def media_info(self, media_id: str) -> dict:
        self._require_config()
        try:
            r = self.client.get(f"{GRAPH_BASE}/{self.version}/{media_id}", headers=self._headers())
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"media-info mislukt: {exc}", retriable=True) from exc
        if r.status_code >= 400:
            raise self._error(r)
        return r.json()

    def download(self, url: str, dest: Path) -> int:
        size = 0
        try:
            with self.client.stream("GET", url, headers=self._headers(), timeout=120.0) as r:
                if r.status_code >= 400:
                    raise WhatsAppError(f"download HTTP {r.status_code}", http_status=r.status_code,
                                        retriable=True)
                with open(dest, "wb") as fh:
                    for chunk in r.iter_bytes(1024 * 256):
                        size += len(chunk)
                        if size > MAX_INBOUND_MEDIA_BYTES:
                            raise WhatsAppError("bestand groter dan 100 MB", retriable=False, code=131052)
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"download mislukt: {exc}", retriable=True) from exc
        return size


class MockBackend:
    """Local development and tests: records outbound messages; media come from var/dev_media."""
    name = "mock"

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.read: list[str] = []
        self.fail_send: list[WhatsAppError] = []
        self.fail_media: dict[str, int] = {}
        self.media_dir = get_settings().var_dir / "dev_media"
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def send(self, payload: dict) -> str:
        if self.fail_send:
            raise self.fail_send.pop(0)
        wamid = "wamid.MOCK" + uuid.uuid4().hex[:24].upper()
        self.sent.append({"wamid": wamid, **payload})
        return wamid

    def mark_read(self, wamid: str) -> None:
        self.read.append(wamid)

    def media_info(self, media_id: str) -> dict:
        remaining = self.fail_media.get(media_id, 0)
        if remaining:
            self.fail_media[media_id] = remaining - 1
            raise WhatsAppError("tijdelijke fout (gesimuleerd)", retriable=True, http_status=503)
        meta_path = self.media_dir / f"{media_id}.json"
        if not meta_path.exists():
            raise WhatsAppError("media niet gevonden of verlopen", code=100, http_status=404)
        meta = json.loads(meta_path.read_text())
        return {"id": media_id, "url": f"mock://{media_id}", **meta}

    def download(self, url: str, dest: Path) -> int:
        media_id = url.removeprefix("mock://")
        src = self.media_dir / f"{media_id}.bin"
        shutil.copyfile(src, dest)
        return dest.stat().st_size


def register_dev_media(path: Path, mime: str, corrupt_checksum: bool = False) -> tuple[str, str]:
    """Put a file into the dev media store as if WhatsApp hosted it. Returns (media_id, sha256_b64)."""
    media_dir = get_settings().var_dir / "dev_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media_id = str(uuid.uuid4().int)[:16]
    data = Path(path).read_bytes()
    sha_b64 = base64.b64encode(hashlib.sha256(data).digest()).decode()
    (media_dir / f"{media_id}.bin").write_bytes(data)
    reported = "AAAA" + sha_b64[4:] if corrupt_checksum else sha_b64  # simulate a checksum that never matches
    meta = {"mime_type": mime, "sha256": reported, "file_size": len(data), "messaging_product": "whatsapp"}
    (media_dir / f"{media_id}.json").write_text(json.dumps(meta))
    return media_id, reported


_backend = None


def get_backend():
    global _backend
    if _backend is None:
        _backend = CloudBackend() if get_settings().whatsapp_backend == "cloud" else MockBackend()
    return _backend


def set_backend(backend) -> None:
    global _backend
    _backend = backend


# --------------------------------------------------------------------------- inbound webhook ingestion
def parse_ts(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def extract_text(m: dict) -> str:
    t = m.get("type")
    if t == "text":
        return (m.get("text") or {}).get("body", "")
    if t == "button":
        return (m.get("button") or {}).get("text", "")
    if t == "interactive":
        inter = m.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title", "")
    if t in ("image", "video", "document"):
        return (m.get(t) or {}).get("caption", "")
    return ""


def extract_button_id(m: dict) -> str:
    if m.get("type") == "interactive":
        inter = m.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("id", "")
    if m.get("type") == "button":
        return (m.get("button") or {}).get("payload", "")
    return ""


def resolve_identity(session: Session, phone: str | None, bsuid: str | None, profile_name: str = "") -> WhatsAppIdentity:
    ident = None
    if bsuid:
        ident = session.scalar(select(WhatsAppIdentity).where(WhatsAppIdentity.bsuid == bsuid))
    ph = phone_hash(phone) if phone else None
    if ident is None and ph:
        ident = session.scalar(select(WhatsAppIdentity).where(WhatsAppIdentity.phone_hash == ph))
    if ident is None:
        ident = WhatsAppIdentity()
        session.add(ident)
    if bsuid and not ident.bsuid:
        ident.bsuid = bsuid
    if ph and ident.phone_hash != ph:
        other = session.scalar(select(WhatsAppIdentity).where(WhatsAppIdentity.phone_hash == ph,
                                                              WhatsAppIdentity.id != ident.id))
        if other is not None:  # number moved to another WhatsApp account: detach it from the old identity
            other.phone_hash = None
            other.phone_enc = None
            session.flush()
        ident.phone_hash = ph
        ident.phone_enc = encrypt_phone(phone)
    if profile_name:
        ident.profile_name = profile_name[:200]
    ident.last_inbound_at = utcnow()
    session.flush()
    return ident


def _match_contact(contacts: list, m: dict) -> dict:
    for c in contacts:
        if (m.get("from") and c.get("wa_id") == m.get("from")) or \
           (m.get("from_user_id") and c.get("user_id") == m.get("from_user_id")):
            return c
    return contacts[0] if len(contacts) == 1 else {}


def _scrub(m: dict) -> dict:
    clean = {k: v for k, v in m.items() if k not in ("from",)}
    if clean.get("type") == "contacts":
        clean["contacts"] = "[niet opgeslagen]"
    return clean


def _store_inbound(session: Session, m: dict, contact: dict) -> bool:
    wamid = m.get("id")
    if not wamid:
        return False
    if session.scalar(select(WhatsAppMessage.id).where(WhatsAppMessage.wamid == wamid)):
        return False  # duplicate delivery (Meta retries until it gets a 200)
    phone = m.get("from") or contact.get("wa_id")
    bsuid = m.get("from_user_id") or contact.get("user_id")
    profile = (contact.get("profile") or {}).get("name", "")
    ident = resolve_identity(session, phone, bsuid, profile)
    ctx = m.get("context") or {}
    msg = WhatsAppMessage(
        direction="in", wamid=wamid, identity_id=ident.id, msg_type=m.get("type", ""), context_wamid=ctx.get("id"),
        forwarded=bool(ctx.get("forwarded") or ctx.get("frequently_forwarded")),
        body_text=extract_text(m)[:4000],
        payload={"message": _scrub(m), "profile_name": profile, "has_phone": bool(phone)},
        wa_timestamp=parse_ts(m.get("timestamp")), status="received")
    try:
        with session.begin_nested():
            session.add(msg)
            session.flush()
    except IntegrityError:
        return False
    enqueue(session, "process_inbound", {"message_id": msg.id}, dedupe_key=f"inbound:{wamid}")
    return True


def ingest_webhook(session: Session, raw: bytes, signature: str | None) -> dict:
    """Verify, persist and enqueue. Called by the HTTP route and by the local simulator (same code path)."""
    if not verify_meta_signature(raw, signature):
        raise SignatureError("ongeldige handtekening")
    payload = json.loads(raw.decode("utf-8"))
    counts = {"messages": 0, "duplicates": 0, "statuses": 0, "other": 0}
    if payload.get("object") != "whatsapp_business_account":
        counts["other"] += 1
        return counts
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            field = change.get("field")
            value = change.get("value") or {}
            if field == "messages":
                contacts = value.get("contacts") or []
                for m in value.get("messages") or []:
                    if _store_inbound(session, m, _match_contact(contacts, m)):
                        counts["messages"] += 1
                    else:
                        counts["duplicates"] += 1
                for st in value.get("statuses") or []:
                    slim = {k: st.get(k) for k in ("id", "status", "timestamp", "errors", "pricing", "recipient_user_id")}
                    enqueue(session, "process_status", {"status": slim},
                            dedupe_key=f"status:{st.get('id')}:{st.get('status')}")
                    counts["statuses"] += 1
                for err in value.get("errors") or []:
                    log.warning("WhatsApp webhook meldt fout: %s", err)
            elif field == "user_id_update":
                enqueue(session, "process_identity_update", {"value": value})
                counts["other"] += 1
            else:
                counts["other"] += 1
    return counts


# --------------------------------------------------------------------------- building webhook payloads
def build_webhook(messages: list[dict] | None = None, statuses: list[dict] | None = None, *, phone: str = "",
                  bsuid: str = "", name: str = "") -> dict:
    """Cloud-API-shaped notification (used by the dev simulator and tests to exercise the real pipeline)."""
    s = get_settings()
    value: dict = {"messaging_product": "whatsapp",
                   "metadata": {"display_phone_number": s.whatsapp_link_number or "31200000000",
                                "phone_number_id": s.whatsapp_phone_number_id or "DEV_PHONE_NUMBER_ID"}}
    if messages:
        contact: dict = {"profile": {"name": name or "Onbekend"}}
        if phone:
            contact["wa_id"] = phone
        if bsuid:
            contact["user_id"] = bsuid
        value["contacts"] = [contact]
        for m in messages:
            if phone:
                m.setdefault("from", phone)
            if bsuid:
                m.setdefault("from_user_id", bsuid)
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": s.whatsapp_waba_id or "DEV_WABA", "changes": [{"value": value, "field": "messages"}]}]}


def new_wamid() -> str:
    return "wamid.SIM" + uuid.uuid4().hex.upper()
