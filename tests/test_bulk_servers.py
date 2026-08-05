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
