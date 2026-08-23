"""
The things you do to a customer.

Each of these loads the subscription behind whichever key id was in the URL,
asks the aggregate what should happen to *all* of its members, and hands the
result to the executor. That indirection is the fix for A1: the endpoint still
names one key, because that is what the dashboard and Outline both understand,
but nothing downstream can act on one member any more.

`load` is the only place that turns "a server id and a key id" back into a
customer. A key with no subscription token — one that was unlinked from a
multi-server sub — is a subscription of one, so every path here works the same
whether the customer is on one server or four.
"""

from __future__ import annotations

import time

from ..core.utils import gb_to_bytes
from ..domain.subscription import Grant, Subscription


async def load(db, server_id: str, key_id: str) -> Subscription:
    """The customer behind this key, with every server they are on."""
    row = await db.get_key(server_id, key_id)
    if row is None:
        return Subscription(token="", members=())
    token = row.get("sub_token")
    if not token:
        return Subscription.from_rows("", [row])
    rows = await db.get_keys_by_sub_token(token)
    return Subscription.from_rows(token, rows or [row])


async def usage_of(registry, sub: Subscription) -> dict[tuple[str, str], int]:
    """Bytes spent per member.

    One `get_transfer_metrics` per *server*, not per member — several members of
    one subscription on one server is unusual but the call is the expensive part.
    A server that will not answer contributes nothing rather than failing the
    operation; the worst case is a reset that under-credits, which the next
    scheduler pass corrects.
    """
    from ..core.outline_api import OutlineError
    out: dict[tuple[str, str], int] = {}
    per_server: dict[str, dict] = {}
    for m in sub.members:
        if m.server_id not in per_server:
            api = registry.get(m.server_id)
            try:
                per_server[m.server_id] = await api.get_transfer_metrics() if api else {}
            except OutlineError:
                per_server[m.server_id] = {}
        out[(m.server_id, m.key_id)] = int(
            per_server[m.server_id].get(str(m.key_id), 0))
    return out


async def suspend(db, executor, server_id: str, key_id: str) -> list[dict]:
    sub = await load(db, server_id, key_id)
    return await executor.run(sub.suspend(), sub.token or None)


async def resume(db, executor, server_id: str, key_id: str) -> list[dict]:
    sub = await load(db, server_id, key_id)
    return await executor.run(sub.resume(), sub.token or None)


async def set_allowance(db, executor, server_id: str, key_id: str,
                        limit_bytes: int | None) -> list[dict]:
    sub = await load(db, server_id, key_id)
    return await executor.run(sub.set_allowance(limit_bytes), sub.token or None)


async def set_monthly(db, executor, registry, server_id: str, key_id: str,
                      monthly_bytes: int | None,
                      cycle_seconds: int) -> list[dict]:
    sub = await load(db, server_id, key_id)
    usage = await usage_of(registry, sub) if monthly_bytes else {}
    first_reset = int(time.time()) + cycle_seconds if monthly_bytes else None
    return await executor.run(
        sub.set_monthly(monthly_bytes, first_reset, usage), sub.token or None)


async def reset_usage(db, executor, registry, server_id: str,
                      key_id: str) -> tuple[list[dict], int | None]:
    """Fresh allowance now. Returns (deferred, the primary member's new ceiling)
    — the second value only so the existing response shape survives."""
    sub = await load(db, server_id, key_id)
    usage = await usage_of(registry, sub)
    commands = sub.reset_usage(usage)
    deferred = await executor.run(commands, sub.token or None)
    new_limit = next((c.limit_bytes for c in commands
                      if getattr(c, "server_id", None) == server_id
                      and getattr(c, "key_id", None) == key_id
                      and hasattr(c, "tell_node")), None)
    return deferred, new_limit


async def extend(db, executor, server_id: str, key_id: str,
                 days: int) -> list[dict]:
    sub = await load(db, server_id, key_id)
    return await executor.run(sub.extend(days, int(time.time())),
                              sub.token or None)


async def apply_package(db, executor, server_id: str, key_id: str,
                        package: dict) -> tuple[list[dict], int | None]:
    """A renewal bought from the price list. Returns (deferred, new ceiling of
    the member that was named), again only to keep the response shape."""
    sub = await load(db, server_id, key_id)
    grant = Grant(gb=package.get("gb"), days=package.get("days"),
                  monthly_gb=package.get("monthly_gb"))
    commands = sub.apply_grant(grant, int(time.time()), gb_to_bytes)
    deferred = await executor.run(commands, sub.token or None)
    new_limit = next((c.limit_bytes for c in commands
                      if getattr(c, "server_id", None) == server_id
                      and getattr(c, "key_id", None) == key_id
                      and hasattr(c, "tell_node")), None)
    return deferred, new_limit


async def remove(db, executor, server_id: str, key_id: str) -> list[dict]:
    """Delete the customer everywhere. The subscription token is returned to
    nobody: the caller already knows it, and the cache invalidation is its job."""
    sub = await load(db, server_id, key_id)
    return await executor.run(sub.remove(), sub.token or None)
