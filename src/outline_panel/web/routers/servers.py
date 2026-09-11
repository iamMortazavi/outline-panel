"""Server registry management + per-server settings."""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core import errors
from ...core.concurrency import map_concurrently
from ...core.outline_api import OutlineAPI, OutlineError, parse_access_config
from ...core.utils import gb_to_bytes
from .. import subcache
from ..deps import api_or_404, current_admin, db, enforce_scope, host, reg, require, scoped_ids

router = APIRouter(prefix="/api", tags=["servers"],
                   dependencies=[Depends(enforce_scope)])


class ServerBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    apiUrl: str = Field(min_length=1)


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LimitBody(BaseModel):
    limit_gb: float = Field(ge=0)


class MetricsBody(BaseModel):
    enabled: bool


async def _server_info(sid: str) -> dict:
    m = reg.meta(sid)
    if m is None:   # removed between listing the ids and asking about them
        return {"id": sid, "name": None, "host": "", "reachable": False,
                "serverName": None, "version": None}
    info, reachable = {}, False
    try:
        info = await m["api"].get_server_info()
        reachable = True
    except OutlineError:
        pass
    return {
        "id": sid, "name": m["name"], "host": host(m["api_url"]),
        "reachable": reachable,
        "serverName": info.get("name"), "version": info.get("version"),
    }


@router.get("/servers/health")
async def servers_health(days: int = 7, admin: dict = Depends(current_admin)):
    """Uptime and mean latency per server over the window, plus how many checks
    each has failed in a row right now."""
    days = max(1, min(90, days))
    since = int(time.time()) - days * 86400
    summary = await db.health_summary(since)
    out = []
    for sid in scoped_ids(admin):
        row = summary.get(sid, {"probes": 0, "ok": 0, "uptimePct": None,
                                "avgLatencyMs": None})
        out.append({"id": sid, "name": (reg.meta(sid) or {}).get("name"),
                    "failingNow": await db.consecutive_failures(sid), **row})
    return {"days": days, "servers": out}


@router.get("/servers/{sid}/health")
async def server_health(sid: str, limit: int = 100):
    """Recent probe results for one server, newest first."""
    api_or_404(sid)
    return {"history": await db.health_history(sid, max(1, min(500, limit)))}


@router.get("/servers")
async def list_servers(admin: dict = Depends(current_admin)):
    # No {sid}, so enforce_scope does not cover this one: filter by hand or the
    # whole panel's servers leak into a scoped admin's list.
    # Probe concurrently (like stats.py): serially, N unreachable servers each
    # burn the full 15s timeout and the whole list times out behind a proxy.
    return {"servers": await map_concurrently(scoped_ids(admin), _server_info)}


@router.post("/servers", dependencies=[Depends(require("servers.manage"))])
async def add_server(body: ServerBody):
    try:
        url, cert_sha256 = parse_access_config(body.apiUrl)
    except OutlineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    probe = OutlineAPI(url, cert_sha256)
    try:
        await probe.get_server_info()
    except OutlineError as e:
        await probe.close()
        raise HTTPException(status_code=400, detail=f"Could not reach server: {e}")
    await probe.close()
    sid = uuid.uuid4().hex[:8]
    await reg.add(sid, body.name, url, cert_sha256)
    return {"ok": True, "id": sid}


@router.put("/servers/{sid}", dependencies=[Depends(require("servers.manage"))])
async def rename_server_local(sid: str, body: NameBody):
    if not reg.meta(sid):
        raise errors.unknown_server()
    await db.rename_server_local(sid, body.name)
    reg.servers[sid]["name"] = body.name
    return {"ok": True}


@router.delete("/servers/{sid}", dependencies=[Depends(require("servers.manage"))])
async def delete_server(sid: str):
    if not reg.meta(sid):
        raise errors.unknown_server()
    # Removing a server drops its key rows, which changes the membership of
    # every subscription that had a config on it — and a cached summary went on
    # handing that config out afterwards, for a server this panel no longer
    # manages. Same rule as unlinking one: collect the tokens while the rows are
    # still there, then drop their cached copies.
    tokens = {k["sub_token"] for k in await db.keys_for(sid) if k.get("sub_token")}
    await reg.remove(sid)
    for token in tokens:
        subcache.invalidate(token)
    return {"ok": True}


# ----------------------------------------------------- per-server settings
@router.get("/servers/{sid}/settings")
async def get_server_settings(sid: str):
    api = api_or_404(sid)
    out = {"id": sid, "label": reg.meta(sid)["name"], "host": host(reg.meta(sid)["api_url"]),
           "name": None, "version": None, "metricsEnabled": None, "globalLimit": None}
    try:
        info = await api.get_server_info()
        out["name"] = info.get("name")
        out["version"] = info.get("version")
        out["globalLimit"] = (info.get("accessKeyDataLimit") or {}).get("bytes")
    except OutlineError:
        pass
    try:
        out["metricsEnabled"] = await api.get_metrics_enabled()
    except OutlineError:
        pass
    return out


@router.put("/servers/{sid}/settings/metrics", dependencies=[Depends(require("servers.manage"))])
async def set_metrics(sid: str, body: MetricsBody):
    api = api_or_404(sid)
    try:
        await api.set_metrics_enabled(body.enabled)
    except OutlineError as e:
        raise errors.upstream(str(e))
    return {"ok": True}


@router.put("/servers/{sid}/settings/global-limit", dependencies=[Depends(require("servers.manage"))])
async def set_global_limit(sid: str, body: LimitBody):
    api = api_or_404(sid)
    try:
        if body.limit_gb > 0:
            await api.set_global_data_limit(gb_to_bytes(body.limit_gb))
        else:
            await api.remove_global_data_limit()
    except OutlineError as e:
        raise errors.upstream(str(e))
    return {"ok": True}


@router.put("/servers/{sid}/settings/name", dependencies=[Depends(require("servers.manage"))])
async def set_server_name(sid: str, body: NameBody):
    api = api_or_404(sid)
    try:
        await api.rename_server(body.name)
    except OutlineError as e:
        raise errors.upstream(str(e))
    return {"ok": True}
