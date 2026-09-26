"""WhatsApp pipeline job handlers.

inbound message -> process_inbound -> (download_media -> prepare_audio -> acknowledge + transcribe
-> compose_story) ; scheduler -> send_prompt ; delivery statuses -> process_status.

Rules implemented here (see architecture/whatsapp-flow.md):
- a recording is stored before anything else happens and is never deleted by the pipeline;
- the storyteller only hears back after the audio is safely stored (thanks twice, then a heart reaction);
- mapping: explicit reply > open 3-hour session > latest unanswered question > 7-day addendum > free story;
  anything ambiguous is flagged for the family instead of being silently attached;
- forwarded audio, missing consent and multi-project ambiguity put recordings on hold, not in the bin;
- after the paid period recordings are still stored and downloadable; processing continues for 30 days only
  for questions that were sent during the period."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import copy_nl
from .ai import ComposeError, ComposeInput, LocalComposer, TranscriptionError, compose_checked, get_transcriber
from .analytics import duration_bucket, track
from .audio import AudioError, probe, split_for_transcription, to_mp3
from .audit import audit
from .config import get_settings
from .db import utcnow
from .jobs import PermanentFailure, RetryLater, enqueue, job
from .models import (Chapter, Consent, Membership, MediaAsset, Photo, Project, Prompt, PromptSchedule, Recording,
                     Story, StoryRevision, Storyteller, Transcript, User, WhatsAppIdentity, WhatsAppMessage)
from .notify import notify, notify_organizers
from .prompts import categories, next_prompt, title_for_prompt
from .qr import ensure_qr
from .scheduler import compute_next_send
from .security import decrypt_phone, encrypt_phone, phone_hash
from .storage import base_mime, ext_for_mime, get_storage, new_key, store_path
from .whatsapp import (MAX_INBOUND_MEDIA_BYTES, Recipient, WhatsAppError, buttons_message, extract_button_id,
                       get_backend, reaction_message, template_message, text_message)

log = logging.getLogger("vertelschat.pipeline")

SESSION_WINDOW = timedelta(hours=3)
ADDENDUM_WINDOW = timedelta(days=7)
NOTE_WINDOW = timedelta(hours=2)
SERVICE_WINDOW = timedelta(hours=23, minutes=45)
UNKNOWN_REPLY_EVERY = timedelta(hours=24)
QUARANTINE = timedelta(days=30)
MEDIA_ID_LIFETIME = timedelta(days=6, hours=12)  # Meta: media ids from webhooks expire after 7 days
LONG_TEXT_WORDS = 12
LATE_PROMPT_DAYS = 14
EXTRA_QUESTIONS_PER_DAY = 3
DOWNLOAD_DELAYS = [30, 120, 600, 1800, 3600, 7200, 14400]  # then every 6 hours until the media id expires
JOIN_RE = re.compile(r"\bVT[-\s]?([ACDEFGHJKLMNPQRTUVWXY34679]{6})\b", re.IGNORECASE)

KEYWORDS = {
    "stop": {"stop", "stoppen", "afmelden", "uitschrijven", "stop aub", "afmelden aub"},
    "start": {"start", "aanmelden", "hervat", "hervatten", "doorgaan", "weer starten"},
    "pause": {"pauze", "pauzeer", "pauzeren", "even pauze"},
    "more": {"vraag", "nog een vraag", "meer", "volgende vraag", "nieuwe vraag", "nog een"},
    "help": {"help", "hulp", "info", "?", "hoe werkt het"},
}
YES_WORDS = {"ja", "ja ik doe mee", "ja graag", "ik doe mee", "akkoord", "oke", "ok", "prima", "goed", "ja hoor", "jazeker"}
CHITCHAT = {"ok", "oke", "dank je", "dankjewel", "dank je wel", "bedankt", "dank u", "dank u wel", "top", "prima",
            "goed", "ja", "nee", "hoi", "hallo", "doei", "groetjes", "merci", "super", "leuk", "mooi", "fijn"}


# =========================================================================== helpers
def _norm_text(text: str) -> str:
    t = text.strip().lower().replace("é", "e").replace("ë", "e")
    return re.sub(r"[^\w\s?]", "", t).strip()


def keyword_of(text: str) -> str | None:
    t = _norm_text(text)
    for key, words in KEYWORDS.items():
        if t in words:
            return key
    return None


def _emoji_only(text: str) -> bool:
    return bool(text.strip()) and not re.search(r"[\w]", text)


def recipient_for(ident: WhatsAppIdentity) -> Recipient:
    return Recipient(phone=decrypt_phone(ident.phone_enc), bsuid=ident.bsuid or "")


def window_open(ident: WhatsAppIdentity | None, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return bool(ident and ident.last_inbound_at and now - ident.last_inbound_at < SERVICE_WINDOW)


def organizer_name(session: Session, project: Project) -> str:
    if project.gift_from_name:
        return project.gift_from_name
    user = session.scalar(select(User).join(Membership, Membership.user_id == User.id)
                          .where(Membership.family_id == project.family_id, Membership.role == "owner")
                          .order_by(Membership.created_at))
    return user.first_name if user else ""


def add_flag(story: Story, code: str, text: str, level: str = "check") -> None:
    flags = [f for f in (story.review_flags or []) if f.get("code") != code]
    flags.append({"code": code, "text": text, "level": level})
    story.review_flags = flags
    if level == "check":
        story.status = "needs_review" if story.status != "processing" else story.status


def processing_allowed(project: Project, prompt: Prompt | None, now: datetime) -> bool:
    if project.active_until is None or now <= project.active_until:
        return True
    grace_end = project.active_until + timedelta(days=project.grace_days)
    return bool(prompt and prompt.sent_at and prompt.sent_at <= project.active_until and now <= grace_end)


def context_terms(session: Session, project: Project) -> list[str]:
    terms: list[str] = []
    st = project.storyteller
    if st:
        terms += [st.name, st.address_as]
    names = session.scalars(select(User.name).join(Membership, Membership.user_id == User.id)
                            .where(Membership.family_id == project.family_id)).all()
    terms += [n.split(" ")[0] for n in names if n]
    terms += [t.strip() for t in re.split(r"[,\n;]", project.vocabulary or "") if t.strip()]
    seen, out = set(), []
    for t in terms:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def send_whatsapp(session: Session, ident: WhatsAppIdentity, payload: dict, *, key: str, project_id: str | None = None,
                  prompt_id: str | None = None, msg_type: str = "text", body_text: str = "") -> tuple[str, WhatsAppMessage]:
    """Idempotent send. The outbound row is committed as 'sending' before the API call, so a crash between the
    request and storing the answer is detected ('unknown') instead of causing a duplicate message."""
    row = session.scalar(select(WhatsAppMessage).where(WhatsAppMessage.idempotency_key == key))
    if row is not None and row.status in ("sent", "delivered", "read"):
        return "already_sent", row
    if row is not None and row.status in ("sending", "unknown"):
        row.status = "unknown"
        session.commit()
        return "unknown", row
    stored_payload = {k: v for k, v in payload.items() if k != "to"}
    if row is None:
        row = WhatsAppMessage(direction="out", idempotency_key=key, identity_id=ident.id, project_id=project_id,
                              prompt_id=prompt_id, msg_type=msg_type, body_text=body_text[:4000],
                              payload=stored_payload, status="sending")
        session.add(row)
    else:
        row.status, row.error_code, row.error_text = "sending", "", ""
    session.commit()
    try:
        wamid = get_backend().send(payload)
    except WhatsAppError as exc:
        row.status = "unknown" if exc.unknown_outcome else "failed"
        row.error_code = str(exc.code or "")
        row.error_text = str(exc)[:500]
        session.commit()
        if exc.unknown_outcome:
            return "unknown", row
        raise
    row.status = "sent"
    row.wamid = wamid
    session.commit()
    return "sent", row


def reply_text(session: Session, ident: WhatsAppIdentity, body: str, *, key: str, project_id: str | None = None) -> None:
    """Service replies (only inside the 24-hour window, which is always open right after an inbound message)."""
    try:
        send_whatsapp(session, ident, text_message(recipient_for(ident), body), key=key, project_id=project_id,
                      body_text=body)
    except WhatsAppError as exc:
        if exc.retriable:
            raise RetryLater(f"antwoord versturen mislukt: {exc}") from exc
        log.warning("reply %s not sent: %s", key, exc)


def reply_buttons(session: Session, ident: WhatsAppIdentity, body: str, buttons: list[tuple[str, str]], *, key: str,
                  project_id: str | None = None) -> None:
    try:
        send_whatsapp(session, ident, buttons_message(recipient_for(ident), body, buttons), key=key,
                      project_id=project_id, msg_type="interactive", body_text=body)
    except WhatsAppError as exc:
        if exc.retriable:
            raise RetryLater(f"knoppen versturen mislukt: {exc}") from exc
        log.warning("buttons %s not sent: %s", key, exc)


def linked_storytellers(session: Session, ident: WhatsAppIdentity) -> list[Storyteller]:
    return list(session.scalars(select(Storyteller).where(Storyteller.identity_id == ident.id)).all())


def ensure_chapter(session: Session, project_id: str, category: str) -> str:
    label = categories().get(category) or "Meer verhalen"
    ch = session.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.title == label))
    if ch is None:
        pos = (session.scalar(select(func.max(Chapter.position)).where(Chapter.project_id == project_id)) or 0) + 1
        ch = Chapter(project_id=project_id, title=label, position=pos)
        session.add(ch)
        session.flush()
    return ch.id


def bump_compose(session: Session, story: Story, delay: float | None = None) -> None:
    story.compose_generation = (story.compose_generation or 0) + 1
    enqueue(session, "compose_story", {"story_id": story.id, "generation": story.compose_generation},
            dedupe_key=f"compose:{story.id}:{story.compose_generation}",
            delay=get_settings().compose_delay_seconds if delay is None else delay)


# =========================================================================== inbound dispatch
@job("process_inbound", max_attempts=10)
def process_inbound(session: Session, payload: dict) -> None:
    msg = session.get(WhatsAppMessage, payload["message_id"])
    if msg is None or msg.status != "received":
        return
    ident = session.get(WhatsAppIdentity, msg.identity_id)
    m = (msg.payload or {}).get("message") or {}
    result = _dispatch(session, msg, ident, m, m.get("type", ""))
    msg.status = result or "processed"
    msg.processed_at = utcnow()


def _dispatch(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, m: dict, mtype: str) -> str:
    if mtype == "system":
        return _handle_system(session, ident, m)
    if mtype in ("revoke", "deleted") or m.get("revoke"):
        return _handle_revoke(session, m)
    if mtype in ("reaction", "sticker", "location", "contacts", "order", "ephemeral"):
        return "ignored"
    text = msg.body_text or ""
    button_id = extract_button_id(m)
    if button_id:
        return _handle_button(session, msg, ident, button_id)
    if mtype in ("text", "button", "interactive") and (code := JOIN_RE.search(text)):
        return _handle_join(session, msg, ident, "VT-" + code.group(1).upper())
    tellers = linked_storytellers(session, ident)
    if not tellers:
        return _handle_unknown(session, msg, ident, m, mtype)
    if mtype == "text":
        pending = [t for t in tellers if t.consent_status == "pending"]
        if pending and _norm_text(text) in YES_WORDS:
            for t in pending:
                give_consent(session, t, method="whatsapp_text", evidence=msg.wamid or "")
            return "processed"
        kw = keyword_of(text)
        if kw:
            return _handle_keyword(session, msg, ident, tellers, kw)
        return _handle_text(session, msg, ident, tellers, text)
    if mtype == "audio":
        return _handle_media_part(session, msg, ident, tellers, m, "audio")
    if mtype == "image":
        return _handle_image(session, msg, ident, tellers, m)
    if mtype in ("video", "document"):
        return _handle_file(session, msg, ident, tellers, m, mtype)
    return _handle_unsupported(session, msg, ident)


# --------------------------------------------------------------------------- joining & consent
def _handle_join(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, code: str) -> str:
    s = get_settings()
    st = session.scalar(select(Storyteller).where(Storyteller.join_code == code))
    if st is None:
        reply_text(session, ident, copy_nl.UNKNOWN_CODE, key=f"badcode:{msg.id}")
        return "processed"
    project = session.get(Project, st.project_id)
    msg.project_id = project.id
    if st.identity_id and st.identity_id != ident.id and st.consent_status == "given":
        reply_text(session, ident, copy_nl.code_taken(organizer_name(session, project)), key=f"taken:{msg.id}",
                   project_id=project.id)
        notify_organizers(session, project, "needs_attention",
                          f"Een ander nummer probeerde zich aan te melden als {st.name}",
                          "Het project blijft gekoppeld aan het huidige nummer. Wil je wisselen? Ontkoppel dan eerst "
                          "het oude nummer op de pagina Familie.", url=f"/p/{project.id}/familie")
        return "processed"
    first_link = st.identity_id != ident.id
    st.identity_id = ident.id
    st.connected_at = st.connected_at or utcnow()
    if project.status == "setup":
        project.status = "awaiting_storyteller"
    if first_link:
        audit(session, "storyteller_connected", project_id=project.id, actor_kind="storyteller",
              target_type="storyteller", target_id=st.id)
        track(session, "whatsapp_opt_in_started", project.id)
    adopt_quarantine(session, ident, st)
    if st.consent_status == "given":
        reply_text(session, ident, f"Je bent al aangemeld, {st.greeting_name}. Je volgende vraag komt vanzelf. "
                                   "Wil je nu al een vraag? Stuur dan VRAAG.", key=f"already:{msg.id}",
                   project_id=project.id)
        return "processed"
    sched = project.schedule
    body = copy_nl.welcome(project.locale, st.greeting_name, organizer_name(session, project),
                           sched.cadence_days if sched else 7, f"{s.storefront_url}/pages/privacy",
                           project.purchase_kind == "gift")
    reply_buttons(session, ident, body, [(f"consent:yes:{st.id}", copy_nl.BTN_YES),
                                         (f"consent:no:{st.id}", copy_nl.BTN_NOT_ME)],
                  key=f"welcome:{msg.id}", project_id=project.id)
    return "processed"


def _handle_button(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, button_id: str) -> str:
    parts = button_id.split(":")
    if parts[0] == "consent" and len(parts) == 3:
        st = session.get(Storyteller, parts[2])
        if st is None or st.identity_id != ident.id:
            return "ignored"
        msg.project_id = st.project_id
        if parts[1] == "yes":
            give_consent(session, st, method="whatsapp_button", evidence=msg.wamid or "")
        else:
            _not_me(session, st, ident, msg)
        return "processed"
    if parts[0] == "assign" and len(parts) == 3:
        rec = session.get(Recording, parts[1])
        st = session.get(Storyteller, parts[2])
        if rec is None or st is None or st.identity_id != ident.id or rec.hold_reason != "assignment":
            return "ignored"
        assign_recording(session, rec, st)
        project = session.get(Project, st.project_id)
        reply_text(session, ident, copy_nl.assign_done(project.title), key=f"assigned:{rec.id}", project_id=project.id)
        return "processed"
    return "ignored"


def give_consent(session: Session, st: Storyteller, *, method: str, evidence: str, restart: bool = False) -> None:
    project = session.get(Project, st.project_id)
    ident = session.get(WhatsAppIdentity, st.identity_id) if st.identity_id else None
    newly = st.consent_status != "given"
    st.consent_status = "given"
    st.consent_at = st.consent_at or utcnow()
    was_opted_out = st.opted_out_at is not None
    st.opted_out_at = None
    kinds = ("whatsapp_questions",) if not newly else ("whatsapp_questions", "recording_processing")
    for kind in kinds:
        session.add(Consent(storyteller_id=st.id, kind=kind, status="given", method=method, evidence=evidence))
    audit(session, "consent_given", project_id=project.id, actor_kind="storyteller", target_type="storyteller",
          target_id=st.id, method=method)
    activate_project(session, project)
    if newly:
        track(session, "whatsapp_opt_in_completed", project.id, via=method)
    release_holds(session, st, "consent")
    if ident is None:
        return
    if (restart or was_opted_out) and not newly:
        reply_text(session, ident, copy_nl.start_confirm(st.greeting_name), key=f"restart:{st.id}:{evidence}",
                   project_id=project.id)
        return
    reply_text(session, ident, copy_nl.consent_thanks(st.greeting_name), key=f"consent-thanks:{st.id}",
               project_id=project.id)
    first = next_prompt(session, project)
    if first is not None:
        first.status = "scheduled"
        enqueue(session, "send_prompt", {"prompt_id": first.id, "first": True}, dedupe_key=f"send_prompt:{first.id}")
    notify(session, project, "storyteller_connected", f"{st.name} doet mee",
           f"{st.name} heeft ja gezegd via WhatsApp. De eerste vraag is net verstuurd. Nieuwe verhalen verschijnen "
           "vanzelf in Vertelschat.", url=f"/p/{project.id}")


def activate_project(session: Session, project: Project) -> None:
    if project.status in ("setup", "awaiting_storyteller"):
        now = utcnow()
        project.status = "active"
        project.activated_at = now
        project.active_until = now + timedelta(days=project.period_days)
        sched = project.schedule or session.get(PromptSchedule, project.id)
        if sched is None:
            sched = PromptSchedule(project_id=project.id)
            session.add(sched)
            session.flush()
        sched.next_send_at = compute_next_send(sched, now + timedelta(days=3))
        audit(session, "project_activated", project_id=project.id)


def _not_me(session: Session, st: Storyteller, ident: WhatsAppIdentity, msg: WhatsAppMessage) -> None:
    project = session.get(Project, st.project_id)
    st.identity_id = None
    st.connected_at = None
    reply_text(session, ident, copy_nl.NOT_ME, key=f"notme:{msg.id}", project_id=project.id)
    audit(session, "storyteller_link_rejected", project_id=project.id, actor_kind="storyteller", target_id=st.id)
    notify_organizers(session, project, "needs_attention", "De uitnodiging kwam bij de verkeerde persoon terecht",
                      f"Het nummer dat de uitnodiging voor {st.name} gebruikte, gaf aan niet de verteller te zijn. "
                      "Stuur de uitnodiging naar de juiste persoon.", url=f"/p/{project.id}/familie")


def release_holds(session: Session, st: Storyteller, reason: str) -> int:
    recs = session.scalars(select(Recording).where(Recording.storyteller_id == st.id,
                                                   Recording.hold_reason == reason)).all()
    for rec in recs:
        rec.hold_reason = None
        _continue_processing(session, rec)
    return len(recs)


def _continue_processing(session: Session, rec: Recording) -> None:
    if rec.kind == "text":
        story = session.get(Story, rec.story_id) if rec.story_id else None
        if story:
            bump_compose(session, story)
    elif rec.status == "stored":
        enqueue(session, "transcribe", {"recording_id": rec.id}, dedupe_key=f"transcribe:{rec.id}")


def adopt_quarantine(session: Session, ident: WhatsAppIdentity, st: Storyteller) -> int:
    """Media received from this number before it was linked are attached to the project, never discarded."""
    assets = session.scalars(select(MediaAsset).where(MediaAsset.identity_id == ident.id,
                                                      MediaAsset.quarantined.is_(True),
                                                      MediaAsset.status != "purged")).all()
    for asset in assets:
        asset.quarantined = False
        asset.purge_after = None
        asset.project_id = st.project_id
        if asset.kind == "audio":
            rec = Recording(project_id=st.project_id, storyteller_id=st.id, kind="audio", original_media_id=asset.id,
                            wamid=asset.source_wamid, status="downloading" if asset.status == "pending" else "stored",
                            received_at=asset.wa_received_at or asset.created_at,
                            hold_reason=None if st.consent_status == "given" else "consent")
            session.add(rec)
            session.flush()
            story = _new_story(session, st.project_id, None, rec, "free")
            add_flag(story, "early", "Dit bericht kwam binnen voordat het nummer gekoppeld was. Hoort het bij deze "
                                     "familie? Anders kun je het verwijderen.")
            if asset.status == "stored":
                enqueue(session, "prepare_audio", {"recording_id": rec.id}, dedupe_key=f"prepare:{rec.id}")
        elif asset.kind == "image":
            session.add(Photo(project_id=st.project_id, media_id=asset.id, source="whatsapp"))
    return len(assets)


# --------------------------------------------------------------------------- unknown senders & keywords
def _handle_unknown(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, m: dict, mtype: str) -> str:
    now = utcnow()
    if mtype in ("audio", "image", "video", "document"):
        media = m.get(mtype) or {}
        if media.get("id") and not session.scalar(select(MediaAsset.id).where(MediaAsset.wa_media_id == media["id"])):
            asset = MediaAsset(project_id=None, identity_id=ident.id, kind=mtype if mtype != "document" else "document",
                               mime_type=media.get("mime_type") or "application/octet-stream", source="whatsapp",
                               wa_media_id=media["id"], expected_sha256_b64=media.get("sha256", ""),
                               source_wamid=msg.wamid, wa_received_at=msg.wa_timestamp or now, status="pending",
                               quarantined=True, purge_after=now + QUARANTINE)
            session.add(asset)
            session.flush()
            enqueue(session, "download_media", {"asset_id": asset.id}, dedupe_key=f"download:{asset.id}")
    if not ident.last_unknown_reply_at or now - ident.last_unknown_reply_at > UNKNOWN_REPLY_EVERY:
        ident.last_unknown_reply_at = now
        reply_text(session, ident, copy_nl.unknown_sender(get_settings().support_email), key=f"unknown:{msg.id}")
    return "held"


def _handle_keyword(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, tellers: list[Storyteller],
                    kw: str) -> str:
    now = utcnow()
    first_project = session.get(Project, tellers[0].project_id)
    if kw == "stop":
        for st in tellers:
            if st.opted_out_at is None:
                st.opted_out_at = now
                session.add(Consent(storyteller_id=st.id, kind="whatsapp_questions", status="withdrawn",
                                    method="whatsapp_keyword", evidence=msg.wamid or ""))
                project = session.get(Project, st.project_id)
                audit(session, "storyteller_opted_out", project_id=project.id, actor_kind="storyteller", target_id=st.id)
                track(session, "storyteller_opted_out", project.id)
                notify_organizers(session, project, "opted_out", f"{st.name} heeft zich afgemeld voor de vragen",
                                  "Er worden geen vragen meer verstuurd. Alles wat al verteld is, blijft bewaard en te "
                                  f"downloaden. {st.name} kan altijd weer START sturen.", url=f"/p/{project.id}")
        reply_text(session, ident, copy_nl.stop_confirm(), key=f"kw:{msg.id}")
    elif kw == "start":
        for st in tellers:
            if st.opted_out_at is not None or st.consent_status != "given":
                give_consent(session, st, method="whatsapp_keyword", evidence=msg.wamid or "", restart=True)
    elif kw == "pause":
        until = now + timedelta(days=28)
        for st in tellers:
            sched = session.get(PromptSchedule, st.project_id)
            if sched:
                sched.paused_until = until
        reply_text(session, ident, copy_nl.pause_confirm(nl_date(until)), key=f"kw:{msg.id}")
    elif kw == "more":
        st = next((t for t in tellers if t.can_receive_prompts
                   and session.get(Project, t.project_id).is_active(now)), None)
        if st is None:
            reply_text(session, ident, copy_nl.MORE_NONE, key=f"kw:{msg.id}")
            return "processed"
        today = now.strftime("%Y-%m-%d")
        if st.extra_questions_date != today:
            st.extra_questions_date, st.extra_questions_count = today, 0
        if st.extra_questions_count >= EXTRA_QUESTIONS_PER_DAY:
            reply_text(session, ident, copy_nl.MORE_LIMIT, key=f"kw:{msg.id}")
            return "processed"
        project = session.get(Project, st.project_id)
        sched = session.get(PromptSchedule, project.id)
        if sched:
            sched.paused_until = None
        p = next_prompt(session, project)
        if p is None:
            reply_text(session, ident, copy_nl.MORE_NONE, key=f"kw:{msg.id}")
            return "processed"
        st.extra_questions_count += 1
        p.status = "scheduled"
        enqueue(session, "send_prompt", {"prompt_id": p.id, "extra": True}, dedupe_key=f"send_prompt:{p.id}")
    elif kw == "help":
        reply_text(session, ident, copy_nl.help_text(get_settings().support_email), key=f"kw:{msg.id}",
                   project_id=first_project.id)
    return "processed"


def nl_date(dt: datetime) -> str:
    months = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
              "november", "december"]
    return f"{dt.day} {months[dt.month - 1]}"


# --------------------------------------------------------------------------- routing recordings to stories
def route(session: Session, tellers: list[Storyteller], ctx_wamid: str | None, now: datetime):
    """Returns (storyteller | None, prompt | None, how, candidates)."""
    if ctx_wamid:
        p = session.scalar(select(Prompt).where(Prompt.wa_message_id == ctx_wamid))
        if p:
            for t in tellers:
                if t.project_id == p.project_id:
                    return t, p, "reply", []
        om = session.scalar(select(WhatsAppMessage).where(WhatsAppMessage.wamid == ctx_wamid,
                                                          WhatsAppMessage.direction == "out"))
        if om and om.project_id:
            for t in tellers:
                if t.project_id == om.project_id:
                    p = session.get(Prompt, om.prompt_id) if om.prompt_id else None
                    return t, p, ("reply" if p else "auto"), []
    if len(tellers) == 1:
        return tellers[0], None, "auto", []
    active = [t for t in tellers if session.get(Project, t.project_id).is_active(now)] or tellers
    if len(active) == 1:
        return active[0], None, "auto", []
    likely = []
    for t in active:
        last = session.scalar(select(Story).where(Story.project_id == t.project_id, Story.hidden_at.is_(None))
                              .order_by(Story.last_part_at.desc()))
        if last and last.last_part_at and now - last.last_part_at <= SESSION_WINDOW:
            return t, None, "auto", []
        open_prompt = session.scalar(select(Prompt).where(Prompt.project_id == t.project_id, Prompt.status == "sent",
                                                          Prompt.sent_at >= now - timedelta(days=7)))
        if open_prompt:
            likely.append(t)
    if len(likely) == 1:
        return likely[0], None, "auto", []
    return None, None, "ambiguous", active


def _new_story(session: Session, project_id: str, prompt: Prompt | None, rec: Recording, how: str) -> Story:
    project = session.get(Project, project_id)
    pos = (session.scalar(select(func.max(Story.position)).where(Story.project_id == project_id)) or 0) + 1
    story = Story(project_id=project_id, prompt_id=prompt.id if prompt else None, status="processing",
                  first_part_at=rec.received_at, last_part_at=rec.received_at, position=pos,
                  title=title_for_prompt(prompt, project.locale) if prompt else "",
                  chapter_id=ensure_chapter(session, project_id, prompt.category if prompt else ""))
    session.add(story)
    session.flush()
    rec.story_id = story.id
    rec.assignment = how
    rec.prompt_id = prompt.id if prompt else None
    if prompt and prompt.status != "answered":
        prompt.status = "answered"
        prompt.answered_at = rec.received_at
        track(session, "prompt_answered", project_id)
    if prompt is None and how == "free":
        add_flag(story, "free", "Verteld zonder vraag. Geef het verhaal gerust een eigen titel.", level="info")
    return story


def _append(story: Story, rec: Recording, how: str) -> None:
    rec.story_id = story.id
    rec.assignment = how
    rec.prompt_id = story.prompt_id
    story.last_part_at = max(story.last_part_at or rec.received_at, rec.received_at)


def place_recording(session: Session, rec: Recording, st: Storyteller, prompt: Prompt | None, how: str,
                    now: datetime) -> Story:
    project_id = st.project_id
    if how == "reply" and prompt is not None:
        story = session.scalar(select(Story).where(Story.prompt_id == prompt.id, Story.hidden_at.is_(None)))
        if story is None:
            return _new_story(session, project_id, prompt, rec, "reply")
        late = story.last_part_at and now - story.last_part_at > SESSION_WINDOW
        _append(story, rec, "reply")
        if late:
            add_flag(story, "addendum", "Er kwam later nog een aanvulling binnen als antwoord op deze vraag.",
                     level="info")
        return story
    latest_story = session.scalar(select(Story).where(Story.project_id == project_id, Story.hidden_at.is_(None))
                                  .order_by(Story.last_part_at.desc()))
    latest_prompt = session.scalar(select(Prompt).where(Prompt.project_id == project_id, Prompt.sent_at.is_not(None))
                                   .order_by(Prompt.sent_at.desc()))
    if (latest_story and latest_story.last_part_at and now - latest_story.last_part_at <= SESSION_WINDOW and
            (latest_prompt is None or latest_prompt.sent_at <= latest_story.last_part_at
             or latest_story.prompt_id == latest_prompt.id)):
        _append(latest_story, rec, "session")
        return latest_story
    if latest_prompt is not None and latest_prompt.answered_at is None:
        story = _new_story(session, project_id, latest_prompt, rec, "current_prompt")
        days = (now - latest_prompt.sent_at).days
        if days > LATE_PROMPT_DAYS:
            add_flag(story, "late", f"Ingesproken {days} dagen na de vraag. Hoort het bij deze vraag? Je kunt het "
                                    "anders verplaatsen.")
        return story
    if latest_story and latest_story.last_part_at and now - latest_story.last_part_at <= ADDENDUM_WINDOW:
        _append(latest_story, rec, "addendum")
        add_flag(latest_story, "addendum", "Dit deel kwam later binnen, zonder nieuwe vraag. Hoort het bij dit verhaal? "
                                           "Je kunt het anders als los verhaal bewaren.")
        return latest_story
    return _new_story(session, project_id, None, rec, "free")


def assign_recording(session: Session, rec: Recording, st: Storyteller) -> None:
    now = utcnow()
    rec.project_id = st.project_id
    rec.storyteller_id = st.id
    rec.hold_reason = None if st.consent_status == "given" else "consent"
    if rec.original_media_id:
        asset = session.get(MediaAsset, rec.original_media_id)
        asset.project_id = st.project_id
    place_recording(session, rec, st, None, "auto", rec.received_at or now)
    audit(session, "recording_assigned", project_id=st.project_id, actor_kind="storyteller", target_id=rec.id)
    if rec.hold_reason is None:
        if rec.status == "stored":
            _continue_processing(session, rec)
        elif rec.status in ("transcribed",) and rec.story_id:
            bump_compose(session, session.get(Story, rec.story_id))


# --------------------------------------------------------------------------- media parts
def _handle_media_part(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, tellers: list[Storyteller],
                       m: dict, kind: str) -> str:
    if session.scalar(select(Recording.id).where(Recording.message_id == msg.id)):
        return "processed"
    media = m.get(kind) or {}
    now = msg.wa_timestamp or utcnow()
    st, prompt, how, candidates = route(session, tellers, msg.context_wamid, now)
    owner = st or candidates[0]
    asset = MediaAsset(project_id=owner.project_id, identity_id=ident.id, kind="audio",
                       mime_type=media.get("mime_type") or "audio/ogg", source="whatsapp", wa_media_id=media.get("id"),
                       expected_sha256_b64=media.get("sha256", ""), source_wamid=msg.wamid, wa_received_at=now,
                       status="pending")
    session.add(asset)
    session.flush()
    enqueue(session, "download_media", {"asset_id": asset.id}, dedupe_key=f"download:{asset.id}")
    rec = Recording(project_id=owner.project_id, storyteller_id=owner.id, message_id=msg.id, wamid=msg.wamid,
                    kind="audio", original_media_id=asset.id, status="downloading", forwarded=msg.forwarded,
                    received_at=now)
    session.add(rec)
    session.flush()
    msg.project_id = owner.project_id
    first_audio = not session.scalar(select(Recording.id).where(Recording.project_id == owner.project_id,
                                                                Recording.kind == "audio", Recording.id != rec.id))
    if first_audio:
        track(session, "first_voice_note_received", owner.project_id)
    if st is None:
        rec.hold_reason = "assignment"
        buttons = []
        for c in candidates[:3]:
            proj = session.get(Project, c.project_id)
            buttons.append((f"assign:{rec.id}:{c.id}", (organizer_name(session, proj) or proj.title)[:20]))
        reply_buttons(session, ident, copy_nl.ASSIGN_QUESTION, buttons, key=f"assign-ask:{rec.id}")
        return "held"
    project = session.get(Project, st.project_id)
    story = place_recording(session, rec, st, prompt, how, now)
    if st.consent_status != "given":
        rec.hold_reason = "consent"
        if not st.consent_reminder_sent_at or utcnow() - st.consent_reminder_sent_at > timedelta(hours=24):
            st.consent_reminder_sent_at = utcnow()
            reply_buttons(session, ident, copy_nl.consent_reminder(st.greeting_name),
                          [(f"consent:yes:{st.id}", copy_nl.BTN_YES), (f"consent:no:{st.id}", copy_nl.BTN_NOT_ME)],
                          key=f"consent-remind:{msg.id}", project_id=project.id)
    elif msg.forwarded:
        rec.hold_reason = "forwarded"
        add_flag(story, "forwarded", f"Dit spraakbericht is doorgestuurd. Is het de stem van {st.name}? Bevestig het "
                                     "voordat het verder wordt verwerkt.")
        notify_organizers(session, project, "needs_attention", "Doorgestuurd spraakbericht ontvangen",
                          f"{st.name} stuurde een doorgestuurd spraakbericht. Bekijk of het in het verhaal hoort.",
                          url=f"/p/{project.id}/verhalen/{story.id}")
    if not processing_allowed(project, session.get(Prompt, rec.prompt_id) if rec.prompt_id else None, now):
        rec.after_period = True
        rec.hold_reason = rec.hold_reason or "after_period"
        add_flag(story, "after_period", "Ontvangen na afloop van het verteljaar. De opname is bewaard en te downloaden.",
                 level="info")
        notify_organizers(session, project, "late_recording", f"{st.name} stuurde nog een verhaal",
                          "Het verteljaar is afgelopen, maar we bewaren alles wat nog binnenkomt. Je kunt de opname "
                          "gewoon beluisteren en downloaden.", url=f"/p/{project.id}/verhalen/{story.id}")
    return "processed"


def _handle_text(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, tellers: list[Storyteller],
                 text: str) -> str:
    if session.scalar(select(Recording.id).where(Recording.message_id == msg.id)):
        return "processed"
    now = msg.wa_timestamp or utcnow()
    words = len(text.split())
    st, prompt, how, candidates = route(session, tellers, msg.context_wamid, now)
    owner = st or candidates[0]
    project = session.get(Project, owner.project_id)
    latest_story = session.scalar(select(Story).where(Story.project_id == project.id, Story.hidden_at.is_(None))
                                  .order_by(Story.last_part_at.desc()))
    is_note = (words < LONG_TEXT_WORDS and latest_story is not None and latest_story.last_part_at is not None
               and now - latest_story.last_part_at <= NOTE_WINDOW and _norm_text(text) not in CHITCHAT
               and not _emoji_only(text))
    if words >= LONG_TEXT_WORDS or is_note:
        rec = Recording(project_id=project.id, storyteller_id=owner.id, message_id=msg.id, wamid=msg.wamid, kind="text",
                        text_content=text, status="transcribed", received_at=now, forwarded=msg.forwarded)
        session.add(rec)
        session.flush()
        if is_note and how != "reply":
            _append(latest_story, rec, "note")
            story = latest_story
        else:
            story = place_recording(session, rec, owner, prompt, how, now)
        session.add(Transcript(recording_id=rec.id, text=text, provider="whatsapp-tekst"))
        if owner.consent_status != "given":
            rec.hold_reason = "consent"
        elif not processing_allowed(project, prompt, now):
            rec.after_period = True
            rec.hold_reason = "after_period"
        else:
            bump_compose(session, story)
        enqueue(session, "acknowledge", {"recording_id": rec.id}, dedupe_key=f"ack:{rec.id}")
        return "processed"
    if _emoji_only(text) or _norm_text(text) in CHITCHAT:
        return "ignored"
    notify_organizers(session, project, "storyteller_message", f"Bericht van {owner.name}", f"\u201c{text[:300]}\u201d",
                      url=f"/p/{project.id}")
    if not owner.text_hint_sent:
        owner.text_hint_sent = True
        reply_text(session, ident, copy_nl.TEXT_HINT, key=f"hint:{msg.id}", project_id=project.id)
    return "processed"


def _handle_image(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, tellers: list[Storyteller],
                  m: dict) -> str:
    img = m.get("image") or {}
    if not img.get("id") or session.scalar(select(MediaAsset.id).where(MediaAsset.wa_media_id == img["id"])):
        return "processed"
    now = msg.wa_timestamp or utcnow()
    st, _p, _how, candidates = route(session, tellers, msg.context_wamid, now)
    owner = st or candidates[0]
    asset = MediaAsset(project_id=owner.project_id, identity_id=ident.id, kind="image",
                       mime_type=img.get("mime_type") or "image/jpeg", source="whatsapp", wa_media_id=img["id"],
                       expected_sha256_b64=img.get("sha256", ""), source_wamid=msg.wamid, wa_received_at=now,
                       status="pending")
    session.add(asset)
    session.flush()
    story = session.scalar(select(Story).where(Story.project_id == owner.project_id, Story.hidden_at.is_(None),
                                               Story.last_part_at >= now - timedelta(hours=24))
                           .order_by(Story.last_part_at.desc()))
    session.add(Photo(project_id=owner.project_id, story_id=story.id if story else None, media_id=asset.id,
                      caption=(img.get("caption") or "")[:500], source="whatsapp"))
    enqueue(session, "download_media", {"asset_id": asset.id}, dedupe_key=f"download:{asset.id}")
    return "processed"


def _handle_file(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity, tellers: list[Storyteller],
                 m: dict, mtype: str) -> str:
    media = m.get(mtype) or {}
    if not media.get("id") or session.scalar(select(MediaAsset.id).where(MediaAsset.wa_media_id == media["id"])):
        return "processed"
    now = msg.wa_timestamp or utcnow()
    owner = route(session, tellers, msg.context_wamid, now)[0] or tellers[0]
    story = session.scalar(select(Story).where(Story.project_id == owner.project_id, Story.hidden_at.is_(None),
                                               Story.last_part_at >= now - timedelta(hours=24))
                           .order_by(Story.last_part_at.desc()))
    asset = MediaAsset(project_id=owner.project_id, identity_id=ident.id, kind=mtype,
                       mime_type=media.get("mime_type") or "application/octet-stream", source="whatsapp",
                       wa_media_id=media["id"], expected_sha256_b64=media.get("sha256", ""), source_wamid=msg.wamid,
                       wa_received_at=now, status="pending", story_id=story.id if story else None,
                       original_filename=(media.get("filename") or "")[:300])
    session.add(asset)
    session.flush()
    enqueue(session, "download_media", {"asset_id": asset.id}, dedupe_key=f"download:{asset.id}")
    return "processed"


def _handle_unsupported(session: Session, msg: WhatsAppMessage, ident: WhatsAppIdentity) -> str:
    now = utcnow()
    if not ident.last_unsupported_reply_at or now - ident.last_unsupported_reply_at > timedelta(hours=24):
        ident.last_unsupported_reply_at = now
        reply_text(session, ident, copy_nl.UNSUPPORTED, key=f"unsupported:{msg.id}")
    return "ignored"


def _handle_system(session: Session, ident: WhatsAppIdentity, m: dict) -> str:
    sysm = m.get("system") or {}
    kind = sysm.get("type", "")
    if kind in ("user_changed_number", "customer_changed_number") and (new := sysm.get("wa_id") or sysm.get("new_wa_id")):
        ph = phone_hash(new)
        other = session.scalar(select(WhatsAppIdentity).where(WhatsAppIdentity.phone_hash == ph,
                                                              WhatsAppIdentity.id != ident.id))
        if other:
            other.phone_hash, other.phone_enc = None, None
            session.flush()
        ident.phone_hash, ident.phone_enc = ph, encrypt_phone(new)
    if kind == "user_changed_user_id" and (new_id := sysm.get("user_id")):
        clash = session.scalar(select(WhatsAppIdentity).where(WhatsAppIdentity.bsuid == new_id,
                                                              WhatsAppIdentity.id != ident.id))
        if clash:
            clash.bsuid = None
            session.flush()
        ident.bsuid = new_id
    audit(session, "whatsapp_identity_changed", actor_kind="webhook", target_type="identity", target_id=ident.id,
          change=kind)
    return "processed"


def _handle_revoke(session: Session, m: dict) -> str:
    """Storyteller deleted a message for everyone. Respect it: hide from story and book; purge after 30 days.
    NOTE: verify field names against the current 'revoke' webhook reference before launch (launch checklist)."""
    rv = m.get("revoke") or {}
    original = rv.get("original_message_id") or (m.get("context") or {}).get("id")
    rec = session.scalar(select(Recording).where(Recording.wamid == original)) if original else None
    if rec is None:
        return "ignored"
    rec.retracted_at = utcnow()
    story = session.get(Story, rec.story_id) if rec.story_id else None
    if story:
        add_flag(story, "retracted", "De verteller heeft een bericht in WhatsApp verwijderd. We hebben het uit het "
                                     "verhaal gehaald; het wordt na 30 dagen gewist.", level="info")
        bump_compose(session, story, delay=5)
    audit(session, "recording_retracted", project_id=rec.project_id, actor_kind="storyteller", target_id=rec.id)
    return "processed"


# =========================================================================== download, prepare, acknowledge
def _download_delay(attempt: int) -> int:
    return DOWNLOAD_DELAYS[attempt - 1] if attempt - 1 < len(DOWNLOAD_DELAYS) else 21600


def _download_dead(session: Session, payload: dict, error: str) -> None:
    asset = session.get(MediaAsset, payload["asset_id"])
    if asset is None:
        return
    asset.status = "failed"
    asset.last_error = error[:500]
    rec = session.scalar(select(Recording).where(Recording.original_media_id == asset.id))
    if rec is None or not rec.project_id:
        return
    rec.status = "download_failed"
    rec.failure_reason = "Het bestand kon niet bij WhatsApp worden opgehaald."
    project = session.get(Project, rec.project_id)
    st = project.storyteller
    story = session.get(Story, rec.story_id) if rec.story_id else None
    if story:
        add_flag(story, "download_failed", "Een spraakbericht kon niet worden opgehaald bij WhatsApp. Vraag de verteller "
                                           "om het opnieuw in te spreken; het staat nog in haar of zijn eigen chat.")
    notify_organizers(session, project, "download_failed",
                      f"Een spraakbericht van {st.name if st else 'de verteller'} kon niet worden opgehaald",
                      "WhatsApp gaf het bestand niet meer vrij. Het bericht staat nog wel in de eigen WhatsApp-chat van "
                      "de verteller. Vraag of het opnieuw ingesproken of doorgestuurd kan worden.",
                      url=f"/p/{project.id}/verhalen/{story.id}" if story else f"/p/{project.id}")


def _sha_matches(data_sha: bytes, expected: str) -> bool:
    if not expected:
        return True
    return expected in (base64.b64encode(data_sha).decode(), data_sha.hex())


@job("download_media", max_attempts=40, on_dead=_download_dead)
def download_media(session: Session, payload: dict) -> None:
    asset = session.get(MediaAsset, payload["asset_id"])
    if asset is None or asset.status in ("stored", "purged"):
        return
    asset.attempts += 1
    session.commit()  # the attempt counter must survive a RetryLater rollback
    received = asset.wa_received_at or asset.created_at
    expired = utcnow() - received > MEDIA_ID_LIFETIME
    backend = get_backend()
    try:
        info = backend.media_info(asset.wa_media_id)
        if int(info.get("file_size") or 0) > MAX_INBOUND_MEDIA_BYTES:
            raise PermanentFailure("bestand groter dan 100 MB")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "media"
            backend.download(info["url"], tmp)
            digest = hashlib.sha256(tmp.read_bytes()).digest()
            expected = asset.expected_sha256_b64 or info.get("sha256", "")
            if not _sha_matches(digest, expected):
                if asset.attempts < 4:
                    raise RetryLater("checksum komt niet overeen", delay=_download_delay(asset.attempts))
                asset.last_error = "checksum wijkt af van WhatsApp; bestand toch bewaard"
            mime = info.get("mime_type") or asset.mime_type
            key = new_key(asset.kind, ext_for_mime(mime))
            get_storage().put_file(key, tmp, base_mime(mime))
            asset.storage_key = key
            asset.mime_type = mime
            asset.size_bytes = tmp.stat().st_size
            asset.sha256 = digest.hex()
            asset.status = "stored"
            if asset.kind == "image":
                _image_dimensions(asset, tmp)
    except WhatsAppError as exc:
        asset.last_error = str(exc)[:500]
        session.commit()
        if expired or (not exc.retriable and exc.http_status not in (404, None)):
            raise PermanentFailure(str(exc)) from exc
        raise RetryLater(str(exc), delay=_download_delay(asset.attempts)) from exc
    _after_store(session, asset)


def _image_dimensions(asset: MediaAsset, path: Path) -> None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            asset.width, asset.height = im.size
    except Exception:  # noqa: BLE001 - unreadable images are still kept as originals
        asset.last_error = "afbeelding kon niet worden gelezen"


def _after_store(session: Session, asset: MediaAsset) -> None:
    if asset.quarantined:
        return
    rec = session.scalar(select(Recording).where(Recording.original_media_id == asset.id))
    if rec is not None:
        rec.status = "stored" if rec.status == "downloading" else rec.status
        enqueue(session, "prepare_audio", {"recording_id": rec.id}, dedupe_key=f"prepare:{rec.id}")
        return
    if asset.source_wamid and asset.identity_id:
        enqueue(session, "react", {"identity_id": asset.identity_id, "wamid": asset.source_wamid},
                dedupe_key=f"react:{asset.source_wamid}")
    if asset.kind in ("video", "document") and asset.project_id:
        project = session.get(Project, asset.project_id)
        what = "een video" if asset.kind == "video" else "een document"
        notify_organizers(session, project, "attachment", f"{project.storyteller.name if project.storyteller else 'De verteller'} stuurde {what}",
                          "Het bestand staat bij de bijlagen en zit ook in je download.", url=f"/p/{project.id}/downloads")


@job("react", max_attempts=4)
def react(session: Session, payload: dict) -> None:
    ident = session.get(WhatsAppIdentity, payload["identity_id"])
    if ident is None or not window_open(ident):
        return
    try:
        send_whatsapp(session, ident, reaction_message(recipient_for(ident), payload["wamid"]),
                      key=f"react:{payload['wamid']}", msg_type="reaction")
    except WhatsAppError as exc:
        if exc.retriable:
            raise RetryLater(str(exc)) from exc


def _prepare_dead(session: Session, payload: dict, error: str) -> None:
    rec = session.get(Recording, payload["recording_id"])
    if rec and rec.status in ("stored", "downloading"):
        rec.failure_reason = "Omzetten van de audio lukte niet; het origineel is bewaard."


@job("prepare_audio", max_attempts=6, on_dead=_prepare_dead)
def prepare_audio(session: Session, payload: dict) -> None:
    rec = session.get(Recording, payload["recording_id"])
    if rec is None or rec.original_media_id is None:
        return
    asset = session.get(MediaAsset, rec.original_media_id)
    if asset.status != "stored":
        return
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RetryLater("ffmpeg ontbreekt op de server", delay=600)
    if rec.playback_media_id is None:
        with get_storage().local_path(asset.storage_key) as src:
            try:
                info = probe(src)
            except AudioError as exc:
                _mark_corrupt(session, rec, str(exc))
                return
            asset.duration_seconds = info["duration"]
            rec.duration_seconds = info["duration"]
            with tempfile.TemporaryDirectory() as td:
                mp3 = Path(td) / "weergave.mp3"
                to_mp3(src, mp3)
                pb = store_path(session, mp3, kind="audio", mime="audio/mpeg", project_id=rec.project_id,
                                source="generated", is_original=False, derived_from_id=asset.id,
                                duration=info["duration"])
                rec.playback_media_id = pb.id
    if rec.status in ("downloading", "stored"):
        rec.status = "stored"
    enqueue(session, "acknowledge", {"recording_id": rec.id}, dedupe_key=f"ack:{rec.id}")
    if rec.hold_reason is None and not rec.retracted_at:
        enqueue(session, "transcribe", {"recording_id": rec.id}, dedupe_key=f"transcribe:{rec.id}")


def _mark_corrupt(session: Session, rec: Recording, reason: str) -> None:
    rec.status = "corrupt"
    rec.failure_reason = reason[:300]
    project = session.get(Project, rec.project_id)
    st = session.get(Storyteller, rec.storyteller_id) if rec.storyteller_id else None
    story = session.get(Story, rec.story_id) if rec.story_id else None
    if story:
        add_flag(story, "corrupt", "Een spraakbericht kwam beschadigd binnen. Het origineel is bewaard; we hebben de "
                                   "verteller gevraagd het opnieuw te sturen.")
    notify_organizers(session, project, "needs_attention", "Een spraakbericht kwam beschadigd binnen",
                      "We hebben het origineel bewaard en de verteller vriendelijk gevraagd het opnieuw in te spreken.",
                      url=f"/p/{project.id}/verhalen/{story.id}" if story else f"/p/{project.id}")
    ident = session.get(WhatsAppIdentity, st.identity_id) if st and st.identity_id else None
    if ident and window_open(ident) and (not st.resend_request_sent_at or
                                         utcnow() - st.resend_request_sent_at > timedelta(hours=24)):
        st.resend_request_sent_at = utcnow()
        reply_text(session, ident, copy_nl.CORRUPT_RESEND, key=f"resend:{rec.id}", project_id=project.id)


@job("acknowledge", max_attempts=6)
def acknowledge(session: Session, payload: dict) -> None:
    rec = session.get(Recording, payload["recording_id"])
    if rec is None or rec.acknowledged or rec.source != "whatsapp":
        return
    st = session.get(Storyteller, rec.storyteller_id) if rec.storyteller_id else None
    ident = session.get(WhatsAppIdentity, st.identity_id) if st and st.identity_id else None
    rec.acknowledged = True
    if ident is None or not rec.wamid or not window_open(ident):
        return
    get_backend().mark_read(rec.wamid)
    rcpt = recipient_for(ident)
    try:
        if rec.after_period and not st.after_period_notice_sent:
            st.after_period_notice_sent = True
            body = copy_nl.after_period(st.greeting_name)
            send_whatsapp(session, ident, text_message(rcpt, body), key=f"thanks:{rec.id}", project_id=rec.project_id,
                          body_text=body)
        elif st.thanks_sent < 2 and rec.assignment in ("reply", "current_prompt", "free") and rec.kind == "audio":
            body = copy_nl.thanks(st.greeting_name, st.thanks_sent)
            st.thanks_sent += 1
            send_whatsapp(session, ident, text_message(rcpt, body), key=f"thanks:{rec.id}", project_id=rec.project_id,
                          body_text=body)
        else:
            send_whatsapp(session, ident, reaction_message(rcpt, rec.wamid), key=f"react:{rec.wamid}",
                          project_id=rec.project_id, msg_type="reaction")
    except WhatsAppError as exc:
        if exc.retriable:
            rec.acknowledged = False
            raise RetryLater(str(exc)) from exc


# =========================================================================== transcription & composition
def _transcribe_dead(session: Session, payload: dict, error: str) -> None:
    rec = session.get(Recording, payload["recording_id"])
    if rec is None:
        return
    rec.status = "transcription_failed"
    rec.failure_reason = "Automatisch uitschrijven lukte niet."
    story = session.get(Story, rec.story_id) if rec.story_id else None
    if story:
        add_flag(story, "transcription_failed", "Dit spraakbericht kon (nog) niet automatisch worden uitgeschreven. "
                                                "De opname is veilig. Probeer het opnieuw of typ het zelf uit.")
        if story.status == "processing":
            story.status = "needs_review"
        project = session.get(Project, story.project_id)
        notify_organizers(session, project, "needs_attention", "Een verhaal kon niet worden uitgeschreven",
                          "De opname is veilig bewaard en te beluisteren. Je kunt het uitschrijven opnieuw proberen of "
                          "de tekst zelf invullen.", url=f"/p/{project.id}/verhalen/{story.id}")


@job("transcribe", max_attempts=8, on_dead=_transcribe_dead)
def transcribe(session: Session, payload: dict) -> None:
    rec = session.get(Recording, payload["recording_id"])
    if rec is None or rec.retracted_at or rec.hold_reason or rec.kind != "audio":
        return
    story = session.get(Story, rec.story_id) if rec.story_id else None
    if rec.transcript is not None:
        if story:
            bump_compose(session, story)
        return
    asset = session.get(MediaAsset, rec.original_media_id)
    project = session.get(Project, rec.project_id)
    backend = get_transcriber()
    terms = context_terms(session, project)
    rec.status = "transcribing"
    try:
        with get_storage().local_path(asset.storage_key) as src:
            limit = backend.max_chunk_seconds
            if limit and (rec.duration_seconds or 0) > limit:
                with tempfile.TemporaryDirectory() as td:
                    chunks = split_for_transcription(src, Path(td), limit)
                    results = [backend.transcribe(c, terms) for c in chunks]
                text = "\n\n".join(r.text for r in results if r.text)
                result = results[0]
                result.text = text
            else:
                result = backend.transcribe(src, terms)
    except TranscriptionError as exc:
        if exc.manual:
            rec.status = "transcription_failed"
            rec.failure_reason = str(exc)[:300]
            if story:
                add_flag(story, "manual_transcript", "Nog geen tekst: automatisch uitschrijven staat niet aan. Luister "
                                                     "de opname en typ het verhaal zelf uit, of probeer het later opnieuw.")
                story.status = "needs_review"
            return
        if exc.retriable:
            raise RetryLater(str(exc)) from exc
        raise PermanentFailure(str(exc)) from exc
    except AudioError as exc:
        raise RetryLater(str(exc)) from exc
    session.add(Transcript(recording_id=rec.id, text=result.text, provider=result.provider, model=result.model,
                           language=result.language, segments=result.segments or []))
    rec.status = "transcribed"
    if story:
        bump_compose(session, story)


def _compose_dead(session: Session, payload: dict, error: str) -> None:
    story = session.get(Story, payload["story_id"])
    if story is None or payload.get("generation") != story.compose_generation:
        return
    _compose(session, story, payload["generation"], force_local=True,
             extra_note="De automatische redactie was niet bereikbaar; dit is een eenvoudige bewerking van de transcriptie.")


@job("compose_story", max_attempts=8, on_dead=_compose_dead)
def compose_story(session: Session, payload: dict) -> None:
    story = session.get(Story, payload["story_id"])
    if story is None or story.hidden_at or payload.get("generation") != story.compose_generation:
        return  # superseded by a newer part; that job will compose everything at once
    try:
        _compose(session, story, payload["generation"])
    except ComposeError as exc:
        if exc.retriable:
            raise RetryLater(str(exc)) from exc
        _compose(session, story, payload["generation"], force_local=True,
                 extra_note=f"De automatische redactie gaf een fout ({exc}); dit is een eenvoudige bewerking.")


def _compose(session: Session, story: Story, generation: int, force_local: bool = False, extra_note: str = "") -> None:
    parts = [r for r in story.recordings if not r.retracted_at and r.hold_reason is None]
    if any(r.kind == "audio" and r.status in ("downloading", "stored", "transcribing") for r in parts):
        return
    texts = [r.transcript.text for r in parts if r.transcript is not None and r.transcript.text.strip()]
    if not texts:
        return
    project = session.get(Project, story.project_id)
    st = project.storyteller
    prompt = story.prompt
    data = ComposeInput(question=prompt.text if prompt else "", parts=texts, storyteller_name=st.name if st else "",
                        locale=project.locale,
                        title_hint=title_for_prompt(prompt, project.locale) if prompt and prompt.library_key else "",
                        known_names=context_terms(session, project))
    if force_local:
        result, notes = LocalComposer().compose(data), [extra_note] if extra_note else []
    else:
        result, notes = compose_checked(data)
    title = (result.title or story.title or "Een verhaal").strip()
    if story.edited_by_user:
        story.ai_suggestion_title, story.ai_suggestion_body = title, result.body
        add_flag(story, "ai_suggestion", "Er is een nieuwe automatische versie (bijvoorbeeld door een extra "
                                         "spraakbericht). Jouw bewerking is niet aangepast.", level="info")
    else:
        story.title, story.body = title, result.body
    session.add(StoryRevision(story_id=story.id, title=title, body=result.body, author_kind="ai",
                              note=f"{result.provider} {result.model}".strip()))
    for note in notes:
        add_flag(story, "ai_check", note)
    story.composed_generation = generation
    story.status = "needs_review" if any(f.get("level") == "check" for f in story.review_flags or []) else "ready"
    ensure_qr(session, story)
    existing = set(session.scalars(select(Prompt.text).where(Prompt.project_id == project.id)).all())
    for q in result.followups:
        if q not in existing:
            session.add(Prompt(project_id=project.id, text=q, category="vervolg", source="followup", status="suggested",
                               parent_story_id=story.id))
    now = utcnow()
    track(session, "story_generated", project.id, provider=result.provider,
          duration_bucket=duration_bucket(sum(r.duration_seconds or 0 for r in parts)))
    if project.first_story_at is None:
        project.first_story_at = now
        track(session, "first_story_completed", project.id)
    if story.notified_at is None:
        story.notified_at = now
        minutes = int(sum(r.duration_seconds or 0 for r in parts) // 60)
        length = f"{minutes} minuten" if minutes > 1 else "een paar minuten" if minutes == 1 else "een kort bericht"
        notify(session, project, "story_ready", f"Nieuw verhaal van {st.name if st else 'de verteller'}: {story.title}",
               f"Er is een nieuw verhaal binnen ({length} in eigen stem). Luister en lees het in Vertelschat.",
               url=f"/p/{project.id}/verhalen/{story.id}")


# =========================================================================== questions
PERMANENT_SEND_CODES = {"131026", "131021", "131051", "132000", "132001", "132005", "132007", "132012", "132015",
                        "132016", "133010", "131030"}


def _send_prompt_dead(session: Session, payload: dict, error: str) -> None:
    p = session.get(Prompt, payload["prompt_id"])
    if p is None or p.status in ("sent", "answered"):
        return
    p.status = "failed"
    p.failure_reason = error[:300]
    project = session.get(Project, p.project_id)
    notify_organizers(session, project, "needs_attention", "Een vraag kon niet worden verstuurd",
                      "We hebben het meerdere keren geprobeerd. Je kunt de vraag opnieuw versturen vanaf de pagina Vragen.",
                      url=f"/p/{project.id}/vragen")


@job("send_prompt", max_attempts=12, on_dead=_send_prompt_dead)
def send_prompt(session: Session, payload: dict) -> None:
    p = session.get(Prompt, payload["prompt_id"])
    if p is None or p.status in ("sent", "answered", "send_unknown", "rejected", "skipped", "suggested"):
        return
    project = session.get(Project, p.project_id)
    st = project.storyteller
    now = utcnow()
    if st is None or not st.can_receive_prompts or not (project.is_active(now) or payload.get("first")):
        p.status = "queued"
        return
    ident = session.get(WhatsAppIdentity, st.identity_id)
    s = get_settings()
    rcpt = recipient_for(ident)
    asker = ""
    if p.source in ("family", "custom") and p.suggested_by_id:
        user = session.get(User, p.suggested_by_id)
        asker = user.first_name if user else ""
    answered = session.scalar(select(func.count(Prompt.id)).where(Prompt.project_id == project.id,
                                                                  Prompt.status == "answered")) or 0
    first_weeks = answered < 3
    use_session = window_open(ident, now) and p.failure_reason != "window_closed"
    if use_session:
        body = copy_nl.question_session(project.locale, st.greeting_name, p.text, asker=asker, first_weeks=first_weeks,
                                        extra=bool(payload.get("extra")))
        wa_payload, via, template = text_message(rcpt, body), "session", ""
    else:
        # Family questions use the same utility templates, with the asker inside the question: Meta classified a
        # separate family template as marketing (costlier, and marketing messages can be withheld).
        question = f"{asker} vroeg zich af: {p.text}" if asker else p.text
        template = "vt_vraag_v1" if first_weeks else "vt_vraag_kort_v1"
        params = [st.greeting_name, question]
        wa_payload, via = template_message(rcpt, template, s.whatsapp_template_lang, params), "template"
        body = copy_nl.TEMPLATES[template]
        for i, value in enumerate(params, start=1):
            body = body.replace("{{%d}}" % i, value)
    key = f"prompt:{p.id}{payload.get('attempt_key', '')}"
    try:
        outcome, row = send_whatsapp(session, ident, wa_payload, key=key, project_id=project.id, prompt_id=p.id,
                                     msg_type="template" if via == "template" else "text", body_text=body)
    except WhatsAppError as exc:
        code = str(exc.code or "")
        if code == "131047" and use_session:
            p.failure_reason = "window_closed"
            raise RetryLater("klantservicevenster gesloten, opnieuw als template", delay=1) from exc
        if code == "131049":
            raise RetryLater("WhatsApp beperkt tijdelijk berichten aan deze gebruiker", delay=24 * 3600) from exc
        if code in PERMANENT_SEND_CODES or not exc.retriable:
            p.status = "failed"
            p.failure_reason = f"{code} {exc}"[:300]
            notify_organizers(session, project, "needs_attention", "Een vraag kon niet worden verstuurd",
                              f"WhatsApp meldde: {exc}. Controleer of het nummer van {st.name} nog klopt.",
                              url=f"/p/{project.id}/vragen")
            return
        raise RetryLater(str(exc)) from exc
    if outcome == "unknown":
        p.status = "send_unknown"
        p.failure_reason = "We weten niet zeker of deze vraag is aangekomen (verbinding viel weg tijdens verzenden)."
        notify_organizers(session, project, "needs_attention", "Is de vraag aangekomen?",
                          "Tijdens het versturen viel de verbinding weg. Om dubbele berichten te voorkomen sturen we "
                          "niet automatisch opnieuw. Je kunt dat zelf doen op de pagina Vragen.",
                          url=f"/p/{project.id}/vragen")
        return
    first_ever = not session.scalar(select(Prompt.id).where(Prompt.project_id == project.id,
                                                            Prompt.sent_at.is_not(None), Prompt.id != p.id))
    p.status = "sent"
    p.sent_at = now
    p.sent_via = via
    p.template_name = template if via == "template" else ""
    p.wa_message_id = row.wamid
    p.failure_reason = ""
    sched = session.get(PromptSchedule, project.id)
    if sched:
        sched.last_sent_at = now
    if first_ever:
        track(session, "first_prompt_sent", project.id, via=via)


@job("process_status", max_attempts=5)
def process_status(session: Session, payload: dict) -> None:
    st = payload.get("status") or {}
    row = session.scalar(select(WhatsAppMessage).where(WhatsAppMessage.wamid == st.get("id")))
    if row is None:
        return
    status = st.get("status", "")
    ts = None
    try:
        ts = datetime.fromtimestamp(int(st.get("timestamp")), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        ts = utcnow()
    p = session.get(Prompt, row.prompt_id) if row.prompt_id else None
    if status == "failed":
        errs = st.get("errors") or [{}]
        code = str(errs[0].get("code", ""))
        title = errs[0].get("title") or errs[0].get("message") or "onbekende fout"
        row.status, row.error_code, row.error_text = "failed", code, title[:500]
        if p and p.status == "sent":
            project = session.get(Project, p.project_id)
            if code == "131049":
                p.status = "scheduled"
                enqueue(session, "send_prompt", {"prompt_id": p.id, "attempt_key": ":r131049"},
                        dedupe_key=f"send_prompt:{p.id}:r131049", delay=24 * 3600)
            elif code == "131047":
                p.status, p.failure_reason = "scheduled", "window_closed"
                enqueue(session, "send_prompt", {"prompt_id": p.id, "attempt_key": ":tpl"},
                        dedupe_key=f"send_prompt:{p.id}:tpl")
            else:
                p.status, p.failure_reason = "failed", f"{code} {title}"[:300]
                notify_organizers(session, project, "needs_attention", "Een vraag is niet afgeleverd",
                                  f"WhatsApp meldde: {title}.", url=f"/p/{project.id}/vragen")
        return
    order = {"sending": 0, "sent": 1, "delivered": 2, "read": 3}
    if order.get(status, -1) > order.get(row.status, -1):
        row.status = status
    pricing = st.get("pricing") or {}
    if pricing:
        row.payload = {**(row.payload or {}), "pricing": {"category": pricing.get("category"),
                                                          "billable": pricing.get("billable"),
                                                          "type": pricing.get("type")}}
    if p is not None:
        if status in ("delivered", "read") and not p.delivered_at:
            p.delivered_at = ts
        if status == "read" and not p.read_at:
            p.read_at = ts


@job("process_identity_update", max_attempts=3)
def process_identity_update(session: Session, payload: dict) -> None:
    """user_id_update webhook: a user's BSUID changed. Field names to verify against Meta's reference at launch."""
    value = payload.get("value") or {}
    for item in value.get("user_id_updates") or value.get("updates") or [value]:
        old = item.get("old_user_id") or item.get("previous_user_id")
        new = item.get("new_user_id") or item.get("user_id")
        if not old or not new or old == new:
            continue
        ident = session.scalar(select(WhatsAppIdentity).where(WhatsAppIdentity.bsuid == old))
        if ident and not session.scalar(select(WhatsAppIdentity.id).where(WhatsAppIdentity.bsuid == new)):
            ident.bsuid = new
            audit(session, "whatsapp_bsuid_updated", actor_kind="webhook", target_type="identity", target_id=ident.id)
