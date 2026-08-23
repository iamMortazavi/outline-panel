"""Server registry management + per-server settings."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core import errors
from ...core.outline_api import OutlineAPI, OutlineError, parse_access_config
from ...core.utils import gb_to_bytes
from ...ports.node import MetricsCapable, ServerAdminCapable
from ..deps import api_or_404, current_admin, db, enforce_scope, host, reg, require, scoped_ids
from ..schemas import (
    HealthHistory,
    HealthSummary,
    Ok,
    ServerCreated,
    ServerList,
    ServerSettings,
)

router = APIRouter(prefix="/api", tags=["servers"],
                   dependencies=[Depends(enforce_scope)])


class ServerBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    apiUrl: str = Field(min_length=1)
    # "outline" (the default, and every server that existed before this) or
    # "xray". The rest of the panel never branches on it — only the registry's
    # factory does.
    kind: str = Field(default="outline", pattern="^(outline|xray)$")
    # Xray only: which inbound to put customers in, and the parameters a
    # customer's vless:// link is built from. Ignored for Outline, whose whole
    # configuration is the access URL.
    inboundTag: str = Field(default="", max_length=120)
    link: dict = Field(default_factory=dict)


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LimitBody(BaseModel):
    limit_gb: float = Field(ge=0)


class MetricsBody(BaseModel):
    enabled: bool


async def _server_info(sid: str) -> dict:
    m = reg.meta(sid)
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


@router.get("/servers/health", response_model=HealthSummary, response_model_exclude_unset=True)
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


@router.get("/servers/{sid}/health", response_model=HealthHistory, response_model_exclude_unset=True)
async def server_health(sid: str, limit: int = 100):
    """Recent probe results for one server, newest first."""
    api_or_404(sid)
    return {"history": await db.health_history(sid, max(1, min(500, limit)))}


@router.get("/servers", response_model=ServerList,
            response_model_exclude_unset=True)
async def list_servers(admin: dict = Depends(current_admin)):
    # No {sid}, so enforce_scope does not cover this one: filter by hand or the
    # whole panel's servers leak into a scoped admin's list.
    # Probe concurrently (like stats.py): serially, N unreachable servers each
    # burn the full 15s timeout and the whole list times out behind a proxy.
    return {"servers": list(await asyncio.gather(
        *[_server_info(sid) for sid in scoped_ids(admin)]
    ))}


@router.post("/servers", response_model=ServerCreated, response_model_exclude_unset=True,
             dependencies=[Depends(require("servers.manage"))])
async def add_server(body: ServerBody):
    """Register a server, of whichever kind it is.

    Both paths prove the server is reachable before it is stored. A server that
    cannot be reached at the moment it is added is nearly always a typo in the
    address or a firewall, and finding that out now is much cheaper than finding
    it out when the first customer is sold onto it.
    """
    if body.kind == "xray":
        return await _add_xray(body)
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


async def _add_xray(body: ServerBody):
    """An Xray node: the gRPC API address, the inbound, and the link parameters.

    `apiUrl` here is `host:port` of Xray's API inbound — usually reached over a
    tunnel rather than the open internet, which is also why it is cleartext h2c
    and not HTTPS. It is deliberately the same field as Outline's URL: one
    column, one form, one thing an operator pastes.
    """
    raw = (body.apiUrl or "").strip()
    for prefix in ("grpc://", "http://", "tcp://"):
        raw = raw[len(prefix):] if raw.startswith(prefix) else raw
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise HTTPException(
            status_code=400,
            detail="Enter the Xray API as host:port, e.g. 127.0.0.1:10085")
    tag = body.inboundTag.strip() or "vless-in"
    try:
        from ...core.xray.api import XrayAPI
    except ImportError:
        # The h2 extra is not installed. Say which command fixes it rather than
        # letting an ImportError reach the browser as a 500.
        raise HTTPException(
            status_code=400,
            detail="Xray support needs the h2 package: pip install 'outline-panel[xray]'")
    probe = XrayAPI(host, int(port), tag, body.link or {})
    try:
        await probe.get_server_info()
        await probe.list_keys()          # also proves the inbound tag is right
    except OutlineError as e:
        raise HTTPException(status_code=400, detail=f"Could not reach Xray: {e}")
    finally:
        await probe.close()
    sid = uuid.uuid4().hex[:8]
    await reg.add(sid, body.name, f"{host}:{port}", None, kind="xray",
                  config=json.dumps({"inbound_tag": tag, "link": body.link or {}}))
    return {"ok": True, "id": sid}


@router.put("/servers/{sid}", response_model=Ok, response_model_exclude_unset=True,
            dependencies=[Depends(require("servers.manage"))])
async def rename_server_local(sid: str, body: NameBody):
    if not reg.meta(sid):
        raise errors.unknown_server()
    await db.rename_server_local(sid, body.name)
    reg.servers[sid]["name"] = body.name
    return {"ok": True}


@router.delete("/servers/{sid}", response_model=Ok, response_model_exclude_unset=True,
               dependencies=[Depends(require("servers.manage"))])
async def delete_server(sid: str):
    if not reg.meta(sid):
        raise errors.unknown_server()
    await reg.remove(sid)
    return {"ok": True}


# ----------------------------------------------------- per-server settings
@router.get("/servers/{sid}/settings", response_model=ServerSettings, response_model_exclude_unset=True)
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
    if isinstance(api, MetricsCapable):
        try:
            out["metricsEnabled"] = await api.get_metrics_enabled()
        except OutlineError:
            pass
    return out


@router.put("/servers/{sid}/settings/metrics", response_model=Ok, response_model_exclude_unset=True,
            dependencies=[Depends(require("servers.manage"))])
async def set_metrics(sid: str, body: MetricsBody):
    api = api_or_404(sid)
    if not isinstance(api, MetricsCapable):
        raise HTTPException(
            status_code=400,
            detail="This server does not support metrics sharing.")
    try:
        await api.set_metrics_enabled(body.enabled)
    except OutlineError as e:
        raise errors.upstream(str(e))
    return {"ok": True}


@router.put("/servers/{sid}/settings/global-limit", response_model=Ok, response_model_exclude_unset=True,
            dependencies=[Depends(require("servers.manage"))])
async def set_global_limit(sid: str, body: LimitBody):
    api = api_or_404(sid)
    if not isinstance(api, ServerAdminCapable):
        raise HTTPException(
            status_code=400,
            detail="This server does not support a server-wide data limit.")
    try:
        if body.limit_gb > 0:
            await api.set_global_data_limit(gb_to_bytes(body.limit_gb))
        else:
            await api.remove_global_data_limit()
    except OutlineError as e:
        raise errors.upstream(str(e))
    return {"ok": True}


@router.put("/servers/{sid}/settings/name", response_model=Ok, response_model_exclude_unset=True,
            dependencies=[Depends(require("servers.manage"))])
async def set_server_name(sid: str, body: NameBody):
    api = api_or_404(sid)
    if not isinstance(api, ServerAdminCapable):
        raise HTTPException(
            status_code=400,
            detail="This server does not support renaming itself.")
    try:
        await api.rename_server(body.name)
    except OutlineError as e:
        raise errors.upstream(str(e))
    return {"ok": True}
