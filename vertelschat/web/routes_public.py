"""Public and semi-public routes: QR listening pages, media delivery, health."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..analytics import track
from ..db import SessionLocal, utcnow
from .. import commerce, pricing
from ..models import Book, GiftFulfilment, MediaAsset, Project, QRLink, Recording, Story
from ..security import rate_limiter, signer, unsign, verify_pin
from ..storage import LocalStorage, asset_url, content_disposition, ext_for_mime, get_storage
from .deps import NotFoundError, db_session, project_for_user, render, require_user
from .routes_auth import _client_ip

router = APIRouter()


@router.get("/healthz")
def healthz():
    with SessionLocal() as db:
        db.execute(text("select 1"))
    return JSONResponse({"ok": True})


@router.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def _filename(db: Session, asset: MediaAsset) -> str:
    if asset.original_filename:
        return asset.original_filename
    rec = db.scalar(select(Recording).where((Recording.original_media_id == asset.id) |
                                            (Recording.playback_media_id == asset.id)))
    if rec is not None:
        story = db.get(Story, rec.story_id) if rec.story_id else None
        qr = db.scalar(select(QRLink).where(QRLink.story_id == story.id)) if story else None
        from ..exports import slug
        base = f"{qr.audio_code + '-' if qr else ''}{slug(story.title if story else 'opname')}"
        return f"{base}-{rec.received_at:%Y%m%d}{ext_for_mime(asset.mime_type)}"
    return f"{asset.kind}{ext_for_mime(asset.mime_type)}"


# --------------------------------------------------------------------------- family buy link (public, minimal)
@router.get("/boek/{token}")
def family_book(request: Request, token: str, db: Session = Depends(db_session)):
    """What a relative sees: title, cover, storyteller's first name, page count and price. Nothing else: no stories,
    no names of family members, no settings. The link can be revoked by the organiser at any time."""
    resolved = commerce.resolve_family_link(db, token)
    if resolved is None:
        return render(request, "public/gone.html", status_code=404, reason="family")
    link, project, version = resolved
    book = db.scalar(select(Book).where(Book.project_id == project.id))
    tier = pricing.page_tier(version.page_count) or 0
    st = project.storyteller
    return render(request, "public/family_book.html", token=token, title=book.title if book else project.title,
                  subtitle=book.subtitle if book else "", first_name=(st.greeting_name if st else ""),
                  pages=version.page_count, price_first=pricing.eur(pricing.copy_price("first", tier)),
                  price_next=pricing.eur(pricing.copy_price("next", tier)), tier=tier,
                  has_cover=bool(version.preview_media_ids))


@router.get("/boek/{token}/omslag")
def family_book_cover(token: str, db: Session = Depends(db_session)):
    resolved = commerce.resolve_family_link(db, token)
    if resolved is None or not resolved[2].preview_media_ids:
        raise NotFoundError()
    asset = db.get(MediaAsset, resolved[2].preview_media_ids[0])  # the cover only
    if asset is None:
        raise NotFoundError()
    return RedirectResponse(asset_url(asset, ttl=600), status_code=302)


@router.get("/boek/{token}/bestellen")
def family_book_order(request: Request, token: str, db: Session = Depends(db_session)):
    resolved = commerce.resolve_family_link(db, token)
    if resolved is None:
        return render(request, "public/gone.html", status_code=404, reason="family")
    link, project, version = resolved
    book = db.scalar(select(Book).where(Book.project_id == project.id))
    raw = request.query_params.get("aantal", "1")
    copies = int(raw) if raw.isdigit() else 1
    track(db, "product_viewed", project.id, source="family_link")
    db.commit()
    return RedirectResponse(commerce.family_bridge_url(link, version, book.title if book else project.title, copies),
                            status_code=302)


# --------------------------------------------------------------------------- operations: gift print files
@router.get("/ops/cadeau/{signed}/{what}.pdf")
def gift_files(signed: str, what: str, db: Session = Depends(db_session)):
    """Signed links (30 days) in the operations e-mail: the personal gift card and the storyteller booklet."""
    from fastapi.responses import Response

    from ..giftcard import render_gift_card, render_storyteller_booklet

    gid = unsign("gift-files", signed, max_age=30 * 24 * 3600)
    gf = db.get(GiftFulfilment, gid) if gid else None
    project = db.get(Project, gf.project_id) if gf and gf.project_id else None
    if project is None or what not in ("kaart", "boekje"):
        raise NotFoundError()
    pdf = render_gift_card(project, project.gift_from_name) if what == "kaart" else render_storyteller_booklet(project)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{what}-{project.storyteller.join_code}.pdf"'})


@router.get("/doe-mee/{code}")
def join_redirect(request: Request, code: str, db: Session = Depends(db_session)):
    """Short invitation link: opens WhatsApp with the personal join message ready to send."""
    from ..models import Storyteller
    from ..qr import wa_join_link

    code = code.strip().upper()
    if not code.startswith("VT-"):
        code = "VT-" + code.removeprefix("VT")
    st = db.scalar(select(Storyteller).where(Storyteller.join_code == code))
    if st is None:
        return render(request, "public/gone.html", status_code=404, reason="invite")
    return RedirectResponse(wa_join_link(st.join_code), status_code=302)


@router.get("/m/{asset_id}")
def media(request: Request, asset_id: str, download: int = 0, db: Session = Depends(db_session)):
    user = require_user(request, db)
    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.status != "stored" or not asset.project_id:
        raise NotFoundError()
    project_for_user(db, user, asset.project_id)
    url = asset_url(asset, filename=_filename(db, asset), disposition="attachment" if download else "inline", ttl=3600)
    resp = RedirectResponse(url, status_code=302)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/media/{key:path}")
def local_media(key: str, e: str = "", n: str = "", d: str = "inline", t: str = "", s: str = ""):
    storage = get_storage()
    if not isinstance(storage, LocalStorage) or not storage.verify(key, e, n, d, t, s):
        return PlainTextResponse("Deze link is verlopen. Ga terug en probeer het opnieuw.", status_code=403)
    try:
        path = storage._path(key)
    except Exception:  # noqa: BLE001 - invalid key
        return PlainTextResponse("niet gevonden", status_code=404)
    if not path.exists():
        return PlainTextResponse("niet gevonden", status_code=404)
    return FileResponse(path, media_type=t or "application/octet-stream",
                        headers={"Content-Disposition": content_disposition(n, d),
                                 "Cache-Control": "private, max-age=600"})


def _pin_ok(request: Request, token: str) -> bool:
    raw = request.cookies.get(f"vt_qr_{token}")
    return bool(raw and unsign("qrpin", raw, 180 * 86400) == token)


@router.get("/v/{token}")
def listen(request: Request, token: str, db: Session = Depends(db_session)):
    if not rate_limiter.allow(f"qr:{_client_ip(request)}", 120, 600):
        return render(request, "public/gone.html", status_code=429, reason="busy")
    link = db.scalar(select(QRLink).where(QRLink.token == token.lower()))
    if link is None:
        return render(request, "public/gone.html", status_code=404, reason="unknown")
    if link.revoked_at is not None:
        return render(request, "public/gone.html", status_code=410, reason="revoked")
    story = db.get(Story, link.story_id)
    project = db.get(Project, link.project_id)
    if link.access_mode == "pin" and not _pin_ok(request, link.token):
        return render(request, "public/listen_pin.html", link=link, project=project)
    parts = []
    for rec in db.scalars(select(Recording).where(Recording.story_id == story.id, Recording.kind == "audio",
                                                  Recording.retracted_at.is_(None)).order_by(Recording.received_at)):
        media_id = rec.playback_media_id or rec.original_media_id
        asset = db.get(MediaAsset, media_id) if media_id else None
        if asset is not None and asset.status == "stored":
            parts.append({"url": asset_url(asset, ttl=6 * 3600), "duration": rec.duration_seconds or 0,
                          "mime": asset.mime_type})
    link.scan_count += 1
    link.last_scanned_at = utcnow()
    track(db, "qr_scanned", project.id)
    db.commit()
    return render(request, "public/listen.html", link=link, story=story, project=project, parts=parts,
                  storyteller=project.storyteller)


@router.post("/v/{token}/pin")
async def listen_pin(request: Request, token: str, db: Session = Depends(db_session)):
    form = await request.form()
    link = db.scalar(select(QRLink).where(QRLink.token == token.lower()))
    if link is None:
        return render(request, "public/gone.html", status_code=404, reason="unknown")
    if not rate_limiter.allow(f"qrpin:{_client_ip(request)}:{token}", 10, 900):
        return render(request, "public/listen_pin.html", status_code=429, link=link, project=db.get(Project, link.project_id),
                      error="Te veel pogingen. Probeer het over een kwartier opnieuw.")
    if not verify_pin(str(form.get("pin", "")).strip(), link.pin_hash):
        return render(request, "public/listen_pin.html", status_code=400, link=link,
                      project=db.get(Project, link.project_id), error="Die pincode klopt niet.")
    resp = RedirectResponse(f"/v/{link.token}", status_code=303)
    resp.set_cookie(f"vt_qr_{link.token}", signer("qrpin").dumps(link.token), max_age=180 * 86400, httponly=True,
                    samesite="lax")
    return resp
