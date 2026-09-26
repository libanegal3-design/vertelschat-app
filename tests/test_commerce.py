"""Pricing V2: credits, paid-before-print book orders, family buy links, two storytellers, renewals, gift packages,
lifecycle offers and grandfathering of everything sold before."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from sqlalchemy import select

from conftest import login
from vertelschat import commerce, pricing
from vertelschat import services as svc
from vertelschat.db import SessionLocal, utcnow
from vertelschat.models import (Book, BookVersion, CreditEntry, Entitlement, FamilyLink, GiftFulfilment, Job, Membership,
                               Notification, OutboxMail, PrintOrder, Project, Story, User)

SHIP = {"first_name": "Joost", "last_name": "Jansen", "address1": "Kerkstraat 1", "zip": "3511 AB", "city": "Utrecht",
        "country_code": "NL"}


def shop(lines, email="sanne@test.nl", shipping=None) -> str:
    from vertelschat.security import sign_shopify_body
    from vertelschat.shopify import ingest_webhook
    oid = uuid.uuid4().int % 10**12
    data = {"id": oid, "name": f"#VT{oid % 10000}", "email": email, "total_price": "0.00", "currency": "EUR",
            "customer": {"first_name": "Sanne", "last_name": "Jansen"}, "shipping_address": shipping or SHIP,
            "line_items": [{"sku": sku, "quantity": qty,
                            "properties": [{"name": k, "value": v} for k, v in (props or {}).items()]}
                           for sku, qty, props in lines]}
    raw = json.dumps(data).encode()
    with SessionLocal() as s:
        result = ingest_webhook(s, raw, sign_shopify_body(raw), "orders/paid", f"w-{oid}")
        s.commit()
    return result


def claim(email="sanne@test.nl", name="Marijke Jansen") -> str:
    with SessionLocal() as s:
        user = s.scalar(select(User).where(User.email == email))
        ent = commerce.available_storyteller_entitlements(s, user)[0]
        p = svc.create_project(s, user, storyteller_name=name, entitlement=ent,
                               family=commerce.family_for_claim(s, ent, user))
        s.commit()
        return p.id


def approve(pid: str, pages: int = 120) -> str:
    with SessionLocal() as s:
        book = s.scalar(select(Book).where(Book.project_id == pid))
        v = BookVersion(book_id=book.id, version=1, status="ready", page_count=pages, approved_at=utcnow(),
                        preview_media_ids=[])
        s.add(v)
        s.commit()
        return v.id


def order(pid, vid, qty):
    with SessionLocal() as s:
        p, v = s.get(Project, pid), s.get(BookVersion, vid)
        user = s.get(User, p.created_by_id)
        po, q = commerce.place_order(s, p, v, user, {"naam": "Sanne", "straat": "Laan 2", "postcode": "1000 AA",
                                                    "plaats": "Amsterdam", "land": "Nederland"}, qty)
        s.commit()
        return po.id, q


def jobs(kind="submit_print"):
    with SessionLocal() as s:
        return s.scalars(select(Job).where(Job.kind == kind)).all()


def test_parcel_prices_follow_real_costs():
    assert pricing.parcel_total(paid_copies=1, tier=0) == 4500              # a copy on its own
    assert pricing.parcel_total(paid_copies=3, tier=0) == 4500 + 2 * 3900   # one parcel, one shipment
    assert pricing.parcel_total(paid_copies=2, tier=0, credit_copies=1) == 2 * 3900
    assert pricing.parcel_total(paid_copies=1, tier=1, credit_copies=1) == 1500 + 3900 + 1500
    assert pricing.page_tier(240) == 0 and pricing.page_tier(241) == 1 and pricing.page_tier(400) == 2
    assert pricing.page_tier(401) is None
    for sku, (kind, attrs) in pricing.SKUS.items():  # every SKU maps to a known handler
        assert kind in ("storyteller", "renewal", "gift_package", "book_credit", "page_upgrade", "copy", "family_copy")


def test_included_book_is_ordered_without_payment():
    assert shop([("VT-VERTELJAAR", 1, {})]) == "processed"
    pid = claim()
    vid = approve(pid)
    with SessionLocal() as s:
        assert commerce.balance(s, s.get(Project, pid)) == 1
    po_id, q = order(pid, vid, 1)
    assert q.to_pay == 0
    with SessionLocal() as s:
        po = s.get(PrintOrder, po_id)
        assert po.payment_status == "prepaid" and po.credit_copies == 1
        assert commerce.balance(s, s.get(Project, pid)) == 0
    assert len(jobs()) == 1


def test_extra_copies_are_paid_before_printing():
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    vid = approve(pid)
    po_id, q = order(pid, vid, 3)
    assert (q.credit_copies, q.paid_first, q.paid_next, q.to_pay) == (1, 0, 2, 7800)
    with SessionLocal() as s:
        po = s.get(PrintOrder, po_id)
        assert po.payment_status == "awaiting_payment"
        url = commerce.bridge_url(po, "De verhalen van Marijke")
        assert f"po={po_id}" in url and "next=2" in url and "first=0" in url
    assert jobs() == []  # nothing goes to the printer yet
    shop([("VT-BOEK-N", 1, {"_print_order": po_id})])
    with SessionLocal() as s:
        assert s.get(PrintOrder, po_id).payment_status == "partly_paid"
    assert jobs() == []
    shop([("VT-BOEK-N", 1, {"_print_order": po_id})])
    with SessionLocal() as s:
        po = s.get(PrintOrder, po_id)
        assert po.payment_status == "paid" and po.paid_copies == 2 and po.quantity == 3
    assert len(jobs()) == 1


def test_reorder_years_later_and_cancel_returns_credit():
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    vid = approve(pid)
    order(pid, vid, 1)  # included book used
    _, q = order(pid, vid, 2)
    assert (q.credit_copies, q.paid_first, q.paid_next, q.to_pay) == (0, 1, 1, 8400)
    with SessionLocal() as s:  # a fresh order reserves the credit; cancelling gives it back
        s.add(CreditEntry(project_id=pid, kind="book", delta=1, reason="purchase"))
        s.commit()
    po_id, q = order(pid, vid, 2)
    assert q.credit_copies == 1
    with SessionLocal() as s:
        p = s.get(Project, pid)
        assert commerce.balance(s, p) == 0
        commerce.abandon_order(s, s.get(PrintOrder, po_id), s.get(User, p.created_by_id))
        s.commit()
        assert commerce.balance(s, p) == 1 and s.get(PrintOrder, po_id).status == "cancelled"


def test_thicker_book_is_announced_and_paid_per_copy():
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    vid = approve(pid, pages=288)
    po_id, q = order(pid, vid, 1)
    assert q.tier == 1 and q.upgrades == 1 and q.to_pay == 1500
    shop([("VT-OMVANG-320", 1, {"_print_order": po_id})])
    with SessionLocal() as s:
        assert s.get(PrintOrder, po_id).payment_status == "paid"
    # paying copies of the included size for a thicker book is caught, never silently printed
    shop([("VT-VERTELJAAR", 1, {})], email="an@test.be")
    pid2 = claim("an@test.be", "Riet Peeters")
    vid2 = approve(pid2, pages=300)
    order(pid2, vid2, 1)
    with SessionLocal() as s:  # buy an extra credit so the next order pays only copies
        s.add(CreditEntry(project_id=pid2, kind="pages_1", delta=5, reason="purchase"))
        s.commit()
    po2, q2 = order(pid2, vid2, 2)
    shop([("VT-BOEK-1", 2, {"_print_order": po2})], email="an@test.be")
    with SessionLocal() as s:
        assert s.get(PrintOrder, po2).payment_status == "partly_paid"
        assert s.scalar(select(Notification).where(Notification.project_id == pid2,
                                                   Notification.kind == "print_update")) is not None


def test_family_link_shows_only_the_cover_and_sells_copies(client):
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    with SessionLocal() as s:
        s.add(Story(project_id=pid, title="Het geheime verhaal", body="Dit is privé, alleen voor de familie.",
                    status="ready"))
        s.commit()
    vid = approve(pid)
    with SessionLocal() as s:
        p = s.get(Project, pid)
        link = commerce.create_family_link(s, p, s.get(User, p.created_by_id))
        s.commit()
        token = link.token
    page = client.get(f"/boek/{token}")
    assert page.status_code == 200
    assert "De verhalen van Marijke" in page.text and "€45" in page.text and "€39" in page.text
    for private in ("Het geheime verhaal", "alleen voor de familie", "sanne@test.nl", "Jansen"):
        assert private not in page.text
    go = client.get(f"/boek/{token}/bestellen?aantal=3")
    assert go.status_code == 302 and "book=" in go.headers["location"] and "next=2" in go.headers["location"]
    shop([("VT-FAMILIE-1", 1, {"_book": token}), ("VT-FAMILIE-N", 2, {"_book": token})], email="joost@test.nl")
    with SessionLocal() as s:
        po = s.scalar(select(PrintOrder).where(PrintOrder.source == "family_link"))
        assert po.quantity == 3 and po.payment_status == "paid" and po.shipping["plaats"] == "Utrecht"
        assert s.scalar(select(FamilyLink).where(FamilyLink.token == token)).copies_ordered == 3
        assert s.scalar(select(Notification).where(Notification.kind == "family_order")) is not None
        # the buyer did not become a member of the family
        joost = s.scalar(select(User).where(User.email == "joost@test.nl"))
        assert s.scalar(select(Membership).where(Membership.user_id == joost.id)) is None
        p = s.get(Project, pid)
        commerce.revoke_family_link(s, p, s.get(User, p.created_by_id))
        s.commit()
    assert client.get(f"/boek/{token}").status_code == 404
    shop([("VT-FAMILIE-1", 1, {"_book": token})], email="late@test.nl")
    with SessionLocal() as s:
        assert s.scalar(select(OutboxMail).where(OutboxMail.to == "late@test.nl")) is not None
        assert len(s.scalars(select(PrintOrder).where(PrintOrder.source == "family_link")).all()) == 1


def test_two_storytellers_bought_together_share_one_family():
    shop([("VT-VERTELJAAR-DUO", 1, {"Cadeau": "Ja", "Voor": "Marijke en Henk"})])
    with SessionLocal() as s:
        ents = s.scalars(select(Entitlement).where(Entitlement.kind == "verteljaar")).all()
        assert len(ents) == 2 and len({e.group_id for e in ents}) == 1
        assert sorted(e.tier for e in ents) == ["family", "standard"]
    a = claim(name="Marijke Jansen")
    b = claim(name="Henk Jansen")
    with SessionLocal() as s:
        pa, pb = s.get(Project, a), s.get(Project, b)
        assert pa.family_id == pb.family_id
        assert commerce.balance(s, pa) == 1 and commerce.balance(s, pb) == 1  # each their own book


def test_extra_storyteller_joins_the_existing_family(client):
    shop([("VT-VERTELJAAR", 1, {})])
    first = claim()
    with SessionLocal() as s:
        fam = s.get(Project, first).family_id
    shop([("VT-EXTRA-VERTELLER", 1, {"_family": fam})])
    second = claim(name="Henk Jansen")
    with SessionLocal() as s:
        assert s.get(Project, second).family_id == fam
    # someone outside that family buying with the same property still gets their own family
    shop([("VT-EXTRA-VERTELLER", 1, {"_family": fam})], email="vreemd@test.nl")
    third = claim("vreemd@test.nl", "Truus")
    with SessionLocal() as s:
        assert s.get(Project, third).family_id != fam
    login(client, "sanne@test.nl")
    page = client.get("/app")
    assert "Voeg een verteller toe" in page.text and "extra-verteller?family=" in page.text


def test_renewal_includes_a_book_credit_and_v1_keeps_its_price():
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    with SessionLocal() as s:
        p = s.get(Project, pid)
        p.status, p.activated_at, p.active_until = "active", utcnow() - timedelta(days=340), utcnow() + timedelta(days=25)
        s.commit()
        before = p.active_until
    shop([("VT-VERLENGING-BOEK", 1, {"_project": pid})])
    with SessionLocal() as s:
        p = s.get(Project, pid)
        assert (p.active_until - before).days == 365 and commerce.balance(s, p) == 2
        assert [o["sku"] for o in commerce.renewal_options(p)] == ["VT-VERLENGING-BOEK"]
        p.terms_version = "v1"  # bought before pricing V2
        s.commit()
        assert [o["sku"] for o in commerce.renewal_options(p)] == ["VT-VERLENGING-BOEK", "VT-VERLENGING"]
        assert "tarief=2026" in commerce.renewal_url(p, legacy=True)
    shop([("VT-VERLENGING", 1, {"_project": pid})])
    with SessionLocal() as s:
        assert commerce.balance(s, s.get(Project, pid)) == 2  # legacy renewal: no book credit, as sold


def test_gift_package_is_printed_once_the_project_exists(client):
    shop([("VT-VERTELJAAR", 1, {"Cadeau": "Ja", "Voor": "Marijke"}), ("VT-CADEAUPAKKET", 1, {})])
    with SessionLocal() as s:
        gf = s.scalar(select(GiftFulfilment))
        assert gf.status == "awaiting_setup" and gf.project_id is None and gf.shipping["plaats"] == "Utrecht"
    pid = claim()
    with SessionLocal() as s:
        gf = s.scalar(select(GiftFulfilment))
        assert gf.project_id == pid and gf.status == "ready"
        mail = s.scalar(select(OutboxMail).where(OutboxMail.subject.like("Cadeau versturen%")))
    link = next(w for w in mail.text.split() if "/ops/cadeau/" in w and w.endswith("boekje.pdf"))
    pdf = client.get(link.replace("http://localhost:8000", ""))
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert client.get("/ops/cadeau/nep/kaart.pdf").status_code == 404


def test_legacy_skus_keep_working():
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    shop([("VT-EXTRA-BOEK", 2, {"_project": pid}), ("VT-CADEAUKAART", 1, {"_project": pid})])
    with SessionLocal() as s:
        assert commerce.balance(s, s.get(Project, pid)) == 3
        assert s.scalar(select(GiftFulfilment)).format == "kaart"


def test_old_projects_are_grandfathered_and_backfilled(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from vertelschat.db import migrate_columns
    engine = create_engine(f"sqlite:///{tmp_path / 'oud.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, title VARCHAR(200))"))
        c.execute(text("INSERT INTO projects VALUES ('p1', 'Oud project')"))
        c.execute(text("CREATE TABLE print_orders (id VARCHAR(32) PRIMARY KEY, quantity INTEGER)"))
    added = migrate_columns(engine)
    assert "projects.terms_version" in added and "print_orders.payment_status" in added
    with engine.begin() as c:
        assert c.execute(text("SELECT terms_version FROM projects")).scalar() == "v1"
    assert "credit_copies" in {col["name"] for col in inspect(engine).get_columns("print_orders")}
    # v1 balance is reconstructed from v1 entitlements and orders
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    vid = approve(pid)
    with SessionLocal() as s:
        s.get(Project, pid).terms_version = "v1"  # as if it was bought before pricing V2
        for e in s.scalars(select(CreditEntry).where(CreditEntry.project_id == pid)).all():
            s.delete(e)
        s.add(Entitlement(project_id=pid, kind="extra_book", quantity=2, status="available"))
        s.add(PrintOrder(project_id=pid, book_version_id=vid, quantity=1, status="submitted"))
        s.commit()
        assert commerce.balance(s, s.get(Project, pid)) == 1 + 2 - 1


def test_offers_appear_only_at_the_right_moment():
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    with SessionLocal() as s:
        p = s.get(Project, pid)
        p.status, p.activated_at, p.active_until = "active", utcnow() - timedelta(days=40), utcnow() + timedelta(days=325)
        s.commit()
        assert commerce.dashboard_offer(s, p, "owner") is None  # telling has just begun: nothing to sell
        p.active_until = utcnow() + timedelta(days=20)
        assert commerce.dashboard_offer(s, p, "owner")["key"] == "renewal"
        assert commerce.dashboard_offer(s, p, "viewer") is None  # only organisers
        commerce.dismiss_offer(p, "renewal")
        s.commit()
        assert commerce.dashboard_offer(s, p, "owner") is None
    vid = approve(pid)
    po_id, _ = order(pid, vid, 1)
    with SessionLocal() as s:
        p = s.get(Project, pid)
        assert commerce.dashboard_offer(s, p, "owner")["key"] == "family_share"
        commerce.create_family_link(s, p, s.get(User, p.created_by_id))
        po = s.get(PrintOrder, po_id)
        po.status, po.updated_at = "shipped", utcnow() - timedelta(days=5)
        s.commit()
        assert commerce.dashboard_offer(s, p, "owner") is None  # the book has only just arrived
        po.updated_at = utcnow() - timedelta(days=30)
        s.commit()
        offer = commerce.dashboard_offer(s, p, "owner")
        assert offer["key"] == "second_storyteller" and "€99" in offer["body"]


def test_unpaid_orders_are_never_printed():
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    vid = approve(pid)
    po_id, _ = order(pid, vid, 2)
    with SessionLocal() as s:
        svc.submit_print(s, {"print_order_id": po_id})
        s.commit()
        assert s.get(PrintOrder, po_id).status == "awaiting_submission"
        assert s.scalar(select(OutboxMail).where(OutboxMail.subject.like("Drukorder%"))) is None


def test_app_pages_render_and_order_goes_to_checkout(client):
    from conftest import csrf
    shop([("VT-VERTELJAAR", 1, {})])
    pid = claim()
    vid = approve(pid, pages=260)
    with SessionLocal() as s:
        p = s.get(Project, pid)
        p.status, p.activated_at, p.active_until = "active", utcnow() - timedelta(days=350), utcnow() + timedelta(days=15)
        s.commit()
    login(client, "sanne@test.nl")
    for path in (f"/p/{pid}", f"/p/{pid}/boek", f"/p/{pid}/instellingen", f"/p/{pid}/instellen/verbinden", "/app"):
        r = client.get(path, follow_redirects=True)
        assert r.status_code == 200, path
    dash = client.get(f"/p/{pid}").text
    assert "Nog een jaar vragen?" in dash and "nog-een-verteljaar?project=" in dash
    book = client.get(f"/p/{pid}/boek").text
    assert "260 pagina's" in book and "Hoeveel exemplaren wil je laten maken?" in book and "€15" in book
    token = csrf(client)
    r = client.post(f"/p/{pid}/boek/bestellen", data={"csrf_token": token, "version_id": vid, "quantity": "3",
                                                     "naam": "Sanne", "straat": "Laan 2", "postcode": "1000 AA",
                                                     "plaats": "Amsterdam", "land": "Nederland"})
    assert r.status_code == 303 and "/pages/boek-afrekenen?po=" in r.headers["location"]
    assert "next=2" in r.headers["location"] and "upgrade=1" in r.headers["location"]
    assert "Er staat nog een bestelling klaar om te betalen" in client.get(f"/p/{pid}/boek").text
    r = client.post(f"/p/{pid}/boek/familielink", data={"csrf_token": token})
    assert r.status_code == 303
    assert "/boek/" in client.get(f"/p/{pid}/boek").text
    r = client.post(f"/p/{pid}/aanbod/renewal/verbergen", data={"csrf_token": token})
    assert r.status_code == 303 and "Nog een jaar vragen?" not in client.get(f"/p/{pid}").text


def test_hosted_database_urls_are_normalised():
    from vertelschat.db import normalize_database_url
    assert normalize_database_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
