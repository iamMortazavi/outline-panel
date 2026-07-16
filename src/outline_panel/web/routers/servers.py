"""Server registry management + per-server settings."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core.outline_api import OutlineAPI, OutlineError, parse_access_config
from ...core.utils import gb_to_bytes
from ..deps import api_or_404, db, host, reg, require_session

router = APIRouter(prefix="/api", tags=["servers"],
                   dependencies=[Depends(require_session)])


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


@router.get("/servers")
async def list_servers():
    # Probe concurrently (like stats.py): serially, N unreachable servers each
    # burn the full 15s timeout and the whole list times out behind a proxy.
    return {"servers": list(await asyncio.gather(
        *[_server_info(sid) for sid in reg.ids()]
    ))}


@router.post("/servers")
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


@router.put("/servers/{sid}")
async def rename_server_local(sid: str, body: NameBody):
    if not reg.meta(sid):
        raise HTTPException(status_code=404, detail="Unknown server")
    await db.rename_server_local(sid, body.name)
    reg.servers[sid]["name"] = body.name
    return {"ok": True}


@router.delete("/servers/{sid}")
async def delete_server(sid: str):
    if not reg.meta(sid):
        raise HTTPException(status_code=404, detail="Unknown server")
    await reg.remove(sid)
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


@router.put("/servers/{sid}/settings/metrics")
async def set_metrics(sid: str, body: MetricsBody):
    api = api_or_404(sid)
    try:
        await api.set_metrics_enabled(body.enabled)
    except OutlineError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


@router.put("/servers/{sid}/settings/global-limit")
async def set_global_limit(sid: str, body: LimitBody):
    api = api_or_404(sid)
    try:
        if body.limit_gb > 0:
            await api.set_global_data_limit(gb_to_bytes(body.limit_gb))
        else:
            await api.remove_global_data_limit()
    except OutlineError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


@router.put("/servers/{sid}/settings/name")
async def set_server_name(sid: str, body: NameBody):
    api = api_or_404(sid)
    try:
        await api.rename_server(body.name)
    except OutlineError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}
