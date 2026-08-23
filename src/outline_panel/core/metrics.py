"""
Counters, and the Prometheus text format.

Hand-rolled rather than pulling in `prometheus_client`: the exposition format is
a dozen lines of text, and the alternative is a dependency plus a registry
object plus a multiprocess mode to think about. If this ever needs histograms
with proper bucket semantics, take the library then.

Process-local on purpose. Each worker reports its own numbers and the scraper
sums them, which is how Prometheus expects to see a multi-process service.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_counters: dict[tuple[str, tuple], float] = {}
_gauges: dict[tuple[str, tuple], float] = {}

# name -> (help, type). Kept beside the values so the exposition can carry HELP
# and TYPE lines, which is what makes a metric readable in a dashboard.
_META: dict[str, tuple[str, str]] = {}

STARTED = time.time()


def _key(name: str, labels: dict | None) -> tuple[str, tuple]:
    return name, tuple(sorted((labels or {}).items()))


def declare(name: str, help_text: str, kind: str = "counter") -> None:
    _META[name] = (help_text, kind)


def inc(name: str, labels: dict | None = None, by: float = 1.0) -> None:
    with _lock:
        _counters[_key(name, labels)] = _counters.get(_key(name, labels), 0.0) + by


def observe(name: str, value: float, labels: dict | None = None) -> None:
    """Last-value gauge. Not a histogram: a p99 needs buckets, and this is here
    to answer "is Outline slow right now", which a last-value plus a count of
    failures already does."""
    with _lock:
        _gauges[_key(name, labels)] = value


def _escape(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render() -> str:
    """The whole registry in Prometheus text exposition format."""
    with _lock:
        counters = dict(_counters)
        gauges = dict(_gauges)
    out: list[str] = []
    for store, default_kind in ((counters, "counter"), (gauges, "gauge")):
        by_name: dict[str, list[tuple[tuple, float]]] = {}
        for (name, labels), value in store.items():
            by_name.setdefault(name, []).append((labels, value))
        for name, rows in sorted(by_name.items()):
            help_text, kind = _META.get(name, ("", default_kind))
            if help_text:
                out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")
            for labels, value in sorted(rows):
                if labels:
                    rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                    out.append(f"{name}{{{rendered}}} {value:g}")
                else:
                    out.append(f"{name} {value:g}")
    return "\n".join(out) + "\n"


def reset() -> None:
    """Tests only."""
    with _lock:
        _counters.clear()
        _gauges.clear()


declare("outline_panel_http_requests_total", "HTTP requests served.")
declare("outline_panel_http_request_seconds", "Duration of the last request.", "gauge")
declare("outline_panel_keys_created_total", "Access keys created.")
declare("outline_panel_keys_deleted_total", "Access keys deleted.")
declare("outline_panel_keys_capped_total",
        "Keys cut off for reaching their allowance on a backend that cannot "
        "enforce a limit itself.")
declare("outline_panel_keys_rotated_total", "Access keys replaced for a customer.")
declare("outline_panel_credit_charged_total", "Credit taken for purchases.")
declare("outline_panel_credit_added_total", "Credit granted (top-ups and reversals).")
declare("outline_panel_outline_calls_total", "Calls to an Outline server, by outcome.")
declare("outline_panel_outline_seconds", "Duration of the last Outline call.", "gauge")
declare("outline_panel_scheduler_passes_total", "Completed scheduler passes.")
declare("outline_panel_scheduler_leader", "1 when this process holds the lease.", "gauge")
declare("outline_panel_keys", "Access keys known to the panel.", "gauge")
declare("outline_panel_servers", "Configured Outline servers.", "gauge")
declare("outline_panel_outbox_depth",
        "Effects the panel has decided but a server has not been told yet.",
        "gauge")
declare("outline_panel_credit_drift", "Admins whose balance disagrees with their ledger.",
        "gauge")
declare("outline_panel_uptime_seconds", "Seconds since this process started.", "gauge")
