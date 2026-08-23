"""
A1 — a customer is one subscription, not one key.

Every write endpoint is keyed by `(server_id, key_id)`. A customer's identity is
the `sub_token`, and `mirror_onto` gives them a member on each server they were
sold. Nothing keeps those members in step afterwards, so today:

  * suspending a customer leaves them fully live on every mirror;
  * renewing one moves only the primary's expiry, and the scheduler cuts the
    mirrors off at the old date;
  * deleting one leaves working keys upstream that the public subscription link
    keeps handing out, with no panel row left to find them by.

These tests describe the behaviour the panel is supposed to have. They are
`xfail(strict=True)`, so they keep CI honest in both directions: red is expected
until step 3 of MODERNIZATION.md lands, and the day one of them starts passing
by accident, pytest says so instead of quietly agreeing.

`mirror_onto`'s undivided-allowance rule (invariant M8) is deliberate and is
preserved here: propagating means every member gets the *same* new value, not a
share of it. Usage, though, is counted per key upstream, so anything derived
from usage has to be recomputed per member.
"""

import os
import sys
import tempfile

import httpx
import pytest

from test_features import FakeOutline

GB = 1024 ** 3
xfail_a1 = pytest.mark.xfail(strict=True, reason="A1 — fixed in step 3 of MODERNIZATION.md")


@pytest.fixture
async def two_servers():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "a1.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    os.environ.pop("BOT_TOKEN", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    sys.path.insert(0, os.path.dirname(__file__))
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    fakes = {}
    for sid, name in (("s1", "Tokyo"), ("s2", "Berlin")):
        f = FakeOutline()
        fakes[sid] = f
        deps.reg.servers[sid] = {"id": sid, "name": name,
                                 "api_url": f"https://10.0.0.1:1/{sid}",
                                 "cert_sha256": None, "api": f}
        await deps.db.add_server(sid, name, f"https://10.0.0.1:1/{sid}")
    yield appmod.app, deps, fakes
    await deps.db.close()


async def _sold_on_both(two_servers, **overrides):
    """One customer, bought once, living on two servers. Returns
    (client, primary key id, token, members-by-server-id)."""
    application, deps, fakes = two_servers
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                          base_url="http://panel.example.com")
    assert (await c.post("/api/login", json={"password": "pw"})).status_code == 200
    body = {"name": "two-servers", "limit_gb": 40, "days": 30,
            "start_now": True, "extra_servers": ["s2"]}
    body.update(overrides)
    r = await c.post("/api/servers/s1/keys", json=body)
    assert r.status_code == 200, r.text
    kid = r.json()["id"]
    token = (await deps.db.get_key("s1", kid))["sub_token"]
    members = {m["server_id"]: m for m in await deps.db.get_keys_by_sub_token(token)}
    assert set(members) == {"s1", "s2"}, "the fixture itself did not mirror"
    return c, kid, token, members


async def _members(deps, token):
    return {m["server_id"]: m for m in await deps.db.get_keys_by_sub_token(token)}


@xfail_a1
async def test_A1_1_suspending_a_customer_suspends_every_server(two_servers):
    """Suspension has to mean suspension. A customer who has stopped paying, or
    who is being cut off for abuse, must not keep a working config on the
    mirror — which is the server they will fail over to the moment the primary
    stops answering."""
    application, deps, fakes = two_servers
    c, kid, token, _ = await _sold_on_both(two_servers)

    assert (await c.post(f"/api/servers/s1/keys/{kid}/disable")).status_code == 200

    members = await _members(deps, token)
    for sid, m in members.items():
        assert m["disabled"] == 1, f"{sid} still marked live in the panel"
        assert fakes[sid].limits[m["key_id"]] == 0, f"{sid} still serving traffic"
    await c.aclose()


@xfail_a1
async def test_A1_2_resuming_a_customer_resumes_every_server(two_servers):
    """The mirror image, and the one that bites the honest customer: if suspend
    ever propagates but enable does not, paying again brings back one server."""
    application, deps, fakes = two_servers
    c, kid, token, members = await _sold_on_both(two_servers)
    # suspend both by hand, so this test fails for its own reason and not A1-1's
    for sid, m in members.items():
        await deps.db.set_disabled(sid, m["key_id"], True)
        await fakes[sid].set_data_limit(m["key_id"], 0)

    assert (await c.post(f"/api/servers/s1/keys/{kid}/enable")).status_code == 200

    for sid, m in (await _members(deps, token)).items():
        assert m["disabled"] == 0, f"{sid} still suspended in the panel"
        assert fakes[sid].limits[m["key_id"]] == 40 * GB, f"{sid} still blocked"
    await c.aclose()


@xfail_a1
async def test_A1_3_renewing_moves_every_members_clock(two_servers):
    """The customer paid for another 30 days. The scheduler expires each member
    on its own `expiry_ts`, so a renewal that moves one row cuts the customer
    off everywhere else at the old date — after they paid."""
    application, deps, fakes = two_servers
    c, kid, token, members = await _sold_on_both(two_servers)
    before = {sid: m["expiry_ts"] for sid, m in members.items()}
    assert before["s1"] == before["s2"], "the mirror should start in step"

    assert (await c.post(f"/api/servers/s1/keys/{kid}/extend",
                         json={"days": 30})).status_code == 200

    after = {sid: m["expiry_ts"] for sid, m in (await _members(deps, token)).items()}
    assert after["s1"] > before["s1"], "the primary was not extended at all"
    assert after["s2"] == after["s1"], "the mirror was left on the old date"
    await c.aclose()


@xfail_a1
async def test_A1_4_deleting_a_customer_removes_every_key(two_servers):
    """Deleting the primary currently leaves live keys on every mirror *and*
    leaves the public subscription serving them — with no panel row left to
    find them by. That is an unbilled, unmanageable, permanent config."""
    application, deps, fakes = two_servers
    c, kid, token, members = await _sold_on_both(two_servers)

    assert (await c.delete(f"/api/servers/s1/keys/{kid}")).status_code == 200

    assert await deps.db.get_keys_by_sub_token(token) == [], \
        "the subscription still has members after the customer was deleted"
    for sid, m in members.items():
        assert m["key_id"] not in fakes[sid].keys, f"{sid} still holds a live key"
    info = await c.get(f"/sub/{token}/info", headers={"accept": "*/*"})
    assert info.status_code == 404, "the public link still serves a deleted customer"
    await c.aclose()


@xfail_a1
async def test_A1_5_allowance_changes_reach_every_server(two_servers):
    """`limit_bytes` is the cumulative ceiling Outline counts against (invariant
    T2) and mirroring deliberately gives every member the *same* undivided
    allowance (M8). So raising it must raise it everywhere; a reset must be
    recomputed per member, because usage is counted per key upstream."""
    application, deps, fakes = two_servers
    c, kid, token, members = await _sold_on_both(two_servers, monthly_gb=40)
    # the two members have spent different amounts, as real ones do
    fakes["s1"].usage[members["s1"]["key_id"]] = 30 * GB
    fakes["s2"].usage[members["s2"]["key_id"]] = 5 * GB

    assert (await c.put(f"/api/servers/s1/keys/{kid}/limit",
                        json={"limit_gb": 100})).status_code == 200
    for sid, m in (await _members(deps, token)).items():
        assert m["limit_bytes"] == 100 * GB, f"{sid} kept the old ceiling"
        assert fakes[sid].limits[m["key_id"]] == 100 * GB, f"{sid} not told upstream"

    assert (await c.post(f"/api/servers/s1/keys/{kid}/reset")).status_code == 200
    fresh = await _members(deps, token)
    # used + allowance, per member — not one number copied across
    assert fresh["s1"]["limit_bytes"] == 30 * GB + 40 * GB
    assert fresh["s2"]["limit_bytes"] == 5 * GB + 40 * GB
    await c.aclose()
