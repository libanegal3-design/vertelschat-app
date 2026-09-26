"""Question rhythm, end-of-period handling and housekeeping. tick() runs every minute in the worker."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .db import session_scope, utcnow
from .jobs import enqueue, job, recover_stale
from .models import (LoginToken, MediaAsset, Project, Prompt, PromptSchedule, Recording, WebSession, WhatsAppMessage)
from .notify import notify, notify_organizers

log = logging.getLogger("vertelschat.scheduler")
CADENCES = {7: "Elke week", 14: "Om de week", 28: "Elke vier weken"}
WEEKDAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def compute_next_send(sched: PromptSchedule, after: datetime) -> datetime:
    """First moment at or after `after` (naive UTC) on the chosen weekday and local time. DST-safe."""
    tz = ZoneInfo(sched.timezone or "Europe/Amsterdam")
    local_after = after.replace(tzinfo=timezone.utc).astimezone(tz)
    day = local_after.date() + timedelta(days=(sched.weekday - local_after.weekday()) % 7)
    candidate = datetime(day.year, day.month, day.day, sched.send_hour, sched.send_minute, tzinfo=tz)
    if candidate < local_after:
        day = day + timedelta(days=7)
        candidate = datetime(day.year, day.month, day.day, sched.send_hour, sched.send_minute, tzinfo=tz)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime | None, tz: str = "Europe/Amsterdam") -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))


def _due_schedules(s: Session, now: datetime, counts: dict) -> None:
    from .prompts import next_prompt  # local import keeps module graph acyclic

    rows = s.scalars(select(PromptSchedule).join(Project, Project.id == PromptSchedule.project_id)
                     .where(Project.status == "active", PromptSchedule.next_send_at <= now)).all()
    for sched in rows:
        project = s.get(Project, sched.project_id)
        if project.active_until and now > project.active_until:
            continue
        st = project.storyteller
        if st is None or not st.can_receive_prompts:
            sched.next_send_at = compute_next_send(sched, now + timedelta(days=1))
            continue
        if sched.paused_until and sched.paused_until > now:
            sched.next_send_at = compute_next_send(sched, sched.paused_until)
            continue
        last = s.scalar(select(Prompt).where(Prompt.project_id == project.id, Prompt.sent_at.is_not(None))
                        .order_by(Prompt.sent_at.desc()))
        if (sched.wait_for_answer and last is not None and last.status in ("sent", "send_unknown", "announced")
                and last.answered_at is None and now - last.sent_at < timedelta(days=sched.max_wait_days)):
            sched.next_send_at = now + timedelta(days=1)  # never stack unanswered questions
            counts["postponed"] += 1
            continue
        # an announcement nobody opened goes back to the queue, so the question is offered again rather than lost
        s.query(Prompt).filter(Prompt.project_id == project.id, Prompt.status == "announced").update(
            {Prompt.status: "queued", Prompt.sent_at: None, Prompt.sent_via: "", Prompt.template_name: "",
             Prompt.wa_message_id: None}, synchronize_session=False)
        if s.scalar(select(Prompt.id).where(Prompt.project_id == project.id, Prompt.status == "scheduled")):
            sched.next_send_at = now + timedelta(hours=1)
            continue
        p = next_prompt(s, project)
        sched.next_send_at = compute_next_send(sched, now + timedelta(days=max(1, sched.cadence_days - 1)))
        if p is None:
            notify_organizers(s, project, "needs_attention", "Er staan geen vragen meer klaar",
                              "Voeg zelf een vraag toe of kies er een uit de bibliotheek.", url=f"/p/{project.id}/vragen")
            continue
        p.status = "scheduled"
        # a question offered before (an announcement nobody opened) needs fresh job and message keys
        offered = s.scalar(select(func.count(WhatsAppMessage.id)).where(WhatsAppMessage.prompt_id == p.id,
                                                                        WhatsAppMessage.direction == "out")) or 0
        suffix = f":r{offered}" if offered else ""
        enqueue(s, "send_prompt", {"prompt_id": p.id, "attempt_key": suffix}, dedupe_key=f"send_prompt:{p.id}{suffix}")
        counts["scheduled"] += 1


def _period_events(s: Session, now: datetime, counts: dict) -> None:
    ending = s.scalars(select(Project).where(Project.status == "active", Project.active_until.is_not(None),
                                             Project.active_until <= now + timedelta(days=30),
                                             Project.ending_reminder_sent_at.is_(None))).all()
    for project in ending:
        if project.active_until < now:
            continue
        project.ending_reminder_sent_at = now
        notify_organizers(s, project, "period_ending", "Nog een maand vragen",
                          "Over ongeveer een maand stopt de wekelijkse vraag. Alles wat verteld is blijft daarna "
                          "gewoon bewaard en te downloaden. Dit is een mooi moment om het boek samen te stellen.",
                          url=f"/p/{project.id}/boek")
        counts["reminders"] += 1
    done = s.scalars(select(Project).where(Project.status == "active", Project.active_until.is_not(None),
                                           Project.active_until < now)).all()
    for project in done:
        project.status = "completed"
        project.completed_at = now
        s.query(Prompt).filter(Prompt.project_id == project.id, Prompt.status.in_(["queued", "scheduled"])) \
            .update({Prompt.status: "queued"}, synchronize_session=False)
        enqueue(s, "project_completed", {"project_id": project.id}, dedupe_key=f"completed:{project.id}")
        counts["completed"] += 1


def _cleanup(s: Session, now: datetime, counts: dict) -> None:
    from .storage import get_storage

    storage = get_storage()
    for asset in s.scalars(select(MediaAsset).where(MediaAsset.quarantined.is_(True), MediaAsset.purge_after < now,
                                                    MediaAsset.status != "purged")).all():
        if asset.storage_key:
            storage.delete(asset.storage_key)
        asset.status = "purged"
        counts["purged"] += 1
    for rec in s.scalars(select(Recording).where(Recording.retracted_at.is_not(None),
                                                 Recording.retracted_at < now - timedelta(days=30),
                                                 Recording.status != "purged")).all():
        for media_id in (rec.original_media_id, rec.playback_media_id):
            asset = s.get(MediaAsset, media_id) if media_id else None
            if asset and asset.status != "purged":
                if asset.storage_key:
                    storage.delete(asset.storage_key)
                asset.status = "purged"
        rec.status = "purged"
        counts["purged"] += 1
    s.execute(delete(LoginToken).where(LoginToken.expires_at < now - timedelta(days=2)))
    s.execute(delete(WebSession).where(WebSession.expires_at < now))
    counts["recovered"] += recover_stale(s)


def tick(now: datetime | None = None) -> dict:
    now = now or utcnow()
    counts = {"scheduled": 0, "postponed": 0, "reminders": 0, "completed": 0, "purged": 0, "recovered": 0}
    with session_scope() as s:
        _due_schedules(s, now, counts)
        _period_events(s, now, counts)
        _cleanup(s, now, counts)
    return counts


@job("project_completed", max_attempts=6)
def project_completed(session: Session, payload: dict) -> None:
    from . import copy_nl
    from .config import get_settings
    from .exports import request_export
    from .handlers import recipient_for, send_whatsapp, window_open
    from .models import WhatsAppIdentity
    from .whatsapp import WhatsAppError, template_message, text_message

    project = session.get(Project, payload["project_id"])
    if project is None:
        return
    st = project.storyteller
    if st and st.identity_id and st.consent_status == "given" and st.opted_out_at is None:
        ident = session.get(WhatsAppIdentity, st.identity_id)
        rcpt = recipient_for(ident)
        if window_open(ident):
            body = copy_nl.farewell_session(project.locale, st.greeting_name)
            wa = text_message(rcpt, body)
        else:
            body = copy_nl.TEMPLATES["vt_afsluiting_v1"].replace("{{1}}", st.greeting_name)
            wa = template_message(rcpt, "vt_afsluiting_v1", get_settings().whatsapp_template_lang, [st.greeting_name])
        try:
            send_whatsapp(session, ident, wa, key=f"farewell:{project.id}", project_id=project.id, body_text=body)
        except WhatsAppError as exc:
            if exc.retriable:
                from .jobs import RetryLater
                raise RetryLater(str(exc)) from exc
    notify(session, project, "period_ended", f"Het verteljaar van {st.name if st else 'jullie verteller'} is afgerond",
           "Alles blijft bewaard: de opnames, de verhalen, de foto's en het boek. De QR-codes in het boek blijven werken, "
           "ook zonder abonnement. We zetten een volledig archief voor je klaar om te downloaden. Verder vertellen kan "
           "later altijd met een nieuw verteljaar, maar dat is nooit nodig om bij je verhalen te kunnen.",
           url=f"/p/{project.id}/downloads")
    request_export(session, project, requested_by_id=None)
