"""A subscription belongs to whoever sold it — the token is not a permission.

The customer link circulates by design, so holding a token proves nothing about
who may edit it. These two routes are keyed by token rather than {sid}/{kid},
so `enforce_scope` does not cover them and they must check for themselves.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from test_subadmins import _login, _mk_sub, app  # noqa: F401  (fixtures)


async def _sell_a_customer(application):
    owner = await _login(application, "admin", "pw")
    r = await owner.post("/api/servers/s1/keys",
                         json={"name": "victim", "limit_gb": 10, "days": 30})
    assert r.status_code == 200, r.text
    return owner, r.json()["subToken"]


async def test_a_stranger_cannot_add_a_server_to_someone_elses_subscription(app):
    application, deps, _ = app
    owner, token = await _sell_a_customer(application)
    await _mk_sub(deps, "sara", caps="keys.view,keys.edit", servers="s2")
    sara = await _login(application, "sara", "sara-pw")

    r = await sara.post(f"/api/sub/{token}/servers/s2")
    assert r.status_code == 404, r.text          # not "forbidden" — it isn't hers to see
    assert r.json()["code"] == "sub.unknown"
    await owner.aclose()
    await sara.aclose()


async def test_a_stranger_cannot_unlink_someone_elses_server(app):
    application, deps, _ = app
    owner, token = await _sell_a_customer(application)
    await _mk_sub(deps, "sara", caps="keys.view,keys.edit", servers="s1,s2")
    sara = await _login(application, "sara", "sara-pw")

    r = await sara.delete(f"/api/sub/{token}/servers/s1")
    assert r.status_code == 404, r.text
    # and the customer's config is still being served
    assert (await deps.db.get_keys_by_sub_token(token))
    await owner.aclose()
    await sara.aclose()


async def test_the_seller_can_still_manage_their_own_subscription(app):
    """The guard must not lock resellers out of the customers they sold."""
    application, deps, _ = app
    aid = await _mk_sub(deps, "sara", caps="keys.view,keys.edit,keys.create",
                        servers="s1,s2")
    assert aid
    sara = await _login(application, "sara", "sara-pw")
    r = await sara.post("/api/servers/s1/keys",
                        json={"name": "sara's customer", "limit_gb": 5, "days": 30})
    assert r.status_code == 200, r.text
    token = r.json()["subToken"]

    add = await sara.post(f"/api/sub/{token}/servers/s2")
    assert add.status_code == 200, add.text
    assert {m["serverId"] for m in add.json()["members"]} == {"s1", "s2"}

    rm = await sara.delete(f"/api/sub/{token}/servers/s2")
    assert rm.status_code == 200, rm.text
    assert {m["serverId"] for m in rm.json()["members"]} == {"s1"}
    await sara.aclose()
