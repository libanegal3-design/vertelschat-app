"""Development tools (only mounted when VT_DEV_TOOLS=1 and VT_ENV != production).

The WhatsApp simulator builds Cloud-API-shaped webhook payloads, signs them with the app secret and feeds them to
the same ingest_webhook() the real endpoint uses, then runs the job queue. Nothing here is a separate demo path."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import scheduler
from ..config import DATA_DIR, get_settings
from ..db import utcnow
from ..jobs import drain, retry_dead
from ..models import (Job, MediaAsset, OutboxMail, Project, PromptSchedule, Prompt, Recording, Storyteller, User,
                      WhatsAppIdentity, WhatsAppMessage)
from ..security import decrypt_phone, mask_phone, phone_hash, sign_meta_body, sign_shopify_body
from ..shopify import ingest_webhook as ingest_shopify
from ..storage import asset_url
from ..whatsapp import build_webhook, get_backend, ingest_webhook, new_wamid, register_dev_media
from .deps import db_session, redirect, render, start_session

router = APIRouter(prefix="/dev")
DEMO_AUDIO = DATA_DIR / "demo_audio"


def _fixtures() -> list[dict]:
    idx = DEMO_AUDIO / "fixtures.json"
    if not idx.exists():
        return []
    data = json.loads(idx.read_text(encoding="utf-8"))
    return [{"file": v["file"], "label": v.get("label", v["file"])} for v in data.values()]


def _post(db: Session, payload: dict) -> dict:
    raw = json.dumps(payload).encode()
    counts = ingest_webhook(db, raw, sign_meta_body(raw))
    db.commit()
    last = get_settings().var_dir / "dev_last_webhook.json"
    last.write_bytes(raw)
    return counts


def _identities(db: Session) -> list[dict]:
    out = []
    for ident in db.scalars(select(WhatsAppIdentity).order_by(WhatsAppIdentity.created_at)).all():
        phone = decrypt_phone(ident.phone_enc) if ident.phone_enc else ""
        tellers = db.scalars(select(Storyteller).where(Storyteller.identity_id == ident.id)).all()
        out.append({"ident": ident, "phone": phone, "label": ", ".join(t.name for t in tellers) or "onbekend",
                    "masked": mask_phone(phone) if phone else ident.bsuid})
    return out


@router.get("")
def index(request: Request, db: Session = Depends(db_session)):
    users = db.scalars(select(User).order_by(User.created_at)).all()
    projects = db.scalars(select(Project).order_by(Project.created_at)).all()
    jobs = {s: db.query(Job).filter(Job.status == s).count() for s in ("queued", "running", "done", "dead")}
    return render(request, "dev/index.html", users=users, projects=projects, jobs=jobs,
                  mails=db.query(OutboxMail).count())


@router.get("/login")
def dev_login(email: str, next: str = "/app", db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        user = User(email=email.lower())
        db.add(user)
        db.flush()
    resp = RedirectResponse(next if next.startswith("/") else "/app", status_code=303)
    start_session(db, resp, user)
    db.commit()
    return resp


@router.get("/whatsapp", response_class=HTMLResponse)
def simulator(request: Request, nr: str = "", db: Session = Depends(db_session)):
    idents = _identities(db)
    pending = db.scalars(select(Storyteller).where(Storyteller.identity_id.is_(None))).all()
    phone = nr or (idents[0]["phone"] if idents else "31612345678")
    ident = db.scalar(select(WhatsAppIdentity).where(WhatsAppIdentity.phone_hash == phone_hash(phone))) if phone else None
    thread = []
    if ident is not None:
        rows = db.scalars(select(WhatsAppMessage).where(WhatsAppMessage.identity_id == ident.id)
                          .order_by(WhatsAppMessage.created_at, WhatsAppMessage.id)).all()
        for m in rows:
            item = {"m": m, "buttons": [], "audio": None, "image": None}
            payload = m.payload or {}
            if m.direction == "out" and m.msg_type == "interactive":
                for b in ((payload.get("interactive") or {}).get("action") or {}).get("buttons") or []:
                    item["buttons"].append({"id": b["reply"]["id"], "title": b["reply"]["title"]})
            if m.direction == "in" and m.msg_type in ("audio", "image"):
                media_id = ((payload.get("message") or {}).get(m.msg_type) or {}).get("id")
                asset = db.scalar(select(MediaAsset).where(MediaAsset.wa_media_id == media_id)) if media_id else None
                if asset is not None and asset.status == "stored":
                    item[m.msg_type] = f"/dev/media/{asset.id}"
                item["media_status"] = asset.status if asset else "?"
                rec = db.scalar(select(Recording).where(Recording.message_id == m.id))
                item["rec"] = rec
            thread.append(item)
    outbound = [t["m"] for t in thread if t["m"].direction == "out" and t["m"].wamid]
    return render(request, "dev/whatsapp.html", idents=idents, pending=pending, phone=phone, ident=ident,
                  thread=thread, outbound=outbound[-8:], fixtures=_fixtures(),
                  jobs_waiting=db.query(Job).filter(Job.status == "queued").count())


@router.get("/media/{asset_id}")
def dev_media(asset_id: str, db: Session = Depends(db_session)):
    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.status != "stored":
        return HTMLResponse("niet gevonden", status_code=404)
    return RedirectResponse(asset_url(asset, ttl=3600), status_code=302)


@router.post("/whatsapp")
async def simulator_action(request: Request, db: Session = Depends(db_session)):
    form = await request.form()
    phone = "".join(ch for ch in str(form.get("phone", "")) if ch.isdigit()) or "31612345678"
    name = str(form.get("name", "")) or "Simulator"
    action = str(form.get("action", ""))
    ctx_id = str(form.get("context", "")) or None
    forwarded = form.get("forwarded") == "1"
    ts = str(int(time.time()))
    msg: dict = {"id": new_wamid(), "timestamp": ts}
    if ctx_id:
        msg["context"] = {"id": ctx_id, "from": get_settings().whatsapp_link_number or "31200000000"}
    if forwarded:
        msg.setdefault("context", {})["forwarded"] = True
    tmpdir = None
    try:
        if action == "text":
            msg.update(type="text", text={"body": str(form.get("text", ""))})
        elif action == "join":
            code = str(form.get("code", "")).strip()
            msg.update(type="text", text={"body": f"Hallo Vertelschat, ik doe mee! Mijn code is {code}"})
        elif action == "button":
            msg.update(type="interactive", interactive={"type": "button_reply", "button_reply": {
                "id": str(form.get("button_id")), "title": str(form.get("button_title", ""))}})
        elif action in ("voice", "corrupt", "faildownload", "badchecksum"):
            if action == "corrupt":
                tmpdir = Path(tempfile.mkdtemp())
                src = tmpdir / "kapot.ogg"
                src.write_bytes(b"OggS" + bytes(range(256)) * 40)
            elif form.get("upload") is not None and getattr(form.get("upload"), "filename", ""):
                tmpdir = Path(tempfile.mkdtemp())
                src = tmpdir / Path(form["upload"].filename).name
                src.write_bytes(await form["upload"].read())
            else:
                src = DEMO_AUDIO / str(form.get("fixture", ""))
            media_id, sha = register_dev_media(src, "audio/ogg; codecs=opus", corrupt_checksum=action == "badchecksum")
            if action == "faildownload":
                get_backend().fail_media[media_id] = int(form.get("failures", 3) or 3)
            msg.update(type="audio", audio={"id": media_id, "mime_type": "audio/ogg; codecs=opus", "sha256": sha,
                                            "voice": True})
        elif action == "image":
            src = DATA_DIR / "demo_photos" / str(form.get("photo", "huis.jpg"))
            if form.get("upload") is not None and getattr(form.get("upload"), "filename", ""):
                tmpdir = Path(tempfile.mkdtemp())
                src = tmpdir / Path(form["upload"].filename).name
                src.write_bytes(await form["upload"].read())
            media_id, sha = register_dev_media(src, "image/jpeg")
            msg.update(type="image", image={"id": media_id, "mime_type": "image/jpeg", "sha256": sha,
                                            "caption": str(form.get("caption", ""))})
        elif action == "sticker":
            msg.update(type="sticker", sticker={"id": "0", "mime_type": "image/webp", "animated": False})
        elif action == "unsupported":
            msg.update(type="unsupported", errors=[{"code": 131051, "title": "Message type unknown"}])
        elif action == "revoke":
            last = db.scalar(select(WhatsAppMessage).where(WhatsAppMessage.direction == "in",
                                                           WhatsAppMessage.msg_type == "audio")
                             .order_by(WhatsAppMessage.created_at.desc()))
            if last is None:
                return redirect(f"/dev/whatsapp?nr={phone}", "Geen spraakbericht om in te trekken.", "error")
            msg.update(type="revoke", revoke={"original_message_id": last.wamid})
        elif action == "duplicate":
            last = get_settings().var_dir / "dev_last_webhook.json"
            if last.exists():
                raw = last.read_bytes()
                counts = ingest_webhook(db, raw, sign_meta_body(raw))
                db.commit()
                drain(advance_time=True)
                return redirect(f"/dev/whatsapp?nr={phone}", f"Webhook opnieuw afgeleverd: {counts}")
            return redirect(f"/dev/whatsapp?nr={phone}", "Nog geen webhook om te herhalen.", "error")
        elif action == "status":
            wamid = str(form.get("wamid", ""))
            status = str(form.get("status", "delivered"))
            st: dict = {"id": wamid, "status": status, "timestamp": ts, "recipient_id": phone}
            if status == "failed":
                code = int(form.get("code", 131026) or 131026)
                st["errors"] = [{"code": code, "title": {131049: "Ecosystem engagement limit",
                                                         131026: "Message undeliverable",
                                                         131047: "Re-engagement message"}.get(code, "Fout")}]
            else:
                st["pricing"] = {"billable": True, "category": "utility", "type": "regular"}
            _post(db, build_webhook(statuses=[st]))
            drain(advance_time=True)
            return redirect(f"/dev/whatsapp?nr={phone}", f"Status '{status}' afgeleverd.")
        else:
            return redirect(f"/dev/whatsapp?nr={phone}", "Onbekende actie.", "error")
        counts = _post(db, build_webhook([msg], phone=phone, name=name))
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    if form.get("process", "1") == "1":
        drain(advance_time=True)
    return redirect(f"/dev/whatsapp?nr={phone}", f"Webhook afgeleverd ({counts['messages']} nieuw, "
                                                 f"{counts['duplicates']} dubbel).")


@router.get("/mail")
def mail(request: Request, id: int | None = None, db: Session = Depends(db_session)):
    mails = db.scalars(select(OutboxMail).order_by(OutboxMail.created_at.desc()).limit(60)).all()
    current = db.get(OutboxMail, id) if id else (mails[0] if mails else None)
    return render(request, "dev/mail.html", mails=mails, current=current)


@router.get("/mail/{mid}/html", response_class=HTMLResponse)
def mail_html(mid: int, db: Session = Depends(db_session)):
    m = db.get(OutboxMail, mid)
    return HTMLResponse(m.html if m else "niet gevonden")


@router.get("/jobs")
def jobs(request: Request, db: Session = Depends(db_session)):
    rows = db.scalars(select(Job).order_by(Job.id.desc()).limit(80)).all()
    return render(request, "dev/jobs.html", rows=rows)


@router.post("/jobs/{action}")
async def jobs_action(request: Request, action: str, db: Session = Depends(db_session)):
    form = await request.form()
    if action == "drain":
        n = drain(advance_time=True)
        msg = f"{n} taken uitgevoerd."
    elif action == "tick":
        counts = scheduler.tick()
        n = drain(advance_time=False)
        msg = f"Planner: {counts}; {n} taken uitgevoerd."
    elif action == "retry":
        retry_dead(db, int(form.get("job_id")))
        db.commit()
        n = drain(advance_time=True)
        msg = f"Opnieuw geprobeerd; {n} taken uitgevoerd."
    else:
        msg = "Onbekende actie."
    back = request.headers.get("referer") or "/dev/jobs"
    return redirect(back, msg)


@router.get("/shop")
def shop(request: Request):
    return render(request, "dev/shop.html")


@router.post("/shop")
async def shop_order(request: Request, db: Session = Depends(db_session)):
    form = await request.form()
    sku = str(form.get("sku", "VT-VERTELJAAR"))
    oid = int(time.time() * 1000)
    props = []
    if form.get("gift") == "1":
        props = [{"name": "Cadeau", "value": "Ja"}, {"name": "Voor", "value": str(form.get("voor", ""))},
                 {"name": "Boodschap", "value": str(form.get("boodschap", ""))}]
    if form.get("project"):
        props.append({"name": "_project", "value": str(form.get("project"))})
    order = {"id": oid, "name": f"#VT{str(oid)[-5:]}", "email": str(form.get("email", "")), "currency": "EUR",
             "total_price": {"VT-VERTELJAAR": "129.00", "VT-EXTRA-BOEK": "45.00", "VT-VERLENGING": "69.00",
                             "VT-CADEAUKAART": "4.95"}.get(sku, "0.00"),
             "customer": {"first_name": str(form.get("first_name", "")), "last_name": str(form.get("last_name", ""))},
             "line_items": [{"sku": sku, "quantity": int(form.get("quantity", 1) or 1), "properties": props}]}
    raw = json.dumps(order).encode()
    result = ingest_shopify(db, raw, sign_shopify_body(raw), "orders/paid", f"dev-{oid}")
    db.commit()
    drain(advance_time=True)
    return redirect("/dev/mail", f"Shopify orders/paid verwerkt: {result}. Bekijk de mail.")


@router.post("/tijdreis")
async def time_travel(request: Request, db: Session = Depends(db_session)):
    """Shift a project's clock so the end of the storytelling year can be tested (permanent-access checks)."""
    form = await request.form()
    project = db.get(Project, str(form.get("project_id", "")))
    if project is None:
        return redirect("/dev", "Project niet gevonden.", "error")
    days = int(form.get("days", 400) or 400)
    delta = timedelta(days=days)
    for attr in ("activated_at", "active_until"):
        if getattr(project, attr):
            setattr(project, attr, getattr(project, attr) - delta)
    sched = db.get(PromptSchedule, project.id)
    if sched and sched.next_send_at:
        sched.next_send_at -= delta
    for p in db.scalars(select(Prompt).where(Prompt.project_id == project.id, Prompt.sent_at.is_not(None))).all():
        p.sent_at -= delta
    db.commit()
    counts = scheduler.tick()
    drain(advance_time=True)
    return redirect("/dev", f"{days} dagen vooruit gereisd voor {project.title}. Planner: {counts}")
