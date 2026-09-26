"""The human-written question library (data/prompts.nl.json) and question selection."""
from __future__ import annotations

import json
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai import title_from_question
from .config import DATA_DIR
from .models import Project, Prompt, Storyteller


@lru_cache(maxsize=1)
def library() -> dict:
    return json.loads((DATA_DIR / "prompts.nl.json").read_text(encoding="utf-8"))


def categories() -> dict[str, str]:
    return library()["categories"]


@lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    return {e["key"]: e for e in library()["prompts"]}


def by_key(key: str) -> dict | None:
    return _index().get(key)


def localized(entry: dict, locale: str) -> tuple[str, str]:
    if locale == "nl-BE" and entry.get("nl_BE"):
        return entry["nl_BE"]["question"], entry["nl_BE"]["title"]
    return entry["question"], entry["title"]


def eligible(entry: dict, project: Project, storyteller: Storyteller | None) -> bool:
    region = "BE" if project.locale == "nl-BE" else "NL"
    if entry.get("region") and entry["region"] != region:
        return False
    if entry.get("sensitive") and not project.sensitive_topics:
        return False
    by = storyteller.birth_year if storyteller else None
    if entry.get("max_birth_year") or entry.get("min_birth_year"):
        if not by:
            return False  # era questions only when we know it fits
        if entry.get("max_birth_year") and by > entry["max_birth_year"]:
            return False
        if entry.get("min_birth_year") and by < entry["min_birth_year"]:
            return False
    return True


def used_keys(session: Session, project_id: str) -> set[str]:
    rows = session.scalars(select(Prompt.library_key).where(Prompt.project_id == project_id,
                                                           Prompt.library_key != "")).all()
    return set(rows)


def title_for_prompt(prompt: Prompt | None, locale: str) -> str:
    if prompt is None:
        return ""
    if prompt.library_key and (entry := by_key(prompt.library_key)):
        return localized(entry, locale)[1]
    return title_from_question(prompt.text)


def next_position(session: Session, project_id: str) -> int:
    current = session.scalar(select(func.max(Prompt.position)).where(Prompt.project_id == project_id))
    return (current or 0) + 1


def pick_library_entry(session: Session, project: Project) -> dict | None:
    used = used_keys(session, project.id)
    st = project.storyteller
    pool = [e for e in library()["prompts"] if e["key"] not in used and eligible(e, project, st)]
    if not pool:
        return None
    answered = session.scalar(select(func.count(Prompt.id)).where(Prompt.project_id == project.id,
                                                                  Prompt.status == "answered")) or 0
    starters = [e for e in pool if e.get("starter")]
    if answered < 3 and starters:
        return starters[0]
    recent = session.scalars(select(Prompt.category).where(Prompt.project_id == project.id, Prompt.sent_at.is_not(None))
                             .order_by(Prompt.sent_at.desc()).limit(4)).all()
    for e in pool:
        if e["category"] not in recent:
            return e
    return pool[0]


def add_from_library(session: Session, project: Project, key: str, *, status: str = "queued",
                     user_id: str | None = None) -> Prompt | None:
    entry = by_key(key)
    if entry is None:
        return None
    question, _ = localized(entry, project.locale)
    p = Prompt(project_id=project.id, text=question, category=entry["category"], library_key=key, source="library",
               status=status, position=next_position(session, project.id), suggested_by_id=user_id)
    session.add(p)
    session.flush()
    return p


def next_prompt(session: Session, project: Project, autofill: bool = True) -> Prompt | None:
    """First queued prompt in the family's order; otherwise a fresh one from the library."""
    p = session.scalar(select(Prompt).where(Prompt.project_id == project.id, Prompt.status == "queued")
                       .order_by(Prompt.position, Prompt.created_at))
    if p is not None or not autofill:
        return p
    entry = pick_library_entry(session, project)
    if entry is None:
        return None
    return add_from_library(session, project, entry["key"])


def starter_suggestions(project: Project, storyteller: Storyteller | None, n: int = 8) -> list[dict]:
    items = [e for e in library()["prompts"] if eligible(e, project, storyteller)]
    items.sort(key=lambda e: (not e.get("starter"), e["key"]))
    return items[:n]


def browse(session: Session, project: Project) -> list[tuple[str, str, list[dict]]]:
    used = used_keys(session, project.id)
    st = project.storyteller
    out = []
    for cat, label in categories().items():
        entries = []
        for e in library()["prompts"]:
            if e["category"] != cat or not eligible(e, project, st):
                continue
            q, t = localized(e, project.locale)
            entries.append({"key": e["key"], "question": q, "title": t, "used": e["key"] in used})
        if entries:
            out.append((cat, label, entries))
    return out
