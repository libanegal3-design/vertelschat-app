"""Privacy-safe product analytics: event names + coarse properties. No story content, names, e-mail or phone."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AnalyticsEvent

EVENTS = {
    # storefront (sent client-side to Plausible by the Shopify theme) and server-side events
    "product_viewed", "add_to_cart", "checkout_started", "purchase_completed", "onboarding_started",
    "storyteller_invited", "whatsapp_opt_in_started", "whatsapp_opt_in_completed", "first_prompt_sent",
    "first_voice_note_received", "first_story_completed", "family_member_invited", "prompt_answered",
    "story_generated", "book_preview_generated", "book_approved", "extra_book_ordered",
    "subscription_extended", "full_archive_exported", "qr_scanned", "storyteller_opted_out",
}
ALLOWED_PROPS = {"source", "sku", "quantity", "kind", "count", "duration_bucket", "locale", "gift", "pages",
                 "via", "provider", "status", "step", "role", "after_period"}


def duration_bucket(seconds: float | None) -> str:
    if not seconds:
        return "0"
    for limit, label in ((60, "<1m"), (180, "1-3m"), (600, "3-10m"), (1800, "10-30m")):
        if seconds < limit:
            return label
    return "30m+"


def track(session: Session, name: str, project_id: str | None = None, **props) -> None:
    if name not in EVENTS:
        raise ValueError(f"unknown analytics event {name}")
    clean = {k: v for k, v in props.items() if k in ALLOWED_PROPS and isinstance(v, (str, int, float, bool))}
    session.add(AnalyticsEvent(name=name, project_id=project_id, props=clean))
