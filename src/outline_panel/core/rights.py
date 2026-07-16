"""
Who may do what — the pure rules, with no web or bot imports.

The dashboard, the Telegram Mini App and the bot all decide access from these.
They live in core precisely so there is one definition: `web.deps` cannot be
imported from `bot.dispatcher` (deps → bot.manager → bot.dispatcher is a cycle),
and a second copy of the rules is how Telegram quietly becomes a back door with
laxer permissions than the panel.
"""

from __future__ import annotations

# Capabilities a sub-admin can be granted. Deliberately NOT here, and never
# delegatable: backup/restore (its export carries every password hash and the
# bot token, and restore rewrites the panel), managing admins (you could grant
# yourself anything), and the owner's own password/2FA.
CAPS = ("keys.view", "keys.create", "keys.edit", "keys.delete",
        "servers.manage", "bot.manage")


def csv_list(value: str | None) -> list[str]:
    return [x for x in (value or "").split(",") if x]


def is_owner(admin: dict) -> bool:
    return bool(admin.get("is_owner"))


def has_cap(admin: dict, cap: str) -> bool:
    return is_owner(admin) or cap in csv_list(admin.get("caps"))


def can_see(admin: dict, sid: str) -> bool:
    """The owner, and any sub-admin whose allowlist is empty, see every server."""
    if is_owner(admin):
        return True
    allowed = csv_list(admin.get("servers"))
    return not allowed or sid in allowed


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
