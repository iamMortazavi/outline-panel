"""Putting a whole selection of customers on a server, and taking them off.

Doing this for fifteen customers used to mean a script against the database,
because the panel only knew how to do it one customer at a time.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from test_subadmins import _login, _mk_sub, app  # noqa: F401  (fixtures)


async def _sell(client, n=3, sid="s1"):
    """n customers on one server. Returns their key refs."""
    refs = []
    for i in range(n):
        r = await client.post(f"/api/servers/{sid}/keys",
                              json={"name": f"customer {i}", "limit_gb": 10, "days": 30})
        assert r.status_code == 200, r.text
        refs.append({"server_id": sid, "key_id": r.json()["id"]})
    return refs


def _servers_of(keys, key_id):
    return {k["serverId"] for k in keys if str(k["id"]) == str(key_id)}


async def test_a_selection_lands_on_the_new_server_in_one_call(app):
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    refs = await _sell(c, 3)

    r = await c.post("/api/servers/s2/bulk-servers",
                     json={"keys": refs, "action": "add"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["done"]) == 3 and body["failed"] == []

    # every one of them is now served on both servers under the same link
    for ref in refs:
        meta = await deps.db.get_key(ref["server_id"], ref["key_id"])
        members = await deps.db.get_keys_by_sub_token(meta["sub_token"])
        assert {m["server_id"] for m in members} == {"s1", "s2"}
        # the mirror carries the primary's allowance, not a fresh one
        assert all(m["limit_bytes"] == members[0]["limit_bytes"] for m in members)
    await c.aclose()


async def test_removing_takes_them_back_off_that_server(app):
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    refs = await _sell(c, 2)
    await c.post("/api/servers/s2/bulk-servers", json={"keys": refs, "action": "add"})

    r = await c.post("/api/servers/s2/bulk-servers",
                     json={"keys": refs, "action": "remove"})
    assert r.status_code == 200, r.text
    assert len(r.json()["done"]) == 2

    for ref in refs:
        meta = await deps.db.get_key(ref["server_id"], ref["key_id"])
        members = await deps.db.get_keys_by_sub_token(meta["sub_token"])
        assert {m["server_id"] for m in members} == {"s1"}
    await c.aclose()


async def test_one_bad_key_does_not_sink_the_others(app):
    """Partial success is the normal outcome — report it, don't fail the batch."""
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    refs = await _sell(c, 2)
    refs.append({"server_id": "s1", "key_id": "does-not-exist"})

    r = await c.post("/api/servers/s2/bulk-servers",
                     json={"keys": refs, "action": "add"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["done"]) == 2
    assert len(body["failed"]) == 1
    assert "s1/does-not-exist" == body["failed"][0]["key"]
    await c.aclose()


async def test_a_subadmin_cannot_sweep_up_someone_elses_customers(app):
    """The selection is a list of ids the browser sent. None of it is trusted."""
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    victims = await _sell(owner, 2)

    await _mk_sub(deps, "sara", caps="keys.view,keys.edit", servers="s1,s2")
    sara = await _login(application, "sara", "sara-pw")

    r = await sara.post("/api/servers/s2/bulk-servers",
                        json={"keys": victims, "action": "add"})
    assert r.status_code == 200, r.text          # the call is allowed…
    assert r.json()["done"] == []                # …and moves nothing
    assert len(r.json()["failed"]) == 2

    for ref in victims:                          # still on s1 only
        meta = await deps.db.get_key(ref["server_id"], ref["key_id"])
        members = await deps.db.get_keys_by_sub_token(meta["sub_token"])
        assert {m["server_id"] for m in members} == {"s1"}
    await owner.aclose()
    await sara.aclose()


async def test_a_server_out_of_scope_is_not_a_destination(app):
    application, deps, _ = app
    await _mk_sub(deps, "sara", caps="keys.view,keys.edit,keys.create", servers="s1")
    sara = await _login(application, "sara", "sara-pw")
    refs = await _sell(sara, 1)

    r = await sara.post("/api/servers/s2/bulk-servers",
                        json={"keys": refs, "action": "add"})
    assert r.status_code == 404, r.text          # s2 does not exist, to her
    await sara.aclose()


async def test_an_older_key_with_no_subscription_still_gets_one(app):
    """The oldest customers must not be the ones this cannot help."""
    application, deps, fakes = app
    c = await _login(application, "admin", "pw")
    key = await fakes["s1"].create_key(name="adopted")
    await deps.db.add_key("s1", key["id"], "adopted", None, None)   # no sub_token
    assert (await deps.db.get_key("s1", key["id"]))["sub_token"] is None

    r = await c.post("/api/servers/s2/bulk-servers",
                     json={"keys": [{"server_id": "s1", "key_id": key["id"]}],
                           "action": "add"})
    assert r.status_code == 200, r.text
    assert len(r.json()["done"]) == 1
    tok = (await deps.db.get_key("s1", key["id"]))["sub_token"]
    assert tok
    assert {m["server_id"] for m in await deps.db.get_keys_by_sub_token(tok)} == {"s1", "s2"}
    await c.aclose()


# ---------------------------------------------------- doing it at real sizes
async def _add_server(deps, sid, name):
    from test_features import FakeOutline
    f = FakeOutline()
    deps.reg.servers[sid] = {"id": sid, "name": name, "api_url": "https://5.6.7.8:1/x",
                             "cert_sha256": None, "api": f}
    await deps.db.add_server(sid, name, "https://5.6.7.8:1/x")
    return f


async def test_two_selected_rows_of_one_subscription_mirror_once(app):
    """A selection is a list of rows, and two rows can be the same customer.

    The subscription sheet lets members be ticked individually, so a batch can
    legitimately name two members of one subscription. The work is per
    *subscription*, not per row — doing it twice would put two keys on the
    destination for one customer, and the second is unbilled and invisible.

    This holds while the loop is sequential, because mirror_onto checks
    membership first. It is written down because dispatching the batch
    concurrently is exactly what would break it.
    """
    application, deps, _ = app
    await _add_server(deps, "s3", "Oslo")
    c = await _login(application, "admin", "pw")
    refs = await _sell(c, 1)
    token = (await deps.db.get_key("s1", refs[0]["key_id"]))["sub_token"]
    assert (await c.post(f"/api/sub/{token}/servers/s2")).status_code == 200
    members = await deps.db.get_keys_by_sub_token(token)
    assert len(members) == 2
    both = [{"server_id": m["server_id"], "key_id": m["key_id"]} for m in members]

    r = await c.post("/api/servers/s3/bulk-servers",
                     json={"keys": both, "action": "add"})
    assert r.status_code == 200, r.text
    on_s3 = [m for m in await deps.db.get_keys_by_sub_token(token)
             if m["server_id"] == "s3"]
    assert len(on_s3) == 1, f"one customer, {len(on_s3)} keys on the destination"
    # and the caller still hears a verdict for both rows it asked about
    assert len(r.json()["done"]) == 2
    await c.aclose()


async def test_a_big_batch_does_not_take_the_sum_of_its_parts(app):
    """A4: the upstream calls ran one after another.

    Sixteen customers at ~50ms each is most of a second; the real case is
    hundreds against a server across the sea, which is minutes — long enough
    that a reverse proxy closes the connection first and the operator is left
    not knowing what landed.
    """
    import asyncio
    import time
    application, deps, fakes = app
    c = await _login(application, "admin", "pw")
    refs = await _sell(c, 16)

    slow = fakes["s2"]
    real = slow.create_key

    async def latent(*a, **k):
        await asyncio.sleep(0.05)          # a plausible round trip
        return await real(*a, **k)
    slow.create_key = latent

    started = time.monotonic()
    r = await c.post("/api/servers/s2/bulk-servers",
                     json={"keys": refs, "action": "add"})
    elapsed = time.monotonic() - started

    assert r.status_code == 200, r.text
    assert len(r.json()["done"]) == 16 and r.json()["failed"] == []
    serial = 16 * 0.05
    assert elapsed < serial * 0.5, (
        f"{elapsed:.2f}s for 16 customers — barely better than the {serial:.2f}s "
        f"a sequential loop costs")
    await c.aclose()


async def test_the_batch_is_still_checked_one_key_at_a_time(app):
    """Concurrency must not become a hole. Every row is authorised on its own,
    and rows belonging to someone else fail while the caller's own lands."""
    application, deps, _ = app
    owner = await _login(application, "admin", "pw")
    theirs = await _sell(owner, 3)
    aid = await _mk_sub(deps, "sara", caps="keys.view,keys.edit", servers="s1")
    hers = await _sell(owner, 1)
    await deps.db.set_key_owner("s1", hers[0]["key_id"], aid)

    sara = await _login(application, "sara", "sara-pw")
    r = await sara.post("/api/servers/s1/bulk-servers",
                        json={"keys": theirs + hers, "action": "add"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["failed"]) == 3, body
    assert len(body["done"]) == 1
    assert body["done"][0]["keyId"] == hers[0]["key_id"]
    await sara.aclose()
    await owner.aclose()
