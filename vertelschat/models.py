"""Data model. Originals (audio, transcripts, AI stories, user edits) are always stored separately.

Conventions
- string primary keys (uuid4 hex) for everything that can appear in a URL, so ids are not guessable;
- naive UTC timestamps (db.utcnow);
- JSON columns are replaced, never mutated in place.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


def new_id() -> str:
    return uuid.uuid4().hex


def _id() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- people & access
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _id()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    locale: Mapped[str] = mapped_column(String(10), default="nl")
    notify_mode: Mapped[str] = mapped_column(String(20), default="immediate")  # immediate | daily | off
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    @property
    def first_name(self) -> str:
        return (self.name or self.email.split("@")[0]).split(" ")[0]


class LoginToken(Base):
    __tablename__ = "login_tokens"
    id: Mapped[str] = _id()
    email: Mapped[str] = mapped_column(String(320), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    next_url: Mapped[str] = mapped_column(String(500), default="/")
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class WebSession(Base):
    __tablename__ = "web_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of cookie value
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    user: Mapped[User] = relationship()


class Family(Base):
    __tablename__ = "families"
    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = _created()
    memberships: Mapped[list["Membership"]] = relationship(back_populates="family", cascade="all, delete-orphan")


ROLES = ("owner", "editor", "contributor", "viewer")
ROLE_RANK = {"viewer": 0, "contributor": 1, "editor": 2, "owner": 3}


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("family_id", "user_id"),)
    id: Mapped[str] = _id()
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    created_at: Mapped[datetime] = _created()
    family: Mapped[Family] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship()


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[str] = _id()
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(20), default="contributor")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# --------------------------------------------------------------------------- projects
class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = _id()
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    # setup -> awaiting_storyteller -> active -> completed (never deleted automatically)
    status: Mapped[str] = mapped_column(String(30), default="setup", index=True)
    locale: Mapped[str] = mapped_column(String(10), default="nl-NL")  # nl-NL | nl-BE
    purchase_kind: Mapped[str] = mapped_column(String(10), default="self")  # self | gift
    gift_from_name: Mapped[str] = mapped_column(String(200), default="")
    gift_message: Mapped[str] = mapped_column(Text, default="")
    sensitive_topics: Mapped[bool] = mapped_column(Boolean, default=False)
    vocabulary: Mapped[str] = mapped_column(Text, default="")  # names/places to help transcription
    period_days: Mapped[int] = mapped_column(Integer, default=365)
    grace_days: Mapped[int] = mapped_column(Integer, default=30)
    created_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    active_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ending_reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    first_story_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    deletion_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # purged 14 days later
    terms_version: Mapped[str] = mapped_column(String(10), default="v2")  # v1 = bought before pricing V2
    dismissed_offers: Mapped[list] = mapped_column(JSON, default=list)
    family: Mapped[Family] = relationship()
    storyteller: Mapped[Optional["Storyteller"]] = relationship(back_populates="project", uselist=False)
    schedule: Mapped[Optional["PromptSchedule"]] = relationship(back_populates="project", uselist=False)

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.status == "active" and (self.active_until is None or now <= self.active_until)


class WhatsAppIdentity(Base):
    """A WhatsApp user as seen by our business number. Phone numbers are encrypted at rest; the
    business-scoped user id (BSUID) is matched first because the phone number may be omitted."""
    __tablename__ = "whatsapp_identities"
    id: Mapped[str] = _id()
    phone_enc: Mapped[Optional[str]] = mapped_column(Text)
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    bsuid: Mapped[Optional[str]] = mapped_column(String(120), unique=True)
    profile_name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = _created()
    last_inbound_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_unknown_reply_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_unsupported_reply_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Storyteller(Base):
    __tablename__ = "storytellers"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), unique=True)
    identity_id: Mapped[Optional[str]] = mapped_column(ForeignKey("whatsapp_identities.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    address_as: Mapped[str] = mapped_column(String(60), default="")  # "mam", "opa", "Marijke"
    birth_year: Mapped[Optional[int]] = mapped_column(Integer)
    join_code: Mapped[str] = mapped_column(String(12), unique=True)
    consent_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|given|withdrawn
    consent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    opted_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    thanks_sent: Mapped[int] = mapped_column(Integer, default=0)
    text_hint_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    after_period_notice_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    resend_request_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    extra_questions_date: Mapped[str] = mapped_column(String(10), default="")
    extra_questions_count: Mapped[int] = mapped_column(Integer, default=0)
    project: Mapped[Project] = relationship(back_populates="storyteller")
    identity: Mapped[Optional[WhatsAppIdentity]] = relationship()

    @property
    def greeting_name(self) -> str:
        return self.address_as or self.name

    @property
    def can_receive_prompts(self) -> bool:
        return bool(self.identity_id) and self.consent_status == "given" and self.opted_out_at is None


class Consent(Base):
    __tablename__ = "consents"
    id: Mapped[str] = _id()
    storyteller_id: Mapped[str] = mapped_column(ForeignKey("storytellers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # whatsapp_questions | recording_processing
    status: Mapped[str] = mapped_column(String(20))  # given | withdrawn
    method: Mapped[str] = mapped_column(String(40))  # whatsapp_button | whatsapp_keyword | family_confirmed
    evidence: Mapped[str] = mapped_column(String(200), default="")
    text_version: Mapped[str] = mapped_column(String(40), default="welkom-v1")
    created_at: Mapped[datetime] = _created()


class PromptSchedule(Base):
    __tablename__ = "prompt_schedules"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    cadence_days: Mapped[int] = mapped_column(Integer, default=7)
    weekday: Mapped[int] = mapped_column(Integer, default=3)  # Monday=0
    send_hour: Mapped[int] = mapped_column(Integer, default=10)
    send_minute: Mapped[int] = mapped_column(Integer, default=0)
    timezone: Mapped[str] = mapped_column(String(40), default="Europe/Amsterdam")
    wait_for_answer: Mapped[bool] = mapped_column(Boolean, default=True)
    max_wait_days: Mapped[int] = mapped_column(Integer, default=14)
    paused_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    next_send_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    project: Mapped[Project] = relationship(back_populates="schedule")


class Prompt(Base):
    __tablename__ = "prompts"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="")
    library_key: Mapped[str] = mapped_column(String(40), default="", index=True)
    source: Mapped[str] = mapped_column(String(20), default="library")  # library|custom|family|followup
    suggested_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    parent_story_id: Mapped[Optional[str]] = mapped_column(String(32))
    # suggested -> queued -> scheduled -> sent -> answered ; also rejected | skipped | failed | send_unknown
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    sent_via: Mapped[str] = mapped_column(String(20), default="")  # template | session
    template_name: Mapped[str] = mapped_column(String(60), default="")
    wa_message_id: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    failure_reason: Mapped[str] = mapped_column(String(300), default="")
    suggested_by: Mapped[Optional[User]] = relationship()


# --------------------------------------------------------------------------- messages & media
class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"
    id: Mapped[str] = _id()
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    wamid: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    identity_id: Mapped[Optional[str]] = mapped_column(ForeignKey("whatsapp_identities.id", ondelete="SET NULL"), index=True)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    prompt_id: Mapped[Optional[str]] = mapped_column(String(32))
    msg_type: Mapped[str] = mapped_column(String(30), default="")
    context_wamid: Mapped[Optional[str]] = mapped_column(String(200))
    forwarded: Mapped[bool] = mapped_column(Boolean, default=False)
    body_text: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # inbound: received|processed|ignored|error ; outbound: sending|sent|delivered|read|failed|unknown
    status: Mapped[str] = mapped_column(String(20), default="received")
    error_code: Mapped[str] = mapped_column(String(20), default="")
    error_text: Mapped[str] = mapped_column(String(500), default="")
    wa_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = _created()
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[str] = _id()
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    identity_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)  # for quarantined media
    story_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)  # attachments (video/document)
    kind: Mapped[str] = mapped_column(String(20))  # audio|image|video|document|pdf|preview|export
    storage_key: Mapped[Optional[str]] = mapped_column(String(300), unique=True)
    mime_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    expected_sha256_b64: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(20), default="whatsapp")  # whatsapp|upload|generated
    is_original: Mapped[bool] = mapped_column(Boolean, default=True)
    derived_from_id: Mapped[Optional[str]] = mapped_column(String(32))
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(300), default="")
    wa_media_id: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    source_wamid: Mapped[Optional[str]] = mapped_column(String(200))
    wa_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="stored")  # pending|stored|failed|purged
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(500), default="")
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    purge_after: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = _created()


class Story(Base):
    __tablename__ = "stories"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    prompt_id: Mapped[Optional[str]] = mapped_column(ForeignKey("prompts.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="processing")  # processing|ready|needs_review
    compose_generation: Mapped[int] = mapped_column(Integer, default=0)
    composed_generation: Mapped[int] = mapped_column(Integer, default=0)
    edited_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_suggestion_title: Mapped[str] = mapped_column(String(300), default="")
    ai_suggestion_body: Mapped[str] = mapped_column(Text, default="")
    review_flags: Mapped[list] = mapped_column(JSON, default=list)
    include_in_book: Mapped[bool] = mapped_column(Boolean, default=True)
    chapter_id: Mapped[Optional[str]] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    first_part_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_part_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    hidden_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    prompt: Mapped[Optional[Prompt]] = relationship()
    recordings: Mapped[list["Recording"]] = relationship(back_populates="story", order_by="Recording.received_at")


class Recording(Base):
    """One part of a story: a voice note (kind=audio) or a typed message (kind=text)."""
    __tablename__ = "recordings"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    storyteller_id: Mapped[Optional[str]] = mapped_column(ForeignKey("storytellers.id", ondelete="SET NULL"))
    story_id: Mapped[Optional[str]] = mapped_column(ForeignKey("stories.id", ondelete="SET NULL"), index=True)
    prompt_id: Mapped[Optional[str]] = mapped_column(String(32))
    message_id: Mapped[Optional[str]] = mapped_column(String(32))
    wamid: Mapped[Optional[str]] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default="audio")
    source: Mapped[str] = mapped_column(String(20), default="whatsapp")  # whatsapp|upload
    original_media_id: Mapped[Optional[str]] = mapped_column(ForeignKey("media_assets.id", ondelete="SET NULL"))
    playback_media_id: Mapped[Optional[str]] = mapped_column(ForeignKey("media_assets.id", ondelete="SET NULL"))
    text_content: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    # downloading|stored|transcribing|transcribed|download_failed|corrupt|transcription_failed|retracted
    status: Mapped[str] = mapped_column(String(30), default="downloading", index=True)
    hold_reason: Mapped[Optional[str]] = mapped_column(String(30))  # consent|forwarded|assignment|after_period
    assignment: Mapped[str] = mapped_column(String(20), default="")  # reply|session|current_prompt|addendum|free|note|manual
    forwarded: Mapped[bool] = mapped_column(Boolean, default=False)
    after_period: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str] = mapped_column(String(300), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    retracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = _created()
    story: Mapped[Optional[Story]] = relationship(back_populates="recordings")
    original: Mapped[Optional[MediaAsset]] = relationship(foreign_keys=[original_media_id])
    playback: Mapped[Optional[MediaAsset]] = relationship(foreign_keys=[playback_media_id])
    transcript: Mapped[Optional["Transcript"]] = relationship(back_populates="recording", uselist=False)


class Transcript(Base):
    __tablename__ = "transcripts"
    id: Mapped[str] = _id()
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"), unique=True)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(10), default="nl")
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80), default="")
    segments: Mapped[list] = mapped_column(JSON, default=list)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    recording: Mapped[Recording] = relationship(back_populates="transcript")


class StoryRevision(Base):
    __tablename__ = "story_revisions"
    id: Mapped[str] = _id()
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    author_kind: Mapped[str] = mapped_column(String(10))  # ai | user | system
    author_id: Mapped[Optional[str]] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = _created()


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    story_id: Mapped[Optional[str]] = mapped_column(ForeignKey("stories.id", ondelete="SET NULL"), index=True)
    media_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id", ondelete="CASCADE"))
    caption: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(20), default="upload")
    uploaded_by_id: Mapped[Optional[str]] = mapped_column(String(32))
    include_in_book: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    media: Mapped[MediaAsset] = relationship()


class Chapter(Base):
    __tablename__ = "chapters"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------- book, QR, commerce
class Book(Base):
    __tablename__ = "books"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    subtitle: Mapped[str] = mapped_column(String(200), default="")
    dedication: Mapped[str] = mapped_column(Text, default="")
    cover_style: Mapped[str] = mapped_column(String(20), default="nacht")  # nacht | schemer | foto
    cover_photo_id: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class BookVersion(Base):
    __tablename__ = "book_versions"
    id: Mapped[str] = _id()
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="rendering")  # rendering|ready|failed
    interior_media_id: Mapped[Optional[str]] = mapped_column(String(32))
    cover_media_id: Mapped[Optional[str]] = mapped_column(String(32))
    screen_media_id: Mapped[Optional[str]] = mapped_column(String(32))
    preview_media_ids: Mapped[list] = mapped_column(JSON, default=list)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    story_ids: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = _created()
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    approved_by_id: Mapped[Optional[str]] = mapped_column(String(32))


class QRLink(Base):
    """Stable, non-guessable token printed in the book. Never expires; survives the paid period."""
    __tablename__ = "qr_links"
    __table_args__ = (UniqueConstraint("project_id", "audio_code"),)
    id: Mapped[str] = _id()
    token: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id", ondelete="CASCADE"), unique=True)
    audio_code: Mapped[str] = mapped_column(String(8))  # A01, A02 ... printed next to the QR code
    access_mode: Mapped[str] = mapped_column(String(10), default="link")  # link | pin
    pin_hash: Mapped[str] = mapped_column(String(200), default="")
    scan_count: Mapped[int] = mapped_column(Integer, default=0)
    last_scanned_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = _created()


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = _id()
    provider: Mapped[str] = mapped_column(String(20), default="shopify")
    external_id: Mapped[str] = mapped_column(String(100), unique=True)
    order_number: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    total: Mapped[str] = mapped_column(String(20), default="0.00")
    status: Mapped[str] = mapped_column(String(20), default="paid")  # paid | refunded | cancelled
    created_at: Mapped[datetime] = _created()


class Entitlement(Base):
    __tablename__ = "entitlements"
    id: Mapped[str] = _id()
    order_id: Mapped[Optional[str]] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    # verteljaar (a storyteller year) | book_credit | renewal | gift_package | copy | family_copy | page_upgrade
    # (v1 names still read: extra_book, extension, gift_card)
    kind: Mapped[str] = mapped_column(String(20))
    sku: Mapped[str] = mapped_column(String(40), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="available")  # available | used | refunded
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False)
    recipient_name: Mapped[str] = mapped_column(String(200), default="")
    gift_message: Mapped[str] = mapped_column(Text, default="")
    family_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)   # extra storyteller: which family
    group_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)    # storytellers bought together
    tier: Mapped[str] = mapped_column(String(20), default="standard")          # standard | family
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class CreditEntry(Base):
    """Ledger of what a project may still print: book credits and larger-book (page) credits. Balance = sum(delta).
    Credits come from purchases (included book, renewal, v1 prepaid copies); print orders consume them."""
    __tablename__ = "credit_entries"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))       # book | pages_1 | pages_2
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(30))     # included | renewal | purchase | print | refund | backfill
    entitlement_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    print_order_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = _created()


class FamilyLink(Base):
    """Private buy link for an approved book. Shows only title, cover and page count; never stories."""
    __tablename__ = "family_links"
    id: Mapped[str] = _id()
    token: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    book_version_id: Mapped[str] = mapped_column(ForeignKey("book_versions.id", ondelete="CASCADE"))
    created_by_id: Mapped[Optional[str]] = mapped_column(String(32))
    copies_ordered: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class GiftFulfilment(Base):
    """A physical gift item (cadeaupakket, or the v1 posted card). Printed once the project exists, because the
    card carries the storyteller's personal join code."""
    __tablename__ = "gift_fulfilments"
    id: Mapped[str] = _id()
    order_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    entitlement_id: Mapped[Optional[str]] = mapped_column(String(32))
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    format: Mapped[str] = mapped_column(String(10), default="pakket")   # pakket | kaart
    status: Mapped[str] = mapped_column(String(20), default="awaiting_setup")  # awaiting_setup|ready|sent|cancelled
    shipping: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class PrintOrder(Base):
    __tablename__ = "print_orders"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    book_version_id: Mapped[str] = mapped_column(ForeignKey("book_versions.id", ondelete="CASCADE"))
    entitlement_id: Mapped[Optional[str]] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(20), default="manual")
    external_id: Mapped[str] = mapped_column(String(100), default="")
    # awaiting_submission|submitted|in_production|shipped|delivered|failed|cancelled
    status: Mapped[str] = mapped_column(String(30), default="awaiting_submission")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    credit_copies: Mapped[int] = mapped_column(Integer, default=0)      # covered by book credits
    paid_copies: Mapped[int] = mapped_column(Integer, default=0)        # paid in the shop
    page_tier: Mapped[int] = mapped_column(Integer, default=0)
    # prepaid (credits only) | awaiting_payment | paid | partly_paid | abandoned
    payment_status: Mapped[str] = mapped_column(String(20), default="prepaid")
    source: Mapped[str] = mapped_column(String(20), default="app")      # app | family_link | legacy
    family_link_id: Mapped[Optional[str]] = mapped_column(String(32))
    shop_order_ids: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    shipping: Mapped[dict] = mapped_column(JSON, default=dict)
    tracking_url: Mapped[str] = mapped_column(String(500), default="")
    error: Mapped[str] = mapped_column(String(500), default="")
    created_by_id: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    requested_by_id: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|building|ready|failed
    media_id: Mapped[Optional[str]] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = _created()
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# --------------------------------------------------------------------------- operations
class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = _created()
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    emailed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    actor_kind: Mapped[str] = mapped_column(String(20))  # user|storyteller|system|webhook|staff
    actor_id: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(60))
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued|running|done|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    locked_by: Mapped[str] = mapped_column(String(80), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AnalyticsEvent(Base):
    """Product analytics. Never contains story content, names, phone numbers or e-mail addresses."""
    __tablename__ = "analytics_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    props: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class OutboxMail(Base):
    __tablename__ = "mail_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    to: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    html: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|sent|failed|captured
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = _created()
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipts"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(200))
    topic: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = _created()
