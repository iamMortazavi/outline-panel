"""Shared web state and FastAPI dependencies (auth, registry, helpers)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi import Cookie, Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..bot.manager import BotManager
from ..core import config
from ..core.db import DB
from ..core.outline_api import OutlineAPI

# The rules live in core so the bot can use them too (deps → bot.manager →
# bot.dispatcher would be a cycle). Re-exported here because the routers import
# them from deps alongside db/reg.
from ..core.rights import (
    CAPS,
    can_see,
    csv_list,
    has_cap,
    is_owner,
    on_credit,
    owns,
    price_for,
)
from ..core.settings import SettingsStore
from .registry import Registry

_csv = csv_list
__all__ = [  # re-exported: the routers import the rules from here
    "CAPS", "can_see", "csv_list", "_csv", "has_cap", "is_owner", "on_credit",
    "owns", "price_for", "db", "reg", "settings", "botmgr", "signer",
    "COOKIE_NAME", "STATIC_DIR", "current_admin", "require", "require_owner",
    "assert_cap", "assert_key_access", "enforce_scope", "admin_for_telegram",
    "api_or_404", "sids_or_404", "scoped_ids", "host",
]

COOKIE_NAME = "outline_session"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

db = DB(config.DB_PATH)
reg = Registry(db)
settings = SettingsStore(db)
async def create_key_as(admin: dict, sid: str, **fields) -> dict:
    """Create a key exactly the way the dashboard does, on behalf of `admin`.

    The bot used to have its own copy of this and so charged nobody and
    attributed nothing — a credit reseller could mint free keys over Telegram.
    The import is deliberately lazy: routers.keys imports this module, so a
    top-level import here would be a cycle.
    """
    from .routers import keys as keys_router
    return await keys_router.create_key(
        sid, keys_router.CreateBody(**fields), admin)


botmgr = BotManager(db, reg, settings.get_admin_ids, settings.get_webapp_url,
                    resolve_admin=settings.admin_for_telegram,
                    create_key=create_key_as)
signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="session")


async def current_admin(outline_session: str | None = Cookie(default=None)) -> dict:
    """The admin behind this request, loaded fresh from the DB every time.

    Re-reading the row is what makes revocation instant: disabling or deleting
    an admin kills their live session on the next request, with no session
    store to keep in sync.
    """
    if not outline_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        data = signer.loads(outline_session, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        raise HTTPException(status_code=401, detail="Session expired")
    # Pre-identity cookies held a bare random string. There is no honest way to
    # map one to an admin, so they end here and the user logs in again once.
    if not isinstance(data, dict) or "aid" not in data:
        raise HTTPException(status_code=401, detail="Session expired")
    row = await db.get_admin(int(data["aid"]))
    if row is None or row["disabled"]:
        raise HTTPException(status_code=401, detail="Session expired")
    return row


def scoped_ids(admin: dict) -> list[str]:
    return [s for s in reg.ids() if can_see(admin, s)]


def require(*caps: str):
    """Dependency factory: every listed capability is required."""
    async def _check(admin: dict = Depends(current_admin)) -> dict:
        for c in caps:
            if not has_cap(admin, c):
                raise HTTPException(status_code=403,
                                    detail="You do not have permission for this")
        return admin
    return _check


async def require_owner(admin: dict = Depends(current_admin)) -> dict:
    if not is_owner(admin):
        raise HTTPException(status_code=403, detail="Owner only")
    return admin


def assert_cap(admin: dict, cap: str) -> None:
    """Raise unless this admin holds `cap`. The dependency form is require()."""
    if not has_cap(admin, cap):
        raise HTTPException(status_code=403,
                            detail="You do not have permission for this")


async def assert_key_access(admin: dict, sid: str | None,
                            kid: str | None = None) -> None:
    """The scope + ownership rules, callable outside a FastAPI dependency.

    The dashboard reaches these through enforce_scope; the Telegram bot and
    Mini App call them directly. One implementation, so Telegram cannot drift
    into being a back door with laxer rules than the panel.
    """
    if sid and not can_see(admin, sid):
        raise HTTPException(status_code=404, detail="Unknown server")
    if sid and kid and not is_owner(admin):
        if not owns(admin, await db.get_key(sid, kid)):
            raise HTTPException(status_code=404, detail="Unknown key")


async def enforce_scope(request: Request,
                        admin: dict = Depends(current_admin)) -> None:
    """Router-level guard for every route carrying a `{sid}` and/or a `{kid}`.

    Attaching this once per router covers all of them, so a new server- or
    key-scoped route is guarded by construction rather than by remembering.
    Out-of-scope servers and other admins' users 404: to a sub-admin they
    simply do not exist.
    """
    await assert_key_access(admin, request.path_params.get("sid"),
                            request.path_params.get("kid"))


async def admin_for_telegram(uid: int | None) -> dict | None:
    """The panel admin behind a Telegram user — the bot resolves the same way."""
    return await settings.admin_for_telegram(uid)


def api_or_404(sid: str) -> OutlineAPI:
    api = reg.get(sid)
    if api is None:
        raise HTTPException(status_code=404, detail="Unknown server")
    return api


def sids_or_404(server: str | None, admin: dict) -> list[str]:
    """Servers a list/stats query covers: the named one, or all *of mine*.

    An unknown id must 404, not fall back to "all" — that inverts a filter into
    its opposite and reports every server's data as that one server's.
    """
    if not server:
        return scoped_ids(admin)
    if reg.meta(server) is None or not can_see(admin, server):
        raise HTTPException(status_code=404, detail="Unknown server")
    return [server]


def host(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""
