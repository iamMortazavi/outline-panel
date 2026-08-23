"""
Closing the gap between what the panel decided and what the servers enforce.

The executor writes the panel's intent immediately and queues whatever could not
reach its Outline server. Without something draining that queue, "converged" is
just a nicer word for "lost", so this is the other half of the design:

* `drain` retries queued effects with a backoff.
* `plan` compares every configured server against the panel's rows and reports
  the differences — read-only, so an operator can look before anything moves.
* `reconcile` turns that report into queued effects.

`plan` exists separately from `reconcile` on purpose. The first real
reconciliation of a panel that has been running with the A1 defect will find
customers who were suspended months ago and have been connected ever since, and
applying that silently is not an upgrade, it is a surprise. So the report is
free to look at and the applying is opt-in (`reconcile_enabled`).

**A key upstream that the panel has no row for is never touched.** Keys made
straight from Outline Manager are legitimate and adoption is `ensure_local`'s
job; a reconciler that deleted them would be a data-loss bug that runs on a
timer.
"""

from __future__ import annotations

import logging

from ..core.outline_api import OutlineError
from ..domain.commands import Suspend
from .executor import apply_upstream, decode, encode

log = logging.getLogger("application.convergence")

# Attempt N waits this long before N+1. A dead server should not be hammered,
# and a blip should not wait an hour.
_BACKOFF = (30, 120, 300, 900, 3600)


def backoff_for(attempts: int) -> int:
    return _BACKOFF[min(max(attempts, 1), len(_BACKOFF)) - 1]


async def drain(db, registry, now: int, limit: int = 100) -> dict:
    """Retry every queued effect that is due. Returns a small summary."""
    applied = failed = dropped = 0
    for row in await db.due_commands(now, limit):
        api = registry.get(row["server_id"])
        if api is None:
            # The server is not configured any more. There is nothing left to
            # converge with, and keeping the row means retrying forever.
            await db.drop_command(row["id"])
            dropped += 1
            continue
        try:
            command = decode(row["command"])
        except (ValueError, KeyError):
            log.exception("undecodable outbox row %s — dropping", row["id"])
            await db.drop_command(row["id"])
            dropped += 1
            continue
        try:
            await apply_upstream(api, command)
        except OutlineError as e:
            await db.reschedule_command(row["id"],
                                        backoff_for(int(row["attempts"]) + 1), str(e))
            failed += 1
            continue
        await db.drop_command(row["id"])
        applied += 1
    return {"applied": applied, "failed": failed, "dropped": dropped}


async def plan(db, registry) -> list[dict]:
    """What each server enforces versus what the panel believes, per key.

    Read-only. The only difference reported is the one that matters
    operationally: whether the data limit Outline is applying matches the
    ceiling the panel thinks the customer has — which is also how a suspension
    is expressed, as a limit of zero.
    """
    out: list[dict] = []
    for sid in registry.ids():
        api = registry.get(sid)
        if api is None:
            continue
        try:
            upstream = {str(k["id"]): (k.get("dataLimit") or {}).get("bytes")
                        for k in await api.list_keys()}
        except OutlineError as e:
            out.append({"serverId": sid, "keyId": None, "issue": "unreachable",
                        "detail": str(e)})
            continue
        for row in await db.keys_for(sid):
            kid = str(row["key_id"])
            if kid not in upstream:
                # Deleted straight from Outline Manager. Recreating it would
                # undo a deliberate act, so this is reported and left alone.
                out.append({"serverId": sid, "keyId": kid, "issue": "missing",
                            "want": None, "have": None})
                continue
            want = 0 if row["disabled"] else row["limit_bytes"]
            have = upstream[kid]
            if want != have:
                out.append({"serverId": sid, "keyId": kid, "issue": "limit",
                            "want": want, "have": have,
                            "suspended": bool(row["disabled"])})
    return out


async def reconcile(db, registry, apply: bool = False) -> dict:
    """Report the drift and, when asked, queue the effects that would close it.

    Only suspensions are queued. A panel row saying "40 GB" while Outline says
    "50 GB" can be a stale read, a hand edit in Outline Manager, or a rotation
    in flight, and quietly overwriting an operator's manual change is worse than
    reporting it. A customer the panel has suspended who is still connected is
    never ambiguous, and is exactly the leak A1 caused.
    """
    differences = await plan(db, registry)
    queued = 0
    if apply:
        for item in differences:
            if item["issue"] == "limit" and item.get("suspended"):
                await db.enqueue_command(
                    None, item["serverId"], item["keyId"],
                    encode(Suspend(item["serverId"], item["keyId"])),
                    "reconciliation: suspended here, still serving there")
                queued += 1
    return {"differences": differences, "queued": queued}
