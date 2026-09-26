"""Magic-link login (no passwords), logout and family invitations."""
from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..mailer import send_mail
from ..models import Family, Invitation, LoginToken, Membership, Project, User
from ..security import hash_token, new_token, rate_limiter
from ..services import accept_invitation
from .deps import db_session, end_session, form_with_csrf, get_user, redirect, render, safe_next, start_session

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


@router.get("/")
def home(request: Request, db: Session = Depends(db_session)):
    return RedirectResponse("/app" if get_user(request, db) else "/inloggen", status_code=303)


@router.get("/inloggen")
def login_form(request: Request, next: str = "/app", db: Session = Depends(db_session)):
    if get_user(request, db):
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "auth/login.html", next=safe_next(next))


@router.post("/inloggen")
async def login_submit(request: Request, db: Session = Depends(db_session)):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    next_url = safe_next(str(form.get("next", "")))
    if not EMAIL_RE.match(email):
        return render(request, "auth/login.html", status_code=400, next=next_url, email=email,
                      error="Vul een geldig e-mailadres in.")
    ip = _client_ip(request)
    if not rate_limiter.allow(f"login-ip:{ip}", 12, 900) or not rate_limiter.allow(f"login-mail:{email}", 5, 3600):
        return render(request, "auth/login.html", status_code=429, next=next_url, email=email,
                      error="Je hebt al een paar keer een link aangevraagd. Kijk even in je mail (ook bij spam) of "
                            "probeer het over een kwartier opnieuw.")
    s = get_settings()
    user = db.scalar(select(User).where(User.email == email))
    invited = db.scalar(select(Invitation.id).where(Invitation.email == email, Invitation.accepted_at.is_(None),
                                                    Invitation.revoked_at.is_(None), Invitation.expires_at > utcnow()))
    if user or invited or s.dev_tools:
        token = new_token()
        db.add(LoginToken(email=email, token_hash=hash_token(token), next_url=next_url,
                          expires_at=utcnow() + timedelta(minutes=30)))
        send_mail(db, email, "Je inloglink voor Vertelschat",
                  "Klik op de knop om in te loggen. De link werkt 30 minuten en één keer.\n\nHeb je dit niet zelf "
                  "aangevraagd? Dan kun je deze mail negeren.",
                  cta_url=f"{s.base_url}/inloggen/{token}", cta_label="Inloggen bij Vertelschat")
    else:
        send_mail(db, email, "Inloggen bij Vertelschat",
                  "Iemand vroeg een inloglink aan voor dit e-mailadres, maar we kennen het nog niet. Misschien heb je "
                  "besteld of bent je uitgenodigd met een ander adres?\n\nWe helpen je graag: antwoord gewoon op deze "
                  f"mail of schrijf naar {s.support_email}.")
    db.commit()
    return redirect(f"/inloggen/verstuurd?e={quote(email)}")


@router.get("/inloggen/verstuurd")
def login_sent(request: Request, e: str = ""):
    return render(request, "auth/check_email.html", email=e)


@router.get("/inloggen/{token}")
def login_confirm(request: Request, token: str, db: Session = Depends(db_session)):
    """Shows a confirm button instead of logging in on GET: mail scanners that open links cannot use them up."""
    lt = db.scalar(select(LoginToken).where(LoginToken.token_hash == hash_token(token)))
    if lt is None or lt.used_at or lt.expires_at < utcnow():
        return render(request, "auth/expired.html", status_code=400)
    return render(request, "auth/confirm.html", token=token, email=lt.email)


@router.post("/inloggen/{token}")
def login_consume(request: Request, token: str, db: Session = Depends(db_session)):
    lt = db.scalar(select(LoginToken).where(LoginToken.token_hash == hash_token(token)))
    if lt is None or lt.used_at or lt.expires_at < utcnow():
        return render(request, "auth/expired.html", status_code=400)
    lt.used_at = utcnow()
    user = db.scalar(select(User).where(User.email == lt.email))
    if user is None:
        user = User(email=lt.email)
        db.add(user)
        db.flush()
    user.last_login_at = utcnow()
    resp = RedirectResponse(safe_next(lt.next_url), status_code=303)
    start_session(db, resp, user)
    db.commit()
    return resp


@router.post("/uitloggen")
async def logout(request: Request, db: Session = Depends(db_session)):
    get_user(request, db)
    await form_with_csrf(request)
    resp = redirect("/inloggen", "Je bent uitgelogd.")
    end_session(request, db, resp)
    db.commit()
    return resp


@router.get("/uitnodiging/{token}")
def invitation(request: Request, token: str, db: Session = Depends(db_session)):
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    if inv is None or inv.revoked_at or inv.expires_at < utcnow():
        return render(request, "auth/expired.html", status_code=400, invitation=True)
    project = db.scalar(select(Project).where(Project.family_id == inv.family_id).order_by(Project.created_at))
    user = get_user(request, db)
    if user is not None:
        accept_invitation(db, token, user)
        db.commit()
        return redirect(f"/p/{project.id}" if project else "/app", "Welkom! Je hoort nu bij de familie.")
    inviter = db.get(User, inv.invited_by_id) if inv.invited_by_id else None
    return render(request, "auth/invite.html", invitation=inv, project=project, inviter=inviter,
                  next=f"/uitnodiging/{token}")
