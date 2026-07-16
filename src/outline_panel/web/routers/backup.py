"""Download / restore a full panel backup (servers, keys, settings) as JSON."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from ...core.settings import BOT_ENABLED, BOT_TOKEN
from ..deps import botmgr, db, reg, require_session, settings

log = logging.getLogger("webapp")

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
    # "settings" is not optional: import_all wipes the table, so a payload
    # without it would restore a panel with no admin password — unloggable-into.
    for field in ("servers", "keys", "settings"):
        if payload.get(field) is None:
            raise HTTPException(status_code=400,
                                detail=f"Not a valid backup file (missing {field})")
    try:
        await db.import_all(payload)
    except Exception as e:  # noqa: BLE001 — a bad row is a bad file, and the DB rolled back
        raise HTTPException(status_code=400, detail=f"Not a valid backup file: {e}")
    # rebuild in-memory state from the restored DB
    settings._cache.clear()
    await reg.close_all()
    reg.servers.clear()
    await reg.load()
    # the restored bot token may differ from the one currently polling
    try:
        token = await settings.get(BOT_TOKEN)
        if await settings.get_bool(BOT_ENABLED) and token:
            await botmgr.start(token)
        else:
            await botmgr.stop()
    except Exception as e:  # noqa: BLE001 — a bad token must not fail the restore
        log.warning("bot did not restart after restore: %s", e)
    return {"ok": True,
            "servers": len(payload["servers"]),
            "keys": len(payload["keys"])}
