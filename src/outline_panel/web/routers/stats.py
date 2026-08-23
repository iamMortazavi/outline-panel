"""Aggregated server statistics."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from ...core.outline_api import OutlineError
from ...ports.node import MetricsCapable
from ..deps import current_admin, reg, require, settings, sids_or_404
from ..schemas import Stats

router = APIRouter(prefix="/api", tags=["stats"],
                   dependencies=[Depends(require("keys.view"))])


async def _stats_for(sid: str, ttl: int) -> dict:
    m = reg.meta(sid)
    if m is None:  # server removed between snapshot and fetch
        return {"id": sid, "name": None, "available": False, "tunnelSec": 0,
                "dataBytes": 0, "bwCurrent": 0, "bwPeak": 0, "bwTs": None,
                "locations": []}
    api = m["api"]
    if not isinstance(api, MetricsCapable):
        # A backend with no advanced-metrics surface at all. Reported as
        # unavailable, which is the same thing the dashboard already renders
        # for an Outline server with metrics sharing switched off.
        sm, avail = {}, False
    else:
        try:
            sm = await api.get_server_metrics_cached("30d", ttl)
            avail = True
        except OutlineError:
            sm, avail = {}, False
    srv = sm.get("server", {}) or {}
    bw = srv.get("bandwidth", {}) or {}
    return {
        "id": sid, "name": m["name"], "available": avail,
        "tunnelSec": (srv.get("tunnelTime") or {}).get("seconds") or 0,
        "dataBytes": (srv.get("dataTransferred") or {}).get("bytes") or 0,
        "bwCurrent": ((bw.get("current") or {}).get("data") or {}).get("bytes") or 0,
        "bwPeak": ((bw.get("peak") or {}).get("data") or {}).get("bytes") or 0,
        # timestamp of the current-bandwidth sample; Outline only refreshes it
        # every ~minute, so the UI uses it to add a graph point only on change.
        "bwTs": (bw.get("current") or {}).get("timestamp"),
        "locations": srv.get("locations", []) or [],
    }


def aggregate(per: list[dict]) -> dict:
    """Roll per-server samples into the shape the dashboard reads.

    Pulled out of the route so the live stream can reuse it: the stream sends a
    sub-admin the totals for *their* servers only, which means re-aggregating a
    subset of the same samples. Two implementations of this would drift, and the
    symptom would be a reseller's KPI row quietly disagreeing with their own key
    list.
    """
    locmap: dict = {}
    for p in per:
        for loc in p["locations"]:
            key = (loc.get("location"), loc.get("asn"))
            e = locmap.setdefault(key, {"location": loc.get("location"), "asn": loc.get("asn"),
                                        "asOrg": loc.get("asOrg"), "bytes": 0})
            e["bytes"] += (loc.get("dataTransferred") or {}).get("bytes") or 0
    locations = [{"location": v["location"], "asn": v["asn"], "asOrg": v["asOrg"],
                  "dataTransferred": {"bytes": v["bytes"]}} for v in locmap.values()]
    return {
        "available": any(p["available"] for p in per),
        "serverCount": len(per),
        "tunnelSec": sum(p["tunnelSec"] for p in per),
        "dataBytes": sum(p["dataBytes"] for p in per),
        "bwCurrent": sum(p["bwCurrent"] for p in per),
        "bwPeak": sum(p["bwPeak"] for p in per),
        "bwTs": max([p.get("bwTs") or 0 for p in per], default=0) or None,
        "locations": locations,
        "perServer": per,
    }


async def sample(sids: list[str], ttl: int) -> list[dict]:
    """One reading per server. The stream's sampler calls this directly."""
    return list(await asyncio.gather(*[_stats_for(s, ttl) for s in sids])) if sids else []


@router.get("/stats", response_model=Stats, response_model_exclude_unset=True)
async def stats(server: str | None = None,
                admin: dict = Depends(current_admin)):
    sids = sids_or_404(server, admin)
    return aggregate(await sample(sids, await settings.num("metrics_ttl")))
