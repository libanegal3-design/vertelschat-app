"""Stable QR links for printed books. A token is created once per story and never changes or expires."""
from __future__ import annotations

from urllib.parse import quote

import segno
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import QRLink, Recording, Story
from .security import generate_qr_token


def qr_url(token: str) -> str:
    return f"{get_settings().qr_base_url}/{token}"


def qr_display_url(token: str) -> str:
    return qr_url(token).split("://", 1)[-1]


def ensure_qr(session: Session, story: Story) -> QRLink | None:
    has_audio = session.scalar(select(Recording.id).where(Recording.story_id == story.id, Recording.kind == "audio",
                                                          Recording.retracted_at.is_(None)))
    if not has_audio:
        return None
    existing = session.scalar(select(QRLink).where(QRLink.story_id == story.id))
    if existing is not None:
        return existing
    n = (session.scalar(select(func.count(QRLink.id)).where(QRLink.project_id == story.project_id)) or 0) + 1
    codes = set(session.scalars(select(QRLink.audio_code).where(QRLink.project_id == story.project_id)).all())
    while f"A{n:02d}" in codes:
        n += 1
    token = generate_qr_token()
    while session.scalar(select(QRLink.id).where(QRLink.token == token)):
        token = generate_qr_token()
    link = QRLink(token=token, project_id=story.project_id, story_id=story.id, audio_code=f"A{n:02d}")
    session.add(link)
    session.flush()
    return link


def qr_svg(data: str, scale: int = 6, dark: str = "#17324D", light: str | None = "#FFFFFF", border: int = 2) -> str:
    return segno.make(data, error="q").svg_inline(scale=scale, dark=dark, light=light, border=border)


def qr_matrix(data: str) -> list[list[bool]]:
    return [[bool(v) for v in row] for row in segno.make(data, error="q").matrix]


def join_message(join_code: str) -> str:
    return f"Hallo Vertelschat, ik doe mee! Mijn code is {join_code}"


def wa_join_link(join_code: str) -> str:
    number = get_settings().whatsapp_link_number
    return f"https://wa.me/{number}?text={quote(join_message(join_code))}"


def wa_share_link(text: str) -> str:
    return f"https://wa.me/?text={quote(text)}"
