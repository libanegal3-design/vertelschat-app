"""Request helpers: sessions, CSRF, access control, rendering, Dutch formatting filters."""
from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audio import format_duration, spoken_duration
from ..config import get_settings
from ..db import SessionLocal, utcnow
from ..models import ROLE_RANK, Membership, Project, User, WebSession
from ..scheduler import WEEKDAYS, to_local
from ..security import hash_token, mask_phone, new_token, signer, unsign

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
SESSION_COOKIE = "vt_session"
FLASH_COOKIE = "vt_flash"
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
          "november", "december"]
ROLE_LABELS = {"owner": "Eigenaar", "editor": "Redacteur", "contributor": "Meeverteller", "viewer": "Lezer"}
ROLE_HELP = {"owner": "Beheert alles, ook betalingen en de verteller.",
             "editor": "Bewerkt verhalen, vragen en het boek.",
             "contributor": "Luistert mee, stelt vragen voor en voegt foto's toe.",
             "viewer": "Luistert en leest mee."}


class LoginRequired(Exception):
    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


class NotFoundError(Exception):
    pass


class Forbidden(Exception):
    pass


class CSRFError(Exception):
    pass


def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------- formatting
def nl_date(dt: datetime | None, year: bool = True, tz: str = "Europe/Amsterdam") -> str:
    if not dt:
        return ""
    local = to_local(dt, tz)
    return f"{local.day} {MONTHS[local.month - 1]}" + (f" {local.year}" if year else "")


def nl_datetime(dt: datetime | None, tz: str = "Europe/Amsterdam") -> str:
    if not dt:
        return ""
    local = to_local(dt, tz)
    return f"{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]}, {local:%H:%M}"


def relative(dt: datetime | None) -> str:
    if not dt:
        return ""
    now = utcnow()
    days = (to_local(now).date() - to_local(dt).date()).days
    if days <= 0:
        mins = int((now - dt).total_seconds() // 60)
        if mins < 2:
            return "zojuist"
        if mins < 60:
            return f"{mins} minuten geleden"
        return f"vandaag, {to_local(dt):%H:%M}"
    if days == 1:
        return "gisteren"
    if days < 7:
        return f"{days} dagen geleden"
    return nl_date(dt, year=to_local(dt).year != to_local(now).year)


def excerpt(text: str, n: int = 190) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "\u2026"


def paras(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


env = templates.env
env.filters.update(nl_date=nl_date, nl_datetime=nl_datetime, relative=relative, excerpt=excerpt, paras=paras,
                   duration=format_duration, spoken=spoken_duration, mask_phone=mask_phone,
                   role_label=lambda r: ROLE_LABELS.get(r, r))
env.globals.update(plural=plural, ROLE_LABELS=ROLE_LABELS, ROLE_HELP=ROLE_HELP, WEEKDAYS=WEEKDAYS,
                   ROLE_RANK=ROLE_RANK)


# --------------------------------------------------------------------------- sessions & auth
def get_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    ws = db.get(WebSession, hash_token(token))
    if ws is None or ws.expires_at < utcnow():
        return None
    request.state.web_session = ws
    if not ws.last_seen_at or utcnow() - ws.last_seen_at > timedelta(minutes=10):
        ws.last_seen_at = utcnow()
        db.commit()
    user = db.get(User, ws.user_id)
    request.state.user = user
    return user


def require_user(request: Request, db: Session) -> User:
    user = get_user(request, db)
    if user is None:
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequired(nxt)
    return user


def start_session(db: Session, response, user: User) -> None:
    s = get_settings()
    token = new_token()
    db.add(WebSession(id=hash_token(token), user_id=user.id, csrf_token=new_token(24),
                      expires_at=utcnow() + timedelta(days=s.session_days)))
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=s.base_url.startswith("https"),
                        samesite="lax", max_age=s.session_days * 86400, path="/")


def end_session(request: Request, db: Session, response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        ws = db.get(WebSession, hash_token(token))
        if ws is not None:
            db.delete(ws)
    response.delete_cookie(SESSION_COOKIE, path="/")


async def form_with_csrf(request: Request):
    form = await request.form()
    ws = getattr(request.state, "web_session", None)
    token = form.get("csrf_token") or request.headers.get("x-csrf-token") or ""
    if ws is None or not token or not hmac.compare_digest(str(token), ws.csrf_token):
        raise CSRFError()
    return form


def safe_next(url: str | None, default: str = "/app") -> str:
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return default
    return url


def project_for_user(db: Session, user: User, project_id: str, min_role: str = "viewer") -> tuple[Project, str]:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError()
    m = db.scalar(select(Membership).where(Membership.family_id == project.family_id, Membership.user_id == user.id))
    if m is None:
        raise NotFoundError()  # never reveal that a project exists
    if ROLE_RANK.get(m.role, 0) < ROLE_RANK[min_role]:
        raise Forbidden()
    return project, m.role


# --------------------------------------------------------------------------- responses
def redirect(url: str, message: str | None = None, kind: str = "ok") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if message:
        resp.set_cookie(FLASH_COOKIE, signer("flash").dumps({"m": message, "k": kind}), max_age=120, httponly=True,
                        samesite="lax", path="/")
    return resp


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    raw = request.cookies.get(FLASH_COOKIE)
    flash = unsign("flash", raw, 300) if raw else None
    ws = getattr(request.state, "web_session", None)
    context = {"user": getattr(request.state, "user", None), "csrf_token": ws.csrf_token if ws else "",
               "flash": flash, "settings": get_settings(), "now": utcnow()}
    context.update(ctx)
    resp = templates.TemplateResponse(request, name, context, status_code=status_code)
    if raw:
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp
