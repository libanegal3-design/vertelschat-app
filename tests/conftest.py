"""Test fixtures. Every test gets its own database, storage folder and mock WhatsApp backend."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

PHONE = "31611112222"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("VT_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("VT_ENV", "test")
    monkeypatch.setenv("VT_COMPOSE_DELAY_SECONDS", "0")
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "+31 20 123 4567")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from vertelschat import config, db, whatsapp
    from vertelschat.security import rate_limiter
    config.get_settings.cache_clear()
    db.reset_engine()
    whatsapp.set_backend(None)
    rate_limiter.reset()
    db.init_db()
    yield
    db.reset_engine()
    config.get_settings.cache_clear()
    whatsapp.set_backend(None)


class Helpers:
    phone = PHONE

    @staticmethod
    def post(messages=None, statuses=None, phone=PHONE, name="Marijke"):
        from vertelschat.db import SessionLocal
        from vertelschat.security import sign_meta_body
        from vertelschat.whatsapp import build_webhook, ingest_webhook
        raw = json.dumps(build_webhook(messages, statuses, phone=phone, name=name)).encode()
        with SessionLocal() as s:
            counts = ingest_webhook(s, raw, sign_meta_body(raw))
            s.commit()
        return counts

    @staticmethod
    def run():
        from vertelschat import registry  # noqa: F401
        from vertelschat.jobs import drain
        return drain(advance_time=True)

    @staticmethod
    def msg(**kw):
        from vertelschat.whatsapp import new_wamid
        return {"id": new_wamid(), "timestamp": str(int(time.time())), **kw}

    def voice(self, file="01-huis-zolder.ogg", context=None, forwarded=False, path=None, corrupt_checksum=False):
        from vertelschat.config import DATA_DIR
        from vertelschat.whatsapp import register_dev_media
        media_id, sha = register_dev_media(path or (DATA_DIR / "demo_audio" / file), "audio/ogg; codecs=opus",
                                           corrupt_checksum=corrupt_checksum)
        m = self.msg(type="audio", audio={"id": media_id, "mime_type": "audio/ogg; codecs=opus", "sha256": sha,
                                          "voice": True})
        if context:
            m["context"] = {"id": context}
        if forwarded:
            m.setdefault("context", {})["forwarded"] = True
        return m

    def text(self, body, context=None):
        m = self.msg(type="text", text={"body": body})
        if context:
            m["context"] = {"id": context}
        return m

    def button(self, bid, title="Ja"):
        return self.msg(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": bid, "title": title}})

    def image(self, file="huis.jpg", caption=""):
        from vertelschat.config import DATA_DIR
        from vertelschat.whatsapp import register_dev_media
        media_id, sha = register_dev_media(DATA_DIR / "demo_photos" / file, "image/jpeg")
        return self.msg(type="image", image={"id": media_id, "mime_type": "image/jpeg", "sha256": sha, "caption": caption})

    @staticmethod
    def project(email="sanne@test.nl", name="Marijke Jansen", prompts=("t01", "j15", "w01"), locale="nl-NL"):
        from vertelschat import services as svc
        from vertelschat.db import SessionLocal
        from vertelschat.shopify import get_or_create_user
        with SessionLocal() as s:
            user = get_or_create_user(s, email, "Sanne Jansen")
            p = svc.create_project(s, user, storyteller_name=name, address_as=name.split()[0], birth_year=1951,
                                   locale=locale, purchase_kind="gift", gift_from_name="Sanne")
            for key in prompts:
                svc.add_library_prompt(s, p, user, key)
            s.commit()
            return {"pid": p.id, "st": p.storyteller.id, "code": p.storyteller.join_code, "uid": user.id}

    def connect(self, proj, phone=PHONE):
        self.post([self.text(f"Hallo Vertelschat, ik doe mee! Mijn code is {proj['code']}")], phone=phone)
        self.run()
        self.post([self.button(f"consent:yes:{proj['st']}", "Ja, ik doe mee")], phone=phone)
        self.run()

    @staticmethod
    def sent():
        from vertelschat.whatsapp import get_backend
        return get_backend().sent

    @staticmethod
    def sent_texts():
        from vertelschat.whatsapp import get_backend
        out = []
        for p in get_backend().sent:
            if p.get("type") == "text":
                out.append(p["text"]["body"])
            elif p.get("type") == "interactive":
                out.append(p["interactive"]["body"]["text"])
            elif p.get("type") == "template":
                out.append("TEMPLATE:" + p["template"]["name"])
        return out

    @staticmethod
    def last_prompt_wamid(pid):
        from sqlalchemy import select
        from vertelschat.db import SessionLocal
        from vertelschat.models import Prompt
        with SessionLocal() as s:
            p = s.scalar(select(Prompt).where(Prompt.project_id == pid, Prompt.sent_at.is_not(None))
                         .order_by(Prompt.sent_at.desc()))
            return p.wa_message_id if p else None


@pytest.fixture
def h():
    return Helpers()


@pytest.fixture
def db():
    from vertelschat.db import SessionLocal
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from vertelschat.web.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def login(client, email):
    r = client.get(f"/dev/login?email={email}")
    assert r.status_code == 303
    return client


def csrf(client) -> str:
    """Read the CSRF token of the current session from any rendered page."""
    import re
    r = client.get("/app", follow_redirects=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, "no csrf token on page"
    return m.group(1)
