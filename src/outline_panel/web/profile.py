"""
The customer-facing profile host.

A reseller hands a customer one link — `https://star.example.com/<token>` — and
that is the only address the customer ever needs. It survives their config being
replaced, because the token belongs to the person, not to the Outline key.

The hostname is **gated**: when `profile_base_url` is set, requests arriving on
that host may reach the profile and nothing else. The dashboard, the login form
and the whole `/api` surface answer 404 there.

That is the point of a separate host rather than a prettier path. This URL is
handed to every customer, forwarded, pasted into groups and eventually posted
somewhere public. Whoever ends up with it should not also be holding the address
of the panel that administers the servers.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .deps import STATIC_DIR, settings
from .routers import subscription

router = APIRouter(tags=["profile"])

# What the profile host is allowed to serve. Everything else on that host 404s.
_ALLOWED_PREFIXES = ("/static/", "/sub/", "/healthz")

# The app's own top-level names. Checked *before* the token pattern, because a
# token is only recognised by its shape and several real routes share it —
# "metrics" is seven alphanumerics and matched happily, so /metrics sailed
# through the guard and answered 401 instead of 404. A denylist is exact; a
# pattern that has to avoid every current and future route name is not.
_RESERVED = {"api", "static", "sub", "tma", "metrics", "healthz",
             "docs", "redoc", "openapi.json", "favicon.ico"}

# `<key id>-<random>`, plus the older bare-random tokens still in use. Anchored,
# so this cannot match a path with a slash in it.
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{6,80}$")


def host_of(request: Request) -> str:
    """The requested hostname, lowercased, without the port."""
    raw = (request.headers.get("host") or "").strip().lower()
    if raw.startswith("["):                     # IPv6 literal
        return raw.split("]")[0] + "]"
    return raw.split(":")[0]


async def profile_host_guard(request: Request, call_next):
    """Serve only the profile on the profile host.

    Deliberately fails *open* when the setting is unreadable: a database blip
    should degrade to today's behaviour — one host serving everything — rather
    than 404 the panel and the customer pages at the same time.
    """
    try:
        phost = await settings.get_profile_host()
    except Exception:  # noqa: BLE001
        phost = None
    if phost and host_of(request) == phost:
        path = request.url.path
        # `/` is NOT allowed: it serves index.html, which is the dashboard. The
        # root of the customer host has nothing to show and must not become the
        # one page that gives the panel away.
        allowed = path.startswith(_ALLOWED_PREFIXES) or _is_profile_path(path)
        if not allowed:
            # Returned, not raised: middleware sits outside the handler chain,
            # so FastAPI's exception handler never sees a raise here and it
            # escapes as a 500 instead of the 404 we meant.
            return JSONResponse(status_code=404, content={"detail": "Not found"})
    return await call_next(request)


def _is_profile_path(path: str) -> bool:
    """`/<token>` or `/<token>/info`, and nothing deeper."""
    parts = [p for p in path.split("/") if p]
    if not parts or len(parts) > 2:
        return False
    if parts[0].lower() in _RESERVED:
        return False
    if len(parts) == 2 and parts[1] != "info":
        return False
    return bool(_TOKEN.match(parts[0]))


# --------------------------------------------------------------------- routes
# Registered last in app.py so every real route wins the match first; these only
# see paths nothing else claimed.
@router.get("/{token}")
async def profile_page(token: str, request: Request):
    """The customer's page. Same document the subscription link serves — one
    page to maintain, and it already knows how to read its own token."""
    if not _TOKEN.match(token):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(STATIC_DIR / "sub.html")


@router.get("/{token}/info")
async def profile_info(token: str, request: Request):
    """Usage summary. Delegates rather than reimplementing, so the profile can
    never drift from what /sub/<token>/info reports."""
    if not _TOKEN.match(token):
        raise HTTPException(status_code=404, detail="Not found")
    return await subscription.subscription_info(token, request)
