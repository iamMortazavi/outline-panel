"""
Scheduled database snapshots.

Backup was a button the owner had to remember to press, which is the same as no
backup. This runs on the scheduler's pass.

`VACUUM INTO` rather than copying the file: it is SQLite's own primitive for
exactly this, it takes a consistent snapshot without stopping writers, and it
cannot catch the database mid-transaction the way `cp` can — with WAL, copying
the `.db` alone silently loses everything still in the `-wal` file.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger("backup")

PREFIX = "outline-panel-"
SUFFIX = ".db"
# Nothing here interpolates user input, but the filename is later handed back to
# a download route, so the shape is pinned rather than trusted.
_NAME = re.compile(r"^outline-panel-\d{8}-\d{6}\.db$")


def backup_dir(raw: str | None, db_path: str) -> Path:
    """Where snapshots live: the configured path, else a `backups/` beside the
    database, which is the one directory the panel is already known to own."""
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path(db_path).resolve().parent / "backups"


def is_backup_name(name: str) -> bool:
    return bool(_NAME.match(name))


def list_backups(directory: Path) -> list[dict]:
    """Newest first, ordered by the stamp in the filename.

    Not by mtime: the stamps are zero-padded, so lexicographic order is
    chronological, and the name is the only record of when a snapshot was
    *taken*. An mtime is whatever last touched the file — a restore, an rsync,
    or several snapshots landing inside the same second, which made rotation
    pick an arbitrary victim and could delete the newest one.
    """
    if not directory.is_dir():
        return []
    out = []
    for p in directory.iterdir():
        if not p.is_file() or not is_backup_name(p.name):
            continue
        out.append({"name": p.name, "bytes": p.stat().st_size,
                    "ts": int(p.stat().st_mtime)})
    return sorted(out, key=lambda r: r["name"], reverse=True)


async def make_backup(db, directory: Path, keep: int, now: int | None = None) -> dict:
    """Write one snapshot and drop the oldest beyond `keep`. Returns its record."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now or time.time()))
    target = directory / f"{PREFIX}{stamp}{SUFFIX}"
    if target.exists():          # same second — nothing to do
        return {"name": target.name, "bytes": target.stat().st_size,
                "ts": int(target.stat().st_mtime), "skipped": True}
    tmp = target.with_suffix(".partial")
    tmp.unlink(missing_ok=True)
    # Not a bound parameter: VACUUM INTO takes a literal. The path is built from
    # a configured directory plus a strftime stamp, never from a request.
    await db.conn.execute(f"VACUUM INTO '{str(tmp)}'")
    # Rename last, so a crash mid-VACUUM leaves a .partial rather than a
    # truncated file that looks like a usable backup.
    os.replace(tmp, target)
    prune(directory, keep)
    rec = {"name": target.name, "bytes": target.stat().st_size,
           "ts": int(target.stat().st_mtime), "skipped": False}
    log.info("backup written: %s (%d bytes)", rec["name"], rec["bytes"])
    return rec


def prune(directory: Path, keep: int) -> int:
    dropped = 0
    for old in list_backups(directory)[max(1, keep):]:
        try:
            (directory / old["name"]).unlink()
            dropped += 1
        except OSError as e:  # noqa: PERF203 — one bad file must not stop the rest
            log.warning("could not remove old backup %s: %s", old["name"], e)
    return dropped


async def maybe_backup(db, settings, db_path: str, now: int) -> None:
    """Called every scheduler pass; writes at most one snapshot per interval.

    The last run is kept in `settings` rather than in memory so it survives a
    restart — otherwise a panel that restarts often would back up every time it
    came up, and one that never restarts would still be fine, which is the wrong
    way round.
    """
    if not await settings.get_bool("backup_enabled", True):
        return
    every = await settings.num("backup_interval_hours") * 3600
    try:
        last = int(await settings.get("backup_last_ts") or 0)
    except (TypeError, ValueError):
        last = 0
    if now - last < every:
        return
    directory = backup_dir(await settings.get("backup_dir"), db_path)
    await make_backup(db, directory, await settings.num("backup_keep"), now)
    await settings.set("backup_last_ts", str(now))
