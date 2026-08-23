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
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core import config
from ..core import settings as core_settings
from ..core.outline_api import OutlineError
from ..core.scheduler import expiry_loop
from ..core.settings import BOT_ENABLED, BOT_TOKEN
from .audit import audit_middleware
from .deps import (
    COOKIE_NAME,
    STATIC_DIR,
    botmgr,
    current_admin,
    db,
    reg,
    require_owner,
    settings,
)
from .observability import (
    configure_logging,
    metrics_endpoint,
    observability_middleware,
)
from .profile import profile_host_guard
from .profile import router as profile_router
from .routers import (
    admins,
    audit,
    auth,
    backup,
    keys,
    miniapp,
    packages,
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
    configure_logging(os.getenv("LOG_FORMAT", "text"), os.getenv("LOG_LEVEL", "INFO"))
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
            expiry_loop(reg, db, config.EXPIRY_CHECK_INTERVAL,
                        notifier=botmgr.notify, settings=settings)
        )
    else:
        log.info("ENABLE_SCHEDULER=false — background scheduler not started.")
    yield
    if task:
        task.cancel()
    await botmgr.stop()
    await reg.close_all()
    await db.close()


# `/docs`, `/redoc` and `/openapi.json` are off unless PANEL_DOCS=1.
#
# They describe every route of an internet-facing admin panel to anyone who
# asks, and nothing in the panel needs them at runtime: the schema used to
# generate the frontend's types is produced offline from `app.openapi()`, with
# no HTTP involved (see scripts/dump_openapi.py). The profile host already
# 404s these names; this closes them on the panel host too.
_DOCS = os.getenv("PANEL_DOCS", "").strip().lower() in ("1", "true", "yes", "on")

app = FastAPI(
    title="Outline Panel",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
)

# Registered first so it wraps outermost and sees the final status of every
# mutating request, including ones rejected by a dependency.
app.middleware("http")(audit_middleware)
# Outermost of the two, so the request id is set before anything else logs and
# the timing covers the whole chain.
app.middleware("http")(observability_middleware)
# Outermost: the profile host must be gated before anything else looks at the
# request, so a 404 there costs nothing and leaks nothing.
app.middleware("http")(profile_host_guard)


@app.middleware("http")
async def settings_scope(request: Request, call_next):
    """Read each setting at most once per request.

    The store deliberately has no long-lived cache — a stale one is what made
    the bot poll with a replaced token. But `metrics_ttl` and `profile_base` are
    read inside per-server loops, so a ten-server panel paid ~25 redundant
    SELECTs on every dashboard poll. This memo is discarded before the response
    is sent, so it can hold a value for milliseconds and never across a write.
    """
    with core_settings.read_scope():
        return await call_next(request)


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
app.include_router(audit.router)
app.include_router(packages.router)
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


@app.get("/metrics")
async def metrics_route(request: Request):
    """Prometheus scrape.

    Not public: the labels carry server hostnames and the values carry customer
    and revenue counts. A scraper uses a bearer token set in the panel; a person
    can just be signed in as the owner.
    """
    token = (await settings.get("metrics_token") or "").strip()
    auth = (request.headers.get("Authorization") or "").strip()
    if token and secrets.compare_digest(auth, f"Bearer {token}"):
        return await metrics_endpoint(db, reg)
    try:
        await require_owner(await current_admin(request,
                                                request.cookies.get(COOKIE_NAME)))
    except HTTPException:
        raise HTTPException(status_code=401, detail="Metrics require the owner "
                                                    "session or a metrics token")
    return await metrics_endpoint(db, reg)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(OutlineError)
async def outline_exc_handler(request, exc: OutlineError):
    """An Outline server said no, and no member of the subscription got through.

    The use cases raise this rather than importing the HTTP error helpers, so
    the application layer stays free of FastAPI. The body is the same envelope
    `errors.upstream()` produces, because to a client this is the same event.
    """
    return JSONResponse(status_code=502, content={
        "detail": str(exc), "code": "outline.unavailable",
        "params": {"message": str(exc)}})


@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc: HTTPException):
    """`detail` stays the English sentence it always was, so nothing that reads
    it breaks. A PanelError adds `code`/`params` beside it, which is what lets
    the UI translate — and its absence is what makes the fallback to `detail`
    the correct behaviour rather than a bug."""
    body: dict = {"detail": exc.detail}
    code = getattr(exc, "code", None)
    if code:
        body["code"] = code
        if getattr(exc, "params", None):
            body["params"] = exc.params
    return JSONResponse(status_code=exc.status_code, content=body)


# Registered after every real route, so `/{token}` only sees what nothing else
# claimed — /healthz, /metrics, /tma and the API all match first.
app.include_router(profile_router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
