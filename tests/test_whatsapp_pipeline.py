"""The storyteller side: WhatsApp webhooks -> stored recordings -> stories. Covers the error-case list."""
from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from vertelschat.db import SessionLocal, utcnow
from vertelschat.models import (Consent, MediaAsset, Notification, Photo, Project, Prompt, PromptSchedule, QRLink,
                               Recording, Story, Storyteller, Transcript, WhatsAppIdentity, WhatsAppMessage)


def _one(model, **where):
    with SessionLocal() as s:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return s.scalars(q).all()


def test_signature_and_verification(client):
    r = client.post("/webhooks/whatsapp", content=b'{"object":"whatsapp_business_account"}',
                    headers={"X-Hub-Signature-256": "sha256=00"})
    assert r.status_code == 401
    ok = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "dev-verify-token",
                                                  "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                   "hub.challenge": "1"})
    assert bad.status_code == 403


def test_full_flow_join_consent_prompt_voice_story(h):
    proj = h.project()
    h.post([h.text(f"Hallo Vertelschat, ik doe mee! Mijn code is {proj['code']}")])
    h.run()
    welcome = h.sent()[-1]
    assert welcome["type"] == "interactive"
    ids = [b["reply"]["id"] for b in welcome["interactive"]["action"]["buttons"]]
    assert f"consent:yes:{proj['st']}" in ids
    h.post([h.button(f"consent:yes:{proj['st']}")])
    h.run()
    with SessionLocal() as s:
        st = s.get(Storyteller, proj["st"])
        project = s.get(Project, proj["pid"])
        assert st.consent_status == "given"
        assert s.scalar(select(func.count(Consent.id)).where(Consent.storyteller_id == st.id)) == 2
        assert project.status == "active"
        assert abs((project.active_until - project.activated_at).days - 365) <= 1
        first = s.scalar(select(Prompt).where(Prompt.project_id == project.id, Prompt.status == "sent"))
        assert first is not None and first.library_key == "t01" and first.sent_via == "session"
    h.post([h.voice(context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    with SessionLocal() as s:
        rec = s.scalar(select(Recording).where(Recording.project_id == proj["pid"]))
        story = s.get(Story, rec.story_id)
        assert rec.status == "transcribed" and rec.playback_media_id and rec.duration_seconds > 40
        assert s.get(MediaAsset, rec.original_media_id).status == "stored"
        assert story.title == "Het huis waar ik opgroeide"
        assert "Assendorp" in story.body and story.status == "ready"
        assert s.scalar(select(QRLink).where(QRLink.story_id == story.id)).audio_code == "A01"
        assert s.scalar(select(Prompt).where(Prompt.id == story.prompt_id)).status == "answered"
        assert s.scalar(select(func.count(Notification.id)).where(Notification.kind == "story_ready")) >= 1
    assert any("Dankjewel" in t or "dank" in t.lower() for t in h.sent_texts()[-2:])


def test_duplicate_webhook_is_ignored(h):
    proj = h.project()
    h.connect(proj)
    m = h.voice(context=h.last_prompt_wamid(proj["pid"]))
    first = h.post([m])
    second = h.post([m])
    assert first["messages"] == 1 and second["duplicates"] == 1
    h.run()
    assert len(_one(Recording, project_id=proj["pid"])) == 1


def test_multi_part_story_in_one_session(h):
    proj = h.project()
    h.connect(proj)
    h.post([h.voice("01-huis-zolder.ogg", context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    h.post([h.voice("02-huis-was.ogg")])
    h.run()
    recs = _one(Recording, project_id=proj["pid"])
    assert len(recs) == 2 and recs[0].story_id == recs[1].story_id
    with SessionLocal() as s:
        story = s.get(Story, recs[0].story_id)
        assert "doolhof" in story.body and "Assendorp" in story.body


def test_text_messages(h):
    proj = h.project()
    h.connect(proj)
    long_text = "Ik schrijf het maar even op, want praten lukt vandaag niet zo goed met mijn verkoudheid, sorry."
    h.post([h.text(long_text, context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    recs = _one(Recording, project_id=proj["pid"])
    assert len(recs) == 1 and recs[0].kind == "text"
    h.post([h.text("Dank je!")])
    h.run()
    assert len(_one(Recording, project_id=proj["pid"])) == 1  # chit-chat is not a story part
    h.post([h.text("Het was in Kampen.")])  # short note right after a part: attached as a note
    h.run()
    assert len(_one(Recording, project_id=proj["pid"])) == 2


def test_photo_is_attached_to_recent_story(h):
    proj = h.project()
    h.connect(proj)
    h.post([h.voice(context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    h.post([h.image(caption="Ons huis")])
    h.run()
    photos = _one(Photo, project_id=proj["pid"])
    assert len(photos) == 1 and photos[0].story_id and photos[0].caption == "Ons huis"
    with SessionLocal() as s:
        assert s.get(MediaAsset, photos[0].media_id).status == "stored"


def test_unknown_sender_is_quarantined_then_adopted(h):
    proj = h.project()
    other = "31699990000"
    h.post([h.voice()], phone=other)
    h.run()
    h.post([h.voice("03-zondag.ogg")], phone=other)
    h.run()
    assets = _one(MediaAsset, quarantined=True)
    assert len(assets) == 2 and all(a.status == "stored" for a in assets)
    assert sum("nog niet" in t or "niet herkennen" in t or "kennen" in t for t in h.sent_texts()) == 1
    h.post([h.text(f"Mijn code is {proj['code']}")], phone=other)
    h.run()
    recs = _one(Recording, project_id=proj["pid"])
    assert len(recs) == 2 and all(r.hold_reason == "consent" for r in recs)
    h.post([h.button(f"consent:yes:{proj['st']}")], phone=other)
    h.run()
    with SessionLocal() as s:
        assert all(r.status == "transcribed" for r in s.scalars(select(Recording).where(Recording.project_id == proj["pid"])))


def test_download_retries_then_succeeds(h):
    from vertelschat.whatsapp import get_backend
    proj = h.project()
    h.connect(proj)
    m = h.voice(context=h.last_prompt_wamid(proj["pid"]))
    get_backend().fail_media[m["audio"]["id"]] = 3
    h.post([m])
    h.run()
    with SessionLocal() as s:
        asset = s.scalar(select(MediaAsset).where(MediaAsset.wa_media_id == m["audio"]["id"]))
        assert asset.status == "stored" and asset.attempts == 4


def test_download_expired_media_notifies_family(h):
    proj = h.project()
    h.connect(proj)
    m = h.voice(context=h.last_prompt_wamid(proj["pid"]))
    m["audio"]["id"] = "999999999"  # WhatsApp no longer has it (media ids expire after 7 days)
    m["timestamp"] = str(int((utcnow() - timedelta(days=8)).timestamp()))
    h.post([m])
    h.run()
    with SessionLocal() as s:
        rec = s.scalar(select(Recording).where(Recording.project_id == proj["pid"]))
        assert rec.status == "download_failed"
        assert s.scalar(select(Notification).where(Notification.kind == "download_failed")) is not None


def test_checksum_mismatch_is_retried_and_file_kept(h):
    proj = h.project()
    h.connect(proj)
    m = h.voice(context=h.last_prompt_wamid(proj["pid"]), corrupt_checksum=True)
    h.post([m])
    h.run()
    with SessionLocal() as s:
        asset = s.scalar(select(MediaAsset).where(MediaAsset.wa_media_id == m["audio"]["id"]))
        assert asset.status == "stored" and "checksum" in asset.last_error
        assert s.scalar(select(Recording)).status == "transcribed"


def test_corrupt_audio_is_kept_and_resend_requested(h, tmp_path):
    proj = h.project()
    h.connect(proj)
    bad = tmp_path / "kapot.ogg"
    bad.write_bytes(b"OggS" + bytes(range(256)) * 20)
    h.post([h.voice(path=bad, context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    with SessionLocal() as s:
        rec = s.scalar(select(Recording))
        assert rec.status == "corrupt"
        assert s.get(MediaAsset, rec.original_media_id).status == "stored"  # never lose the original
        story = s.get(Story, rec.story_id)
        assert any(f["code"] == "corrupt" for f in story.review_flags)
    assert any("nog een keer inspreken" in t for t in h.sent_texts())


def test_long_audio_is_split_for_transcription(tmp_path):
    from vertelschat.audio import probe, split_for_transcription
    src = tmp_path / "lang.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=1300", "-ac", "1",
                    "-c:a", "libopus", "-b:a", "8k", str(src)], check=True)
    chunks = split_for_transcription(src, tmp_path / "delen", 600)
    assert len(chunks) == 3
    assert all(probe(c)["duration"] <= 601 for c in chunks)


def test_long_recording_uses_chunks_in_transcribe_job(h, monkeypatch, tmp_path):
    from vertelschat import handlers
    from vertelschat.ai import TranscriptionResult
    calls = []

    class Chunky:
        name = "chunky"
        max_chunk_seconds = 20

        def transcribe(self, path, terms):
            calls.append(Path(path).name)
            return TranscriptionResult(text=f"deel {len(calls)}", provider="test", model="chunky")

    monkeypatch.setattr(handlers, "get_transcriber", lambda: Chunky())
    proj = h.project()
    h.connect(proj)
    h.post([h.voice(context=h.last_prompt_wamid(proj["pid"]))])  # ~57 seconds -> 3 chunks of 20 s
    h.run()
    assert len(calls) == 3
    with SessionLocal() as s:
        assert s.scalar(select(Transcript)).text == "deel 1\n\ndeel 2\n\ndeel 3"


def test_unsupported_and_sticker(h):
    proj = h.project()
    h.connect(proj)
    before = len(h.sent())
    h.post([h.msg(type="sticker", sticker={"id": "1", "mime_type": "image/webp"})])
    h.post([h.msg(type="unsupported", errors=[{"code": 131051}])])
    h.post([h.msg(type="unsupported", errors=[{"code": 131051}])])
    h.run()
    assert len(h.sent()) == before + 1  # one friendly explanation, no spam


def test_manual_transcription_mode(h, monkeypatch):
    from vertelschat import config
    from vertelschat import services as svc
    from vertelschat.models import User
    monkeypatch.setenv("TRANSCRIPTION_BACKEND", "manual")
    config.get_settings.cache_clear()
    proj = h.project()
    h.connect(proj)
    h.post([h.voice(context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    with SessionLocal() as s:
        rec = s.scalar(select(Recording))
        assert rec.status == "transcription_failed" and rec.playback_media_id
        story = s.get(Story, rec.story_id)
        assert any(f["code"] == "manual_transcript" for f in story.review_flags)
        svc.set_manual_transcript(s, rec, "We woonden in Zwolle, in een smal huis.", s.get(User, proj["uid"]))
        s.commit()
    h.run()
    with SessionLocal() as s:
        assert "Zwolle" in s.scalar(select(Story)).body


def test_editor_failure_falls_back_to_local_editor(h, monkeypatch):
    from vertelschat import handlers
    from vertelschat.ai import ComposeError

    def broken(data):
        raise ComposeError("editor down", retriable=False)

    monkeypatch.setattr(handlers, "compose_checked", broken)
    proj = h.project()
    h.connect(proj)
    h.post([h.voice(context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    with SessionLocal() as s:
        story = s.scalar(select(Story))
        assert "Assendorp" in story.body
        assert story.status == "needs_review" and any(f["code"] == "ai_check" for f in story.review_flags)


def test_fabrication_guard():
    from vertelschat.ai import ComposeInput, ComposeResult, check_fabrication, compose_checked
    src = "We woonden in Zwolle. Mijn broer Henk sliep op zolder."
    assert check_fabrication("Ze woonden in Zwolle met Henk.", src) == []
    assert "1958" in check_fabrication("In 1958 woonden ze in Zwolle.", src)
    assert "Amsterdam" in check_fabrication("Later verhuisden ze naar Amsterdam.", src)

    class Liar:
        name = "liar"

        def compose(self, data, strict_words=None):
            return ComposeResult(title="Zolder", body="In 1958 kocht Kees een huis in Deventer.", unclear=[],
                                 followups=[], provider="liar", model="x")

    import vertelschat.ai as ai
    old = ai.get_composer
    ai.get_composer = lambda: Liar()
    try:
        result, notes = compose_checked(ComposeInput(question="", parts=[src], storyteller_name="Marijke"))
    finally:
        ai.get_composer = old
    assert "Deventer" not in result.body and notes  # replaced by the local editor, with a review note


def test_same_number_in_two_projects_asks_which(h):
    a = h.project(email="sanne@test.nl", name="Marijke Jansen", prompts=("t01",))
    b = h.project(email="joost@test.nl", name="Marijke Jansen", prompts=("j15",))
    h.connect(a)
    h.connect(b)
    h.post([h.voice()])
    h.run()
    ask = [m for m in h.sent() if m.get("type") == "interactive"][-1]  # the heart reaction may follow it
    assert "Voor wie" in ask["interactive"]["body"]["text"]
    choice = ask["interactive"]["action"]["buttons"][1]["reply"]["id"]
    with SessionLocal() as s:
        rec = s.scalar(select(Recording))
        assert rec.hold_reason == "assignment"
    h.post([h.button(choice, "B")])
    h.run()
    target_st = choice.split(":")[2]
    with SessionLocal() as s:
        rec = s.scalar(select(Recording))
        assert rec.storyteller_id == target_st and rec.hold_reason is None and rec.status == "transcribed"


def test_stop_and_start(h):
    from vertelschat import scheduler
    proj = h.project()
    h.connect(proj)
    h.post([h.text("STOP")])
    h.run()
    with SessionLocal() as s:
        st = s.get(Storyteller, proj["st"])
        assert st.opted_out_at is not None and not st.can_receive_prompts
        s.get(PromptSchedule, proj["pid"]).next_send_at = utcnow() - timedelta(minutes=1)
        s.commit()
    before = len(h.sent())
    scheduler.tick()
    h.run()
    assert len(h.sent()) == before  # no questions after STOP
    h.post([h.voice()])  # a voice note after STOP is still kept and processed
    h.run()
    assert _one(Recording, project_id=proj["pid"])[0].status == "transcribed"
    h.post([h.text("START")])
    h.run()
    with SessionLocal() as s:
        assert s.get(Storyteller, proj["st"]).can_receive_prompts


def test_forwarded_audio_waits_for_confirmation(h):
    from vertelschat import services as svc
    from vertelschat.models import User
    proj = h.project()
    h.connect(proj)
    h.post([h.voice("09-doorgestuurd-henk.ogg", forwarded=True)])
    h.run()
    with SessionLocal() as s:
        rec = s.scalar(select(Recording))
        assert rec.hold_reason == "forwarded" and rec.status == "stored" and rec.transcript is None
        svc.confirm_recording(s, rec, True, s.get(User, proj["uid"]))
        s.commit()
    h.run()
    assert _one(Recording)[0].status == "transcribed"


def test_scheduler_never_stacks_questions(h):
    from vertelschat import scheduler
    proj = h.project()
    h.connect(proj)  # first question sent, unanswered
    with SessionLocal() as s:
        s.get(PromptSchedule, proj["pid"]).next_send_at = utcnow() - timedelta(minutes=1)
        s.commit()
    counts = scheduler.tick()
    assert counts["postponed"] == 1 and counts["scheduled"] == 0
    h.post([h.voice(context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    with SessionLocal() as s:
        s.get(PromptSchedule, proj["pid"]).next_send_at = utcnow() - timedelta(minutes=1)
        s.commit()
    assert scheduler.tick()["scheduled"] == 1
    h.run()
    with SessionLocal() as s:
        assert s.scalar(select(func.count(Prompt.id)).where(Prompt.sent_at.is_not(None))) == 2


def test_interrupted_send_is_never_resent_automatically(h):
    from vertelschat import services as svc
    from vertelschat.jobs import enqueue
    proj = h.project()
    h.connect(proj)
    h.post([h.voice(context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    with SessionLocal() as s:
        p = s.scalar(select(Prompt).where(Prompt.project_id == proj["pid"], Prompt.status == "queued")
                     .order_by(Prompt.position))
        # simulate a crash after the request left: an outbound row stuck in 'sending'
        s.add(WhatsAppMessage(direction="out", idempotency_key=f"prompt:{p.id}", prompt_id=p.id, status="sending"))
        p.status = "scheduled"
        enqueue(s, "send_prompt", {"prompt_id": p.id}, dedupe_key=f"send_prompt:{p.id}")
        s.commit()
        pid = p.id
    before = len(h.sent())
    h.run()
    assert len(h.sent()) == before
    with SessionLocal() as s:
        p = s.get(Prompt, pid)
        assert p.status == "send_unknown"
        svc.resend_prompt(s, p)  # the family decides
        s.commit()
    h.run()
    assert len(h.sent()) == before + 1
    with SessionLocal() as s:
        assert s.get(Prompt, pid).status == "sent"


def test_revoked_message_is_removed_from_story(h):
    proj = h.project()
    h.connect(proj)
    m = h.voice(context=h.last_prompt_wamid(proj["pid"]))
    h.post([m])
    h.run()
    h.post([h.msg(type="revoke", revoke={"original_message_id": m["id"]})])
    h.run()
    with SessionLocal() as s:
        rec = s.scalar(select(Recording))
        assert rec.retracted_at is not None
        assert any(f["code"] == "retracted" for f in s.get(Story, rec.story_id).review_flags)


def test_after_the_year_recordings_are_kept_not_processed(h):
    proj = h.project()
    h.connect(proj)
    with SessionLocal() as s:
        p = s.get(Project, proj["pid"])
        p.active_until = utcnow() - timedelta(days=60)
        for pr in s.scalars(select(Prompt).where(Prompt.sent_at.is_not(None))):
            pr.sent_at = utcnow() - timedelta(days=90)
        s.commit()
    h.post([h.voice()])
    h.run()
    with SessionLocal() as s:
        rec = s.scalar(select(Recording))
        assert rec.after_period and rec.hold_reason == "after_period"
        assert rec.playback_media_id and rec.transcript is None
        assert s.scalar(select(Notification).where(Notification.kind == "late_recording")) is not None
    assert any("bewaard" in t for t in h.sent_texts()[-2:])


def test_late_answer_is_flagged(h):
    proj = h.project()
    h.connect(proj)
    with SessionLocal() as s:
        for pr in s.scalars(select(Prompt).where(Prompt.sent_at.is_not(None))):
            pr.sent_at = utcnow() - timedelta(days=20)
        ident = s.scalar(select(WhatsAppIdentity))
        s.commit()
    h.post([h.voice()])
    h.run()
    with SessionLocal() as s:
        story = s.scalar(select(Story))
        assert story.prompt_id and any(f["code"] == "late" for f in story.review_flags)


def test_delivery_statuses(h):
    proj = h.project()
    h.connect(proj)
    wamid = h.last_prompt_wamid(proj["pid"])
    h.post(statuses=[{"id": wamid, "status": "delivered", "timestamp": "1790000000"}])
    h.post(statuses=[{"id": wamid, "status": "read", "timestamp": "1790000100"}])
    h.run()
    with SessionLocal() as s:
        p = s.scalar(select(Prompt).where(Prompt.wa_message_id == wamid))
        assert p.delivered_at and p.read_at
