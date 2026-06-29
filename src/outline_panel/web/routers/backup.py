"""Download / restore a full panel backup (servers, keys, settings) as JSON."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from ..deps import db, reg, require_session, settings

router = APIRouter(prefix="/api", tags=["backup"],
                   dependencies=[Depends(require_session)])


@router.get("/backup")
async def download_backup():
    data = await db.export_all()
    # The backup contains secrets (tokens, password hash) — never let a proxy or
    # the browser cache it to disk.
    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": "attachment; filename=outline-panel-backup.json",
            "Cache-Control": "no-store",
        },
    )


@router.post("/restore")
async def restore_backup(payload: dict):
    if not isinstance(payload, dict) or "keys" not in payload or "servers" not in payload:
        raise HTTPException(status_code=400, detail="Not a valid backup file")
    await db.import_all(payload)
    # rebuild in-memory state from the restored DB
    settings._cache.clear()
    await reg.close_all()
    reg.servers.clear()
    await reg.load()
    return {"ok": True,
            "servers": len(payload.get("servers", [])),
            "keys": len(payload.get("keys", []))}
