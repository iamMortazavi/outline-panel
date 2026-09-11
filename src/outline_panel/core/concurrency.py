"""Fan-out across servers, with a ceiling.

Nearly every expensive thing this panel does has the same shape: take the list
of configured servers and ask each one something over the network. That was
written four separate times — twice as a bare ``asyncio.gather`` and twice as a
plain ``for`` loop — and the two shapes fail in opposite directions.

A sequential loop pays the *sum* of the servers' latencies, and an unreachable
server costs its whole connect timeout before the next one is even tried. A bare
``gather`` pays only the slowest, but opens every connection at once: on a panel
with fifty servers that is fifty simultaneous TLS handshakes, on a dashboard
that polls every five seconds.

One helper, so the choice is made once and in a place worth reading.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

# Large enough that an ordinary fleet still goes out in one round, small enough
# that a big one cannot open an unbounded number of sockets on a timer.
DEFAULT_LIMIT = 16


async def map_concurrently(
    items: Iterable[T],
    fn: Callable[[T], Awaitable[R]],
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[R]:
    """``[await fn(i) for i in items]``, run concurrently, at most `limit` at once.

    Results are returned in the order of `items`, never completion order —
    callers line them up against their inputs and several of them depend on it.

    Exceptions propagate exactly as ``asyncio.gather`` propagates them: the
    first one raises and the rest are left to finish. Every caller here already
    catches ``OutlineError`` inside `fn`, which is the right place for it — a
    server being down is an answer about that server, not a failure of the
    whole fan-out.
    """
    items = list(items)
    if not items:
        return []
    if len(items) == 1:  # the common single-server panel: no semaphore, no tasks
        return [await fn(items[0])]

    sem = asyncio.Semaphore(max(1, limit))

    async def run(item: T) -> R:
        async with sem:
            return await fn(item)

    return await asyncio.gather(*[run(i) for i in items])
