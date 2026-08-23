"""
The live view, as one stream instead of a poll loop per tab.

Server-Sent Events rather than WebSockets: this is one-directional, every write
already has a REST route, and SSE reconnects on its own with no protocol
upgrade for a reverse proxy to get wrong.

The response carries `X-Accel-Buffering: no` because an nginx in front of this
will otherwise buffer the stream and deliver nothing until it closes. Caddy is
fine either way; the header costs nothing and turns a silent failure into a
working one.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..deps import current_admin, settings
from ..stream import event, hub, visible_to

log = logging.getLogger("web.live")

router = APIRouter(prefix="/api", tags=["live"])


@router.get("/stream")
async def stream(request: Request, admin: dict = Depends(current_admin)):
    """Push this admin's view of the panel whenever it changes.

    Identity is resolved once, at connect. That is the one thing this route does
    differently from every other: a REST request re-reads the admin row each
    time, which is what makes revoking access instant. A stream can be open for
    hours, so it re-checks the row on every tick and closes when the admin is
    disabled or deleted — same guarantee, enforced on the same schedule as the
    data it is sending.
    """
    queue = await hub.subscribe()
    heartbeat = await settings.num("stream_heartbeat")

    async def frames():
        # `last` is per connection, not per snapshot: two admins see different
        # slices of the same reading, so "did anything change" is a different
        # question for each of them.
        last: str | None = None
        try:
            yield event("hello", {"interval": await settings.num("stream_interval")})
            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except TimeoutError:
                    # A comment frame. Keeps proxies and phones from deciding
                    # the connection is dead during a quiet spell.
                    yield b": keep-alive\n\n"
                    continue
                from ..deps import db
                fresh = await db.get_admin(admin["id"])
                if fresh is None or fresh["disabled"]:
                    yield event("bye", {"reason": "access revoked"})
                    return
                payload = visible_to(fresh, snapshot)
                body = event("snapshot", payload)
                if body.decode() == last:
                    continue          # nothing this admin can see has moved
                last = body.decode()
                yield body
        except asyncio.CancelledError:      # the tab went away
            raise
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
