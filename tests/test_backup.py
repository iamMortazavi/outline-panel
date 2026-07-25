"""
Scheduled snapshots. Backup used to be a button someone had to remember, which
is the same as no backup.
"""
import os
import sys
import tempfile

import httpx
import pytest

from outline_panel.core import backup as bk
from outline_panel.core.db import DB


@pytest.fixture
async def db():
    d = DB(os.path.join(tempfile.mkdtemp(), "b.db"))
    await d.init()
    await d.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    await d.add_key("s1", "1", "Ali", 1024, 30)
    yield d
    await d.close()


async def test_a_snapshot_is_a_usable_database(db):
    """VACUUM INTO, not a file copy: with WAL, copying the .db alone silently
    loses everything still in the -wal file."""
    out = os.path.join(tempfile.mkdtemp(), "snaps")
    rec = await bk.make_backup(db, bk.Path(out), keep=5)
    assert rec["bytes"] > 0 and not rec["skipped"]

    restored = DB(os.path.join(out, rec["name"]))
    await restored.init()
    assert [s["name"] for s in await restored.all_servers()] == ["Tokyo"]
    assert (await restored.get_key("s1", "1"))["name"] == "Ali"
    await restored.close()


async def test_rotation_keeps_the_newest(db):
    """Six snapshots land in the same second of real time, so mtime cannot
    order them — the stamp in the name is what says which is newest."""
    out = bk.Path(os.path.join(tempfile.mkdtemp(), "snaps"))
    made = [(await bk.make_backup(db, out, keep=3,
                                  now=1_700_000_000 + i * 3600))["name"]
            for i in range(6)]
    names = [b["name"] for b in bk.list_backups(out)]
    assert names == sorted(made, reverse=True)[:3]
    assert len(names) == 3


async def test_a_crash_mid_vacuum_leaves_no_usable_looking_file(db):
    """The rename is last on purpose: a truncated file that passes the name
    check would look like a backup you could restore from."""
    out = bk.Path(os.path.join(tempfile.mkdtemp(), "snaps"))
    await bk.make_backup(db, out, keep=5)
    partials = [p.name for p in out.iterdir() if p.suffix == ".partial"]
    assert partials == []
    assert all(bk.is_backup_name(p.name) for p in out.iterdir())


async def test_the_schedule_writes_at_most_one_per_interval(db):
    from outline_panel.core.settings import SettingsStore
    s = SettingsStore(db)
    out = os.path.join(tempfile.mkdtemp(), "snaps")
    await s.set("backup_dir", out)
    await s.set("backup_interval_hours", "24")

    now = 1_700_000_000
    await bk.maybe_backup(db, s, db.path, now)
    await bk.maybe_backup(db, s, db.path, now + 3600)      # too soon
    assert len(bk.list_backups(bk.Path(out))) == 1
    await bk.maybe_backup(db, s, db.path, now + 25 * 3600)  # a day later
    assert len(bk.list_backups(bk.Path(out))) == 2


async def test_backups_can_be_switched_off(db):
    from outline_panel.core.settings import SettingsStore
    s = SettingsStore(db)
    out = os.path.join(tempfile.mkdtemp(), "snaps")
    await s.set("backup_dir", out)
    await s.set_bool("backup_enabled", False)
    await bk.maybe_backup(db, s, db.path, 1_700_000_000)
    assert bk.list_backups(bk.Path(out)) == []


async def test_the_last_run_survives_a_restart(db):
    """Kept in settings, not memory: otherwise a panel that restarts often backs
    up every time it comes up."""
    from outline_panel.core.settings import SettingsStore
    out = os.path.join(tempfile.mkdtemp(), "snaps")
    s = SettingsStore(db)
    await s.set("backup_dir", out)
    await bk.maybe_backup(db, s, db.path, 1_700_000_000)
    fresh = SettingsStore(db)                     # a "new process"
    await bk.maybe_backup(db, fresh, db.path, 1_700_000_000 + 60)
    assert len(bk.list_backups(bk.Path(out))) == 1


# ------------------------------------------------------------------ the API
@pytest.fixture
async def app():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    await deps.settings.set("backup_dir", os.path.join(tempfile.mkdtemp(), "snaps"))
    yield appmod.app, deps
    await deps.db.close()


async def _login(application, user="admin", pw="pw"):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://x")
    assert (await c.post("/api/login", json={"username": user, "password": pw})).status_code == 200
    return c


async def test_the_owner_can_take_list_download_and_delete(app):
    application, deps = app
    c = await _login(application)
    rec = (await c.post("/api/snapshots")).json()
    assert rec["bytes"] > 0

    listed = (await c.get("/api/snapshots")).json()
    assert [s["name"] for s in listed["snapshots"]] == [rec["name"]]
    assert listed["enabled"] is True

    got = await c.get(f"/api/snapshots/{rec['name']}")
    assert got.status_code == 200 and got.content[:15] == b"SQLite format 3"
    assert got.headers["cache-control"] == "no-store"

    assert (await c.delete(f"/api/snapshots/{rec['name']}")).status_code == 200
    assert (await c.get("/api/snapshots")).json()["snapshots"] == []
    await c.aclose()


@pytest.mark.parametrize("name", [
    "../../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "outline_bot.db",
    "outline-panel-20240101-000000.db/../../secret", "....//outline-panel-1.db",
])
async def test_the_download_route_refuses_anything_it_did_not_write(app, name):
    """This joins a caller-supplied string onto a server path — an allowlist on
    the generated filename shape is the only check with no way around it."""
    application, deps = app
    c = await _login(application)
    r = await c.get(f"/api/snapshots/{name}")
    assert r.status_code in (404, 307, 405), r.status_code
    if r.status_code == 200:
        raise AssertionError("path traversal")
    await c.aclose()


async def test_snapshots_are_owner_only(app):
    application, deps = app
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    await deps.db.add_admin("sara", h, s, caps="keys.view", servers="s1")
    c = await _login(application, "sara", "sara-pw")
    assert (await c.get("/api/snapshots")).status_code == 403
    assert (await c.post("/api/snapshots")).status_code == 403
    await c.aclose()
