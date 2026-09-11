"""
Who did what, recorded in one place.

A middleware rather than a call in each handler: there are ~30 mutating routes
and the one that matters is always the one somebody forgot to annotate. Sitting
in the request path means a route added tomorrow is covered without anyone
remembering — the same reason `enforce_scope` is attached per-router instead of
per-route.

`credit_ledger` already answers "where did the money go". This answers
"who disabled that key", "who changed the price", "who restored a backup".
"""

from __future__ import annotations

import json
import logging

from fastapi import Request

from ..core import config
from .state import db

log = logging.getLogger("web.audit")

# Only state-changing verbs. Logging every GET would bury the signal under
# dashboard polling — /api/keys and /api/stats alone run every few seconds.
_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Never store the contents of these, at any depth. Login bodies carry the
# password; the bot body carries a token that owns the whole Telegram account;
# restore carries every password hash in the panel.
_SECRET_KEYS = {"password", "current", "new", "totp", "token", "pw_hash",
                "pw_salt", "secret", "code", "settings", "admins"}

# Bodies we refuse to walk at all — a restore payload is the entire database.
_NO_BODY = ("/api/restore",)

_MAX_DETAIL = 2000


def _redact(value, depth: int = 0):
    """A copy of `value` with secrets replaced. Depth-capped: the audit row is a
    summary, and a deeply nested body is not worth unbounded work."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _SECRET_KEYS else _redact(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value[:20]]
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "…"
    return value


def _client_ip(request: Request) -> str | None:
    # Same rule as the login rate limiter: forwarded headers are only evidence
    # when a proxy we control sets them.
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _target(request: Request) -> str | None:
    """The thing acted on, from the path params the router already parsed."""
    p = request.path_params
    parts = [str(p[k]) for k in ("sid", "kid", "admin_id", "pkg_id", "token",
                                 "target") if k in p]
    return "/".join(parts) or None


async def _body_summary(request: Request) -> str | None:
    if request.url.path.startswith(_NO_BODY):
        return None
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001 — a body we cannot read is not worth failing over
        return None
    if not raw or len(raw) > 100_000:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    try:
        return json.dumps(_redact(parsed), ensure_ascii=False)[:_MAX_DETAIL]
    except (TypeError, ValueError):
        return None


async def audit_middleware(request: Request, call_next):
    """Record the request, then let it through unchanged.

    The write happens after the response so the status is known, and every
    failure here is swallowed: an audit backend that cannot write must not take
    the panel down with it. A missing row is a gap in the record; a raised
    exception here would be a 500 on a request that otherwise succeeded.
    """
    if request.method not in _METHODS or not request.url.path.startswith("/api/"):
        return await call_next(request)

    # Read the body *before* the handler. Starlette caches it on the request, so
    # the route still receives it; reading afterwards would find the stream
    # already consumed.
    detail = await _body_summary(request)
    response = await call_next(request)

    try:
        # current_admin stashes the row here. It is absent when the request
        # never authenticated — a failed login, an expired session — and those
        # are exactly the ones worth keeping, with a null actor.
        admin = getattr(request.state, "audit_admin", None)
        await db.add_audit(
            actor_admin_id=admin["id"] if admin else None,
            actor_name=admin["username"] if admin else None,
            action=f"{request.method} {request.scope.get('route').path}"
                   if request.scope.get("route") else
                   f"{request.method} {request.url.path}",
            target=_target(request),
            status=response.status_code,
            ip=_client_ip(request),
            detail=detail,
        )
    except Exception:  # noqa: BLE001 — never fail a served request over bookkeeping
        log.exception("could not write an audit record")
    return response
