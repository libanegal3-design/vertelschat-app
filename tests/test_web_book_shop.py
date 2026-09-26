"""Family web app, permanent access, QR codes, Shopify, book and archive."""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from conftest import csrf, login
from vertelschat.db import SessionLocal, utcnow
from vertelschat.models import (BookVersion, Entitlement, Export, MediaAsset, OutboxMail, Project, QRLink, Recording,
                               Story, User)


def _story_project(h):
    proj = h.project()
    h.connect(proj)
    h.post([h.voice(context=h.last_prompt_wamid(proj["pid"]))])
    h.run()
    with SessionLocal() as s:
        story = s.scalar(select(Story).where(Story.project_id == proj["pid"]))
        proj["story"] = story.id
        proj["qr"] = s.scalar(select(QRLink).where(QRLink.story_id == story.id)).token
        proj["rec"] = s.scalar(select(Recording).where(Recording.story_id == story.id)).playback_media_id
    return proj


def test_magic_link_login(client, h):
    h.project(email="sanne@test.nl")
    r = client.post("/inloggen", data={"email": "sanne@test.nl", "next": "/app"})
    assert r.status_code == 303
    with SessionLocal() as s:
        mail = s.scalar(select(OutboxMail).where(OutboxMail.to == "sanne@test.nl").order_by(OutboxMail.id.desc()))
    link = re.search(r"/inloggen/([A-Za-z0-9_-]+)", mail.text + mail.html).group(1)
    confirm = client.get(f"/inloggen/{link}")
    assert confirm.status_code == 200 and "vt_session" not in confirm.cookies  # scanners cannot log in
    done = client.post(f"/inloggen/{link}")
    assert done.status_code == 303 and "vt_session" in done.cookies
    assert client.get("/app").status_code in (200, 303)
    again = client.post(f"/inloggen/{link}")
    assert again.status_code == 400  # single use


def test_other_families_cannot_see_a_project(client, h):
    proj = h.project(email="sanne@test.nl")
    login(client, "vreemde@test.nl")
    assert client.get(f"/p/{proj['pid']}").status_code == 404
    assert client.get(f"/p/{proj['pid']}/downloads").status_code == 404


def test_csrf_is_required(client, h):
    proj = h.project(email="sanne@test.nl")
    login(client, "sanne@test.nl")
    r = client.post(f"/p/{proj['pid']}/vragen/nieuw", data={"text": "Wat was je lievelingsliedje?"})
    assert r.status_code == 400
    token = csrf(client)
    ok = client.post(f"/p/{proj['pid']}/vragen/nieuw", data={"text": "Wat was je lievelingsliedje?", "csrf_token": token})
    assert ok.status_code == 303


def test_media_links_are_signed_and_tamper_proof(client, h):
    proj = _story_project(h)
    login(client, "sanne@test.nl")
    r = client.get(f"/m/{proj['rec']}")
    assert r.status_code == 302
    url = r.headers["location"]
    good = client.get(url)
    assert good.status_code == 200 and good.content[:3] == b"ID3" or len(good.content) > 1000
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    tampered = url.replace(q["s"][0], "0" * len(q["s"][0]))
    assert client.get(tampered).status_code == 403
    other_key = url.replace(parsed.path, parsed.path[:-6] + "abcdef")
    assert client.get(other_key).status_code in (403, 404)
    anonymous = client.__class__(client.app, follow_redirects=False)
    assert anonymous.get(f"/m/{proj['rec']}").status_code == 303  # login required


def test_permanent_access_after_the_year(client, h):
    proj = _story_project(h)
    with SessionLocal() as s:
        p = s.get(Project, proj["pid"])
        p.status = "completed"
        p.active_until = utcnow() - timedelta(days=400)
        s.commit()
    login(client, "sanne@test.nl")
    dash = client.get(f"/p/{proj['pid']}")
    assert dash.status_code == 200 and "Alles blijft van jullie" in dash.text
    assert client.get(f"/p/{proj['pid']}/downloads").status_code == 200
    assert client.get(f"/m/{proj['rec']}?download=1").status_code == 302
    assert client.get(f"/v/{proj['qr']}").status_code == 200  # QR codes keep working
    token = csrf(client)
    r = client.post(f"/p/{proj['pid']}/downloads/archief", data={"csrf_token": token})
    assert r.status_code == 303
    h.run()
    with SessionLocal() as s:
        assert s.scalar(select(Export)).status == "ready"  # free export, no reactivation
    before = None
    with SessionLocal() as s:
        before = s.get(Story, proj["story"]).body
    r = client.post(f"/p/{proj['pid']}/verhalen/{proj['story']}/bewaar",
                    data={"csrf_token": token, "title": "Nieuw", "body": "Nieuwe tekst"})
    assert r.status_code == 303
    with SessionLocal() as s:
        assert s.get(Story, proj["story"]).body == before  # editing belongs to an active year


def test_qr_page_public_and_optional_pin(client, h):
    from vertelschat.security import hash_pin
    proj = _story_project(h)
    r = client.get(f"/v/{proj['qr']}")
    assert r.status_code == 200 and "Het huis waar ik opgroeide" in r.text and "<audio" in r.text
    assert client.get("/v/abcdefghjkmn").status_code == 404
    with SessionLocal() as s:
        link = s.scalar(select(QRLink))
        link.access_mode, link.pin_hash = "pin", hash_pin("2468")
        s.commit()
    assert "pincode" in client.get(f"/v/{proj['qr']}").text.lower()
    assert client.post(f"/v/{proj['qr']}/pin", data={"pin": "1111"}).status_code == 400
    ok = client.post(f"/v/{proj['qr']}/pin", data={"pin": "2468"})
    assert ok.status_code == 303
    assert "<audio" in client.get(f"/v/{proj['qr']}").text


def test_qr_host_serves_short_paths(client, h):
    proj = _story_project(h)
    r = client.get(f"/{proj['qr']}", headers={"host": "v.vertelschat.nl"})
    assert r.status_code == 200 and "Audiocode" in r.text


def test_join_short_link_redirects_to_whatsapp(client, h):
    proj = h.project()
    r = client.get(f"/doe-mee/{proj['code']}")
    assert r.status_code == 302 and r.headers["location"].startswith("https://wa.me/")
    assert proj["code"].replace("-", "%2D") in r.headers["location"] or proj["code"] in r.headers["location"]
    assert client.get("/doe-mee/VT-XXXXXX").status_code == 404


def test_shopify_hmac_and_idempotency(client, h):
    from vertelschat.security import sign_shopify_body
    order = {"id": 777, "name": "#VT777", "email": "koper@test.nl", "total_price": "129.00", "currency": "EUR",
             "customer": {"first_name": "Joost"},
             "line_items": [{"sku": "VT-VERTELJAAR", "quantity": 1,
                             "properties": [{"name": "Cadeau", "value": "Ja"}, {"name": "Voor", "value": "Riet"}]}]}
    raw = json.dumps(order).encode()
    headers = {"X-Shopify-Topic": "orders/paid", "X-Shopify-Webhook-Id": "w-1"}
    bad = client.post("/webhooks/shopify", content=raw, headers={**headers, "X-Shopify-Hmac-Sha256": "bm9wZQ=="})
    assert bad.status_code == 401
    good = client.post("/webhooks/shopify", content=raw, headers={**headers, "X-Shopify-Hmac-Sha256": sign_shopify_body(raw)})
    assert good.status_code == 200 and good.json()["result"] == "processed"
    again = client.post("/webhooks/shopify", content=raw, headers={**headers, "X-Shopify-Hmac-Sha256": sign_shopify_body(raw)})
    assert again.json()["result"] == "duplicate"
    retry = client.post("/webhooks/shopify", content=raw,
                        headers={**headers, "X-Shopify-Webhook-Id": "w-2", "X-Shopify-Hmac-Sha256": sign_shopify_body(raw)})
    assert retry.json()["result"] == "duplicate"
    with SessionLocal() as s:
        ents = s.scalars(select(Entitlement)).all()
        assert len(ents) == 1 and ents[0].is_gift and ents[0].recipient_name == "Riet"
        assert s.scalar(select(OutboxMail).where(OutboxMail.to == "koper@test.nl")) is not None


def test_book_pdf_is_print_ready(h):
    import pypdfium2 as pdfium
    from vertelschat import services as svc
    from vertelschat.storage import get_storage
    proj = _story_project(h)
    with SessionLocal() as s:
        version = svc.generate_preview(s, s.get(Project, proj["pid"]), s.get(User, proj["uid"]))
        s.commit()
        vid = version.id
    h.run()
    with SessionLocal() as s:
        v = s.get(BookVersion, vid)
        assert v.status == "ready" and v.page_count % 4 == 0 and v.page_count >= 12
        interior = s.get(MediaAsset, v.interior_media_id)
        screen = s.get(MediaAsset, v.screen_media_id)
        storage = get_storage()
        with storage.local_path(interior.storage_key) as path:
            raw = path.read_bytes()
            assert b"/TrimBox" in raw and b"/BleedBox" in raw
        with storage.local_path(screen.storage_key) as path:
            pdf = pdfium.PdfDocument(str(path))
            text = "".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
            assert "Luister naar Marijke" in text and "A01" in text and "Het huis waar ik opgroeide" in text
            pdf.close()


def test_archive_contains_everything_in_open_formats(h):
    from vertelschat.exports import request_export
    from vertelschat.storage import get_storage
    proj = _story_project(h)
    h.post([h.image(caption="Ons huis")])
    h.run()
    with SessionLocal() as s:
        request_export(s, s.get(Project, proj["pid"]), None)
        s.commit()
    h.run()
    with SessionLocal() as s:
        export = s.scalar(select(Export))
        asset = s.get(MediaAsset, export.media_id)
        with get_storage().local_path(asset.storage_key) as path:
            z = zipfile.ZipFile(io.BytesIO(path.read_bytes()))
    names = z.namelist()
    root = names[0].split("/")[0] + "/"
    for needed in ("LEESMIJ.txt", "index.html", "projectgegevens/project.json", "projectgegevens/verhalen.csv",
                   "projectgegevens/qr-codes.csv", "projectgegevens/CHECKSUMS-sha256.txt"):
        assert root + needed in names, needed
    audio = [n for n in names if "/audio/" in n]
    assert any(n.endswith(".ogg") and "/A01-" in n for n in audio) and any(n.endswith(".mp3") for n in audio)
    assert any("/transcripties/" in n for n in names) and any("/verhalen/" in n for n in names)
    assert any("/fotos/" in n for n in names)
    sums = z.read(root + "projectgegevens/CHECKSUMS-sha256.txt").decode().strip().splitlines()
    for line in sums:
        digest, name = line.split("  ", 1)
        assert hashlib.sha256(z.read(root + name)).hexdigest() == digest


def test_gift_card_pdf(client, h):
    proj = h.project(email="sanne@test.nl")
    login(client, "sanne@test.nl")
    r = client.get(f"/p/{proj['pid']}/cadeaukaart.pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


def test_custom_question_titles():
    from vertelschat.ai import title_from_question as t
    assert t("Hoe heb je papa leren kennen?") == "Hoe ik papa heb leren kennen"
    assert t("Wat was je eerste fiets?") == "Mijn eerste fiets"
    assert t("Waar ben je geboren?") == "Waar ik geboren ben"


def test_roles_contributor_suggests_editor_approves(client, h):
    from vertelschat.models import Membership, Prompt
    from vertelschat.shopify import get_or_create_user
    proj = h.project(email="sanne@test.nl")
    with SessionLocal() as s:
        emma = get_or_create_user(s, "emma@test.nl", "Emma")
        s.add(Membership(family_id=s.get(Project, proj["pid"]).family_id, user_id=emma.id, role="contributor"))
        s.commit()
    login(client, "emma@test.nl")
    token = csrf(client)
    client.post(f"/p/{proj['pid']}/vragen/nieuw", data={"text": "Wat voor opa was jouw vader?", "csrf_token": token})
    with SessionLocal() as s:
        p = s.scalar(select(Prompt).where(Prompt.text == "Wat voor opa was jouw vader?"))
        assert p.status == "suggested"
    r = client.post(f"/p/{proj['pid']}/vragen/{p.id}/goedkeuren", data={"csrf_token": token})
    assert r.status_code == 403  # contributors cannot approve
