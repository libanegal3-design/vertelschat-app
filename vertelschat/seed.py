"""Demo data, created through the real pipeline (signed webhooks -> jobs -> stories), never inserted directly.

    python -m vertelschat.seed --reset

Creates the Jansen family (Sanne organises, Marijke tells, NL) with eight weeks of stories, and the Peeters family
(An organises for Riet, Flanders) that has not connected yet. Everything is fictional."""
from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import timedelta

from sqlalchemy import select

from . import registry  # noqa: F401
from . import services as svc
from .config import DATA_DIR, get_settings
from .db import SessionLocal, init_db, reset_engine, utcnow
from .exports import request_export
from .jobs import drain
from .models import (Entitlement, MediaAsset, Membership, Notification, Photo, Project, Prompt, PromptSchedule,
                     Recording, Story, StoryRevision, Storyteller, Transcript, User, WhatsAppIdentity, WhatsAppMessage)
from .scheduler import compute_next_send
from .security import phone_hash, sign_meta_body, sign_shopify_body
from .shopify import get_or_create_user
from .shopify import ingest_webhook as shop_ingest
from .whatsapp import build_webhook, ingest_webhook, new_wamid, register_dev_media

PHONE = "31612345678"
AUDIO = DATA_DIR / "demo_audio"
PHOTOS = DATA_DIR / "demo_photos"


def _wa(messages: list[dict], name: str = "Marijke") -> None:
    with SessionLocal() as s:
        raw = json.dumps(build_webhook(messages, phone=PHONE, name=name)).encode()
        ingest_webhook(s, raw, sign_meta_body(raw))
        s.commit()
    drain(advance_time=True)


def _msg(**kw) -> dict:
    return {"id": new_wamid(), "timestamp": str(int(time.time())), **kw}


def voice(file: str, context: str | None = None, forwarded: bool = False) -> dict:
    media_id, sha = register_dev_media(AUDIO / file, "audio/ogg; codecs=opus")
    m = _msg(type="audio", audio={"id": media_id, "mime_type": "audio/ogg; codecs=opus", "sha256": sha, "voice": True})
    if context:
        m["context"] = {"id": context}
    if forwarded:
        m.setdefault("context", {})["forwarded"] = True
    return m


def text(body: str) -> dict:
    return _msg(type="text", text={"body": body})


def button(bid: str, title: str) -> dict:
    return _msg(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": bid, "title": title}})


def image(file: str, caption: str) -> dict:
    media_id, sha = register_dev_media(PHOTOS / file, "image/jpeg")
    return _msg(type="image", image={"id": media_id, "mime_type": "image/jpeg", "sha256": sha, "caption": caption})


def last_prompt_wamid(project_id: str) -> str | None:
    with SessionLocal() as s:
        p = s.scalar(select(Prompt).where(Prompt.project_id == project_id, Prompt.sent_at.is_not(None))
                     .order_by(Prompt.sent_at.desc()))
        return p.wa_message_id if p else None


def send_next(project_id: str) -> None:
    with SessionLocal() as s:
        svc.send_now(s, s.get(Project, project_id))
        s.commit()
    drain(advance_time=True)


def shift(project_id: str, days: float) -> None:
    """Move everything that already happened back in time, so the demo reads like weeks of storytelling."""
    d = timedelta(days=days)
    with SessionLocal() as s:
        project = s.get(Project, project_id)
        st = project.storyteller

        def back(obj, *attrs):
            for a in attrs:
                v = getattr(obj, a, None)
                if v is not None:
                    setattr(obj, a, v - d)

        back(project, "created_at", "activated_at", "active_until", "first_story_at")
        for p in s.scalars(select(Prompt).where(Prompt.project_id == project_id)):
            back(p, "created_at", "sent_at", "answered_at", "delivered_at", "read_at")
        for story in s.scalars(select(Story).where(Story.project_id == project_id)):
            back(story, "created_at", "first_part_at", "last_part_at", "notified_at")
            for rev in s.scalars(select(StoryRevision).where(StoryRevision.story_id == story.id)):
                back(rev, "created_at")
        for rec in s.scalars(select(Recording).where(Recording.project_id == project_id)):
            back(rec, "created_at", "received_at")
            for tr in s.scalars(select(Transcript).where(Transcript.recording_id == rec.id)):
                back(tr, "created_at")
        for a in s.scalars(select(MediaAsset).where(MediaAsset.project_id == project_id)):
            back(a, "created_at", "wa_received_at")
        for ph in s.scalars(select(Photo).where(Photo.project_id == project_id)):
            back(ph, "created_at")
        for n in s.scalars(select(Notification).where(Notification.project_id == project_id)):
            back(n, "created_at", "read_at", "emailed_at")
        if st and st.identity_id:
            back(st, "connected_at", "consent_at")
            ident = s.get(WhatsAppIdentity, st.identity_id)
            back(ident, "last_inbound_at", "created_at")
            for m in s.scalars(select(WhatsAppMessage).where(WhatsAppMessage.identity_id == ident.id)):
                back(m, "created_at", "wa_timestamp", "processed_at")
        sched = s.get(PromptSchedule, project_id)
        if sched:
            back(sched, "last_sent_at")
            sched.next_send_at = compute_next_send(sched, utcnow() + timedelta(days=1))
        s.commit()


def reset() -> None:
    settings = get_settings()
    reset_engine()
    for path in (settings.var_dir / "vertelschat.db", settings.var_dir / "vertelschat.db-wal",
                 settings.var_dir / "vertelschat.db-shm"):
        path.unlink(missing_ok=True)
    for d in (settings.storage_local_path, settings.var_dir / "dev_media"):
        shutil.rmtree(d, ignore_errors=True)


def order(email: str, first: str, last: str, voor: str, boodschap: str, oid: int) -> None:
    payload = {"id": oid, "name": f"#VT{oid}", "email": email, "currency": "EUR", "total_price": "129.00",
               "customer": {"first_name": first, "last_name": last},
               "line_items": [{"sku": "VT-VERTELJAAR", "quantity": 1, "properties": [
                   {"name": "Cadeau", "value": "Ja"}, {"name": "Voor", "value": voor},
                   {"name": "Boodschap", "value": boodschap}]}]}
    raw = json.dumps(payload).encode()
    with SessionLocal() as s:
        shop_ingest(s, raw, sign_shopify_body(raw), "orders/paid", f"seed-{oid}")
        s.commit()
    drain(advance_time=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe the local database and storage first")
    args = ap.parse_args()
    if args.reset:
        reset()
    init_db()

    # 1. Two purchases arrive from the shop (signed orders/paid webhooks)
    order("sanne@voorbeeld.nl", "Sanne", "Jansen", "Marijke Jansen",
          "Mam, ik ben zo benieuwd naar je verhalen. Vertel je ze aan ons?", 10231)
    order("an.peeters@voorbeeld.be", "An", "Peeters", "Riet Peeters", "Voor ons moeke, met veel liefde.", 10232)

    # 2. Sanne sets up the storytelling year
    with SessionLocal() as s:
        sanne = s.scalar(select(User).where(User.email == "sanne@voorbeeld.nl"))
        ent = s.scalar(select(Entitlement).where(Entitlement.user_id == sanne.id, Entitlement.status == "available"))
        project = svc.create_project(s, sanne, storyteller_name="Marijke Jansen", address_as="Marijke",
                                     birth_year=1951, locale="nl-NL", purchase_kind="gift", gift_from_name="Sanne",
                                     gift_message=ent.gift_message, entitlement=ent)
        svc.set_schedule(s, project, cadence_days=7, weekday=6, hour=10, minute=0, wait_for_answer=True)
        project.vocabulary = "Henk, Assendorp, Zwolle, Ommen, Vecht, bakkerij Brink"
        joost = get_or_create_user(s, "joost@voorbeeld.nl", "Joost Jansen")
        emma = get_or_create_user(s, "emma@voorbeeld.nl", "Emma de Vries")
        s.add(Membership(family_id=project.family_id, user_id=joost.id, role="editor"))
        s.add(Membership(family_id=project.family_id, user_id=emma.id, role="contributor"))
        svc.add_library_prompt(s, project, sanne, "t01")
        svc.add_library_prompt(s, project, sanne, "j15")
        svc.add_custom_prompt(s, project, joost, "Hoe heb je papa leren kennen?", "editor")
        for key in ("w01", "e01", "r03", "j08", "j02"):
            svc.add_library_prompt(s, project, sanne, key)
        svc.invite_member(s, project, sanne, "henk.jansen@voorbeeld.nl", "contributor")
        s.commit()
        pid, code, st_id = project.id, project.storyteller.join_code, project.storyteller.id

    # 3. Marijke joins with one tap on the link, and says yes
    _wa([text(f"Hallo Vertelschat, ik doe mee! Mijn code is {code}")])
    _wa([button(f"consent:yes:{st_id}", "Ja, ik doe mee")])

    # 4. Weeks of storytelling: answers, an addendum, a photo, a family question, a correction by text
    _wa([voice("01-huis-zolder.ogg", context=last_prompt_wamid(pid))])
    _wa([voice("02-huis-was.ogg")])
    _wa([image("huis.jpg", "Ons huis in Assendorp")])
    shift(pid, 7)
    send_next(pid)
    _wa([voice("03-zondag.ogg", context=last_prompt_wamid(pid))])
    shift(pid, 7)
    send_next(pid)
    _wa([voice("04-ijsbaan.ogg", context=last_prompt_wamid(pid))])
    _wa([text("Het was trouwens de winter van 1972, niet 1973!")])
    shift(pid, 7)
    send_next(pid)
    _wa([voice("05-bakker.ogg", context=last_prompt_wamid(pid))])
    shift(pid, 7)
    send_next(pid)
    _wa([voice("06-hachee.ogg", context=last_prompt_wamid(pid))])
    shift(pid, 7)
    send_next(pid)
    _wa([voice("07-ommen.ogg", context=last_prompt_wamid(pid))])
    _wa([image("tent.jpg", "Kamperen aan de Vecht")])
    shift(pid, 8)
    # 5. Marijke tells something without being asked, then forwards a voice note that is not hers
    _wa([voice("08-elfstedentocht.ogg")])
    _wa([voice("09-doorgestuurd-henk.ogg", forwarded=True)])
    shift(pid, 1)

    # 6. The family at work: a suggestion, a reviewed story, a book preview and an archive
    with SessionLocal() as s:
        project = s.get(Project, pid)
        emma = s.scalar(select(User).where(User.email == "emma@voorbeeld.nl"))
        sanne = s.scalar(select(User).where(User.email == "sanne@voorbeeld.nl"))
        svc.add_custom_prompt(s, project, emma, "Wat voor opa was jouw vader voor mij, toen ik klein was?", "contributor")
        first = s.scalar(select(Story).where(Story.project_id == pid).order_by(Story.position))
        for code in [f["code"] for f in (first.review_flags or [])]:
            svc.resolve_flag(first, code)
        free = s.scalar(select(Story).where(Story.project_id == pid, Story.prompt_id.is_(None)))
        if free is not None:  # Sanne gives the unprompted story its own title (a normal family edit)
            svc.save_story(s, free, sanne, "De Elfstedentocht van 1963", free.body)
            svc.resolve_flag(free, "free")
        svc.generate_preview(s, project, sanne)
        s.commit()
    drain(advance_time=True)
    send_next(pid)  # this week's question is out and waiting for an answer
    with SessionLocal() as s:
        project = s.get(Project, pid)
        request_export(s, project, None)
        s.commit()
    drain(advance_time=True)

    # 7. The Peeters family has set up but not connected yet (for the onboarding screens)
    with SessionLocal() as s:
        an = s.scalar(select(User).where(User.email == "an.peeters@voorbeeld.be"))
        an.name = "An Peeters"
        ent = s.scalar(select(Entitlement).where(Entitlement.user_id == an.id, Entitlement.status == "available"))
        p2 = svc.create_project(s, an, storyteller_name="Riet Peeters", address_as="Riet", birth_year=1946,
                                locale="nl-BE", purchase_kind="gift", gift_from_name="An",
                                gift_message=ent.gift_message, entitlement=ent)
        svc.set_schedule(s, p2, cadence_days=7, weekday=2, hour=14, minute=0, wait_for_answer=True)
        for key in ("t01", "j02", "j15"):
            svc.add_library_prompt(s, p2, an, key)
        s.commit()
    drain(advance_time=True)

    with SessionLocal() as s:
        stories = s.scalars(select(Story).where(Story.project_id == pid).order_by(Story.position)).all()
        print(f"Demo klaar: {len(stories)} verhalen voor Marijke.")
        for st in stories:
            print(f"  - {st.title!r} [{st.status}] flags={[f['code'] for f in st.review_flags or []]}")
        print("Log in via /dev/login?email=sanne@voorbeeld.nl")


if __name__ == "__main__":
    main()
