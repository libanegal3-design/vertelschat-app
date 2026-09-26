"""The family web app. All routes require login and project membership; edits require the editor role unless noted."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import services as svc
from ..analytics import track
from ..config import get_settings
from ..db import utcnow
from ..exports import request_export
from ..giftcard import render_gift_card
from ..models import (ROLE_RANK, Book, BookVersion, Chapter, Entitlement, Export, Invitation, MediaAsset, Membership,
                      Notification, Photo, PrintOrder, Project, Prompt, QRLink, Recording, Story, StoryRevision,
                      Transcript, User, WhatsAppIdentity)
from ..prompts import browse, categories, starter_suggestions
from ..qr import qr_display_url, qr_svg, qr_url, wa_join_link, wa_share_link
from ..scheduler import CADENCES
from ..security import decrypt_phone, hash_pin, mask_phone
from .. import commerce, pricing
from ..shopify import extension_url, extra_book_url
from .deps import (NotFoundError, db_session, form_with_csrf, project_for_user, redirect, render, require_user)

router = APIRouter()
MAX_PHOTO = 25 * 1024 * 1024
MAX_AUDIO = 200 * 1024 * 1024
STEPS = [("ritme", "Ritme"), ("vragen", "Eerste vragen"), ("familie", "Familie"), ("verbinden", "Verbinden")]


# --------------------------------------------------------------------------- helpers
def _load(request: Request, db: Session, pid: str, min_role: str = "viewer"):
    user = require_user(request, db)
    project, role = project_for_user(db, user, pid, min_role)
    return user, project, role


def _nav(db: Session, project: Project, role: str, section: str) -> dict:
    review = db.scalar(select(func.count(Story.id)).where(Story.project_id == project.id, Story.hidden_at.is_(None),
                                                          Story.status == "needs_review")) or 0
    suggested = db.scalar(select(func.count(Prompt.id)).where(Prompt.project_id == project.id,
                                                              Prompt.status == "suggested")) or 0
    editable = svc.can_edit(project)
    return {"project": project, "role": role, "section": section, "storyteller": project.storyteller,
            "editable": editable, "can_edit": editable and ROLE_RANK[role] >= ROLE_RANK["editor"],
            "is_editor": ROLE_RANK[role] >= ROLE_RANK["editor"], "is_contributor": ROLE_RANK[role] >= ROLE_RANK["contributor"],
            "badge_review": review, "badge_suggested": suggested}


def _story(db: Session, project: Project, sid: str) -> Story:
    story = db.get(Story, sid)
    if story is None or story.project_id != project.id:
        raise NotFoundError()
    return story


async def _save_upload(upload, limit: int) -> tuple[Path, str, str, Path]:
    tmpdir = Path(tempfile.mkdtemp(prefix="vt-upload-"))
    name = Path(getattr(upload, "filename", "") or "bestand").name
    dest = tmpdir / "upload"
    size = 0
    with dest.open("wb") as fh:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise svc.ServiceError(f"Dit bestand is te groot (maximaal {limit // (1024 * 1024)} MB).")
            fh.write(chunk)
    if size == 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise svc.ServiceError("Kies eerst een bestand.")
    return dest, name, getattr(upload, "content_type", "") or "", tmpdir


def _connect_ctx(project: Project) -> dict:
    st = project.storyteller
    link = wa_join_link(st.join_code)
    invite = svc.storyteller_invite_text(project)
    return {"join_link": link, "join_qr": Markup(qr_svg(link, scale=5, border=1)), "invite_text": invite,
            "share_link": wa_share_link(invite), "join_code": st.join_code,
            "display_number": get_settings().whatsapp_display_number,
            "gift_package_url": commerce.gift_package_url(project),
            "gift_package_price": pricing.eur(pricing.GIFT_PACKAGE, short=False)}


def _story_rows(db: Session, stories: list[Story]) -> list[dict]:
    rows = []
    for s in stories:
        recs = db.scalars(select(Recording).where(Recording.story_id == s.id, Recording.retracted_at.is_(None))
                          .order_by(Recording.received_at)).all()
        audio = [r for r in recs if r.kind == "audio"]
        first = next((r for r in audio if r.playback_media_id), None)
        prompt = db.get(Prompt, s.prompt_id) if s.prompt_id else None
        photos = db.scalar(select(func.count(Photo.id)).where(Photo.story_id == s.id)) or 0
        qr = db.scalar(select(QRLink).where(QRLink.story_id == s.id))
        rows.append({"story": s, "parts": len(recs), "audio_parts": len(audio),
                     "duration": sum(r.duration_seconds or 0 for r in audio), "play_id": first.playback_media_id if first else None,
                     "question": prompt.text if prompt else "", "photos": photos, "qr": qr,
                     "waiting": sum(1 for r in recs if r.hold_reason)})
    return rows


def _entitlements(db: Session, user: User) -> list[Entitlement]:
    return commerce.available_storyteller_entitlements(db, user)


@router.get("/app")
def projects(request: Request, db: Session = Depends(db_session)):
    user = require_user(request, db)
    rows = db.execute(select(Project, Membership.role).join(Membership, Membership.family_id == Project.family_id)
                      .where(Membership.user_id == user.id).order_by(Project.created_at)).all()
    ents = _entitlements(db, user)
    if len(rows) == 1 and not ents:
        return redirect(f"/p/{rows[0][0].id}")
    if not rows and ents:
        return redirect("/start")
    families = {}
    for project, role in rows:
        if ROLE_RANK.get(role, 0) >= ROLE_RANK["editor"]:
            families.setdefault(project.family_id, commerce.extra_storyteller_url(project))
    return render(request, "app/projects.html", rows=rows, entitlements=ents, add_urls=families,
                  second_price=pricing.eur(pricing.SECOND_STORYTELLER))


@router.get("/start")
def start(request: Request, db: Session = Depends(db_session)):
    user = require_user(request, db)
    ents = _entitlements(db, user)
    if not ents and not get_settings().dev_tools:
        return render(request, "app/projects.html", rows=[], entitlements=[], no_entitlement=True)
    ent = ents[0] if ents else None
    family = commerce.family_for_claim(db, ent, user) if ent else None
    return render(request, "app/start.html", entitlement=ent, steps=STEPS, step_index=-1, family=family)


@router.post("/start")
async def start_submit(request: Request, db: Session = Depends(db_session)):
    user = require_user(request, db)
    form = await form_with_csrf(request)
    ents = _entitlements(db, user)
    if not ents and not get_settings().dev_tools:
        return redirect("/app", "Er is geen verteljaar beschikbaar om te starten.", "error")
    birth = str(form.get("birth_year", "")).strip()
    birth_year = int(birth) if birth.isdigit() and 1900 <= int(birth) <= 2015 else None
    your_name = str(form.get("your_name", "")).strip()
    if your_name and not user.name:
        user.name = your_name[:200]
    try:
        project = svc.create_project(db, user, storyteller_name=str(form.get("name", "")),
                                     address_as=str(form.get("address_as", "")), birth_year=birth_year,
                                     locale=str(form.get("locale", "nl-NL")),
                                     purchase_kind="gift" if form.get("kind") != "self" else "self",
                                     gift_from_name=your_name or user.first_name,
                                     gift_message=str(form.get("gift_message", "")),
                                     entitlement=ents[0] if ents else None,
                                     family=commerce.family_for_claim(db, ents[0], user) if ents else None)
    except svc.ServiceError as exc:
        return redirect("/start", str(exc), "error")
    db.commit()
    return redirect(f"/p/{project.id}/instellen/ritme")


@router.get("/p/{pid}/instellen/{step}")
def onboarding(request: Request, pid: str, step: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    idx = next((i for i, (k, _) in enumerate(STEPS) if k == step), None)
    if idx is None:
        raise NotFoundError()
    ctx = {**_nav(db, project, role, "setup"), "steps": STEPS, "step": step, "step_index": idx,
           "schedule": project.schedule, "cadences": CADENCES}
    if step == "vragen":
        ctx["suggestions"] = starter_suggestions(project, project.storyteller, 10)
        ctx["chosen"] = {p.library_key for p in db.scalars(select(Prompt).where(Prompt.project_id == project.id)).all()}
    if step == "familie":
        ctx["invitations"] = db.scalars(select(Invitation).where(Invitation.family_id == project.family_id)).all()
    if step == "verbinden":
        ctx.update(_connect_ctx(project))
    return render(request, "app/onboarding.html", **ctx)


@router.post("/p/{pid}/instellen/{step}")
async def onboarding_submit(request: Request, pid: str, step: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    form = await form_with_csrf(request)
    try:
        if step == "ritme":
            svc.set_schedule(db, project, cadence_days=int(form.get("cadence", 7)), weekday=int(form.get("weekday", 6)),
                             hour=int(form.get("hour", 10)), minute=0, wait_for_answer=form.get("wait") == "1")
            nxt = "vragen"
        elif step == "vragen":
            existing = {p.library_key for p in db.scalars(select(Prompt).where(Prompt.project_id == project.id)).all()}
            for key in form.getlist("keys"):
                if key not in existing:
                    svc.add_library_prompt(db, project, user, str(key))
            if str(form.get("custom", "")).strip():
                svc.add_custom_prompt(db, project, user, str(form.get("custom")), role)
            nxt = "familie"
        elif step == "familie":
            for i in range(1, 4):
                email = str(form.get(f"email{i}", "")).strip()
                if email:
                    svc.invite_member(db, project, user, email, str(form.get(f"role{i}", "contributor")))
            nxt = "verbinden"
        else:
            raise NotFoundError()
    except svc.ServiceError as exc:
        return redirect(f"/p/{pid}/instellen/{step}", str(exc), "error")
    db.commit()
    return redirect(f"/p/{pid}/instellen/{nxt}")


@router.get("/p/{pid}/verbinden")
def connect(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    return render(request, "app/connect.html", **_nav(db, project, role, "familie"), **_connect_ctx(project))


@router.get("/p/{pid}/cadeaukaart.pdf")
def gift_card(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    pdf = render_gift_card(project, project.gift_from_name or user.first_name)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="cadeaukaart-{project.storyteller.join_code}.pdf"'})


@router.get("/api/p/{pid}/status")
def status(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    st = project.storyteller
    first = db.scalar(select(Prompt.id).where(Prompt.project_id == project.id, Prompt.sent_at.is_not(None)))
    stories = db.scalar(select(func.count(Story.id)).where(Story.project_id == project.id, Story.hidden_at.is_(None)))
    return JSONResponse({"connected": bool(st and st.identity_id), "consent": st.consent_status if st else "pending",
                         "first_prompt_sent": bool(first), "stories": stories or 0, "status": project.status})


# --------------------------------------------------------------------------- dashboard
@router.get("/p/{pid}")
def dashboard(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    st = project.storyteller
    latest = db.scalars(select(Story).where(Story.project_id == project.id, Story.hidden_at.is_(None))
                        .order_by(Story.last_part_at.desc().nulls_last()).limit(4)).all()
    queued = db.scalar(select(Prompt).where(Prompt.project_id == project.id, Prompt.status.in_(["queued", "scheduled"]))
                       .order_by(Prompt.position))
    last_sent = db.scalar(select(Prompt).where(Prompt.project_id == project.id, Prompt.sent_at.is_not(None))
                          .order_by(Prompt.sent_at.desc()))
    attention = {
        "review": db.scalar(select(func.count(Story.id)).where(Story.project_id == project.id, Story.hidden_at.is_(None),
                                                               Story.status == "needs_review")) or 0,
        "held": db.scalar(select(func.count(Recording.id)).where(Recording.project_id == project.id,
                                                                 Recording.hold_reason.in_(["forwarded", "assignment"]))) or 0,
        "failed": db.scalar(select(func.count(Prompt.id)).where(Prompt.project_id == project.id,
                                                                Prompt.status.in_(["failed", "send_unknown"]))) or 0,
        "suggested": db.scalar(select(func.count(Prompt.id)).where(Prompt.project_id == project.id,
                                                                   Prompt.status == "suggested")) or 0,
    }
    notes = db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.project_id == project.id)
                       .order_by(Notification.created_at.desc()).limit(6)).all()
    phone = ""
    if st and st.identity_id:
        ident = db.get(WhatsAppIdentity, st.identity_id)
        phone = mask_phone(decrypt_phone(ident.phone_enc)) if ident and ident.phone_enc else "WhatsApp-gebruikersnaam"
    return render(request, "app/dashboard.html", **_nav(db, project, role, "overzicht"), stats=svc.project_stats(db, project),
                  latest=_story_rows(db, latest), queued=queued, last_sent=last_sent, schedule=project.schedule,
                  attention=attention, notes=notes, phone=phone, offer=commerce.dashboard_offer(db, project, role))


@router.post("/p/{pid}/aanbod/{key}/verbergen")
async def offer_dismiss(request: Request, pid: str, key: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    await form_with_csrf(request)
    commerce.dismiss_offer(project, key)
    db.commit()
    return redirect(f"/p/{pid}", "Prima, we laten dit niet meer zien.")


@router.post("/p/{pid}/meldingen/gelezen")
async def notes_read(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    await form_with_csrf(request)
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.project_id == project.id,
                                                   Notification.read_at.is_(None))).all():
        n.read_at = utcnow()
    db.commit()
    return redirect(f"/p/{pid}")


# --------------------------------------------------------------------------- stories
@router.get("/p/{pid}/verhalen")
def stories(request: Request, pid: str, filter: str = "", db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    q = select(Story).where(Story.project_id == project.id)
    if filter == "verborgen":
        q = q.where(Story.hidden_at.is_not(None))
    else:
        q = q.where(Story.hidden_at.is_(None))
        if filter == "controle":
            q = q.where(Story.status == "needs_review")
    items = db.scalars(q.order_by(Story.last_part_at.desc().nulls_last())).all()
    return render(request, "app/stories.html", **_nav(db, project, role, "verhalen"), rows=_story_rows(db, items),
                  filter=filter)


@router.get("/p/{pid}/verhalen/{sid}")
def story_detail(request: Request, pid: str, sid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    story = _story(db, project, sid)
    recs = db.scalars(select(Recording).where(Recording.story_id == story.id).order_by(Recording.received_at)).all()
    parts = []
    for r in recs:
        tr = db.scalar(select(Transcript).where(Transcript.recording_id == r.id))
        parts.append({"rec": r, "transcript": tr})
    photos = []
    for ph in db.scalars(select(Photo).where(Photo.story_id == story.id).order_by(Photo.position, Photo.created_at)).all():
        photos.append({"photo": ph, "asset": db.get(MediaAsset, ph.media_id)})
    attachments = db.scalars(select(MediaAsset).where(MediaAsset.story_id == story.id, MediaAsset.status == "stored")).all()
    revisions = db.scalars(select(StoryRevision).where(StoryRevision.story_id == story.id)
                           .order_by(StoryRevision.created_at.desc()).limit(8)).all()
    others = db.scalars(select(Story).where(Story.project_id == project.id, Story.id != story.id, Story.hidden_at.is_(None))
                        .order_by(Story.last_part_at.desc().nulls_last()).limit(30)).all()
    chapters = db.scalars(select(Chapter).where(Chapter.project_id == project.id).order_by(Chapter.position)).all()
    qr = db.scalar(select(QRLink).where(QRLink.story_id == story.id))
    prompt = db.get(Prompt, story.prompt_id) if story.prompt_id else None
    return render(request, "app/story.html", **_nav(db, project, role, "verhalen"), story=story, parts=parts,
                  photos=photos, attachments=attachments, revisions=revisions, others=others, chapters=chapters,
                  qr=qr, qr_link=qr_url(qr.token) if qr else "", qr_display=qr_display_url(qr.token) if qr else "",
                  qr_svg=Markup(qr_svg(qr_url(qr.token), scale=3, border=1)) if qr else "", prompt=prompt)


@router.post("/p/{pid}/verhalen/{sid}/{action}")
async def story_action(request: Request, pid: str, sid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "contributor")
    form = await form_with_csrf(request)
    story = _story(db, project, sid)
    back = f"/p/{pid}/verhalen/{sid}"
    msg = "Opgeslagen."
    if action != "foto" and ROLE_RANK[role] < ROLE_RANK["editor"]:
        return redirect(back, "Alleen redacteuren en eigenaren kunnen dit aanpassen.", "error")
    tmpdir = None
    try:
        if action == "bewaar":
            svc.save_story(db, story, user, str(form.get("title", "")), str(form.get("body", "")))
            msg = "Het verhaal is opgeslagen. De vorige versie staat bij Versies."
        elif action == "ai":
            if form.get("choice") == "accept":
                svc.accept_ai_suggestion(db, story, user)
                msg = "De nieuwe versie is overgenomen."
            else:
                svc.dismiss_ai_suggestion(story)
                msg = "Je eigen versie blijft staan."
        elif action == "markering":
            svc.resolve_flag(story, str(form.get("code", "")))
            msg = "Gemarkeerd als gecontroleerd."
        elif action == "boek":
            svc.require_edit(project)
            story.include_in_book = not story.include_in_book
            msg = "Dit verhaal komt in het boek." if story.include_in_book else "Dit verhaal komt niet in het boek."
        elif action == "hoofdstuk":
            svc.require_edit(project)
            cid = str(form.get("chapter_id", ""))
            ch = db.get(Chapter, cid) if cid else None
            story.chapter_id = ch.id if ch and ch.project_id == project.id else None
            msg = "Hoofdstuk aangepast."
        elif action == "verberg":
            story.hidden_at = None if story.hidden_at else utcnow()
            msg = "Het verhaal is teruggezet." if not story.hidden_at else \
                "Het verhaal is verborgen. De opnames blijven bewaard en te downloaden."
        elif action == "versie":
            rev = db.get(StoryRevision, str(form.get("revision_id", "")))
            if rev is None or rev.story_id != story.id:
                raise NotFoundError()
            svc.restore_revision(db, story, rev, user)
            msg = "Die versie is teruggezet."
        elif action == "foto":
            upload = form.get("file")
            path, name, ctype, tmpdir = await _save_upload(upload, MAX_PHOTO)
            svc.upload_photo(db, project, user, path, name, story.id, str(form.get("caption", "")))
            msg = "De foto is toegevoegd."
        else:
            raise NotFoundError()
    except svc.ServiceError as exc:
        return redirect(back, str(exc), "error")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    db.commit()
    return redirect(back, msg)


@router.post("/p/{pid}/opnames/upload")
async def recording_upload(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    form = await form_with_csrf(request)
    tmpdir = None
    try:
        path, name, ctype, tmpdir = await _save_upload(form.get("file"), MAX_AUDIO)
        rec = svc.upload_audio(db, project, user, path, name, ctype, story_id=str(form.get("story_id", "")) or None)
    except svc.ServiceError as exc:
        return redirect(f"/p/{pid}/verhalen", str(exc), "error")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    db.commit()
    return redirect(f"/p/{pid}/verhalen/{rec.story_id}", "De opname is toegevoegd en wordt uitgeschreven.")


@router.post("/p/{pid}/opnames/{rid}/{action}")
async def recording_action(request: Request, pid: str, rid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    form = await form_with_csrf(request)
    rec = db.get(Recording, rid)
    if rec is None or rec.project_id != project.id:
        raise NotFoundError()
    back = f"/p/{pid}/verhalen/{rec.story_id}" if rec.story_id else f"/p/{pid}/verhalen"
    try:
        if action == "verplaats":
            target = str(form.get("target", ""))
            story = svc.move_recording(db, rec, None if target == "nieuw" else target, user)
            back, msg = f"/p/{pid}/verhalen/{story.id}", "Het deel is verplaatst."
        elif action == "bevestig":
            keep = form.get("keep") == "1"
            svc.confirm_recording(db, rec, keep, user)
            msg = "Bevestigd, het wordt verwerkt." if keep else "Dit deel is uit het verhaal gehaald."
        elif action == "tekst":
            svc.set_manual_transcript(db, rec, str(form.get("text", "")), user)
            msg = "De tekst is opgeslagen; het verhaal wordt bijgewerkt."
        elif action == "opnieuw":
            svc.retry_transcription(db, rec)
            msg = "We proberen het opnieuw."
        else:
            raise NotFoundError()
    except svc.ServiceError as exc:
        return redirect(back, str(exc), "error")
    db.commit()
    return redirect(back, msg)


# --------------------------------------------------------------------------- questions
@router.get("/p/{pid}/vragen")
def prompts_page(request: Request, pid: str, cat: str = "", db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    queued = db.scalars(select(Prompt).where(Prompt.project_id == project.id, Prompt.status.in_(["queued", "scheduled"]))
                        .order_by(Prompt.status.desc(), Prompt.position, Prompt.created_at)).all()
    suggested = db.scalars(select(Prompt).where(Prompt.project_id == project.id, Prompt.status == "suggested")
                           .order_by(Prompt.created_at.desc())).all()
    history = db.scalars(select(Prompt).where(Prompt.project_id == project.id,
                                              Prompt.status.in_(["sent", "answered", "failed", "send_unknown"]))
                         .order_by(Prompt.sent_at.desc().nulls_first(), Prompt.created_at.desc())).all()
    lib = browse(db, project)
    current = next((c for c in lib if c[0] == cat), lib[0] if lib else None)
    askers = {u.id: u for u in db.scalars(select(User).where(User.id.in_(
        [p.suggested_by_id for p in queued + suggested + history if p.suggested_by_id]))).all()}
    return render(request, "app/prompts.html", **_nav(db, project, role, "vragen"), queued=queued, suggested=suggested,
                  history=history, library=lib, current=current, schedule=project.schedule, cadences=CADENCES,
                  askers=askers, categories=categories())


@router.post("/p/{pid}/vragen/{action}")
async def prompts_action(request: Request, pid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "contributor")
    form = await form_with_csrf(request)
    back = f"/p/{pid}/vragen"
    editor = ROLE_RANK[role] >= ROLE_RANK["editor"]
    try:
        if action == "nieuw":
            p = svc.add_custom_prompt(db, project, user, str(form.get("text", "")), role)
            msg = "De vraag staat in de wachtrij." if p.status == "queued" else \
                "Je voorstel is doorgestuurd naar de redacteuren."
        elif not editor:
            return redirect(back, "Alleen redacteuren en eigenaren kunnen dit aanpassen.", "error")
        elif action == "bibliotheek":
            svc.add_library_prompt(db, project, user, str(form.get("key", "")))
            back = f"/p/{pid}/vragen?cat={form.get('cat', '')}#bibliotheek"
            msg = "Toegevoegd aan de wachtrij."
        elif action == "nu":
            p = svc.send_now(db, project)
            msg = "De vraag wordt nu verstuurd."
        elif action == "ritme":
            svc.set_schedule(db, project, cadence_days=int(form.get("cadence", 7)), weekday=int(form.get("weekday", 6)),
                             hour=int(form.get("hour", 10)), minute=0, wait_for_answer=form.get("wait") == "1")
            msg = "Het ritme is aangepast."
        elif action == "pauze":
            svc.pause(db, project, int(form.get("weeks", 2)))
            msg = "De vragen zijn gepauzeerd."
        elif action == "hervat":
            svc.resume(db, project)
            msg = "De vragen gaan weer verder."
        else:
            raise NotFoundError()
    except svc.ServiceError as exc:
        return redirect(back, str(exc), "error")
    db.commit()
    return redirect(back, msg)


@router.post("/p/{pid}/vragen/{prid}/{action}")
async def prompt_action(request: Request, pid: str, prid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    await form_with_csrf(request)
    p = db.get(Prompt, prid)
    if p is None or p.project_id != project.id:
        raise NotFoundError()
    try:
        svc.require_edit(project)
        if action == "omhoog":
            svc.move_prompt(db, p, -1)
            msg = None
        elif action == "omlaag":
            svc.move_prompt(db, p, 1)
            msg = None
        elif action == "verwijder":
            if p.status in ("queued", "suggested"):
                p.status = "skipped"
            msg = "De vraag is uit de wachtrij gehaald."
        elif action == "goedkeuren":
            svc.approve_prompt(db, p)
            msg = "De vraag staat in de wachtrij."
        elif action == "afwijzen":
            svc.reject_prompt(db, p)
            msg = "Het voorstel is afgewezen."
        elif action == "opnieuw":
            svc.resend_prompt(db, p)
            msg = "De vraag wordt opnieuw verstuurd."
        else:
            raise NotFoundError()
    except svc.ServiceError as exc:
        return redirect(f"/p/{pid}/vragen", str(exc), "error")
    db.commit()
    return redirect(f"/p/{pid}/vragen", msg)


# --------------------------------------------------------------------------- family
@router.get("/p/{pid}/familie")
def family(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    members = db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                         .where(Membership.family_id == project.family_id).order_by(Membership.created_at)).all()
    invitations = db.scalars(select(Invitation).where(Invitation.family_id == project.family_id,
                                                      Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None),
                                                      Invitation.expires_at > utcnow())).all()
    st = project.storyteller
    phone = ""
    if st and st.identity_id:
        ident = db.get(WhatsAppIdentity, st.identity_id)
        phone = mask_phone(decrypt_phone(ident.phone_enc)) if ident and ident.phone_enc else "WhatsApp-gebruikersnaam"
    return render(request, "app/family.html", **_nav(db, project, role, "familie"), members=members,
                  invitations=invitations, phone=phone, me=user,
                  **(_connect_ctx(project) if st else {}))


@router.post("/p/{pid}/familie/{action}")
async def family_action(request: Request, pid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    form = await form_with_csrf(request)
    back = f"/p/{pid}/familie"
    try:
        if action == "uitnodigen":
            svc.invite_member(db, project, user, str(form.get("email", "")), str(form.get("role", "contributor")))
            msg = "De uitnodiging is verstuurd."
        elif action == "verteller":
            birth = str(form.get("birth_year", "")).strip()
            svc.update_storyteller(db, project, name=str(form.get("name", "")), address_as=str(form.get("address_as", "")),
                                   birth_year=int(birth) if birth.isdigit() else None, locale=str(form.get("locale", "")))
            msg = "De gegevens van de verteller zijn bijgewerkt."
        elif action == "ontkoppel":
            if ROLE_RANK[role] < ROLE_RANK["owner"]:
                return redirect(back, "Alleen eigenaren kunnen het WhatsApp-nummer ontkoppelen.", "error")
            svc.disconnect_storyteller(db, project, user)
            msg = "Het nummer is ontkoppeld. Stuur de nieuwe uitnodiging naar het juiste nummer."
        else:
            raise NotFoundError()
    except svc.ServiceError as exc:
        return redirect(back, str(exc), "error")
    db.commit()
    return redirect(back, msg)


@router.post("/p/{pid}/familie/leden/{mid}/{action}")
async def member_action(request: Request, pid: str, mid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "owner")
    form = await form_with_csrf(request)
    m = db.get(Membership, mid)
    if m is None or m.family_id != project.family_id:
        raise NotFoundError()
    try:
        if action == "rol":
            svc.change_role(db, project, m, str(form.get("role", "")))
            msg = "De rol is aangepast."
        elif action == "verwijder":
            svc.remove_member(db, project, m)
            msg = "Dit familielid heeft geen toegang meer."
        else:
            raise NotFoundError()
    except svc.ServiceError as exc:
        return redirect(f"/p/{pid}/familie", str(exc), "error")
    db.commit()
    return redirect(f"/p/{pid}/familie", msg)


@router.post("/p/{pid}/familie/uitnodigingen/{iid}/intrekken")
async def revoke_invitation(request: Request, pid: str, iid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    await form_with_csrf(request)
    inv = db.get(Invitation, iid)
    if inv is None or inv.family_id != project.family_id:
        raise NotFoundError()
    inv.revoked_at = utcnow()
    db.commit()
    return redirect(f"/p/{pid}/familie", "De uitnodiging is ingetrokken.")


# --------------------------------------------------------------------------- book
@router.get("/p/{pid}/boek")
def book_page(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    book = db.scalar(select(Book).where(Book.project_id == project.id))
    chapters = db.scalars(select(Chapter).where(Chapter.project_id == project.id).order_by(Chapter.position)).all()
    stories = db.scalars(select(Story).where(Story.project_id == project.id, Story.hidden_at.is_(None))
                         .order_by(Story.position)).all()
    by_ch: dict = {}
    for s in stories:
        by_ch.setdefault(s.chapter_id, []).append(s)
    versions = db.scalars(select(BookVersion).where(BookVersion.book_id == book.id)
                          .order_by(BookVersion.version.desc())).all()
    approved = next((v for v in versions if v.approved_at and v.status == "ready"), None)
    photos = db.execute(select(Photo, MediaAsset).join(MediaAsset, MediaAsset.id == Photo.media_id)
                        .where(Photo.project_id == project.id, MediaAsset.status == "stored")).all()
    orders = db.scalars(select(PrintOrder).where(PrintOrder.project_id == project.id)
                        .order_by(PrintOrder.created_at.desc())).all()
    pages = approved.page_count if approved else svc.estimate_pages(db, project)
    link = commerce.active_family_link(db, project)
    open_order = commerce.open_unpaid_order(db, project)
    return render(request, "app/book.html", **_nav(db, project, role, "boek"), book=book, chapters=chapters, by_ch=by_ch,
                  versions=versions, approved=approved, photos=photos, credits=svc.print_credits(db, project),
                  pages=pages, tier=pricing.page_tier(pages), orders=orders, pricing=pricing,
                  family_link=link, family_url=commerce.family_link_url(link) if link else "",
                  open_order=open_order, open_order_url=commerce.bridge_url(open_order, book.title) if open_order else "",
                  estimated=approved is None)


@router.get("/p/{pid}/boek/versie/{vid}")
def book_version(request: Request, pid: str, vid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    book = db.scalar(select(Book).where(Book.project_id == project.id))
    version = db.get(BookVersion, vid)
    if version is None or version.book_id != book.id:
        raise NotFoundError()
    return render(request, "app/book_version.html", **_nav(db, project, role, "boek"), book=book, version=version,
                  credits=svc.print_credits(db, project), extra_url=extra_book_url(project))


@router.post("/p/{pid}/boek/{action}")
async def book_action(request: Request, pid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    form = await form_with_csrf(request)
    back = f"/p/{pid}/boek"
    try:
        if action == "instellingen":
            svc.update_book(db, project, title=str(form.get("title", "")), subtitle=str(form.get("subtitle", "")),
                            dedication=str(form.get("dedication", "")), cover_style=str(form.get("cover_style", "nacht")),
                            cover_photo_id=str(form.get("cover_photo_id", "")) or None)
            msg = "De boekinstellingen zijn opgeslagen."
        elif action == "hoofdstuk":
            svc.create_chapter(db, project, str(form.get("title", "")))
            msg = "Hoofdstuk toegevoegd."
        elif action == "voorbeeld":
            version = svc.generate_preview(db, project, user)
            db.commit()
            return redirect(f"/p/{pid}/boek/versie/{version.id}", "We maken het voorbeeld. Dat duurt meestal minder dan "
                                                                  "een minuut.")
        elif action == "bestellen":
            version = db.get(BookVersion, str(form.get("version_id", "")))
            if version is None:
                raise NotFoundError()
            shipping = {k: form.get(k, "") for k in ("naam", "straat", "postcode", "plaats", "land")}
            qty = str(form.get("quantity", "1") or "1")
            po = svc.order_print(db, project, version, user, shipping, quantity=int(qty) if qty.isdigit() else 1)
            db.commit()
            if po.payment_status == "awaiting_payment":
                book = db.scalar(select(Book).where(Book.project_id == project.id))
                return RedirectResponse(commerce.bridge_url(po, book.title), status_code=303)
            msg = "Besteld! We sturen het boek naar de drukker en houden je op de hoogte."
        elif action == "familielink":
            commerce.create_family_link(db, project, user)
            msg = "De familielink staat klaar. Kopieer hem en stuur hem naar wie je wilt."
        elif action == "familielink-intrekken":
            commerce.revoke_family_link(db, project, user)
            msg = "De familielink werkt niet meer. Je kunt altijd een nieuwe maken."
        elif action in ("bestelling-annuleren", "bestelling-afrekenen"):
            po = db.get(PrintOrder, str(form.get("order_id", "")))
            if po is None or po.project_id != project.id:
                raise NotFoundError()
            if action == "bestelling-afrekenen":
                book = db.scalar(select(Book).where(Book.project_id == project.id))
                return RedirectResponse(commerce.bridge_url(po, book.title), status_code=303)
            commerce.abandon_order(db, po, user)
            msg = "De bestelling is geannuleerd. Je boektegoed staat weer klaar."
        else:
            raise NotFoundError()
    except (svc.ServiceError, commerce.CommerceError) as exc:
        return redirect(back, str(exc), "error")
    db.commit()
    return redirect(back, msg)


@router.post("/p/{pid}/boek/versie/{vid}/goedkeuren")
async def book_approve(request: Request, pid: str, vid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    await form_with_csrf(request)
    version = db.get(BookVersion, vid)
    if version is None:
        raise NotFoundError()
    try:
        svc.approve_version(db, project, version, user)
    except svc.ServiceError as exc:
        return redirect(f"/p/{pid}/boek/versie/{vid}", str(exc), "error")
    db.commit()
    return redirect(f"/p/{pid}/boek#bestellen", "Goedgekeurd. Je kunt het boek nu bestellen.")


@router.post("/p/{pid}/boek/hoofdstukken/{cid}/{action}")
async def chapter_action(request: Request, pid: str, cid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    form = await form_with_csrf(request)
    svc.require_edit(project)
    ch = db.get(Chapter, cid)
    if ch is None or ch.project_id != project.id:
        raise NotFoundError()
    msg = None
    if action == "hernoem":
        ch.title = str(form.get("title", "")).strip()[:200] or ch.title
        msg = "Hoofdstuk hernoemd."
    elif action in ("omhoog", "omlaag"):
        svc.move_chapter(db, project, ch, -1 if action == "omhoog" else 1)
    elif action == "verwijder":
        if db.scalar(select(Story.id).where(Story.chapter_id == ch.id, Story.hidden_at.is_(None))):
            return redirect(f"/p/{pid}/boek", "Verplaats eerst de verhalen uit dit hoofdstuk.", "error")
        db.delete(ch)
        msg = "Hoofdstuk verwijderd."
    else:
        raise NotFoundError()
    db.commit()
    return redirect(f"/p/{pid}/boek#indeling", msg)


@router.post("/p/{pid}/boek/verhalen/{sid}/{action}")
async def book_story_action(request: Request, pid: str, sid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid, "editor")
    form = await form_with_csrf(request)
    svc.require_edit(project)
    story = _story(db, project, sid)
    if action in ("omhoog", "omlaag"):
        svc.move_story(db, story, -1 if action == "omhoog" else 1)
    elif action == "hoofdstuk":
        cid = str(form.get("chapter_id", ""))
        ch = db.get(Chapter, cid) if cid else None
        story.chapter_id = ch.id if ch and ch.project_id == project.id else None
    elif action == "boek":
        story.include_in_book = not story.include_in_book
    else:
        raise NotFoundError()
    db.commit()
    return redirect(f"/p/{pid}/boek#indeling")


# --------------------------------------------------------------------------- downloads & settings
@router.get("/p/{pid}/downloads")
def downloads(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    exports = db.scalars(select(Export).where(Export.project_id == project.id)
                         .order_by(Export.created_at.desc()).limit(4)).all()
    stories = db.scalars(select(Story).where(Story.project_id == project.id).order_by(Story.position)).all()
    rows = []
    for s in stories:
        recs = db.scalars(select(Recording).where(Recording.story_id == s.id, Recording.kind == "audio",
                                                  Recording.retracted_at.is_(None)).order_by(Recording.received_at)).all()
        qr = db.scalar(select(QRLink).where(QRLink.story_id == s.id))
        rows.append({"story": s, "recs": recs, "qr": qr})
    loose = db.scalars(select(Recording).where(Recording.project_id == project.id, Recording.story_id.is_(None),
                                               Recording.kind == "audio")).all()
    book = db.scalar(select(Book).where(Book.project_id == project.id))
    version = db.scalar(select(BookVersion).where(BookVersion.book_id == book.id, BookVersion.status == "ready")
                        .order_by(BookVersion.approved_at.is_(None), BookVersion.version.desc()))
    photo_count = db.scalar(select(func.count(Photo.id)).where(Photo.project_id == project.id)) or 0
    exports_assets = {e.id: db.get(MediaAsset, e.media_id) for e in exports if e.media_id}
    return render(request, "app/downloads.html", **_nav(db, project, role, "downloads"), exports=exports,
                  export_assets=exports_assets, rows=rows, loose=loose, version=version, photo_count=photo_count)


@router.post("/p/{pid}/downloads/archief")
async def downloads_archive(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    await form_with_csrf(request)
    request_export(db, project, user.id)
    db.commit()
    return redirect(f"/p/{pid}/downloads", "We zetten alles in één zip-bestand. Je krijgt een mail als het klaar is.")


@router.get("/p/{pid}/instellingen")
def settings_page(request: Request, pid: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    qr_pin = db.scalar(select(QRLink.id).where(QRLink.project_id == project.id, QRLink.access_mode == "pin")) is not None
    return render(request, "app/settings.html", **_nav(db, project, role, "instellingen"), me=user, qr_pin=qr_pin,
                  extension_url=extension_url(project), renewal_options=commerce.renewal_options(project),
                  renewal_url=commerce.renewal_url(project), renewal_legacy_url=commerce.renewal_url(project, True))


@router.post("/p/{pid}/instellingen/{action}")
async def settings_action(request: Request, pid: str, action: str, db: Session = Depends(db_session)):
    user, project, role = _load(request, db, pid)
    form = await form_with_csrf(request)
    back = f"/p/{pid}/instellingen"
    if action == "meldingen":
        mode = str(form.get("notify_mode", "immediate"))
        user.notify_mode = mode if mode in ("immediate", "daily", "off") else "immediate"
        name = str(form.get("name", "")).strip()
        if name:
            user.name = name[:200]
        db.commit()
        return redirect(back, "Je voorkeuren zijn opgeslagen.")
    if ROLE_RANK[role] < ROLE_RANK["owner"]:
        return redirect(back, "Alleen eigenaren kunnen deze instellingen wijzigen.", "error")
    if action == "project":
        project.sensitive_topics = form.get("sensitive") == "1"
        project.vocabulary = str(form.get("vocabulary", ""))[:2000]
        msg = "De projectinstellingen zijn opgeslagen."
    elif action == "qr":
        pin = str(form.get("pin", "")).strip()
        links = db.scalars(select(QRLink).where(QRLink.project_id == project.id)).all()
        if form.get("mode") == "pin":
            if not (pin.isdigit() and 4 <= len(pin) <= 8):
                return redirect(back, "Kies een pincode van 4 tot 8 cijfers.", "error")
            ph = hash_pin(pin)
            for link in links:
                link.access_mode, link.pin_hash = "pin", ph
            msg = "De QR-codes vragen nu om een pincode. Zet die pincode voor in het boek of vertel hem aan de familie."
        else:
            for link in links:
                link.access_mode, link.pin_hash = "link", ""
            msg = "De QR-codes werken weer zonder pincode."
    elif action == "verwijderen":
        if str(form.get("confirm", "")).strip() != project.title:
            return redirect(back, "Typ de titel precies over om te bevestigen.", "error")
        svc.request_deletion(db, project, user)
        msg = "Verwijdering aangevraagd. Over 14 dagen wissen we alles definitief."
    elif action == "annuleren":
        svc.cancel_deletion(db, project, user)
        msg = "De verwijdering is geannuleerd. Alles blijft bewaard."
    else:
        raise NotFoundError()
    db.commit()
    return redirect(back, msg)
