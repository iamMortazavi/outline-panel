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
import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from ..core import config
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


app = FastAPI(title="Outline Panel", lifespan=lifespan)


class _AssetGZip(GZipMiddleware):
    """GZip for the dashboard shell and /static — and nothing else. API
    responses carry key material and are deliberately left uncompressed (the
    BREACH pattern); the fonts are already compressed."""

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path in ("/", "/tma") or (path.startswith("/static/")
                                     and not path.startswith("/static/vendor/fonts/")):
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)

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
async def static_cache(request: Request, call_next):
    """Long-lived caching for what cannot go stale, revalidation for the rest.

    A versioned URL (``?v=<content hash>``) names exactly one version of a file,
    and the fonts never change — both are safe to keep for a year. Anything
    else under /static (the customer page's script, the QR library) is
    revalidated with its ETag, which costs a 304 rather than the file.
    """
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") and resp.status_code == 200:
        if "v" in request.query_params or path.startswith("/static/vendor/fonts/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


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


# The dashboard's CSS and JS are separate files so a browser can keep them
# between visits. Their URLs carry a hash of their contents, stamped into the
# page once at startup: an upgrade changes the hash, so nobody ever runs a
# cached app.js against a newer server, and until then the files are cached
# for a year (see static_cache below).
def _asset_version() -> str:
    h = hashlib.sha256()
    for name in ("fonts.css", "organic.css", "app.js", "i18n.js"):
        h.update((STATIC_DIR / name).read_bytes())
    return h.hexdigest()[:12]


ASSET_VERSION = _asset_version()
_INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__V__", ASSET_VERSION)


@app.get("/")
async def index():
    # no-cache, not no-store: the shell is revalidated every load, so a new
    # version is picked up at once, but it is tiny and carries no secrets.
    return HTMLResponse(_INDEX_HTML, headers={"Cache-Control": "no-cache"})


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
# Added last, so it wraps outermost and compresses the final bytes.
app.add_middleware(_AssetGZip, minimum_size=1024)
