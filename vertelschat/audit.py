from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AuditEvent


def audit(session: Session, action: str, *, project_id: str | None = None, actor_kind: str = "system",
          actor_id: str = "", target_type: str = "", target_id: str = "", **meta) -> None:
    session.add(AuditEvent(project_id=project_id, actor_kind=actor_kind, actor_id=actor_id or "",
                           action=action, target_type=target_type, target_id=str(target_id or ""),
                           meta={k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}))
