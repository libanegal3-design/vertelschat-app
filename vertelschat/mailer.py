"""E-mail: 'outbox' backend captures mail in the database (visible at /dev/mail); 'smtp' sends via a
transactional provider (Postmark/Mailgun EU/Brevo, all speak SMTP). Mail is queued as a job so an SMTP
outage never breaks a request."""
from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .jobs import PermanentFailure, RetryLater, enqueue, job
from .models import OutboxMail


def _html(text: str, cta_url: str = "", cta_label: str = "") -> str:
    paras = "".join(f"<p style=\"margin:0 0 14px\">{html.escape(p).replace(chr(10), '<br>')}</p>"
                    for p in text.strip().split("\n\n") if p.strip())
    button = ""
    if cta_url:
        button = (f"<p style=\"margin:22px 0\"><a href=\"{html.escape(cta_url)}\" style=\"background:#17324D;"
                  f"color:#ffffff;padding:12px 20px;border-radius:8px;text-decoration:none;display:inline-block\">"
                  f"{html.escape(cta_label)}</a></p>")
    return ("<!doctype html><html lang=\"nl\"><body style=\"margin:0;background:#E9EEF2\">"
            "<div style=\"max-width:560px;margin:0 auto;padding:28px 22px;font-family:Georgia,serif;"
            "font-size:17px;line-height:1.55;color:#1A2230;background:#ffffff\">"
            "<p style=\"font-size:20px;margin:0 0 20px;color:#17324D\">vertelschat</p>"
            f"{paras}{button}<p style=\"font-size:13px;color:#5B6574;margin-top:28px\">"
            "Vertelschat, verhalen in hun eigen stem. Je ontvangt deze mail omdat je lid bent van een Vertelschat-familie."
            "</p></div></body></html>")


def send_mail(session: Session, to: str, subject: str, text: str, *, cta_url: str = "", cta_label: str = "") -> OutboxMail:
    body = text.strip()
    if cta_url:
        body += f"\n\n{cta_label}: {cta_url}"
    body += "\n\n-- \nVertelschat"
    mail = OutboxMail(to=to, subject=subject, text=body, html=_html(text, cta_url, cta_label))
    session.add(mail)
    session.flush()
    if get_settings().mail_backend == "outbox":
        mail.status = "captured"
        mail.sent_at = utcnow()
    else:
        enqueue(session, "send_mail", {"mail_id": mail.id}, dedupe_key=f"mail:{mail.id}")
    return mail


@job("send_mail", max_attempts=10)
def _send_mail_job(session: Session, payload: dict) -> None:
    mail = session.get(OutboxMail, payload["mail_id"])
    if mail is None or mail.status == "sent":
        return
    s = get_settings()
    if not s.smtp_host:
        raise PermanentFailure("SMTP_HOST ontbreekt")
    msg = EmailMessage()
    msg["From"] = s.mail_from
    msg["To"] = mail.to
    msg["Subject"] = mail.subject
    msg["Message-ID"] = make_msgid(domain="vertelschat.nl")
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as exc:
        raise RetryLater(str(exc)) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        mail.status = "failed"
        mail.error = str(exc)[:500]
        return
    mail.status = "sent"
    mail.sent_at = utcnow()
