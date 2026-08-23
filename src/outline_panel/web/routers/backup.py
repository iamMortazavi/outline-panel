"""Download / restore a full panel backup (servers, keys, settings) as JSON,
plus the scheduled on-disk snapshots."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from ...core import backup as backup_core
from ...core.settings import BOT_ENABLED, BOT_TOKEN
from ..deps import botmgr, db, reg, require_owner, settings
from ..schemas import Ok, Restored, Snapshot, SnapshotList

log = logging.getLogger("webapp")

router = APIRouter(prefix="/api", tags=["backup"],
                   dependencies=[Depends(require_owner)])


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


@router.post("/restore", response_model=Restored, response_model_exclude_unset=True)
async def restore_backup(payload: dict):
    # "settings" is not optional: import_all wipes the table, so a payload
    # without it would restore a panel with no admin password — unloggable-into.
    # Every table import_all wipes must be present, or the restore silently
    # erases it: no settings meant no admin password, and now no admins/ledger
    # would mean no logins and no record of anyone's credit.
    for field in ("servers", "keys", "settings", "admins", "packages", "ledger"):
        if payload.get(field) is None:
            raise HTTPException(status_code=400,
                                detail=f"Not a valid backup file (missing {field})")
    try:
        await db.import_all(payload)
    except Exception as e:  # noqa: BLE001 — a bad row is a bad file, and the DB rolled back
        raise HTTPException(status_code=400, detail=f"Not a valid backup file: {e}")
    # rebuild in-memory state from the restored DB (settings need no clearing:
    # the store reads through to SQLite)
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


# ------------------------------------------------ scheduled disk snapshots
async def _dir():
    return backup_core.backup_dir(await settings.get("backup_dir"), db.path)


@router.get("/snapshots", response_model=SnapshotList, response_model_exclude_unset=True)
async def list_snapshots():
    directory = await _dir()
    return {
        "enabled": await settings.get_bool("backup_enabled", True),
        "dir": str(directory),
        "everyHours": await settings.num("backup_interval_hours"),
        "keep": await settings.num("backup_keep"),
        "lastTs": int(await settings.get("backup_last_ts") or 0),
        "snapshots": backup_core.list_backups(directory),
    }


@router.post("/snapshots", response_model=Snapshot, response_model_exclude_unset=True)
async def make_snapshot():
    """Take one now, without waiting for the schedule."""
    directory = await _dir()
    try:
        rec = await backup_core.make_backup(
            db, directory, await settings.num("backup_keep"))
    except Exception as e:  # noqa: BLE001 — a full or read-only disk is a 400
        raise HTTPException(status_code=400, detail=f"Could not write a backup: {e}")
    await settings.set("backup_last_ts", str(int(time.time())))
    return rec


@router.get("/snapshots/{name}")
async def download_snapshot(name: str):
    """Send one snapshot.

    `name` is checked against the generated filename pattern rather than
    sanitised: this joins a caller-supplied string onto a server path, and an
    allowlist is the only version of that check with no clever way around it.
    """
    if not backup_core.is_backup_name(name):
        raise HTTPException(status_code=404, detail="Unknown backup")
    path = (await _dir()) / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown backup")
    return FileResponse(path, filename=name, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})


@router.delete("/snapshots/{name}", response_model=Ok, response_model_exclude_unset=True)
async def delete_snapshot(name: str):
    if not backup_core.is_backup_name(name):
        raise HTTPException(status_code=404, detail="Unknown backup")
    path = (await _dir()) / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown backup")
    path.unlink()
    return {"ok": True}
