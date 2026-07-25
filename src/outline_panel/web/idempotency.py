"""
Making a retried purchase safe to send twice.

`create_key` and `extend_key` take the money before they call Outline, which is
correct — a check that does not also reserve the funds lets two tabs both pass
it. But it means a request whose *response* is lost has already cost the admin.
The client cannot tell "never arrived" from "arrived, reply lost", so its only
options were to retry and pay twice, or not retry and maybe lose a sale.

A key sent by the client resolves that: the second request with the same key
replays the first one's answer instead of doing the work again.

The reservation is an INSERT against a PRIMARY KEY, so SQLite decides the race —
the same reasoning as `charge()`'s `WHERE credit >= ?`. Nothing here is
advisory.
"""

from __future__ import annotations

import hashlib
import json
import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .deps import db

log = logging.getLogger("web.idempotency")

HEADER = "Idempotency-Key"
_MAX_KEY = 200


def _hash(endpoint: str, body: bytes) -> str:
    return hashlib.sha256(endpoint.encode() + b"\0" + (body or b"")).hexdigest()


class Purchase:
    """A reserved purchase. `replay` is set when this key has already been used.

    Usage inside a route:

        guard = await begin(request, admin)
        if guard.replay:
            return guard.replay
        try:
            result = ...
        except BaseException:
            await guard.abandon()      # nothing was charged; let a retry through
            raise
        await guard.done(result)
        return result
    """

    def __init__(self, key: str | None, replay: JSONResponse | None = None):
        self.key = key
        self.replay = replay

    async def done(self, result) -> None:
        if not self.key:
            return
        try:
            await db.finish_idempotency(self.key, 200, json.dumps(result, default=str))
        except Exception:  # noqa: BLE001 — the work succeeded; do not undo it now
            log.exception("could not store the result for %s", self.key)

    async def abandon(self) -> None:
        """Release the reservation after a failure that charged nothing.

        Only ever called on a path that reversed its charge (`_reverse`). A
        failure that kept the money must keep its row too, or the retry pays
        again — which is the entire thing this module exists to prevent.
        """
        if not self.key:
            return
        try:
            await db.release_idempotency(self.key)
        except Exception:  # noqa: BLE001
            log.exception("could not release %s", self.key)


async def begin(request: Request | None, admin: dict) -> Purchase:
    """Reserve this request's key, or hand back the earlier response.

    No header means no protection — the old behaviour, so an existing client or
    a direct API call keeps working. The panel's own UI always sends one.

    `request` is None when the caller is not an HTTP request at all: the
    Telegram bot drives `create_key` directly, and a tap on an inline button is
    not a retry the user can accidentally repeat the way a hung fetch is.
    """
    if request is None:
        return Purchase(None)
    key = (request.headers.get(HEADER) or "").strip()
    if not key:
        return Purchase(None)
    if len(key) > _MAX_KEY:
        raise HTTPException(status_code=400, detail=f"{HEADER} is too long")

    endpoint = f"{request.method} {request.url.path}"
    # The body is already read and cached by the audit middleware, so this is
    # free and the route still receives it.
    digest = _hash(endpoint, await request.body())
    # Scope the stored key by admin: two admins must never collide, and one
    # cannot probe another's keys.
    scoped = f"{admin['id']}:{key}"

    existing = await db.claim_idempotency(scoped, admin["id"], endpoint, digest)
    if existing is None:
        return Purchase(scoped)

    if existing["request_hash"] != digest:
        # Same key, different order. Replaying the first answer would be a lie
        # and running this one would break the promise the key makes.
        raise HTTPException(
            status_code=422,
            detail=f"{HEADER} was already used for a different request",
        )
    if existing["status"] is None:
        # The first attempt is still running. Answering "no" is safer than
        # guessing: the client retries and gets the real result.
        raise HTTPException(
            status_code=409,
            detail="That request is still being processed — try again in a moment",
        )
    return Purchase(scoped, JSONResponse(
        status_code=existing["status"],
        content=json.loads(existing["response"] or "null"),
        headers={"Idempotent-Replay": "true"},
    ))
