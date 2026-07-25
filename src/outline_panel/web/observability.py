"""
Request-scoped logging and the metrics endpoint.

Two questions had no answer before: "which request was that log line about" and
"is Outline slow right now". A request id threaded through the log line answers
the first; the counters in core.metrics answer the second.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar

from fastapi import Request
from fastapi.responses import PlainTextResponse

from ..core import metrics

log = logging.getLogger("web.access")

# A ContextVar, not a parameter: log records are emitted from anywhere down the
# call stack, and threading an id through every function to reach a log line is
# a worse trade than one context variable.
request_id: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Opt-in via LOG_FORMAT=json.

    Plain text stays the default because most people run this on one box and
    read the journal with their eyes.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id.get(),
        }
        for key in ("method", "path", "status", "ms", "actor"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(fmt: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    if (fmt or "").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))


async def observability_middleware(request: Request, call_next):
    """Tag the request, time it, count it."""
    incoming = request.headers.get("X-Request-ID", "").strip()
    # Accept an upstream id so a trace survives a reverse proxy, but cap it —
    # it ends up in every log line this request produces.
    rid = incoming[:64] if incoming else uuid.uuid4().hex[:12]
    token = request_id.set(rid)
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        metrics.inc("outline_panel_http_requests_total",
                    {"method": request.method, "status": "500"})
        log.exception("unhandled error", extra={"method": request.method,
                                                "path": request.url.path})
        request_id.reset(token)
        raise
    elapsed = time.monotonic() - started
    # The route template, not the raw path: /api/servers/{sid}/keys is one
    # series, while the raw path would be one series per server id.
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    metrics.inc("outline_panel_http_requests_total",
                {"method": request.method, "status": str(response.status_code)})
    metrics.observe("outline_panel_http_request_seconds", elapsed, {"path": path})
    response.headers["X-Request-ID"] = rid
    if request.url.path.startswith("/api/") and request.method != "GET":
        log.info("%s %s -> %d", request.method, path, response.status_code,
                 extra={"method": request.method, "path": path,
                        "status": response.status_code,
                        "ms": round(elapsed * 1000, 1)})
    request_id.reset(token)
    return response


async def metrics_endpoint(db, reg) -> PlainTextResponse:
    """Prometheus exposition. Gauges that describe *state* are sampled here
    rather than tracked on every write — one query at scrape time cannot drift
    from reality the way an incrementing counter can."""
    try:
        metrics.observe("outline_panel_keys", len(await db.all_keys()))
        metrics.observe("outline_panel_servers", len(reg.ids()))
        metrics.observe("outline_panel_credit_drift", len(await db.credit_drift()))
    except Exception:  # noqa: BLE001 — a scrape must not fail on a busy database
        log.exception("could not sample state for /metrics")
    metrics.observe("outline_panel_uptime_seconds", time.time() - metrics.STARTED)
    return PlainTextResponse(metrics.render(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")
