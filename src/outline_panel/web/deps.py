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
from ..core.settings import SettingsStore
from .registry import Registry

COOKIE_NAME = "outline_session"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

db = DB(config.DB_PATH)
reg = Registry(db)
settings = SettingsStore(db)
botmgr = BotManager(db, reg, settings.get_admin_ids, settings.get_webapp_url)
signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="session")


# Capabilities a sub-admin can be granted. Deliberately NOT here, and never
# delegatable: backup/restore (its export carries every password hash and the
# bot token, and restore rewrites the panel), managing admins (you could grant
# yourself anything), and the owner's own password/2FA.
CAPS = ("keys.view", "keys.create", "keys.edit", "keys.delete",
        "servers.manage", "bot.manage")


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


def is_owner(admin: dict) -> bool:
    return bool(admin.get("is_owner"))


def _csv(value: str | None) -> list[str]:
    return [x for x in (value or "").split(",") if x]


def can_see(admin: dict, sid: str) -> bool:
    """The owner, and any sub-admin whose allowlist is empty, see every server."""
    if is_owner(admin):
        return True
    allowed = _csv(admin.get("servers"))
    return not allowed or sid in allowed


def scoped_ids(admin: dict) -> list[str]:
    return [s for s in reg.ids() if can_see(admin, s)]


def has_cap(admin: dict, cap: str) -> bool:
    return is_owner(admin) or cap in _csv(admin.get("caps"))


def on_credit(admin: dict) -> bool:
    """Whether this admin buys from the price list. The owner never does, and
    an admin left outside the credit system keeps the free-form form."""
    return not is_owner(admin) and bool(admin.get("credit_enabled"))


def price_for(pkg: dict, admin: dict) -> int:
    """What this admin pays for this package, after their personal discount.

    One base price per package plus a per-admin percentage — the single place
    that math happens, so the picker and the charge can never disagree.
    """
    pct = max(0, min(100, int(admin.get("discount_pct") or 0)))
    return round(int(pkg["price"]) * (100 - pct) / 100)


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


def owns(admin: dict, meta: dict | None) -> bool:
    """Whether this admin's page a key belongs on.

    NULL owner means the panel owner's — that is how every key created before
    ownership existed stays theirs, with no backfill. A key Outline knows about
    but the panel has no row for is unattributed, so it is the owner's too.
    """
    if is_owner(admin):
        return True
    if meta is None:
        return False
    return meta.get("owner_admin_id") == admin["id"]


async def enforce_scope(request: Request,
                        admin: dict = Depends(current_admin)) -> None:
    """Router-level guard for every route carrying a `{sid}` and/or a `{kid}`.

    Attaching this once per router covers all of them, so a new server- or
    key-scoped route is guarded by construction rather than by remembering.
    Out-of-scope servers and other admins' users 404: to a sub-admin they
    simply do not exist.
    """
    sid = request.path_params.get("sid")
    if sid and not can_see(admin, sid):
        raise HTTPException(status_code=404, detail="Unknown server")
    kid = request.path_params.get("kid")
    if sid and kid and not is_owner(admin):
        if not owns(admin, await db.get_key(sid, kid)):
            raise HTTPException(status_code=404, detail="Unknown key")


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
