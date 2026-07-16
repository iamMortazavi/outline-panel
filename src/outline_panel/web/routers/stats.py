"""Aggregated server statistics."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from ...core.outline_api import OutlineError
from ..deps import current_admin, reg, require, sids_or_404

router = APIRouter(prefix="/api", tags=["stats"],
                   dependencies=[Depends(require("keys.view"))])


async def _stats_for(sid: str) -> dict:
    m = reg.meta(sid)
    if m is None:  # server removed between snapshot and fetch
        return {"id": sid, "name": None, "available": False, "tunnelSec": 0,
                "dataBytes": 0, "bwCurrent": 0, "bwPeak": 0, "bwTs": None,
                "locations": []}
    api = m["api"]
    try:
        sm = await api.get_server_metrics_cached("30d")
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


@router.get("/stats")
async def stats(server: str | None = None,
                admin: dict = Depends(current_admin)):
    sids = sids_or_404(server, admin)
    per = await asyncio.gather(*[_stats_for(s) for s in sids]) if sids else []
    any_avail = any(p["available"] for p in per)
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
        "available": any_avail,
        "serverCount": len(per),
        "tunnelSec": sum(p["tunnelSec"] for p in per),
        "dataBytes": sum(p["dataBytes"] for p in per),
        "bwCurrent": sum(p["bwCurrent"] for p in per),
        "bwPeak": sum(p["bwPeak"] for p in per),
        "bwTs": max([p.get("bwTs") or 0 for p in per], default=0) or None,
        "locations": locations,
        "perServer": per,
    }
