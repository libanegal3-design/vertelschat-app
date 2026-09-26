"""Shopify integration: paid orders create entitlements; the purchaser gets a login link to set up the project.

Webhook topics: orders/paid (required), refunds/create (optional). HMAC: base64(HMAC-SHA256(secret, raw body))
in X-Shopify-Hmac-Sha256; idempotency via X-Shopify-Webhook-Id and the unique order id."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .analytics import track
from .audit import audit
from .config import get_settings
from .db import utcnow
from .mailer import send_mail
from . import pricing
from .models import Entitlement, Family, FamilyLink, LoginToken, Order, Project, PromptSchedule, User, WebhookReceipt
from .security import hash_token, new_token, verify_shopify_hmac

log = logging.getLogger("vertelschat.shopify")
SKUS = pricing.SKUS  # kept for imports elsewhere


class ShopifySignatureError(Exception):
    pass


def get_or_create_user(session: Session, email: str, name: str = "") -> User:
    email = email.strip().lower()
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=name.strip())
        session.add(user)
        session.flush()
    elif name and not user.name:
        user.name = name.strip()
    return user


def login_link(session: Session, email: str, next_url: str = "/", days: int = 7) -> str:
    token = new_token()
    session.add(LoginToken(email=email.lower(), token_hash=hash_token(token), next_url=next_url,
                           expires_at=utcnow() + timedelta(days=days)))
    return f"{get_settings().base_url}/inloggen/{token}"


def extend_project(session: Session, project: Project) -> None:
    from .scheduler import compute_next_send

    now = utcnow()
    if project.status == "completed" or (project.active_until and project.active_until < now):
        project.status = "active"
        project.active_until = now + timedelta(days=365)
        project.completed_at = None
        project.ending_reminder_sent_at = None
        sched = session.get(PromptSchedule, project.id)
        if sched:
            sched.next_send_at = compute_next_send(sched, now + timedelta(days=1))
    elif project.active_until:
        project.active_until = project.active_until + timedelta(days=365)
        project.ending_reminder_sent_at = None
    track(session, "subscription_extended", project.id)
    audit(session, "project_extended", project_id=project.id)


def ingest_webhook(session: Session, raw: bytes, hmac_header: str | None, topic: str, webhook_id: str) -> str:
    if not verify_shopify_hmac(raw, hmac_header):
        raise ShopifySignatureError("ongeldige Shopify-handtekening")
    data = json.loads(raw.decode("utf-8"))
    receipt_id = webhook_id or f"{topic}:{data.get('id')}"
    try:
        with session.begin_nested():
            session.add(WebhookReceipt(source="shopify", external_id=receipt_id, topic=topic))
            session.flush()
    except IntegrityError:
        return "duplicate"
    if topic == "orders/paid":
        return handle_order_paid(session, data)
    if topic in ("refunds/create", "orders/cancelled"):
        return handle_refund(session, data, topic)
    return "ignored"


def shipping_from_order(data: dict) -> dict:
    a = data.get("shipping_address") or data.get("billing_address") or {}
    name = (a.get("name") or f"{a.get('first_name') or ''} {a.get('last_name') or ''}").strip()
    street = " ".join(x for x in (a.get("address1"), a.get("address2")) if x)
    country = {"NL": "Nederland", "BE": "België"}.get((a.get("country_code") or "").upper(), a.get("country") or "")
    return {"naam": name[:200], "straat": street[:200], "postcode": (a.get("zip") or "")[:20],
            "plaats": (a.get("city") or "")[:100], "land": country[:60]}


def handle_order_paid(session: Session, data: dict) -> str:
    """Turn a paid Shopify order into entitlements and effects. Every SKU ever sold is understood (see pricing.SKUS);
    context comes from hidden line-item properties set by the theme (_project, _family, _print_order, _book)."""
    from . import commerce
    from .models import BookVersion, PrintOrder

    external_id = str(data.get("id"))
    if session.scalar(select(Order.id).where(Order.external_id == external_id)):
        return "duplicate"
    customer = data.get("customer") or {}
    email = (data.get("email") or data.get("contact_email") or customer.get("email") or "").strip().lower()
    if not email:
        log.error("order %s has no e-mail address", external_id)
        return "no_email"
    name = f"{customer.get('first_name') or ''} {customer.get('last_name') or ''}".strip()
    order = Order(external_id=external_id, order_number=str(data.get("name") or data.get("order_number") or ""),
                  email=email, currency=data.get("currency") or "EUR", total=str(data.get("total_price") or "0.00"))
    session.add(order)
    session.flush()
    user = get_or_create_user(session, email, name)
    shipping = shipping_from_order(data)
    storytellers: list[Entitlement] = []
    gift_lines: list[tuple[Entitlement, dict]] = []
    po_payments: dict[str, dict] = {}
    family_buys: dict[str, dict] = {}
    mails: list[tuple[str, str]] = []
    for li in data.get("line_items") or []:
        sku = (li.get("sku") or "").strip().upper()
        kind, attrs = pricing.SKUS.get(sku, (None, {}))
        if not kind:
            continue
        props = {str(p.get("name")): str(p.get("value") or "") for p in (li.get("properties") or [])}
        qty = max(1, int(li.get("quantity") or 1))
        project = session.get(Project, props["_project"]) if props.get("_project") else None
        track(session, "purchase_completed", project.id if project else None, sku=sku, quantity=qty)
        if kind == "storyteller":
            is_gift = props.get("Cadeau", "").strip().lower() in ("ja", "yes", "true", "1")
            family_id = props.get("_family") if props.get("_family") and \
                session.get(Family, props.get("_family")) else None
            for n in range(qty * attrs["count"]):
                tier = attrs.get("tier") or ("family" if attrs["count"] == 2 and n % 2 == 1 else "standard")
                ent = Entitlement(order_id=order.id, user_id=user.id, kind="verteljaar", sku=sku, is_gift=is_gift,
                                  recipient_name=props.get("Voor", "")[:200], gift_message=props.get("Boodschap", "")[:2000],
                                  family_id=family_id, group_id=order.id if attrs["count"] > 1 else None, tier=tier,
                                  meta={"terms": pricing.TERMS_VERSION})
                session.add(ent)
                storytellers.append(ent)
        elif kind == "renewal":
            ent = Entitlement(order_id=order.id, user_id=user.id, project_id=project.id if project else None,
                              kind="renewal", sku=sku, quantity=qty, meta=dict(attrs))
            session.add(ent)
            session.flush()
            if project is None:
                log.error("renewal in order %s without project", external_id)
                continue
            for _ in range(qty):
                commerce.renew(session, project, ent, book_credit=attrs["book_credit"])
            ent.status, ent.used_at = "used", utcnow()
            mails.append(("Nog een verteljaar", f"{project.title} gaat verder: er komen weer nieuwe vragen. "
                          + ("Er staat ook een boektegoed klaar." if attrs["book_credit"] else "")))
        elif kind == "gift_package":
            ent = Entitlement(order_id=order.id, user_id=user.id, project_id=project.id if project else None,
                              kind="gift_package", sku=sku, quantity=qty, meta={"format": attrs["format"]})
            session.add(ent)
            session.flush()
            gift_lines.append((ent, {"project": project, "format": attrs["format"], "qty": qty}))
        elif kind == "book_credit":  # v1 prepaid extra copies, printed from the app later
            ent = Entitlement(order_id=order.id, user_id=user.id, project_id=project.id if project else None,
                              kind="book_credit", sku=sku, quantity=qty, status="used" if project else "available")
            session.add(ent)
            session.flush()
            if project:
                commerce.grant(session, project, "book", qty, "purchase", entitlement_id=ent.id,
                               note="extra exemplaar gekocht in de winkel")
            track(session, "extra_book_ordered", project.id if project else None, quantity=qty)
        elif kind in ("copy", "page_upgrade"):
            po_id = props.get("_print_order", "")
            session.add(Entitlement(order_id=order.id, user_id=user.id, project_id=project.id if project else None,
                                    kind=kind, sku=sku, quantity=qty, status="used", meta={**attrs, "print_order": po_id}))
            pay = po_payments.setdefault(po_id, {"first": 0, "next": 0, "upgrades": 0, "tier": 9})
            if kind == "copy":
                pay["first" if attrs["position"] == "first" else "next"] += qty
                pay["tier"] = min(pay["tier"], attrs["tier"])
            else:
                pay["upgrades"] += qty
                pay["tier"] = min(pay["tier"], attrs["tier"])
            track(session, "extra_book_ordered", project.id if project else None, quantity=qty)
        elif kind == "family_copy":
            token = props.get("_book", "")
            session.add(Entitlement(order_id=order.id, user_id=user.id, kind=kind, sku=sku, quantity=qty,
                                    status="used", meta={**attrs, "book": token[:8]}))
            buy = family_buys.setdefault(token, {"first": 0, "next": 0, "tier": 9})
            buy["first" if attrs["position"] == "first" else "next"] += qty
            buy["tier"] = min(buy["tier"], attrs["tier"])
    session.flush()
    # gift packages wait for the storyteller they were bought with (the card needs that storyteller's join code)
    for ent, info in gift_lines:
        target = storytellers[0] if (info["project"] is None and storytellers) else ent
        for _ in range(info["qty"]):
            commerce.create_gift_fulfilment(session, order, target, info["project"], shipping, info["format"])
    for po_id, pay in po_payments.items():
        po = session.get(PrintOrder, po_id) if po_id else None
        if po is None:
            log.error("book payment in order %s without a known print order (%s)", external_id, po_id)
            mails.append(("We kijken je bestelling na", "We konden je betaling niet direct aan een boekbestelling "
                          "koppelen. We nemen contact met je op."))
            continue
        commerce.apply_payment(session, po, first=pay["first"], next_=pay["next"], upgrades=pay["upgrades"],
                               tier_paid=pay["tier"] if pay["first"] or pay["next"] else None, shop_order_id=order.id)
    for token, buy in family_buys.items():
        link = session.scalar(select(FamilyLink).where(FamilyLink.token == token)) if token else None
        po = commerce.family_order(session, link, first=buy["first"], next_=buy["next"], tier_paid=buy["tier"],
                                   shipping=shipping, shop_order_id=order.id, buyer_email=email) if link else None
        if po is None:
            log.error("family copy in order %s with unknown or revoked link", external_id)
            mails.append(("We kijken je bestelling na", "De link waarmee je bestelde werkt niet meer. We nemen "
                          "contact met je op; je krijgt je boek of je geld terug."))
        else:
            mails.append(("Je exemplaar wordt gemaakt", "Dankjewel! We laten het boek drukken en sturen het naar "
                          f"{shipping.get('plaats') or 'het opgegeven adres'}. Reken op ongeveer twee tot drie weken."))
    audit(session, "order_paid", actor_kind="webhook", target_type="order", target_id=order.id)
    if storytellers:
        link = login_link(session, email, "/start", days=30)
        n = len(storytellers)
        who = "het verteljaar" if n == 1 else f"{n} verteljaren"
        extra = ""
        if any(e.family_id for e in storytellers):
            extra = "\n\nDe nieuwe verteller komt in dezelfde familie: iedereen die al meeleest, hoort er meteen bij."
        if gift_lines:
            extra += ("\n\nHet cadeaupakket sturen we op zodra je het verteljaar hebt klaargezet: de kaart krijgt de "
                      "persoonlijke QR-code van de verteller.")
        send_mail(session, email, "Welkom bij Vertelschat: zet het verteljaar klaar",
                  f"Dankjewel voor je bestelling {order.order_number}.\n\nJe hebt {who} gekocht. In een paar minuten zet "
                  "je alles klaar: voor wie het is, hoe vaak er een vraag komt en welke vragen als eerste. Daarna krijg "
                  "je een WhatsApp-berichtje om door te sturen, of een cadeaukaart om te printen.\n\nGeen app, geen "
                  "wachtwoord: de verteller antwoordt gewoon met spraakberichten in WhatsApp." + extra,
                  cta_url=link, cta_label="Verteljaar instellen")
    elif mails:
        subject, body = mails[0]
        send_mail(session, email, subject, f"Bestelling {order.order_number}. " + body,
                  cta_url=login_link(session, email, "/app") if not family_buys else "",
                  cta_label="Naar Vertelschat" if not family_buys else "")
    return "processed"


def handle_refund(session: Session, data: dict, topic: str) -> str:
    order_id = str(data.get("order_id") or data.get("id"))
    order = session.scalar(select(Order).where(Order.external_id == order_id))
    if order is None:
        return "unknown_order"
    order.status = "refunded" if topic == "refunds/create" else "cancelled"
    for ent in session.scalars(select(Entitlement).where(Entitlement.order_id == order.id)).all():
        if ent.status == "available":
            ent.status = "refunded"
    from .models import PrintOrder
    for po in session.scalars(select(PrintOrder).where(PrintOrder.status == "awaiting_submission")).all():
        if order.id in (po.shop_order_ids or []):
            po.payment_status = "abandoned" if po.source == "app" else po.payment_status
            po.status = "cancelled"
    audit(session, f"order_{order.status}", actor_kind="webhook", target_type="order", target_id=order.id)
    # Data is never deleted because of a refund; an active project keeps running until support decides otherwise.
    return "processed"


def extra_book_url(project: Project) -> str:
    """Extra copies are ordered in the app (book page), where the address and quantity are known."""
    return f"{get_settings().base_url}/p/{project.id}/boek#bestellen"


def extension_url(project: Project) -> str:
    from .commerce import renewal_url

    return renewal_url(project, legacy=(project.terms_version or "v1") == "v1")
