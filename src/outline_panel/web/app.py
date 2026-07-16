"""
Multi-server web dashboard for managing Outline servers.

Run with:
    outline-panel
    # or: uvicorn outline_panel.web.app:app --host 0.0.0.0 --port 8000

Servers can be added from the UI (Settings). On first run, OUTLINE_API_URL from
.env (if set) is imported as the first server.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core import config
from ..core.scheduler import expiry_loop
from ..core.settings import BOT_ENABLED, BOT_TOKEN
from .deps import STATIC_DIR, botmgr, db, reg, settings
from .routers import (
    admins,
    auth,
    backup,
    keys,
    miniapp,
    servers,
    stats,
    subscription,
)
from .routers import (
    settings as settings_router,
)

log = logging.getLogger("webapp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.SESSION_SECRET_SET:
        log.warning(
            "SESSION_SECRET is not set — sessions reset on restart and break "
            "across multiple workers. Set a fixed SESSION_SECRET in production."
        )
    await db.init()
    await settings.bootstrap()
    await reg.load()
    # start the in-process Telegram bot if configured & enabled
    if await settings.get_bool(BOT_ENABLED):
        token = await settings.get(BOT_TOKEN)
        if token:
            try:
                await botmgr.start(token)
            except Exception as e:  # noqa: BLE001 — bad token shouldn't crash the web app
                log.warning("Telegram bot did not start: %s", e)
    task = None
    if config.ENABLE_SCHEDULER:
        task = asyncio.create_task(
            expiry_loop(reg, db, config.EXPIRY_CHECK_INTERVAL, notifier=botmgr.notify)
        )
    else:
        log.info("ENABLE_SCHEDULER=false — background scheduler not started.")
    yield
    if task:
        task.cancel()
    await botmgr.stop()
    await reg.close_all()
    await db.close()


app = FastAPI(title="Outline Panel", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Cheap, always-safe hardening headers on every response."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # The Mini App is framed by Telegram Web; every other page must not be.
    if request.url.path.startswith("/tma"):
        resp.headers.setdefault("Content-Security-Policy",
                                "frame-ancestors https://web.telegram.org "
                                "https://*.telegram.org")
    else:
        resp.headers.setdefault("X-Frame-Options", "DENY")
    # No API response here is cacheable: they carry ss:// keys, api_urls with
    # their secret path, bot tokens. A route may still set its own.
    if request.url.path.startswith(("/api/", "/tma/api/")):
        resp.headers.setdefault("Cache-Control", "no-store")
    # HSTS only when the connection is actually HTTPS (honor a trusted proxy).
    https = request.url.scheme == "https" or (
        config.TRUST_PROXY
        and request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    )
    if https:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp


app.include_router(auth.router)
app.include_router(admins.router)
app.include_router(servers.router)
app.include_router(keys.router)
app.include_router(stats.router)
app.include_router(settings_router.router)
app.include_router(settings_router.bot_router)
app.include_router(backup.router)
app.include_router(subscription.router)
app.include_router(miniapp.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
