"""Family-side operations used by the web app and tests. Authorisation happens in the web layer
(web/deps.py: project_for_user); these functions enforce business rules such as the permanent-access policy."""
from __future__ import annotations

import math
import re
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import copy_nl
from .analytics import track
from .audio import format_duration
from .audit import audit
from .config import get_settings
from .db import utcnow
from .handlers import add_flag, bump_compose, place_recording
from .jobs import enqueue, job, PermanentFailure
from .mailer import send_mail
from .models import (ROLE_RANK, Book, BookVersion, Chapter, Entitlement, Family, Invitation, MediaAsset, Membership,
                     Notification, Photo, PrintOrder, Project, Prompt, PromptSchedule, QRLink, Recording, Story,
                     StoryRevision, Storyteller, Transcript, User, WhatsAppMessage)
from .notify import notify_organizers
from .prompts import add_from_library, next_position, next_prompt
from .qr import wa_join_link
from .scheduler import compute_next_send
from .security import generate_join_code, hash_token, new_token
from .storage import base_mime, get_storage, store_path


class ReadOnlyProject(Exception):
    """Editing belongs to an active storytelling year; viewing, listening and downloading never stop."""


class ServiceError(Exception):
    pass


def can_edit(project: Project) -> bool:
    if project.status in ("setup", "awaiting_storyteller", "active"):
        return True
    if project.active_until:
        return utcnow() <= project.active_until + timedelta(days=project.grace_days)
    return False


def require_edit(project: Project) -> None:
    if not can_edit(project):
        raise ReadOnlyProject("Het verteljaar is afgelopen. Alles blijft te bekijken, beluisteren en downloaden; "
                              "bewerken hoort bij een actief verteljaar.")


# --------------------------------------------------------------------------- projects
def unique_join_code(session: Session) -> str:
    code = generate_join_code()
    while session.scalar(select(Storyteller.id).where(Storyteller.join_code == code)):
        code = generate_join_code()
    return code


def create_project(session: Session, owner: User, *, storyteller_name: str, address_as: str = "",
                   birth_year: int | None = None, locale: str = "nl-NL", purchase_kind: str = "gift",
                   gift_from_name: str = "", gift_message: str = "", entitlement: Entitlement | None = None,
                   family: Family | None = None) -> Project:
    """A storyteller project. With `family` (an extra storyteller) it joins an existing family and its members."""
    from . import commerce

    name = storyteller_name.strip()
    if not name:
        raise ServiceError("Vul de naam van de verteller in.")
    if family is None:
        family = Family(name=f"Familie van {name}")
        session.add(family)
        session.flush()
        session.add(Membership(family_id=family.id, user_id=owner.id, role="owner"))
    title = f"De verhalen van {name.split(' ')[0]}"
    project = Project(family_id=family.id, title=title, locale=locale if locale in ("nl-NL", "nl-BE") else "nl-NL",
                      purchase_kind=purchase_kind, gift_from_name=gift_from_name.strip()[:200],
                      gift_message=gift_message.strip()[:2000], created_by_id=owner.id, status="setup")
    session.add(project)
    session.flush()
    session.add(Storyteller(project_id=project.id, name=name[:120], address_as=(address_as or name.split(" ")[0])[:60],
                            birth_year=birth_year, join_code=unique_join_code(session)))
    session.add(PromptSchedule(project_id=project.id,
                               timezone="Europe/Brussels" if project.locale == "nl-BE" else "Europe/Amsterdam"))
    session.add(Book(project_id=project.id, title=title))
    if entitlement is not None:
        entitlement.status, entitlement.project_id, entitlement.used_at = "used", project.id, utcnow()
    session.flush()
    commerce.on_project_created(session, project, entitlement)
    audit(session, "project_created", project_id=project.id, actor_kind="user", actor_id=owner.id)
    track(session, "onboarding_started", project.id, gift=purchase_kind == "gift", locale=project.locale)
    return project


def join_short_link(project: Project) -> str:
    """Short, readable link for forwarded invitations; it redirects to WhatsApp with the join code filled in."""
    return f"{get_settings().base_url}/doe-mee/{project.storyteller.join_code}"


def storyteller_invite_text(project: Project) -> str:
    return ("Hoi! Ik heb iets voor je geregeld: Vertelschat. Je krijgt af en toe een vraag via WhatsApp over vroeger, "
            "en je antwoordt gewoon met een spraakbericht, wanneer het jou uitkomt. Zo bewaren we je verhalen, in je "
            "eigen stem. Tik op deze link en druk daarna op verzenden:\n"
            f"{join_short_link(project)}")


def update_storyteller(session: Session, project: Project, *, name: str, address_as: str, birth_year: int | None,
                       locale: str) -> None:
    st = project.storyteller
    st.name = name.strip()[:120] or st.name
    st.address_as = address_as.strip()[:60] or st.name.split(" ")[0]
    st.birth_year = birth_year
    if locale in ("nl-NL", "nl-BE"):
        project.locale = locale


def disconnect_storyteller(session: Session, project: Project, user: User) -> None:
    st = project.storyteller
    st.identity_id = None
    st.connected_at = None
    st.consent_status = "pending"
    st.join_code = unique_join_code(session)
    audit(session, "storyteller_disconnected", project_id=project.id, actor_kind="user", actor_id=user.id)


def set_schedule(session: Session, project: Project, *, cadence_days: int, weekday: int, hour: int, minute: int,
                 wait_for_answer: bool) -> None:
    require_edit(project)
    sched = session.get(PromptSchedule, project.id)
    sched.cadence_days = cadence_days if cadence_days in (7, 14, 28) else 7
    sched.weekday = min(6, max(0, weekday))
    sched.send_hour = min(21, max(8, hour))
    sched.send_minute = 0 if minute not in (0, 15, 30, 45) else minute
    sched.wait_for_answer = wait_for_answer
    if project.status == "active":
        base = max(utcnow(), (sched.last_sent_at or utcnow()) + timedelta(days=max(1, sched.cadence_days - 1)))
        sched.next_send_at = compute_next_send(sched, base)


def pause(session: Session, project: Project, weeks: int) -> None:
    sched = session.get(PromptSchedule, project.id)
    sched.paused_until = utcnow() + timedelta(weeks=max(1, min(12, weeks)))


def resume(session: Session, project: Project) -> None:
    sched = session.get(PromptSchedule, project.id)
    sched.paused_until = None
    if project.status == "active":
        sched.next_send_at = compute_next_send(sched, utcnow())


# --------------------------------------------------------------------------- questions
def add_custom_prompt(session: Session, project: Project, user: User, text: str, role: str) -> Prompt:
    require_edit(project)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 8:
        raise ServiceError("Schrijf een iets langere vraag.")
    if len(text) > 300:
        raise ServiceError("Houd de vraag korter dan 300 tekens; korte vragen werken het best.")
    status = "queued" if ROLE_RANK.get(role, 0) >= ROLE_RANK["editor"] else "suggested"
    p = Prompt(project_id=project.id, text=text, source="family", status=status, suggested_by_id=user.id,
               position=next_position(session, project.id), category="familie")
    session.add(p)
    session.flush()
    if status == "suggested":
        notify_organizers(session, project, "prompt_suggested", f"{user.first_name} stelt een vraag voor",
                          f"\u201c{text}\u201d", url=f"/p/{project.id}/vragen", email=False)
    return p


def add_library_prompt(session: Session, project: Project, user: User, key: str) -> Prompt | None:
    require_edit(project)
    return add_from_library(session, project, key, user_id=user.id)


def approve_prompt(session: Session, prompt: Prompt) -> None:
    if prompt.status == "suggested":
        prompt.status = "queued"
        prompt.position = next_position(session, prompt.project_id)


def reject_prompt(session: Session, prompt: Prompt) -> None:
    if prompt.status in ("suggested", "queued"):
        prompt.status = "rejected"


def move_prompt(session: Session, prompt: Prompt, direction: int) -> None:
    queue = session.scalars(select(Prompt).where(Prompt.project_id == prompt.project_id, Prompt.status == "queued")
                            .order_by(Prompt.position, Prompt.created_at)).all()
    for i, p in enumerate(queue):
        p.position = i + 1
    idx = next((i for i, p in enumerate(queue) if p.id == prompt.id), None)
    j = (idx or 0) + direction
    if idx is None or j < 0 or j >= len(queue):
        return
    queue[idx].position, queue[j].position = queue[j].position, queue[idx].position


def send_now(session: Session, project: Project) -> Prompt:
    require_edit(project)
    st = project.storyteller
    if not st or not st.can_receive_prompts:
        raise ServiceError(f"{st.name if st else 'De verteller'} is nog niet verbonden via WhatsApp.")
    if session.scalar(select(Prompt.id).where(Prompt.project_id == project.id, Prompt.status == "scheduled")):
        raise ServiceError("Er wordt al een vraag verstuurd.")
    p = next_prompt(session, project)
    if p is None:
        raise ServiceError("Er staat geen vraag klaar.")
    p.status = "scheduled"
    enqueue(session, "send_prompt", {"prompt_id": p.id}, dedupe_key=f"send_prompt:{p.id}")
    return p


def resend_prompt(session: Session, prompt: Prompt) -> None:
    if prompt.status not in ("failed", "send_unknown"):
        raise ServiceError("Deze vraag hoeft niet opnieuw verstuurd te worden.")
    attempts = session.scalar(select(func.count(WhatsAppMessage.id)).where(WhatsAppMessage.prompt_id == prompt.id)) or 0
    prompt.status = "scheduled"
    prompt.failure_reason = ""
    key = f":r{attempts + 1}"
    enqueue(session, "send_prompt", {"prompt_id": prompt.id, "attempt_key": key},
            dedupe_key=f"send_prompt:{prompt.id}{key}")


# --------------------------------------------------------------------------- family
def invite_member(session: Session, project: Project, inviter: User, email: str, role: str) -> Invitation:
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ServiceError("Dat e-mailadres lijkt niet te kloppen.")
    if role not in ("editor", "contributor", "viewer"):
        role = "contributor"
    token = new_token()
    inv = Invitation(family_id=project.family_id, email=email, role=role, token_hash=hash_token(token),
                     invited_by_id=inviter.id, expires_at=utcnow() + timedelta(days=30))
    session.add(inv)
    session.flush()
    st = project.storyteller
    link = f"{get_settings().base_url}/uitnodiging/{token}"
    send_mail(session, email, f"{inviter.first_name} nodigt je uit voor {project.title}",
              f"{inviter.first_name} verzamelt de verhalen van {st.name if st else 'de familie'} met Vertelschat: "
              "vragen via WhatsApp, antwoorden als spraakbericht, en straks een boek met QR-codes naar de echte stem."
              "\n\nMet deze uitnodiging kun je meeluisteren, meelezen en zelf vragen voorstellen.",
              cta_url=link, cta_label="Uitnodiging bekijken")
    track(session, "family_member_invited", project.id, role=role)
    audit(session, "member_invited", project_id=project.id, actor_kind="user", actor_id=inviter.id, role=role)
    return inv


def accept_invitation(session: Session, token: str, user: User) -> Family | None:
    inv = session.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    if inv is None or inv.revoked_at or inv.expires_at < utcnow():
        return None
    if inv.accepted_at is None:
        existing = session.scalar(select(Membership).where(Membership.family_id == inv.family_id,
                                                           Membership.user_id == user.id))
        if existing is None:
            session.add(Membership(family_id=inv.family_id, user_id=user.id, role=inv.role))
        inv.accepted_at = utcnow()
    return session.get(Family, inv.family_id)


def change_role(session: Session, project: Project, membership: Membership, role: str) -> None:
    if role not in ("owner", "editor", "contributor", "viewer"):
        raise ServiceError("Onbekende rol.")
    owners = session.scalar(select(func.count(Membership.id)).where(Membership.family_id == project.family_id,
                                                                    Membership.role == "owner")) or 0
    if membership.role == "owner" and role != "owner" and owners <= 1:
        raise ServiceError("Er moet altijd iemand eigenaar blijven.")
    membership.role = role


def remove_member(session: Session, project: Project, membership: Membership) -> None:
    change_role(session, project, membership, "viewer")  # validates the last-owner rule
    session.delete(membership)


# --------------------------------------------------------------------------- stories
def save_story(session: Session, story: Story, user: User, title: str, body: str) -> None:
    project = session.get(Project, story.project_id)
    require_edit(project)
    title = title.strip()[:300] or story.title
    body = body.replace("\r\n", "\n").strip()
    if title == story.title and body == story.body:
        return
    story.title, story.body = title, body
    story.edited_by_user = True
    session.add(StoryRevision(story_id=story.id, title=title, body=body, author_kind="user", author_id=user.id))
    audit(session, "story_edited", project_id=project.id, actor_kind="user", actor_id=user.id, target_id=story.id)


def accept_ai_suggestion(session: Session, story: Story, user: User) -> None:
    if not story.ai_suggestion_body:
        return
    save_story(session, story, user, story.ai_suggestion_title or story.title, story.ai_suggestion_body)
    story.ai_suggestion_body = story.ai_suggestion_title = ""
    resolve_flag(story, "ai_suggestion")


def dismiss_ai_suggestion(story: Story) -> None:
    story.ai_suggestion_body = story.ai_suggestion_title = ""
    resolve_flag(story, "ai_suggestion")


def resolve_flag(story: Story, code: str) -> None:
    story.review_flags = [f for f in (story.review_flags or []) if f.get("code") != code]
    if story.status == "needs_review" and not any(f.get("level") == "check" for f in story.review_flags):
        story.status = "ready"


def restore_revision(session: Session, story: Story, revision: StoryRevision, user: User) -> None:
    save_story(session, story, user, revision.title, revision.body)


def confirm_recording(session: Session, rec: Recording, keep: bool, user: User) -> None:
    story = session.get(Story, rec.story_id) if rec.story_id else None
    if keep:
        if rec.hold_reason == "forwarded":
            rec.hold_reason = None
            if rec.status == "stored":
                enqueue(session, "transcribe", {"recording_id": rec.id}, dedupe_key=f"transcribe:{rec.id}")
        if story:
            for code in ("forwarded", "early", "addendum", "late"):
                resolve_flag(story, code)
    else:
        rec.retracted_at = utcnow()
        if story:
            for code in ("forwarded", "early"):
                resolve_flag(story, code)
            bump_compose(session, story, delay=1)
    audit(session, "recording_confirmed" if keep else "recording_removed", project_id=rec.project_id,
          actor_kind="user", actor_id=user.id, target_id=rec.id)


def move_recording(session: Session, rec: Recording, target_story_id: str | None, user: User) -> Story:
    project = session.get(Project, rec.project_id)
    require_edit(project)
    old = session.get(Story, rec.story_id) if rec.story_id else None
    if target_story_id:
        target = session.get(Story, target_story_id)
        if target is None or target.project_id != project.id:
            raise ServiceError("Dat verhaal bestaat niet.")
        rec.story_id, rec.assignment = target.id, "manual"
        target.last_part_at = max(target.last_part_at or rec.received_at, rec.received_at)
    else:
        pos = (session.scalar(select(func.max(Story.position)).where(Story.project_id == project.id)) or 0) + 1
        target = Story(project_id=project.id, status="processing", first_part_at=rec.received_at,
                       last_part_at=rec.received_at, position=pos, title="Een eigen verhaal",
                       chapter_id=old.chapter_id if old else None)
        session.add(target)
        session.flush()
        rec.story_id, rec.assignment = target.id, "manual"
    for s in (old, target):
        if s is not None:
            bump_compose(session, s, delay=1)
    if old is not None:
        resolve_flag(old, "addendum")
        resolve_flag(old, "late")
        remaining = session.scalar(select(func.count(Recording.id)).where(Recording.story_id == old.id,
                                                                          Recording.id != rec.id))
        if not remaining:
            old.hidden_at = utcnow()
    audit(session, "recording_moved", project_id=project.id, actor_kind="user", actor_id=user.id, target_id=rec.id)
    return target


def set_manual_transcript(session: Session, rec: Recording, text: str, user: User) -> None:
    text = text.strip()
    if not text:
        raise ServiceError("De tekst is leeg.")
    tr = session.scalar(select(Transcript).where(Transcript.recording_id == rec.id))
    if tr is None:
        session.add(Transcript(recording_id=rec.id, text=text, provider="handmatig", edited=True))
    else:
        tr.text, tr.edited = text, True
    rec.status = "transcribed"
    story = session.get(Story, rec.story_id) if rec.story_id else None
    if story:
        resolve_flag(story, "manual_transcript")
        resolve_flag(story, "transcription_failed")
        bump_compose(session, story, delay=1)


def retry_transcription(session: Session, rec: Recording) -> None:
    rec.status = "stored"
    rec.failure_reason = ""
    enqueue(session, "transcribe", {"recording_id": rec.id}, dedupe_key=f"transcribe:{rec.id}:{new_token(4)}")


def upload_audio(session: Session, project: Project, user: User, path: Path, filename: str, mime: str,
                 story_id: str | None = None) -> Recording:
    """A family member adds a recording made elsewhere (e.g. on their own phone at the kitchen table)."""
    require_edit(project)
    from .audio import AudioError, probe

    try:
        probe(path)
    except AudioError as exc:
        raise ServiceError("Dit bestand is geen afspeelbare audio.") from exc
    asset = store_path(session, path, kind="audio", mime=mime or "audio/mpeg", project_id=project.id, source="upload",
                       original_filename=filename[:300])
    st = project.storyteller
    rec = Recording(project_id=project.id, storyteller_id=st.id if st else None, kind="audio", source="upload",
                    original_media_id=asset.id, status="stored", received_at=utcnow(), acknowledged=True)
    session.add(rec)
    session.flush()
    if story_id and (story := session.get(Story, story_id)) and story.project_id == project.id:
        rec.story_id, rec.assignment = story.id, "manual"
        story.last_part_at = utcnow()
    else:
        pos = (session.scalar(select(func.max(Story.position)).where(Story.project_id == project.id)) or 0) + 1
        story = Story(project_id=project.id, status="processing", first_part_at=utcnow(), last_part_at=utcnow(),
                      position=pos, title=Path(filename).stem[:120] or "Een opname")
        session.add(story)
        session.flush()
        rec.story_id, rec.assignment = story.id, "manual"
    enqueue(session, "prepare_audio", {"recording_id": rec.id}, dedupe_key=f"prepare:{rec.id}")
    audit(session, "recording_uploaded", project_id=project.id, actor_kind="user", actor_id=user.id, target_id=rec.id)
    return rec


def upload_photo(session: Session, project: Project, user: User, path: Path, filename: str,
                 story_id: str | None, caption: str) -> Photo:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            width, height = im.size
            fmt = (im.format or "").lower()
    except (UnidentifiedImageError, OSError) as exc:
        raise ServiceError("Dit bestand is geen foto die we kunnen lezen (JPG, PNG of WebP).") from exc
    mime = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(fmt)
    if mime is None:
        raise ServiceError("Gebruik een JPG-, PNG- of WebP-foto.")
    asset = store_path(session, path, kind="image", mime=mime, project_id=project.id, source="upload",
                       original_filename=filename[:300], width=width, height=height)
    story = session.get(Story, story_id) if story_id else None
    photo = Photo(project_id=project.id, story_id=story.id if story and story.project_id == project.id else None,
                  media_id=asset.id, caption=caption.strip()[:500], source="upload", uploaded_by_id=user.id)
    session.add(photo)
    session.flush()
    return photo


# --------------------------------------------------------------------------- book
def update_book(session: Session, project: Project, *, title: str, subtitle: str, dedication: str, cover_style: str,
                cover_photo_id: str | None) -> Book:
    require_edit(project)
    book = session.scalar(select(Book).where(Book.project_id == project.id))
    book.title = title.strip()[:120] or book.title
    book.subtitle = subtitle.strip()[:120]
    book.dedication = dedication.strip()[:600]
    book.cover_style = cover_style if cover_style in ("nacht", "schemer", "foto") else "nacht"
    book.cover_photo_id = cover_photo_id or None
    return book


def create_chapter(session: Session, project: Project, title: str) -> Chapter:
    require_edit(project)
    pos = (session.scalar(select(func.max(Chapter.position)).where(Chapter.project_id == project.id)) or 0) + 1
    ch = Chapter(project_id=project.id, title=title.strip()[:200] or "Nieuw hoofdstuk", position=pos)
    session.add(ch)
    session.flush()
    return ch


def move_chapter(session: Session, project: Project, chapter: Chapter, direction: int) -> None:
    chapters = session.scalars(select(Chapter).where(Chapter.project_id == project.id).order_by(Chapter.position)).all()
    idx = next((i for i, c in enumerate(chapters) if c.id == chapter.id), None)
    j = (idx or 0) + direction
    if idx is None or j < 0 or j >= len(chapters):
        return
    for i, c in enumerate(chapters):
        c.position = i + 1
    chapters[idx].position, chapters[j].position = chapters[j].position, chapters[idx].position


def move_story(session: Session, story: Story, direction: int) -> None:
    siblings = session.scalars(select(Story).where(Story.project_id == story.project_id,
                                                   Story.chapter_id == story.chapter_id, Story.hidden_at.is_(None))
                               .order_by(Story.position)).all()
    idx = next((i for i, s in enumerate(siblings) if s.id == story.id), None)
    j = (idx or 0) + direction
    if idx is None or j < 0 or j >= len(siblings):
        return
    siblings[idx].position, siblings[j].position = siblings[j].position, siblings[idx].position


def generate_preview(session: Session, project: Project, user: User) -> BookVersion:
    require_edit(project)
    book = session.scalar(select(Book).where(Book.project_id == project.id))
    busy = session.scalar(select(BookVersion).where(BookVersion.book_id == book.id, BookVersion.status == "rendering"))
    if busy:
        return busy
    n = (session.scalar(select(func.max(BookVersion.version)).where(BookVersion.book_id == book.id)) or 0) + 1
    version = BookVersion(book_id=book.id, version=n, status="rendering")
    session.add(version)
    session.flush()
    enqueue(session, "build_book", {"version_id": version.id}, dedupe_key=f"book:{version.id}")
    return version


def approve_version(session: Session, project: Project, version: BookVersion, user: User) -> None:
    require_edit(project)
    if version.status != "ready":
        raise ServiceError("Dit voorbeeld is nog niet klaar.")
    version.approved_at = utcnow()
    version.approved_by_id = user.id
    track(session, "book_approved", project.id, pages=version.page_count)
    audit(session, "book_approved", project_id=project.id, actor_kind="user", actor_id=user.id, target_id=version.id)


def print_credits(session: Session, project: Project) -> dict:
    from .commerce import credits_summary

    return credits_summary(session, project)


def order_print(session: Session, project: Project, version: BookVersion, user: User, shipping: dict,
                quantity: int = 1) -> PrintOrder:
    """Order copies of an approved book to one address; unpaid copies are paid in the shop before printing."""
    from .commerce import CommerceError, place_order

    try:
        po, _quote = place_order(session, project, version, user, shipping, quantity)
    except CommerceError as exc:
        raise ServiceError(str(exc)) from exc
    return po


@job("submit_print", max_attempts=5)
def submit_print(session: Session, payload: dict) -> None:
    """Default 'manual' fulfilment: operations receives the print files and address and places the order with the
    print partner (Peecho/Gelato dashboards). API adapters are added once a supplier contract is signed."""
    from .storage import asset_url

    po = session.get(PrintOrder, payload["print_order_id"])
    if po is None or po.status != "awaiting_submission" or po.payment_status not in ("prepaid", "paid"):
        return  # never print copies that are not paid
    s = get_settings()
    if po.provider != "manual":
        raise PermanentFailure(f"Drukker-API '{po.provider}' is nog niet gekoppeld; zet PRINT_PROVIDER=manual.")
    version = session.get(BookVersion, po.book_version_id)
    project = session.get(Project, po.project_id)
    links = []
    for mid, label in ((version.interior_media_id, "Binnenwerk"), (version.cover_media_id, "Omslag")):
        asset = session.get(MediaAsset, mid)
        url = asset_url(asset, disposition="attachment", ttl=7 * 24 * 3600)
        links.append(f"{label}: {url if url.startswith('http') else s.base_url + url}")
    addr = po.shipping
    what = {"family_link": "familie-bestelling via deellink", "app": "bestelling uit de app"}.get(po.source, po.source)
    send_mail(session, s.support_email, f"Drukorder {po.id[:8]} ({project.title})",
              f"Nieuwe drukorder ({what}): {po.quantity} {'exemplaar' if po.quantity == 1 else 'exemplaren'}, "
              f"{version.page_count} pagina's, versie {version.version}. Omvangklasse: "
              f"{'tot 240' if not po.page_tier else ('tot 320' if po.page_tier == 1 else 'tot 400')} pagina's."
              f"\n\n" + "\n".join(links) +
              f"\n\nVerzenden naar:\n{addr['naam']}\n{addr['straat']}\n{addr['postcode']} {addr['plaats']}\n{addr['land']}"
              "\n\nZet de status in de beheeromgeving op 'submitted' zodra de order bij de drukker staat.")
    po.status = "submitted"
    if po.source == "family_link":
        return
    notify_organizers(session, project, "print_update", "Je boek is besteld",
                      "We sturen de bestanden naar de drukker. Reken op ongeveer twee tot drie weken. Je hoort van ons "
                      "zodra het boek onderweg is.", url=f"/p/{project.id}/boek")


# --------------------------------------------------------------------------- overview
def estimate_pages(session: Session, project: Project) -> int:
    stories = session.scalars(select(Story).where(Story.project_id == project.id, Story.hidden_at.is_(None),
                                                  Story.include_in_book.is_(True))).all()
    photos = session.scalar(select(func.count(Photo.id)).where(Photo.project_id == project.id,
                                                               Photo.story_id.is_not(None),
                                                               Photo.include_in_book.is_(True))) or 0
    pages = 12
    for s in stories:
        words = len((s.body or "").split())
        pages += max(1, math.ceil((words + 90) / 330))
    pages += math.ceil(photos * 0.45)
    chapters = session.scalar(select(func.count(Chapter.id)).where(Chapter.project_id == project.id)) or 0
    pages += chapters * 2
    return int(math.ceil(pages / 4.0) * 4)


def project_stats(session: Session, project: Project) -> dict:
    stories = session.scalars(select(Story).where(Story.project_id == project.id, Story.hidden_at.is_(None))).all()
    seconds = session.scalar(select(func.coalesce(func.sum(Recording.duration_seconds), 0))
                             .where(Recording.project_id == project.id, Recording.retracted_at.is_(None))) or 0
    photos = session.scalar(select(func.count(Photo.id)).where(Photo.project_id == project.id)) or 0
    sent = session.scalar(select(func.count(Prompt.id)).where(Prompt.project_id == project.id,
                                                              Prompt.sent_at.is_not(None))) or 0
    answered = session.scalar(select(func.count(Prompt.id)).where(Prompt.project_id == project.id,
                                                                  Prompt.status == "answered")) or 0
    members = session.scalar(select(func.count(Membership.id)).where(Membership.family_id == project.family_id)) or 0
    return {"stories": len(stories), "ready": sum(1 for s in stories if s.status == "ready"),
            "review": sum(1 for s in stories if s.status == "needs_review"),
            "processing": sum(1 for s in stories if s.status == "processing"),
            "minutes": int(seconds // 60), "duration": format_duration(seconds), "photos": photos,
            "sent": sent, "answered": answered, "members": members, "pages": estimate_pages(session, project)}


# --------------------------------------------------------------------------- deletion (GDPR erasure)
DELETION_DELAY = timedelta(days=14)


def request_deletion(session: Session, project: Project, user: User) -> None:
    """Deleting everything is the family's right. It is delayed 14 days so a mistake can be undone."""
    project.deletion_requested_at = utcnow()
    enqueue(session, "purge_project", {"project_id": project.id, "requested_at": project.deletion_requested_at.isoformat()},
            dedupe_key=f"purge:{project.id}:{project.deletion_requested_at:%Y%m%d%H%M%S}", delay=DELETION_DELAY.total_seconds())
    audit(session, "deletion_requested", project_id=project.id, actor_kind="user", actor_id=user.id)
    notify_organizers(session, project, "deletion", "Verwijdering aangevraagd",
                      f"{user.first_name} heeft gevraagd om {project.title} te verwijderen. Over 14 dagen wissen we alle "
                      "opnames, verhalen, foto's en boeken definitief, en werken de QR-codes niet meer. Download eerst "
                      "het archief als je iets wilt bewaren. Was dit een vergissing? Annuleer het bij Instellingen.",
                      url=f"/p/{project.id}/instellingen")


def cancel_deletion(session: Session, project: Project, user: User) -> None:
    project.deletion_requested_at = None
    audit(session, "deletion_cancelled", project_id=project.id, actor_kind="user", actor_id=user.id)


@job("purge_project", max_attempts=5)
def purge_project(session: Session, payload: dict) -> None:
    project = session.get(Project, payload["project_id"])
    if project is None or project.deletion_requested_at is None:
        return  # cancelled
    if project.deletion_requested_at.isoformat() != payload.get("requested_at"):
        return  # superseded by a newer request
    from sqlalchemy import delete as sql_delete

    storage = get_storage()
    assets = session.scalars(select(MediaAsset).where(MediaAsset.project_id == project.id)).all()
    asset_ids = [a.id for a in assets]
    for asset in assets:
        if asset.storage_key and asset.status != "purged":
            storage.delete(asset.storage_key)
    family_id, project_id = project.family_id, project.id
    session.expunge_all()
    session.execute(sql_delete(Project).where(Project.id == project_id))  # database cascades to all project rows
    if asset_ids:
        session.execute(sql_delete(MediaAsset).where(MediaAsset.id.in_(asset_ids)))
    audit(session, "project_purged", actor_kind="system", target_type="project", target_id=project_id)
    if not session.scalar(select(Project.id).where(Project.family_id == family_id)):
        session.execute(sql_delete(Family).where(Family.id == family_id))
