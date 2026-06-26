import os
import sqlite3
import tempfile

from outline_panel.db import DB


async def test_add_and_get_key(db):
    await db.add_server("s1", "Srv", "https://x/y")
    await db.add_key("s1", "k1", "Ali", 5_000, 30)
    k = await db.get_key("s1", "k1")
    assert k["name"] == "Ali"
    assert k["limit_bytes"] == 5_000
    assert k["duration_days"] == 30
    assert k["disabled"] == 0
    assert k["activated_ts"] is None


async def test_keys_for_and_all_keys(db):
    await db.add_key("s1", "k1", "A", None, None)
    await db.add_key("s2", "k2", "B", None, None)
    assert len(await db.keys_for("s1")) == 1
    assert len(await db.all_keys()) == 2


async def test_setters(db):
    await db.add_key("s1", "k1", "A", None, None)
    await db.set_name("s1", "k1", "B")
    await db.set_limit("s1", "k1", 999)
    await db.set_disabled("s1", "k1", True)
    k = await db.get_key("s1", "k1")
    assert k["name"] == "B" and k["limit_bytes"] == 999 and k["disabled"] == 1


async def test_monthly(db):
    await db.add_key("s1", "k1", "A", None, None)
    await db.set_monthly("s1", "k1", 1234, 5555)
    k = await db.get_key("s1", "k1")
    assert k["monthly_bytes"] == 1234 and k["reset_ts"] == 5555
    await db.set_monthly("s1", "k1", None, None)
    k = await db.get_key("s1", "k1")
    assert k["monthly_bytes"] is None and k["reset_ts"] is None


async def test_pending_and_expired(db):
    await db.add_key("s1", "pend", "P", None, 30)            # pending activation
    await db.add_key("s1", "exp", "E", None, None)
    await db.set_expiry("s1", "exp", 100)                    # long past
    pend = await db.pending_activation_keys()
    assert [k["key_id"] for k in pend] == ["pend"]
    expired = await db.expired_active_keys(1000)
    assert [k["key_id"] for k in expired] == ["exp"]


async def test_delete_server_cascades_keys(db):
    await db.add_server("s1", "S", "u")
    await db.add_key("s1", "k1", "A", None, None)
    await db.delete_server("s1")
    assert await db.all_keys() == []


async def test_legacy_single_server_migration():
    # build an old keys table without server_id, then init() must migrate it
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE keys (key_id TEXT PRIMARY KEY, name TEXT, limit_bytes INT, "
        "duration_days INT, activated_ts INT, expiry_ts INT, disabled INT DEFAULT 0, "
        "created_ts INT)"
    )
    c.execute("INSERT INTO keys (key_id, name) VALUES ('9', 'Legacy')")
    c.commit()
    c.close()

    d = DB(path)
    await d.init()
    rows = await d.keys_for("default")
    assert len(rows) == 1 and rows[0]["key_id"] == "9" and rows[0]["name"] == "Legacy"
    # new columns exist after migration
    assert "monthly_bytes" in rows[0] and "reset_ts" in rows[0]
    await d.close()
