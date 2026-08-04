"""
The migration runner, and the two shapes of database already in the wild.

`PRAGMA user_version` starts at 0 on a brand-new file *and* on every panel
deployed before migrations existed, so the dangerous case is not the empty
database — it is the full one that has never been stamped.
"""
import os
import sqlite3
import tempfile

import pytest

from outline_panel.core.db import _MIGRATIONS, DB


def _tmp(name="m.db"):
    return os.path.join(tempfile.mkdtemp(), name)


async def test_a_fresh_database_lands_on_the_latest_version():
    db = DB(_tmp())
    await db.init()
    assert await db.schema_version() == len(_MIGRATIONS)
    await db.close()


async def test_init_is_idempotent():
    """Restarting the panel must not re-run anything: step 002 creates its
    indexes without IF NOT EXISTS, so a second pass would raise."""
    path = _tmp()
    for _ in range(3):
        db = DB(path)
        await db.init()
        assert await db.schema_version() == len(_MIGRATIONS)
        await db.close()


# The schema exactly as the last pre-migration release left it: every table at
# its final shape, no audit_log, no idempotency, and user_version never stamped.
# Written out rather than produced by the current code, because the whole point
# is to test against something the current code did NOT build.
_DEPLOYED_V0 = """
CREATE TABLE servers (id TEXT PRIMARY KEY, name TEXT, api_url TEXT,
                      cert_sha256 TEXT, created_ts INTEGER);
CREATE TABLE keys (server_id TEXT, key_id TEXT, name TEXT, limit_bytes INTEGER,
                   duration_days INTEGER, activated_ts INTEGER, expiry_ts INTEGER,
                   disabled INTEGER DEFAULT 0, monthly_bytes INTEGER,
                   reset_ts INTEGER, sub_token TEXT, created_ts INTEGER,
                   owner_admin_id INTEGER, PRIMARY KEY (server_id, key_id));
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE admins (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     username TEXT NOT NULL UNIQUE COLLATE NOCASE, pw_hash TEXT,
                     pw_salt TEXT, is_owner INTEGER DEFAULT 0, caps TEXT DEFAULT '',
                     servers TEXT DEFAULT '', disabled INTEGER DEFAULT 0,
                     created_ts INTEGER, credit INTEGER DEFAULT 0,
                     credit_enabled INTEGER DEFAULT 0, discount_pct INTEGER DEFAULT 0,
                     telegram_id INTEGER);
CREATE TABLE packages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                       gb REAL, days INTEGER, monthly_gb REAL, price INTEGER NOT NULL,
                       created_ts INTEGER);
CREATE TABLE credit_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            admin_id INTEGER NOT NULL, delta INTEGER NOT NULL,
                            balance_after INTEGER NOT NULL, reason TEXT NOT NULL,
                            package_id INTEGER, package_name TEXT,
                            price_before_discount INTEGER, server_id TEXT,
                            key_id TEXT, note TEXT, created_ts INTEGER);

INSERT INTO servers VALUES ('s1','Tokyo','https://1.2.3.4:1/x',NULL,1600000000);
INSERT INTO keys VALUES ('s1','7','Ali',10737418240,30,NULL,NULL,0,NULL,NULL,
                         'tok7',1600000000,NULL);
INSERT INTO admins VALUES (1,'admin',NULL,NULL,1,'','',0,1600000000,0,0,0,NULL);
INSERT INTO admins VALUES (2,'sara','h','s',0,'keys.view','s1',0,1600000000,
                           50000,1,10,555);
INSERT INTO packages VALUES (1,'30GB',30,30,NULL,25000,1600000000);
INSERT INTO credit_ledger VALUES (1,2,50000,50000,'topup',NULL,NULL,NULL,NULL,
                                  NULL,'opening',1600000000);
INSERT INTO settings VALUES ('bot_token','secret');
"""


async def test_a_deployed_database_converges_without_losing_anything():
    """The real upgrade path: a panel installed before migrations existed. Its
    schema is already current, its user_version is 0, and step 001 has to be a
    no-op against it rather than an error."""
    path = _tmp()
    raw = sqlite3.connect(path)
    raw.executescript(_DEPLOYED_V0)
    raw.commit()
    raw.close()

    db = DB(path)
    await db.init()
    assert await db.schema_version() == len(_MIGRATIONS)

    key = await db.get_key("s1", "7")
    assert key["name"] == "Ali" and key["sub_token"] == "tok7"
    assert key["limit_bytes"] == 10 * 1024 ** 3 and key["duration_days"] == 30
    sara = await db.get_admin(2)
    assert sara["credit"] == 50_000 and sara["discount_pct"] == 10
    assert sara["telegram_id"] == 555 and sara["servers"] == "s1"
    assert await db.ledger_sum(2) == 50_000
    assert await db.get_setting("bot_token") == "secret"
    assert [s["name"] for s in await db.all_servers()] == ["Tokyo"]
    assert [p["name"] for p in await db.all_packages()] == ["30GB"]
    # and the tables the new steps add are now usable
    await db.add_audit(2, "sara", "POST /x", "s1/7", 200, "1.1.1.1", "{}")
    assert len(await db.audit_page()) == 1
    await db.close()


async def test_a_half_applied_step_replays_cleanly():
    """sqlite3 autocommits DDL, so a step's CREATEs land before the version bump
    commits. Lose the process in between and the step runs again on the next
    start — against a schema that already has what it creates."""
    path = _tmp()
    db = DB(path)
    await db.init()
    await db.add_audit(1, "admin", "POST /x", None, 200, None, None)
    await db.close()

    raw = sqlite3.connect(path)          # tables present, counter rewound
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()

    db = DB(path)
    await db.init()                       # must not raise "index already exists"
    assert await db.schema_version() == len(_MIGRATIONS)
    assert len(await db.audit_page()) == 1
    await db.close()


async def test_a_pre_multi_server_database_is_carried_over():
    """The oldest shape: `keys` with no server_id, from before the panel could
    hold more than one server. Those rows become the 'default' server's."""
    path = _tmp()
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE keys (key_id TEXT PRIMARY KEY, name TEXT, limit_bytes INTEGER,
                           duration_days INTEGER, activated_ts INTEGER,
                           expiry_ts INTEGER, disabled INTEGER DEFAULT 0,
                           created_ts INTEGER);
        INSERT INTO keys VALUES ('1','Ali',1024,30,NULL,NULL,0,1600000000);
        INSERT INTO keys VALUES ('2','Sara',2048,NULL,1600000000,1700000000,1,1600000000);
    """)
    raw.commit()
    raw.close()

    db = DB(path)
    await db.init()
    rows = {k["key_id"]: k for k in await db.all_keys()}
    assert set(rows) == {"1", "2"}
    assert all(k["server_id"] == "default" for k in rows.values())
    assert rows["1"]["name"] == "Ali" and rows["1"]["duration_days"] == 30
    assert rows["2"]["disabled"] == 1
    # the columns added after that era exist and default to "the owner's"
    assert rows["1"]["owner_admin_id"] is None
    # ...and the backfill reaches even these, so a customer from the very first
    # release still ends up with a page to be sent to
    assert rows["1"]["sub_token"].startswith("1-")
    assert rows["2"]["sub_token"].startswith("2-")
    assert rows["1"]["sub_token"] != rows["2"]["sub_token"]
    await db.close()


async def test_only_the_missing_steps_run():
    """A database stamped half way must not replay what it already has."""
    path = _tmp()
    db = DB(path)
    await db.init()
    await db.add_audit(1, "admin", "POST /x", "s1/7", 200, "1.1.1.1", "{}")
    await db.close()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 2")   # pretend 003 never ran
    raw.commit()
    raw.close()

    db = DB(path)
    await db.init()
    assert await db.schema_version() == len(_MIGRATIONS)
    assert len(await db.audit_page()) == 1    # 002's data survived
    await db.close()


# ------------------------------------------------------------------- audit
async def test_audit_paging_is_newest_first_and_keyset():
    db = DB(_tmp())
    await db.init()
    for i in range(5):
        await db.add_audit(1, "admin", f"POST /{i}", None, 200, "ip", None)
    page = await db.audit_page(limit=2)
    assert [r["action"] for r in page] == ["POST /4", "POST /3"]
    nxt = await db.audit_page(limit=2, before_id=page[-1]["id"])
    assert [r["action"] for r in nxt] == ["POST /2", "POST /1"]
    assert len(await db.audit_page(actor_admin_id=99)) == 0
    await db.close()


async def test_audit_pruning_drops_only_old_rows(monkeypatch):
    import outline_panel.core.db as dbmod
    db = DB(_tmp())
    await db.init()
    monkeypatch.setattr(dbmod.time, "time", lambda: 1000)
    await db.add_audit(1, "a", "old", None, 200, None, None)
    monkeypatch.setattr(dbmod.time, "time", lambda: 9000)
    await db.add_audit(1, "a", "new", None, 200, None, None)
    assert await db.prune_audit(5000) == 1
    assert [r["action"] for r in await db.audit_page()] == ["new"]
    await db.close()


# ------------------------------------------------------------ idempotency
async def test_a_key_can_only_be_claimed_once():
    db = DB(_tmp())
    await db.init()
    assert await db.claim_idempotency("k", 1, "/buy", "h") is None
    again = await db.claim_idempotency("k", 1, "/buy", "h")
    assert again is not None and again["status"] is None   # still in flight
    await db.finish_idempotency("k", 200, '{"id":"7"}')
    done = await db.claim_idempotency("k", 1, "/buy", "h")
    assert done["status"] == 200 and done["response"] == '{"id":"7"}'
    await db.close()


async def test_releasing_only_frees_an_unfinished_claim():
    """A charge that landed must keep its row — releasing it would let the
    retry charge a second time, which is the whole point of the table."""
    db = DB(_tmp())
    await db.init()
    await db.claim_idempotency("live", 1, "/buy", "h")
    await db.release_idempotency("live")
    assert await db.claim_idempotency("live", 1, "/buy", "h") is None

    await db.finish_idempotency("live", 200, "{}")
    await db.release_idempotency("live")                  # must not delete
    assert (await db.claim_idempotency("live", 1, "/buy", "h"))["status"] == 200
    await db.close()


async def test_concurrent_claims_have_exactly_one_winner():
    import asyncio
    db = DB(_tmp())
    await db.init()
    got = await asyncio.gather(*[
        db.claim_idempotency("race", 1, "/buy", "h") for _ in range(12)])
    assert sum(1 for g in got if g is None) == 1
    await db.close()


@pytest.mark.parametrize("table", ["audit_log", "idempotency"])
async def test_restore_does_not_wipe_the_new_tables(table):
    """Who restored what is worth keeping, and replaying a purchase key after a
    restore would be a double charge."""
    db = DB(_tmp())
    await db.init()
    await db.add_audit(1, "admin", "POST /x", None, 200, None, None)
    await db.claim_idempotency("k", 1, "/buy", "h")
    await db.import_all({"servers": [], "keys": [], "settings": {}, "admins": [],
                         "packages": [], "ledger": []})
    cur = await db.conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
    assert (await cur.fetchone())["n"] == 1
    await db.close()
