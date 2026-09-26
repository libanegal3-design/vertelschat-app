"""FastAPI application factory. Run: uvicorn vertelschat.web.app:app"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import registry  # noqa: F401  (register job handlers)
from ..config import get_settings
from ..db import init_db
from ..services import ReadOnlyProject
from .deps import CSRFError, Forbidden, LoginRequired, NotFoundError, redirect, render

QR_PATH = re.compile(r"^/([2-9a-hjkmnp-z]{12})/?$")


def create_app() -> FastAPI:
    s = get_settings()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_db()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = None
        if s.inline_worker:  # development convenience: run jobs inside the web process
            from ..worker import start_inline_worker
            stop = start_inline_worker()
        yield
        if stop is not None:
            stop.set()

    app = FastAPI(title="Vertelschat", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def security_and_qr_host(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        m = QR_PATH.match(request.url.path)
        if host == s.qr_host and m:
            request.scope["path"] = f"/v/{m.group(1)}"
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https:; "
                     "style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; connect-src 'self'; "
                     "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://wa.me")
        if s.is_production:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        from urllib.parse import quote
        return redirect(f"/inloggen?next={quote(exc.next_url)}")

    @app.exception_handler(NotFoundError)
    async def _nf(request: Request, exc: NotFoundError):
        return render(request, "errors/404.html", status_code=404)

    @app.exception_handler(Forbidden)
    async def _fb(request: Request, exc: Forbidden):
        return render(request, "errors/403.html", status_code=403)

    @app.exception_handler(CSRFError)
    async def _csrf(request: Request, exc: CSRFError):
        return render(request, "errors/400.html", status_code=400,
                      message="Dit formulier was verlopen. Ga terug, ververs de pagina en probeer het opnieuw.")

    @app.exception_handler(ReadOnlyProject)
    async def _ro(request: Request, exc: ReadOnlyProject):
        back = request.headers.get("referer") or "/app"
        return redirect(back if back.startswith(s.base_url) or back.startswith("/") else "/app", str(exc), "info")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return render(request, "errors/404.html", status_code=404)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    from . import routes_auth, routes_hooks, routes_project, routes_public
    app.include_router(routes_public.router)
    app.include_router(routes_hooks.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_project.router)
    if s.dev_tools and not s.is_production:
        from . import routes_dev
        app.include_router(routes_dev.router)

    return app


app = create_app()
