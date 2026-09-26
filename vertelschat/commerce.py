"""Commerce after the first purchase: what a family may print, what they still have to pay, and when we offer more.

- A credit ledger (CreditEntry) records book credits (the included book, a renewal's book) and larger-book credits.
- Print orders are placed in the app (address in the app). Copies not covered by credits are paid in the shop via a
  small bridge page; the order is printed only when every copy is paid (never print unpaid copies).
- Family buy links let relatives buy their own copy of an approved book, without any access to the project.
- Gift packages are printed once the project exists (the card carries the storyteller's personal join code).
- Lifecycle offers: at most one calm card on the dashboard, only for organisers, only at the right moment.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import pricing
from .audit import audit
from .config import get_settings
from .db import utcnow
from .jobs import enqueue
from .models import (BookVersion, Book, CreditEntry, Entitlement, Family, FamilyLink, GiftFulfilment, Membership,
                     Order, PrintOrder, Project, PromptSchedule, ROLE_RANK, User)
from .notify import notify_organizers
from .security import signer

log = logging.getLogger("vertelschat.commerce")
SHIPPING_KEYS = ("naam", "straat", "postcode", "plaats", "land")
MAX_COPIES_PER_ORDER = 20


class CommerceError(Exception):
    pass


# =========================================================================== credit ledger
def ensure_ledger(session: Session, project: Project) -> None:
    """Projects from before the ledger (terms v1) get their balance reconstructed once from v1 entitlements and
    orders. Projects created under pricing V2 get their credits from the purchase itself."""
    if (project.terms_version or "v1") != "v1":
        return
    if session.scalar(select(CreditEntry.id).where(CreditEntry.project_id == project.id).limit(1)):
        return
    included = session.scalar(select(func.count(Entitlement.id)).where(Entitlement.project_id == project.id,
                                                                       Entitlement.kind == "verteljaar")) or 0
    extra = session.scalar(select(func.coalesce(func.sum(Entitlement.quantity), 0))
                           .where(Entitlement.project_id == project.id,
                                  Entitlement.kind.in_(("extra_book", "book_credit")),
                                  Entitlement.status == "available")) or 0
    used = session.scalar(select(func.coalesce(func.sum(PrintOrder.quantity), 0))
                          .where(PrintOrder.project_id == project.id, PrintOrder.status != "cancelled")) or 0
    for delta, note in ((included, "boek bij het verteljaar (v1)"), (int(extra), "vooruitbetaalde exemplaren (v1)"),
                        (-int(used), "al bestelde boeken (v1)")):
        if delta:
            session.add(CreditEntry(project_id=project.id, kind="book", delta=delta, reason="backfill", note=note))
    session.flush()


def balance(session: Session, project: Project, kind: str = "book") -> int:
    ensure_ledger(session, project)
    return int(session.scalar(select(func.coalesce(func.sum(CreditEntry.delta), 0))
                              .where(CreditEntry.project_id == project.id, CreditEntry.kind == kind)) or 0)


def grant(session: Session, project: Project, kind: str, delta: int, reason: str, *, entitlement_id: str | None = None,
          print_order_id: str | None = None, note: str = "") -> CreditEntry:
    ensure_ledger(session, project)
    entry = CreditEntry(project_id=project.id, kind=kind, delta=int(delta), reason=reason,
                        entitlement_id=entitlement_id, print_order_id=print_order_id, note=note[:300])
    session.add(entry)
    session.flush()
    return entry


def credits_summary(session: Session, project: Project) -> dict:
    ensure_ledger(session, project)
    rows = session.execute(select(CreditEntry.kind, CreditEntry.delta, CreditEntry.reason)
                           .where(CreditEntry.project_id == project.id)).all()
    book = sum(d for k, d, _ in rows if k == "book")
    granted = sum(d for k, d, r in rows if k == "book" and d > 0)
    used = -sum(d for k, d, r in rows if k == "book" and d < 0)
    return {"available": max(0, book), "granted": granted, "used": used,
            "pages": {1: max(0, sum(d for k, d, _ in rows if k == "pages_1")),
                      2: max(0, sum(d for k, d, _ in rows if k == "pages_2"))},
            # v1 template compatibility
            "included": granted, "extra": 0}


# =========================================================================== storyteller entitlements
def family_for_claim(session: Session, ent: Entitlement, user: User) -> Family | None:
    """An extra storyteller joins an existing family when the claiming user organises that family."""
    candidates = []
    if ent.family_id:
        candidates.append(ent.family_id)
    if ent.group_id:
        sibling = session.scalar(select(Project.family_id).join(Entitlement, Entitlement.project_id == Project.id)
                                 .where(Entitlement.group_id == ent.group_id, Entitlement.id != ent.id))
        if sibling:
            candidates.append(sibling)
    for fid in candidates:
        m = session.scalar(select(Membership).where(Membership.family_id == fid, Membership.user_id == user.id))
        if m is not None and ROLE_RANK.get(m.role, 0) >= ROLE_RANK["editor"]:
            return session.get(Family, fid)
    return None


def available_storyteller_entitlements(session: Session, user: User) -> list[Entitlement]:
    return session.scalars(select(Entitlement).where(Entitlement.user_id == user.id, Entitlement.kind == "verteljaar",
                                                     Entitlement.status == "available")
                           .order_by(Entitlement.created_at)).all()


def on_project_created(session: Session, project: Project, ent: Entitlement | None) -> None:
    """Called by services.create_project: the included book credit, and gift packages waiting for this project."""
    if ent is None:
        return
    grant(session, project, "book", 1, "included", entitlement_id=ent.id, note="boek bij het verteljaar")
    ent.meta = dict(ent.meta or {}, project_id=project.id)
    if ent.group_id:  # storytellers bought together share the family
        for sib in session.scalars(select(Entitlement).where(Entitlement.group_id == ent.group_id,
                                                             Entitlement.id != ent.id,
                                                             Entitlement.status == "available")).all():
            sib.family_id = sib.family_id or project.family_id
    waiting = session.scalars(select(GiftFulfilment).where(GiftFulfilment.status == "awaiting_setup",
                                                           GiftFulfilment.project_id.is_(None),
                                                           GiftFulfilment.entitlement_id == ent.id)).all()
    for gf in waiting:
        gf.project_id = project.id
    if waiting:
        session.flush()
        release_gift_fulfilments(session, project)


# =========================================================================== print orders
@dataclass
class Quote:
    quantity: int
    tier: int
    credit_copies: int
    paid_first: int
    paid_next: int
    upgrades: int                 # page surcharges for credit copies not covered by page credits
    to_pay: int                   # cents
    lines: list[str] = field(default_factory=list)

    @property
    def paid_copies(self) -> int:
        return self.paid_first + self.paid_next


def quote(session: Session, project: Project, version: BookVersion, quantity: int) -> Quote:
    if version.approved_at is None or version.status != "ready":
        raise CommerceError("Keur het boek eerst goed.")
    quantity = int(quantity)
    if not 1 <= quantity <= MAX_COPIES_PER_ORDER:
        raise CommerceError(f"Kies 1 tot {MAX_COPIES_PER_ORDER} exemplaren per adres.")
    tier = pricing.page_tier(version.page_count)
    if tier is None:
        raise CommerceError(f"Dit boek telt {version.page_count} pagina's. Eén band kan maximaal "
                            f"{pricing.MAX_PAGES} pagina's hebben; verdeel de verhalen over twee delen.")
    credit_copies = min(balance(session, project, "book"), quantity)
    paid = quantity - credit_copies
    paid_first = 1 if paid and credit_copies == 0 else 0
    paid_next = paid - paid_first
    upgrades = 0
    if tier and credit_copies:
        upgrades = max(0, credit_copies - balance(session, project, f"pages_{tier}"))
    to_pay = (paid_first * pricing.copy_price("first", tier) + paid_next * pricing.copy_price("next", tier)
              + upgrades * pricing.TIER_SURCHARGE[tier])
    lines = []
    if credit_copies:
        lines.append(f"{credit_copies} × boek uit je tegoed" + (f" (+ {upgrades} × toeslag dikker boek "
                     f"{pricing.eur(pricing.TIER_SURCHARGE[tier])})" if upgrades else ""))
    if paid_first:
        lines.append(f"1 × extra exemplaar {pricing.eur(pricing.copy_price('first', tier))}")
    if paid_next:
        lines.append(f"{paid_next} × extra exemplaar in hetzelfde pakket "
                     f"{pricing.eur(pricing.copy_price('next', tier))}")
    return Quote(quantity, tier, credit_copies, paid_first, paid_next, upgrades, to_pay, lines)


def _clean_shipping(shipping: dict) -> dict:
    clean = {k: str(shipping.get(k, "")).strip()[:200] for k in SHIPPING_KEYS}
    if any(not clean[k] for k in SHIPPING_KEYS):
        raise CommerceError("Vul het volledige verzendadres in.")
    return clean


def place_order(session: Session, project: Project, version: BookVersion, user: User, shipping: dict,
                quantity: int = 1) -> tuple[PrintOrder, Quote]:
    """Order copies of an approved book to one address. Always possible, also long after the storytelling year."""
    q = quote(session, project, version, quantity)
    po = PrintOrder(project_id=project.id, book_version_id=version.id, provider=get_settings().print_provider,
                    quantity=q.quantity, credit_copies=q.credit_copies, paid_copies=0, page_tier=q.tier,
                    shipping=_clean_shipping(shipping), created_by_id=user.id, source="app",
                    meta={"needs": {"first": q.paid_first, "next": q.paid_next, "upgrades": q.upgrades},
                          "paid": {"first": 0, "next": 0, "upgrades": 0}, "to_pay": q.to_pay},
                    payment_status="prepaid" if q.to_pay == 0 else "awaiting_payment", shop_order_ids=[])
    session.add(po)
    session.flush()
    # reserve credits right away so a second order cannot use them twice; released again if abandoned
    if q.credit_copies:
        grant(session, project, "book", -q.credit_copies, "print", print_order_id=po.id, note="gereserveerd")
        covered = q.credit_copies - q.upgrades
        if q.tier and covered:
            grant(session, project, f"pages_{q.tier}", -covered, "print", print_order_id=po.id)
    audit(session, "print_ordered", project_id=project.id, actor_kind="user", actor_id=user.id, target_id=po.id)
    if po.payment_status == "prepaid":
        _release_to_printer(session, po)
    return po, q


def _release_to_printer(session: Session, po: PrintOrder) -> None:
    enqueue(session, "submit_print", {"print_order_id": po.id}, dedupe_key=f"print:{po.id}")


def bridge_url(po: PrintOrder, title: str) -> str:
    """Storefront page that puts exactly the unpaid items of this order in the cart (Shopify Basic, AJAX cart)."""
    needs = (po.meta or {}).get("needs", {})
    params = {"po": po.id, "first": needs.get("first", 0), "next": needs.get("next", 0),
              "upgrade": needs.get("upgrades", 0), "omvang": po.page_tier, "titel": title[:80]}
    return f"{get_settings().storefront_url}/pages/boek-afrekenen?{urlencode(params)}"


def apply_payment(session: Session, po: PrintOrder, *, first: int = 0, next_: int = 0, upgrades: int = 0,
                  tier_paid: int | None = None, shop_order_id: str = "") -> None:
    """Record shop payments for an app order; release it to the printer once everything is paid."""
    meta = dict(po.meta or {})
    paid = dict(meta.get("paid") or {"first": 0, "next": 0, "upgrades": 0})
    paid["first"] += first
    paid["next"] += next_
    paid["upgrades"] += upgrades
    meta["paid"] = paid
    if tier_paid is not None and tier_paid < po.page_tier:
        meta["tier_problem"] = f"betaald voor omvang {tier_paid}, boek heeft omvang {po.page_tier}"
    po.meta = meta
    po.shop_order_ids = list(po.shop_order_ids or []) + ([shop_order_id] if shop_order_id else [])
    session.flush()
    finalize_payment(session, po)


def finalize_payment(session: Session, po: PrintOrder) -> str:
    if po.payment_status not in ("awaiting_payment", "partly_paid"):
        return po.payment_status
    meta = dict(po.meta or {})
    needs, paid = meta.get("needs") or {}, meta.get("paid") or {}
    copies_paid = paid.get("first", 0) + paid.get("next", 0)
    copies_needed = needs.get("first", 0) + needs.get("next", 0)
    project = session.get(Project, po.project_id)
    if meta.get("tier_problem"):
        po.payment_status = "partly_paid"
        notify_organizers(session, project, "print_update", "We kijken even mee met je bestelling",
                          "De betaling past niet helemaal bij de omvang van het boek. We nemen contact met je op; "
                          "er wordt niets gedrukt dat niet klopt.", url=f"/p/{project.id}/boek", email=True)
        return po.payment_status
    if copies_paid >= copies_needed and paid.get("upgrades", 0) >= needs.get("upgrades", 0):
        po.paid_copies = copies_paid
        po.payment_status = "paid"
        _release_to_printer(session, po)
        return "paid"
    po.payment_status = "partly_paid" if copies_paid or paid.get("upgrades") else "awaiting_payment"
    return po.payment_status


def abandon_order(session: Session, po: PrintOrder, user: User) -> None:
    """The organiser cancels an unpaid order: reserved credits go back, nothing is printed."""
    if po.payment_status not in ("awaiting_payment", "partly_paid") or po.status != "awaiting_submission":
        raise CommerceError("Deze bestelling is al betaald of onderweg; neem contact met ons op.")
    project = session.get(Project, po.project_id)
    for entry in session.scalars(select(CreditEntry).where(CreditEntry.print_order_id == po.id,
                                                           CreditEntry.delta < 0)).all():
        grant(session, project, entry.kind, -entry.delta, "cancel", print_order_id=po.id, note="bestelling geannuleerd")
    po.payment_status = "abandoned"
    po.status = "cancelled"
    audit(session, "print_order_cancelled", project_id=project.id, actor_kind="user", actor_id=user.id,
          target_id=po.id)


def open_unpaid_order(session: Session, project: Project) -> PrintOrder | None:
    return session.scalar(select(PrintOrder).where(PrintOrder.project_id == project.id,
                                                   PrintOrder.payment_status.in_(("awaiting_payment", "partly_paid")),
                                                   PrintOrder.status == "awaiting_submission")
                          .order_by(PrintOrder.created_at.desc()))


# =========================================================================== family buy links
def approved_version(session: Session, project: Project) -> BookVersion | None:
    book = session.scalar(select(Book).where(Book.project_id == project.id))
    if book is None:
        return None
    return session.scalar(select(BookVersion).where(BookVersion.book_id == book.id, BookVersion.status == "ready",
                                                    BookVersion.approved_at.is_not(None))
                          .order_by(BookVersion.approved_at.desc()))


def active_family_link(session: Session, project: Project) -> FamilyLink | None:
    return session.scalar(select(FamilyLink).where(FamilyLink.project_id == project.id,
                                                   FamilyLink.revoked_at.is_(None))
                          .order_by(FamilyLink.created_at.desc()))


def create_family_link(session: Session, project: Project, user: User) -> FamilyLink:
    version = approved_version(session, project)
    if version is None:
        raise CommerceError("Keur eerst een versie van het boek goed. Daarna kun je het met familie delen.")
    link = active_family_link(session, project)
    if link and link.book_version_id == version.id:
        return link
    if link:  # a newer approved version replaces the old link
        link.revoked_at = utcnow()
    link = FamilyLink(token=secrets.token_urlsafe(24), project_id=project.id, book_version_id=version.id,
                      created_by_id=user.id)
    session.add(link)
    session.flush()
    audit(session, "family_link_created", project_id=project.id, actor_kind="user", actor_id=user.id,
          target_id=link.id)
    return link


def revoke_family_link(session: Session, project: Project, user: User) -> None:
    link = active_family_link(session, project)
    if link:
        link.revoked_at = utcnow()
        audit(session, "family_link_revoked", project_id=project.id, actor_kind="user", actor_id=user.id,
              target_id=link.id)


def resolve_family_link(session: Session, token: str) -> tuple[FamilyLink, Project, BookVersion] | None:
    link = session.scalar(select(FamilyLink).where(FamilyLink.token == token))
    if link is None or link.revoked_at is not None:
        return None
    project = session.get(Project, link.project_id)
    version = session.get(BookVersion, link.book_version_id)
    if project is None or version is None or project.deletion_requested_at or version.approved_at is None:
        return None
    return link, project, version


def family_link_url(link: FamilyLink) -> str:
    return f"{get_settings().base_url}/boek/{link.token}"


def family_bridge_url(link: FamilyLink, version: BookVersion, title: str, copies: int) -> str:
    copies = max(1, min(int(copies), MAX_COPIES_PER_ORDER))
    params = {"book": link.token, "first": 1, "next": copies - 1, "upgrade": 0,
              "omvang": pricing.page_tier(version.page_count) or 0, "titel": title[:80]}
    return f"{get_settings().storefront_url}/pages/boek-afrekenen?{urlencode(params)}"


def family_order(session: Session, link: FamilyLink, *, first: int, next_: int, tier_paid: int, shipping: dict,
                 shop_order_id: str, buyer_email: str = "") -> PrintOrder | None:
    """A relative paid in the shop; the book goes to the address they entered at checkout."""
    resolved = resolve_family_link(session, link.token)
    copies = first + next_
    if resolved is None or copies <= 0:
        return None
    _, project, version = resolved
    tier = pricing.page_tier(version.page_count) or 0
    po = PrintOrder(project_id=project.id, book_version_id=version.id, provider=get_settings().print_provider,
                    quantity=copies, credit_copies=0, paid_copies=copies, page_tier=tier, source="family_link",
                    family_link_id=link.id, shipping=shipping, shop_order_ids=[shop_order_id],
                    payment_status="paid" if tier_paid >= tier else "partly_paid",
                    meta={"buyer_email": buyer_email, "tier_paid": tier_paid})
    session.add(po)
    session.flush()
    link.copies_ordered += copies
    if po.payment_status == "paid":
        _release_to_printer(session, po)
    notify_organizers(session, project, "family_order",
                      f"Er {'is' if copies == 1 else 'zijn'} {copies} exemplaar{'' if copies == 1 else 'en'} besteld "
                      "via je familielink",
                      "Iemand uit de familie heeft zelf een exemplaar van het boek besteld en betaald.",
                      url=f"/p/{project.id}/boek", email=False)
    return po


# =========================================================================== renewals and extra storytellers
def renewal_options(project: Project) -> list[dict]:
    options = [{"sku": "VT-VERLENGING-BOEK", "price": pricing.eur(pricing.RENEWAL), "variant": "boek",
                "title": "Nog een verteljaar met boektegoed",
                "detail": "Twaalf maanden nieuwe vragen, plus een boektegoed voor deel 2 of een extra exemplaar."}]
    if (project.terms_version or "v1") == "v1":
        options.append({"sku": "VT-VERLENGING", "price": pricing.eur(pricing.RENEWAL_LEGACY), "variant": "2026",
                        "title": "Nog een verteljaar zonder boektegoed (jouw oorspronkelijke tarief)",
                        "detail": "Zo stond het op de site toen je Vertelschat kocht; die prijs blijft voor jou gelden."})
    return options


def renew(session: Session, project: Project, ent: Entitlement, *, book_credit: int) -> None:
    from .shopify import extend_project

    extend_project(session, project)
    if book_credit:
        grant(session, project, "book", book_credit, "renewal", entitlement_id=ent.id, note="boektegoed bij verlenging")


def shop_url(key: str, **params) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{get_settings().storefront_url}/products/{pricing.HANDLES[key]}" + (f"?{query}" if query else "")


def renewal_url(project: Project, legacy: bool = False) -> str:
    return shop_url("renewal", project=project.id, tarief="2026" if legacy else None)


def extra_storyteller_url(project: Project) -> str:
    return shop_url("extra_storyteller", family=project.family_id)


def gift_package_url(project: Project) -> str:
    return shop_url("gift_package", project=project.id)


# =========================================================================== gift fulfilment
def create_gift_fulfilment(session: Session, order: Order, ent: Entitlement, project: Project | None,
                           shipping: dict, fmt: str) -> GiftFulfilment:
    gf = GiftFulfilment(order_id=order.id, entitlement_id=ent.id, project_id=project.id if project else None,
                        format=fmt, shipping=shipping, status="awaiting_setup")
    session.add(gf)
    session.flush()
    if project is not None:
        release_gift_fulfilments(session, project)
    return gf


def release_gift_fulfilments(session: Session, project: Project) -> int:
    """Send print files and address to operations once the storyteller has a join code (project set up)."""
    from .mailer import send_mail

    if project.storyteller is None:
        return 0
    s = get_settings()
    n = 0
    for gf in session.scalars(select(GiftFulfilment).where(GiftFulfilment.project_id == project.id,
                                                           GiftFulfilment.status == "awaiting_setup")).all():
        addr = gf.shipping or {}
        what = "Cadeaupakket (kaart, uitlegboekje, doosje)" if gf.format == "pakket" else "Cadeaukaart per post"
        signed = signer("gift-files").dumps(gf.id)
        send_mail(session, s.support_email, f"Cadeau versturen: {project.title}",
                  f"{what} voor {project.storyteller.name}.\n\nKaart: {s.base_url}/ops/cadeau/{signed}/kaart.pdf\n"
                  f"Boekje: {s.base_url}/ops/cadeau/{signed}/boekje.pdf\n(links 30 dagen geldig)\n\n"
                  f"Verzenden naar:\n{addr.get('naam', '')}\n"
                  f"{addr.get('straat', '')}\n{addr.get('postcode', '')} {addr.get('plaats', '')}\n{addr.get('land', '')}")
        gf.status = "ready"
        n += 1
    if n:
        notify_organizers(session, project, "gift_update", "Het cadeaupakket wordt gemaakt",
                          "We printen de kaart met de persoonlijke QR-code en sturen het pakket binnen twee werkdagen "
                          "op.", url=f"/p/{project.id}", email=True)
    return n


# =========================================================================== lifecycle offers
OFFER_KEYS = ("renewal", "family_share", "second_storyteller")


def dashboard_offer(session: Session, project: Project, role: str, now: datetime | None = None) -> dict | None:
    """At most one calm card, only for organisers, only when it helps. Never during the first months of telling,
    never when the storyteller stopped, never when deletion is pending."""
    if ROLE_RANK.get(role, 0) < ROLE_RANK["editor"] or project.deletion_requested_at:
        return None
    now = now or utcnow()
    dismissed = set(project.dismissed_offers or [])
    st = project.storyteller
    stopped = bool(st and st.opted_out_at)
    if project.active_until and project.activated_at and "renewal" not in dismissed and not stopped:
        days_left = (project.active_until - now).days
        if days_left <= 30:
            opts = renewal_options(project)
            return {"key": "renewal", "title": "Nog een jaar vragen?" if days_left >= 0 else
                    "Nog meer verhalen vastleggen?",
                    "body": ("Over " + (f"{days_left} dagen" if days_left != 1 else "1 dag") + " komt de laatste vraag. "
                             if days_left >= 0 else "Het verteljaar is voorbij; alles blijft gewoon van jullie. ")
                    + f"Wil {st.greeting_name if st else 'de verteller'} verder vertellen? Nog een verteljaar kost "
                    f"{opts[0]['price']}, inclusief een boektegoed.",
                    "cta": "Bekijk nog een verteljaar", "url": renewal_url(project), "extra": opts[1:]}
    version = approved_version(session, project)
    ordered = session.scalar(select(PrintOrder).where(PrintOrder.project_id == project.id,
                                                      PrintOrder.status != "cancelled")
                             .order_by(PrintOrder.created_at))
    if version and ordered and "family_share" not in dismissed and active_family_link(session, project) is None:
        return {"key": "family_share", "title": "Wil de familie ook een boek?",
                "body": "Maak een privélink waarmee broers, zussen of kleinkinderen zelf een exemplaar bestellen en "
                        "betalen. Zij zien alleen de omslag en de titel, niet jullie verhalen.",
                "cta": "Deel met familie", "url": f"/p/{project.id}/boek#familie"}
    if ordered and "second_storyteller" not in dismissed and not stopped:
        others = session.scalar(select(func.count(Project.id)).where(Project.family_id == project.family_id)) or 0
        settled = ordered.updated_at and (now - ordered.updated_at) >= timedelta(days=21) and \
            ordered.status in ("shipped", "delivered", "submitted")
        if others == 1 and settled:
            return {"key": "second_storyteller", "title": "Ook de verhalen van iemand anders bewaren?",
                    "body": f"Een tweede verteller in jullie familie kost {pricing.eur(pricing.SECOND_STORYTELLER)}: "
                            "een eigen jaar vragen en een eigen boek. Jullie familie en alles wat er al is, blijven "
                            "hetzelfde.",
                    "cta": "Voeg een verteller toe", "url": extra_storyteller_url(project)}
    return None


def dismiss_offer(project: Project, key: str) -> None:
    if key in OFFER_KEYS:
        project.dismissed_offers = sorted(set(project.dismissed_offers or []) | {key})
