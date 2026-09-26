"""Inbound webhooks. Both endpoints only persist and enqueue; all processing happens in the worker."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from ..config import get_settings
from ..db import SessionLocal
from ..shopify import ShopifySignatureError
from ..shopify import ingest_webhook as ingest_shopify
from ..whatsapp import SignatureError
from ..whatsapp import ingest_webhook as ingest_whatsapp

router = APIRouter()
log = logging.getLogger("vertelschat.webhooks")
MAX_BODY = 2_000_000


@router.get("/webhooks/whatsapp")
def whatsapp_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == get_settings().whatsapp_verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/webhooks/whatsapp")
async def whatsapp_receive(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return PlainTextResponse("too large", status_code=413)
    with SessionLocal() as db:
        try:
            counts = ingest_whatsapp(db, raw, request.headers.get("x-hub-signature-256"))
            db.commit()
        except SignatureError:
            return PlainTextResponse("invalid signature", status_code=401)
        except Exception:  # noqa: BLE001 - a 500 makes Meta retry; nothing is lost
            db.rollback()
            log.exception("whatsapp webhook failed")
            return PlainTextResponse("error", status_code=500)
    return JSONResponse({"ok": True, **counts})


@router.post("/webhooks/shopify")
async def shopify_receive(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return PlainTextResponse("too large", status_code=413)
    with SessionLocal() as db:
        try:
            result = ingest_shopify(db, raw, request.headers.get("x-shopify-hmac-sha256"),
                                    request.headers.get("x-shopify-topic", ""),
                                    request.headers.get("x-shopify-webhook-id", ""))
            db.commit()
        except ShopifySignatureError:
            return PlainTextResponse("invalid signature", status_code=401)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("shopify webhook failed")
            return PlainTextResponse("error", status_code=500)
    return JSONResponse({"ok": True, "result": result})
