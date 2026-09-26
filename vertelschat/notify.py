"""Family notifications (in-app + e-mail). Storytellers are never notified by e-mail."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .mailer import send_mail
from .models import ROLE_RANK, Membership, Notification, Project, User

IMPORTANT = {"storyteller_connected", "download_failed", "opted_out", "export_ready", "period_ending",
             "period_ended", "book_ready", "print_update", "needs_attention", "late_recording", "welcome"}


def members(session: Session, project: Project, min_role: str = "viewer") -> list[tuple[User, str]]:
    rows = session.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.id)
                           .where(Membership.family_id == project.family_id)).all()
    return [(u, r) for u, r in rows if ROLE_RANK.get(r, 0) >= ROLE_RANK[min_role]]


def notify(session: Session, project: Project, kind: str, title: str, body: str = "", url: str = "", *,
           min_role: str = "viewer", email: bool = True, exclude_user_id: str | None = None) -> int:
    base = get_settings().base_url
    count = 0
    for user, _role in members(session, project, min_role):
        if user.id == exclude_user_id:
            continue
        session.add(Notification(user_id=user.id, project_id=project.id, kind=kind, title=title, body=body, url=url))
        count += 1
        wants_mail = user.notify_mode == "immediate" or (kind in IMPORTANT and user.notify_mode != "off")
        if email and wants_mail:
            send_mail(session, user.email, title, body or title,
                      cta_url=(base + url) if url.startswith("/") else url, cta_label="Bekijk in Vertelschat")
    return count


def notify_organizers(session: Session, project: Project, kind: str, title: str, body: str = "", url: str = "",
                      email: bool = True) -> int:
    return notify(session, project, kind, title, body, url, min_role="editor", email=email)
