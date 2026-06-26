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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..scheduler import expiry_loop
from ..settings import BOT_ENABLED, BOT_TOKEN
from .deps import STATIC_DIR, botmgr, db, reg, settings
from .routers import (
    auth, backup, keys, servers, settings as settings_router, stats, subscription,
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


app = FastAPI(title="Outline Control Room", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(servers.router)
app.include_router(keys.router)
app.include_router(stats.router)
app.include_router(settings_router.router)
app.include_router(backup.router)
app.include_router(subscription.router)


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
