"""The process-wide objects, and the helpers that need nothing but them.

This is the bottom of the web package: it imports from ``core`` and from
``.registry``, and from nothing else under ``web``. That is the whole point of
it existing.

It used to all live in ``deps``, which also had to construct the Telegram bot
manager — and the bot creates keys, which is application logic, which lives in a
router, which imports ``deps``. The cycle was papered over with an import inside
a function body. Splitting the shared objects out lets the dependency arrows run
one way instead:

    state  ->  services  ->  deps  ->  routers

``deps`` still re-exports everything here, so a router importing ``db`` or
``api_or_404`` from ``..deps`` is importing exactly this — no router had to
change, and none should start reaching past ``deps`` to get at it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from itsdangerous import URLSafeTimedSerializer

from ..core import config, errors
from ..core.db import DB
from ..core.outline_api import OutlineAPI

# The rules live in core so the bot can use them too. Re-exported through here
# and then through deps, because that is where the routers have always read them.
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

__all__ = [
    "CAPS", "can_see", "csv_list", "has_cap", "is_owner", "on_credit", "owns",
    "price_for", "db", "reg", "settings", "signer", "COOKIE_NAME", "STATIC_DIR",
    "api_or_404", "scoped_ids", "sids_or_404", "host", "assert_cap",
    "assert_key_access",
]

COOKIE_NAME = "outline_session"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

db = DB(config.DB_PATH)
reg = Registry(db)
settings = SettingsStore(db)
signer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="session")


# ------------------------------------------------------------------ registry
def api_or_404(sid: str) -> OutlineAPI:
    api = reg.get(sid)
    if api is None:
        raise errors.unknown_server()
    return api


def scoped_ids(admin: dict) -> list[str]:
    return [s for s in reg.ids() if can_see(admin, s)]


def sids_or_404(server: str | None, admin: dict) -> list[str]:
    """Servers a list/stats query covers: the named one, or all *of mine*.

    An unknown id must 404, not fall back to "all" — that inverts a filter into
    its opposite and reports every server's data as that one server's.
    """
    if not server:
        return scoped_ids(admin)
    if reg.meta(server) is None or not can_see(admin, server):
        raise errors.unknown_server()
    return [server]


def host(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


# -------------------------------------------------------------------- access
def assert_cap(admin: dict, cap: str) -> None:
    """Raise unless this admin holds `cap`. The dependency form is deps.require()."""
    if not has_cap(admin, cap):
        raise errors.no_permission()


async def assert_key_access(admin: dict, sid: str | None,
                            kid: str | None = None) -> None:
    """The scope + ownership rules, callable outside a FastAPI dependency.

    The dashboard reaches these through deps.enforce_scope; the Telegram bot and
    Mini App call them directly. One implementation, so Telegram cannot drift
    into being a back door with laxer rules than the panel.
    """
    if sid and not can_see(admin, sid):
        raise errors.unknown_server()
    if sid and kid and not is_owner(admin):
        if not owns(admin, await db.get_key(sid, kid)):
            raise errors.unknown_key()
