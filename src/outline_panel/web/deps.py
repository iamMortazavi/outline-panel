"""FastAPI dependencies: who is calling, and whether they may.

The shared objects live in `state` and the key logic the Telegram bot needs
lives in `services.keys`, so this module can import both and build the bot
manager here — at module level, without reaching into a router from inside a
function body the way it used to.

That wiring staying in **one** place matters more than it looks: `bot/run.py`
gets a fully-configured manager simply by importing this module. The standalone
bot once built its dispatcher without `create_key` and without `resolve_admin`,
which made every bot admin a panel owner and broke key creation outright
(REFACTOR_PLAN S2). Nothing is wired at a call site, so no call site can forget.

Everything in `state` is re-exported here, because `..deps` is where the routers
have always imported `db`, `reg`, `settings` and the rights helpers from.
"""

from __future__ import annotations

from fastapi import Cookie, Depends, Request
from itsdangerous import BadSignature

from ..bot.manager import BotManager
from ..core import errors
from .services import keys as key_service
from .state import (
    CAPS,
    COOKIE_NAME,
    STATIC_DIR,
    api_or_404,
    assert_cap,
    assert_key_access,
    can_see,
    csv_list,
    db,
    has_cap,
    host,
    is_owner,
    on_credit,
    owns,
    price_for,
    reg,
    scoped_ids,
    settings,
    sids_or_404,
    signer,
)

_csv = csv_list
__all__ = [  # re-exported: the routers import the rules from here
    "CAPS", "can_see", "csv_list", "_csv", "has_cap", "is_owner", "on_credit",
    "owns", "price_for", "db", "reg", "settings", "botmgr", "signer",
    "COOKIE_NAME", "STATIC_DIR", "current_admin", "require", "require_owner",
    "assert_cap", "assert_key_access", "enforce_scope", "admin_for_telegram",
    "api_or_404", "sids_or_404", "scoped_ids", "host",
]

botmgr = BotManager(db, reg, settings.get_admin_ids, settings.get_webapp_url,
                    resolve_admin=settings.admin_for_telegram,
                    create_key=key_service.create_key_as)


async def current_admin(request: Request,
                        outline_session: str | None = Cookie(default=None)) -> dict:
    """The admin behind this request, loaded fresh from the DB every time.

    Re-reading the row is what makes revocation instant: disabling or deleting
    an admin kills their live session on the next request, with no session
    store to keep in sync.
    """
    if not outline_session:
        raise errors.not_authenticated()
    try:
        data = signer.loads(outline_session,
                            max_age=await settings.num("session_max_age"))
    except BadSignature:
        raise errors.session_expired()
    # Pre-identity cookies held a bare random string. There is no honest way to
    # map one to an admin, so they end here and the user logs in again once.
    if not isinstance(data, dict) or "aid" not in data:
        raise errors.session_expired()
    # The one choke point every authenticated route passes through, so it is
    # where the server list is brought up to date: reg is process-local and a
    # server added by another worker (or the standalone bot) would otherwise
    # never appear here. One local SELECT per request.
    await reg.sync()
    row = await db.get_admin(int(data["aid"]))
    if row is None or row["disabled"]:
        raise errors.session_expired()
    # Hand the actor to the audit middleware, which runs outside the dependency
    # tree and has no other way to learn who this was.
    request.state.audit_admin = row
    return row


def require(*caps: str):
    """Dependency factory: every listed capability is required."""
    async def _check(admin: dict = Depends(current_admin)) -> dict:
        for c in caps:
            if not has_cap(admin, c):
                raise errors.no_permission()
        return admin
    return _check


async def require_owner(admin: dict = Depends(current_admin)) -> dict:
    if not is_owner(admin):
        raise errors.owner_only()
    return admin


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
