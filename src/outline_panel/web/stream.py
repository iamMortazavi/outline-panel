"""
One sampler for the whole panel, instead of one poll loop per open tab.

Every dashboard tab used to call `/api/stats` every 5 seconds and `/api/keys`
every 30, and each of those fans out to every configured Outline server. Ten
tabs on a ten-server panel is 120 upstream requests a minute for numbers that
change about once a minute — Outline only refreshes its bandwidth sample that
often, which the frontend already knew, since it deduplicated on `bwTs`.

So the fan-out moves here. One task samples every server once per tick and every
connected tab is served from that snapshot. Adding tabs now costs nothing
upstream, which is the property that matters: the panel is watched from a phone,
a laptop and a shop counter at the same time.

**Scoping happens per subscriber, not per sample.** The snapshot is taken as the
owner because that is the only way to take it once; each connection then filters
it to what that admin may see. The filter is the same `can_see`/ownership pair
the REST routes use — a stream that leaked one reseller's customers to another
would be the same bug as A-2, arriving faster.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from ..core.rights import can_see, is_owner
from .deps import db, reg, settings
from .routers import keys as keys_router
from .routers import stats as stats_router

log = logging.getLogger("web.stream")

# The snapshot is taken with every server and every customer in view. Nothing is
# served from it unfiltered; see `visible_to`.
_ALL = {"is_owner": 1, "id": None, "caps": "", "servers": ""}


def visible_to(admin: dict, snapshot: dict) -> dict:
    """The part of a snapshot this admin is allowed to see.

    Deliberately the same two rules as `rights.owns` and `rights.can_see`, read
    off the already-shaped rows: a sub-admin sees their own customers on the
    servers they were granted, and an owner sees everything.
    """
    if is_owner(admin):
        return snapshot
    mine = [k for k in snapshot["keys"]
            if can_see(admin, k["serverId"]) and k["ownerAdminId"] == admin["id"]]
    per = [p for p in snapshot["stats"]["perServer"] if can_see(admin, p["id"])]
    return {
        "keys": mine,
        "errors": [e for e in snapshot["errors"] if can_see(admin, e["serverId"])],
        "stats": stats_router.aggregate(per),
    }


class Hub:
    """Holds the subscribers and the one task that feeds them."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._snapshot: dict | None = None

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    async def subscribe(self) -> asyncio.Queue:
        """Join, and get the current snapshot straight away.

        A tab that had to wait a whole tick before showing anything would be a
        worse experience than the polling it replaces.
        """
        # A bounded queue: a tab whose connection has stalled must not grow a
        # backlog of snapshots in memory. Oldest is dropped — the newest reading
        # is the only one worth delivering.
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subs.add(queue)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        if self._snapshot is not None:
            queue.put_nowait(self._snapshot)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subs.discard(queue)
        if not self._subs and self._task is not None:
            # Nobody is watching: stop talking to the Outline servers.
            self._task.cancel()
            self._task = None
            self._snapshot = None

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._subs.clear()

    async def sample(self) -> dict:
        """One reading of every server, shaped exactly as the REST routes shape
        it — `keys_for_server` is reused rather than reimplemented, so the
        stream cannot drift from `/api/keys`."""
        await reg.sync()
        sids = reg.ids()
        names = {a["id"]: a["username"] for a in await db.all_admins()}
        owner = await db.get_owner()
        names[None] = owner["username"] if owner else "owner"
        per_server = await asyncio.gather(
            *[keys_router.keys_for_server(s, _ALL, names) for s in sids])
        stats = stats_router.aggregate(
            await stats_router.sample(sids, await settings.num("metrics_ttl")))
        return {
            "keys": [k for r in per_server for k in r["keys"]],
            "errors": [{"serverId": r["serverId"], "serverName": r["serverName"],
                        "error": r["error"]} for r in per_server if r["error"]],
            "stats": stats,
        }

    async def _loop(self) -> None:
        while True:
            try:
                self._snapshot = await self.sample()
                for queue in list(self._subs):
                    if queue.full():          # a stalled tab: drop the stale one
                        with contextlib.suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(self._snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one bad tick must not end the stream
                log.warning("sampling failed: %s", e)
            await asyncio.sleep(await settings.num("stream_interval"))


hub = Hub()


def event(name: str, payload) -> bytes:
    """One SSE frame. `json.dumps` with no newlines, because a newline inside a
    `data:` line ends the frame."""
    return f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n".encode()
