"""The credit ledger. These are the money paths, so they get the hard tests."""
import asyncio


async def _admin(db, credit=0, enabled=True, discount=0):
    aid = await db.add_admin("sara", "h", "s", caps="keys.create", servers="s1")
    await db.conn.execute(
        "UPDATE admins SET credit_enabled = ?, discount_pct = ? WHERE id = ?",
        (1 if enabled else 0, discount, aid))
    await db.conn.commit()
    # Seed the opening balance THROUGH the ledger, exactly as a real top-up
    # would. Poking the column directly would make ledger_sum != credit by
    # construction and quietly weaken every drift assertion below.
    if credit:
        await db.credit_admin(aid, credit, reason="topup", note="opening balance")
    return aid


async def test_charge_deducts_and_records(db):
    aid = await _admin(db, credit=100_000)
    entry = await db.charge(aid, 30_000, reason="purchase", package_name="5 GB",
                            price_before_discount=30_000, server_id="s1", key_id="1")
    assert entry is not None   # the ledger row it wrote
    assert (await db.get_admin(aid))["credit"] == 70_000
    row = (await db.ledger_for(aid))[0]   # newest first
    assert row["delta"] == -30_000 and row["balance_after"] == 70_000
    assert row["reason"] == "purchase" and row["package_name"] == "5 GB"
    assert row["key_id"] == "1"


async def test_charge_refuses_what_cannot_be_afforded(db):
    aid = await _admin(db, credit=20_000)
    assert await db.charge(aid, 30_000, reason="purchase") is None
    assert (await db.get_admin(aid))["credit"] == 20_000   # untouched
    assert [r["reason"] for r in await db.ledger_for(aid)] == ["topup"]  # unrecorded
    # exactly affordable is affordable
    assert await db.charge(aid, 20_000, reason="purchase") is not None
    assert (await db.get_admin(aid))["credit"] == 0


async def test_zero_credit_cannot_buy(db):
    aid = await _admin(db, credit=0)
    assert await db.charge(aid, 1, reason="purchase") is None


async def test_concurrent_sales_in_one_process_cannot_overdraft(db):
    """Two tabs, one package's worth of credit.

    NOTE: db._lock serializes these, so this passes even with a naive
    read-then-write. It is here to pin the in-process behaviour; the test below
    is the one that actually exercises the guard.
    """
    aid = await _admin(db, credit=30_000)
    results = await asyncio.gather(*[
        db.charge(aid, 30_000, reason="purchase") for _ in range(8)
    ])
    assert sum(r is not None for r in results) == 1, "exactly one sale may succeed"
    assert (await db.get_admin(aid))["credit"] == 0
    sales = [r for r in await db.ledger_for(aid) if r["reason"] == "purchase"]
    assert len(sales) == 1  # a refused sale writes nothing


async def test_two_connections_cannot_overdraft(db):
    """The real guard. Two DB instances on one file have independent asyncio
    locks — exactly what a second uvicorn worker would be. A read-then-write
    guard reads a stale balance here and drives the credit negative; only a
    `WHERE credit >= ?` re-evaluated at write time refuses the second sale.
    """
    from outline_panel.core.db import DB
    aid = await _admin(db, credit=30_000)

    other = DB(db.path)
    await other.init()
    try:
        results = await asyncio.gather(
            db.charge(aid, 30_000, reason="purchase", note="A"),
            other.charge(aid, 30_000, reason="purchase", note="B"),
            db.charge(aid, 30_000, reason="purchase", note="C"),
            other.charge(aid, 30_000, reason="purchase", note="D"),
        )
    finally:
        await other.close()

    bal = (await db.get_admin(aid))["credit"]
    assert bal >= 0, f"overdrafted to {bal}"
    won = sum(r is not None for r in results)
    assert won == 1, f"{won} sales succeeded, want 1"
    assert bal == 0
    assert await db.ledger_sum(aid) == bal


async def test_credit_admin_disabled_cannot_be_charged(db):
    """An admin outside the credit system has no balance to take."""
    aid = await _admin(db, credit=100_000, enabled=False)
    assert await db.charge(aid, 10_000, reason="purchase") is None
    assert (await db.get_admin(aid))["credit"] == 100_000


async def test_reversal_always_lands(db):
    """A failed sale is not a refund policy question — nothing was bought."""
    aid = await _admin(db, credit=50_000)
    await db.charge(aid, 50_000, reason="purchase", package_name="10 GB")
    assert (await db.get_admin(aid))["credit"] == 0
    bal = await db.credit_admin(aid, 50_000, reason="reversal", package_name="10 GB",
                                note="Outline create failed")
    assert bal == 50_000 and (await db.get_admin(aid))["credit"] == 50_000
    assert [r["reason"] for r in await db.ledger_for(aid)] == ["reversal", "purchase", "topup"]


async def test_topup(db):
    aid = await _admin(db, credit=0)
    assert await db.credit_admin(aid, 500_000, reason="topup", note="cash") == 500_000
    assert await db.credit_admin(aid, -100_000, reason="adjust", note="correction") == 400_000
    assert (await db.get_admin(aid))["credit"] == 400_000


async def test_ledger_never_drifts_from_the_balance(db):
    """The column is the guard, the ledger is the story. If they disagree, one
    of them is lying and there is no way to tell which."""
    aid = await _admin(db, credit=0)
    await db.credit_admin(aid, 200_000, reason="topup")
    for _ in range(3):
        await db.charge(aid, 30_000, reason="purchase")
    await db.charge(aid, 999_999, reason="purchase")     # refused, records nothing
    await db.credit_admin(aid, 30_000, reason="reversal")
    await db.credit_admin(aid, -5_000, reason="adjust")

    assert await db.ledger_sum(aid) == (await db.get_admin(aid))["credit"]
    assert (await db.get_admin(aid))["credit"] == 200_000 - 90_000 + 30_000 - 5_000


async def test_history_survives_deleting_the_package(db):
    """The ledger snapshots what was sold, so a price list can be edited freely
    without rewriting the past."""
    aid = await _admin(db, credit=100_000)
    pid = await db.add_package("5 GB", 5, 30, 30_000)
    await db.charge(aid, 30_000, reason="purchase", package_id=pid,
                    package_name="5 GB", price_before_discount=30_000)
    await db.delete_package(pid)
    row = (await db.ledger_for(aid))[0]   # the purchase, newest first
    assert row["package_name"] == "5 GB" and row["price_before_discount"] == 30_000
    assert await db.get_package(pid) is None


async def test_packages_crud(db):
    pid = await db.add_package("Unlimited", None, 30, 90_000)
    p = await db.get_package(pid)
    assert p["gb"] is None and p["days"] == 30 and p["price"] == 90_000
    await db.update_package(pid, price=120_000, name="Unlimited+")
    p = await db.get_package(pid)
    assert p["price"] == 120_000 and p["name"] == "Unlimited+"
    await db.add_package("5 GB", 5, 30, 30_000)
    assert [x["name"] for x in await db.all_packages()] == ["5 GB", "Unlimited+"]


async def test_money_survives_a_backup_roundtrip(db):
    """A table the restore wipes but never refills erased the panel once. Here
    it would erase the money."""
    aid = await _admin(db, credit=0)
    pid = await db.add_package("5 GB", 5, 30, 30_000)
    await db.credit_admin(aid, 100_000, reason="topup")
    await db.charge(aid, 30_000, reason="purchase", package_id=pid, package_name="5 GB")

    dump = await db.export_all()
    assert len(dump["packages"]) == 1 and len(dump["ledger"]) == 2

    await db.import_all({"servers": [], "keys": [], "settings": {}, "admins": [],
                         "packages": [], "ledger": []})
    assert await db.all_packages() == [] and await db.all_ledger() == []

    await db.import_all(dump)
    assert (await db.get_admin(aid))["credit"] == 70_000
    assert await db.ledger_sum(aid) == 70_000
    assert [p["name"] for p in await db.all_packages()] == ["5 GB"]
