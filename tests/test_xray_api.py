"""
The Xray adapter, over a real socket.

Everything here goes through `fake_xray.FakeXray`: a real h2c server that
decodes the real protobuf and answers with real protobuf. The framing and the
encoding are what is actually being tested, and a fake that spoke Python objects
would test neither.

The two places Xray does not match Outline get the most attention, because they
are where an adapter that merely *looks* finished would be wrong:
Xray cannot cap a key, and its counters restart with the process.
"""

import pytest

from fake_xray import FakeXray
from outline_panel.core.outline_api import OutlineError
from outline_panel.core.xray.api import XrayAPI
from outline_panel.core.xray.grpc import GrpcError

LINK = {"address": "vpn.example.com", "port": 443, "security": "reality",
        "sni": "www.microsoft.com", "pbk": "abc123", "sid": "0f0f",
        "fp": "chrome", "flow": "xtls-rprx-vision"}


@pytest.fixture
async def node():
    async with FakeXray() as fake:
        yield fake, XrayAPI("127.0.0.1", fake.port, "vless-in", LINK)


# ------------------------------------------------------------------ the basics
async def test_creating_a_customer_puts_a_real_user_in_the_inbound(node):
    fake, api = node
    key = await api.create_key(name="Ali")

    assert key["id"] in fake.users, "the user never reached the inbound"
    assert fake.users[key["id"]]["id"] == key["id"], "the VLESS id is the key id"
    assert fake.users[key["id"]]["flow"] == "xtls-rprx-vision"
    assert key["accessUrl"].startswith(f"vless://{key['id']}@vpn.example.com:443?")
    assert "#Ali" in key["accessUrl"]


def test_the_access_url_carries_what_a_client_needs():
    api = XrayAPI("127.0.0.1", 1, "vless-in", LINK)
    url = api.access_url("uuid-here", "Sara")
    for expected in ("security=reality", "sni=www.microsoft.com", "pbk=abc123",
                     "sid=0f0f", "fp=chrome", "type=tcp"):
        assert expected in url, f"{expected} missing from {url}"
    assert url.endswith("#Sara")


def test_an_empty_reality_parameter_is_left_out_entirely(node):
    """A `pbk=` with nothing after it is not the same as no pbk — some clients
    refuse the whole link."""
    fake, _ = node
    api = XrayAPI("127.0.0.1", fake.port, "vless-in",
                  {"address": "h", "port": 8443, "pbk": "", "sni": None})
    url = api.access_url("u")
    assert "pbk=" not in url and "sni=" not in url


async def test_listing_and_fetching_go_through_the_inbound(node):
    fake, api = node
    a = await api.create_key(name="A")
    b = await api.create_key(name="B")
    listed = {k["id"] for k in await api.list_keys()}
    assert listed == {a["id"], b["id"]}
    assert (await api.get_key(a["id"]))["id"] == a["id"]


async def test_deleting_removes_the_user(node):
    fake, api = node
    key = await api.create_key()
    await api.delete_key(key["id"])
    assert key["id"] not in fake.users


async def test_a_key_that_is_not_there_is_a_404(node):
    """The executor branches on this: 404 means "the state you asked for is
    already true", and a delete that 404s is success rather than a stuck row."""
    fake, api = node
    with pytest.raises(OutlineError) as caught:
        await api.get_key("nobody")
    assert caught.value.status == 404

    with pytest.raises(OutlineError) as caught:
        await api.delete_key("nobody")
    assert caught.value.status == 404


async def test_renaming_only_changes_the_label(node):
    """Xray has no display name. The panel owns names; this keeps the label on
    a freshly built link from being a bare UUID."""
    fake, api = node
    key = await api.create_key(name="before")
    await api.rename_key(key["id"], "after")
    assert "#after" in (await api.get_key(key["id"]))["accessUrl"]
    assert fake.users[key["id"]]["id"] == key["id"], "the account itself moved"


# ------------------------------------------- mismatch 1: no server-side limit
def test_the_adapter_admits_it_cannot_cap_a_key():
    """The flag the scheduler reads. Getting this wrong in the optimistic
    direction means customers on Xray never stop at their allowance."""
    assert XrayAPI("h", 1, "t").enforces_data_limit is False


async def test_a_limit_of_zero_actually_stops_the_traffic(node):
    """The port says a limit of zero is how the panel suspends someone and that
    it must really stop traffic. Xray has no cap, so the only honest
    implementation is removing the user."""
    fake, api = node
    key = await api.create_key()
    await api.set_data_limit(key["id"], 0)
    assert key["id"] not in fake.users, "a suspended customer can still connect"


async def test_lifting_the_limit_puts_them_back(node):
    fake, api = node
    key = await api.create_key()
    await api.set_data_limit(key["id"], 0)
    await api.set_data_limit(key["id"], 50 * 1024 ** 3)
    assert key["id"] in fake.users


async def test_resuming_someone_who_was_never_suspended_is_not_an_error(node):
    """Xray answers ALREADY_EXISTS, which is the state being asked for. The
    executor retries from the outbox, so this has to be idempotent or a
    recovered server would keep failing the same row forever."""
    fake, api = node
    key = await api.create_key()
    await api.remove_data_limit(key["id"])
    await api.remove_data_limit(key["id"])
    assert key["id"] in fake.users


# --------------------------------------------- mismatch 2: resetting counters
async def test_usage_is_uplink_plus_downlink_per_customer(node):
    fake, api = node
    key = await api.create_key()
    fake.traffic[key["id"]] = (1_000, 9_000)
    assert (await api.get_transfer_metrics())[key["id"]] == 10_000


async def test_an_xray_restart_does_not_hand_back_the_month(node):
    """Xray's counters are cumulative only since it last started. The panel's
    model needs them cumulative full stop — activation is "usage above zero" and
    a quota reset is "current usage plus the allowance", so a counter that
    silently returned to zero would give every customer their allowance back.
    """
    fake, api = node
    key = await api.create_key()

    fake.traffic[key["id"]] = (3_000, 5_000)
    assert (await api.get_transfer_metrics())[key["id"]] == 8_000

    fake.traffic[key["id"]] = (100, 200)          # Xray restarted
    assert (await api.get_transfer_metrics())[key["id"]] == 8_300

    fake.traffic[key["id"]] = (1_000, 1_000)      # and keeps counting
    assert (await api.get_transfer_metrics())[key["id"]] == 10_000


async def test_usage_never_goes_backwards_across_many_restarts(node):
    fake, api = node
    key = await api.create_key()
    seen = []
    for up, down in [(10, 10), (50, 50), (1, 1), (5, 5), (0, 0), (7, 7)]:
        fake.traffic[key["id"]] = (up, down)
        seen.append((await api.get_transfer_metrics())[key["id"]])
    assert seen == sorted(seen), f"usage went backwards: {seen}"


async def test_a_user_with_no_traffic_yet_is_simply_absent(node):
    """Xray keeps no counter until the first byte. The scheduler reads a missing
    entry as zero, which is what "has not connected yet" means."""
    fake, api = node
    await api.create_key()
    assert await api.get_transfer_metrics() == {}


# ------------------------------------------------------------------- failures
async def test_an_unreachable_node_is_an_outline_error_not_a_crash(node):
    """Everything upstream of the adapter guards with `except OutlineError`. A
    connection refused escaping as OSError would 500 the endpoint instead of
    reporting a server that is down."""
    fake, api = node
    await fake.stop()
    with pytest.raises(OutlineError):
        await api.list_keys()


async def test_a_grpc_error_carries_its_message_through(node):
    fake, api = node
    fake.fail_next = "QueryStats"
    with pytest.raises(OutlineError) as caught:
        await api.get_transfer_metrics()
    assert "unavailable" in str(caught.value).lower()


async def test_the_wrong_inbound_tag_says_so(node):
    fake, _ = node
    api = XrayAPI("127.0.0.1", fake.port, "no-such-inbound", LINK)
    with pytest.raises(OutlineError) as caught:
        await api.list_keys()
    assert caught.value.status == 404


async def test_server_info_doubles_as_the_reachability_probe(node):
    fake, api = node
    info = await api.get_server_info()
    assert info["version"] == "25.1.30"
    assert "vless-in" in info["name"]


async def test_a_timeout_is_reported_as_such():
    """Nothing listening on a closed port: the adapter must give up rather than
    hang a scheduler pass."""
    api = XrayAPI("127.0.0.1", 9, "t", LINK, timeout=0.4)
    with pytest.raises(OutlineError):
        await api.get_server_info()


def test_grpc_framing_round_trips():
    from outline_panel.core.xray import grpc
    assert grpc.unframe(grpc.frame(b"hello")) == b"hello"
    assert grpc.unframe(b"") == b""          # an empty response is not an error
    assert grpc.frame(b"ab")[:5] == b"\x00\x00\x00\x00\x02"


def test_a_grpc_error_keeps_its_status_code():
    err = GrpcError("nope", 5)
    assert err.status == 5


# ------------------------------------- the panel enforcing what Xray cannot
async def test_the_scheduler_cuts_off_a_customer_the_node_cannot_cap(node):
    """The other half of `enforces_data_limit = False`.

    Outline stops a key at its ceiling itself. Xray will not, so unless the
    scheduler does it, a customer on an Xray node keeps their tunnel open
    forever and the allowance means nothing. This is the test that makes the
    capability flag more than documentation.
    """
    import os
    import tempfile

    from outline_panel.core import scheduler
    from outline_panel.core.db import DB
    from outline_panel.core.settings import SettingsStore

    fake, api = node
    key = await api.create_key(name="over-quota")
    kid = key["id"]

    db = DB(os.path.join(tempfile.mkdtemp(), "cap.db"))
    await db.init()
    store = SettingsStore(db)
    await db.add_server("x1", "Edge", "grpc://127.0.0.1")
    await db.add_key("x1", kid, "over-quota", 10_000, None)

    class Reg:
        def ids(self): return ["x1"]
        def get(self, sid): return api if sid == "x1" else None
        def meta(self, sid): return {"id": sid, "name": "Edge"}

    fake.traffic[kid] = (4_000, 4_000)          # 8 KB of a 10 KB allowance
    await scheduler._check_once(Reg(), db, None, set(), store)
    assert kid in fake.users, "cut off before reaching the allowance"

    fake.traffic[kid] = (6_000, 6_000)          # 12 KB — past it
    await scheduler._check_once(Reg(), db, None, set(), store)
    assert kid not in fake.users, "ran past the allowance and kept connecting"

    # the row is *not* marked disabled: that flag means an admin suspended them,
    # and setting it here would make the monthly reset skip this very key
    assert (await db.get_key("x1", kid))["disabled"] == 0
    await db.close()


async def test_capping_the_same_customer_twice_is_quiet(node):
    """The scheduler re-applies this every pass. An error each time would be
    noise that trains people to ignore the log."""
    fake, api = node
    key = await api.create_key()
    await api.set_data_limit(key["id"], 0)
    await api.set_data_limit(key["id"], 0)      # already gone
    assert key["id"] not in fake.users


async def test_a_monthly_reset_brings_a_capped_customer_back(node):
    """`set_data_limit(k, n)` on a key the panel cut off has to make them
    connectable again, or the quota refreshes on paper only."""
    fake, api = node
    key = await api.create_key()
    await api.set_data_limit(key["id"], 0)
    assert key["id"] not in fake.users
    await api.set_data_limit(key["id"], 30 * 1024 ** 3)
    assert key["id"] in fake.users


# ------------------------------------------------- registering one, over HTTP
async def test_an_xray_node_can_be_added_through_the_panel():
    """End to end: the operator pastes an address, the panel proves it can
    reach the node *and* that the inbound tag is right, and a customer sold onto
    it afterwards gets a working vless:// link."""
    import os
    import sys
    import tempfile

    import httpx

    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "xr.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    os.environ.pop("BOT_TOKEN", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps

    await deps.db.init()
    await deps.settings.bootstrap()
    async with FakeXray() as fake:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                              base_url="http://panel.example.com")
        await c.post("/api/login", json={"password": "pw"})

        r = await c.post("/api/servers", json={
            "name": "Frankfurt VLESS", "kind": "xray",
            "apiUrl": f"127.0.0.1:{fake.port}", "inboundTag": "vless-in",
            "link": {"address": "de.example.com", "port": 443,
                     "security": "reality", "pbk": "key", "sni": "example.org"}})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]

        # it shows up like any other server, because nothing downstream knows
        servers = (await c.get("/api/servers")).json()["servers"]
        assert [s["name"] for s in servers] == ["Frankfurt VLESS"]
        assert servers[0]["reachable"] is True

        key = (await c.post(f"/api/servers/{sid}/keys",
                            json={"name": "Ali", "limit_gb": 10, "days": 30})).json()
        assert key["accessUrl"].startswith("vless://")
        assert "de.example.com:443" in key["accessUrl"]
        assert key["id"] in fake.users

        # and the ordinary key operations reach the node
        assert (await c.post(
            f"/api/servers/{sid}/keys/{key['id']}/disable")).status_code == 200
        assert key["id"] not in fake.users, "suspending did not stop the traffic"
        assert (await c.post(
            f"/api/servers/{sid}/keys/{key['id']}/enable")).status_code == 200
        assert key["id"] in fake.users

        assert (await c.delete(
            f"/api/servers/{sid}/keys/{key['id']}")).status_code == 200
        assert key["id"] not in fake.users
        await c.aclose()
    await deps.db.close()


async def test_adding_a_node_with_the_wrong_inbound_tag_is_refused():
    """The tag is the one thing a typo in makes everything else silently fail:
    users would be added to an inbound nobody is connecting to."""
    import os
    import sys
    import tempfile

    import httpx

    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "xr2.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    async with FakeXray() as fake:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                              base_url="http://panel.example.com")
        await c.post("/api/login", json={"password": "pw"})
        r = await c.post("/api/servers", json={
            "name": "typo", "kind": "xray", "apiUrl": f"127.0.0.1:{fake.port}",
            "inboundTag": "vless-inn"})
        assert r.status_code == 400
        assert "xray" in r.json()["detail"].lower()
        assert (await c.get("/api/servers")).json()["servers"] == []
        await c.aclose()
    await deps.db.close()


async def test_an_unreachable_xray_is_refused_with_a_readable_reason():
    import os
    import sys
    import tempfile

    import httpx

    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "xr3.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                          base_url="http://panel.example.com")
    await c.post("/api/login", json={"password": "pw"})
    r = await c.post("/api/servers", json={"name": "gone", "kind": "xray",
                                           "apiUrl": "127.0.0.1:9"})
    assert r.status_code == 400
    r = await c.post("/api/servers", json={"name": "nonsense", "kind": "xray",
                                           "apiUrl": "not-an-address"})
    assert r.status_code == 400 and "host:port" in r.json()["detail"]
    await c.aclose()
    await deps.db.close()


# ------------------------------------------- what a second backend flushed out
def test_a_vless_config_keeps_its_query_string_in_a_subscription():
    """The bug a second backend exists to find.

    `_with_label` used to strip everything after `?` for any scheme it did not
    recognise. For Outline that is right — the `/?outline=1` is noise. For VLESS
    the query string *is* the configuration: drop the Reality public key, the
    SNI and the flow and the customer is left with a line that cannot connect.
    """
    from outline_panel.web.routers.subscription import _with_label
    vless = ("vless://uuid@de.example.com:443?type=tcp&security=reality"
             "&sni=www.microsoft.com&pbk=KEY&fp=chrome&flow=xtls-rprx-vision#old")
    out = _with_label(vless, "Ali · Frankfurt")
    for needed in ("security=reality", "pbk=KEY", "sni=www.microsoft.com",
                   "flow=xtls-rprx-vision"):
        assert needed in out, f"{needed} was stripped: {out}"
    assert out.endswith("#Ali%20%C2%B7%20Frankfurt")
    assert out.count("#") == 1, "the old label survived"


def test_an_outline_config_still_loses_its_cruft():
    from outline_panel.web.routers.subscription import _with_label
    out = _with_label("ss://YWVzOnB3@1.2.3.4:8388/?outline=1#stale", "Ali")
    assert out == "ss://YWVzOnB3@1.2.3.4:8388#Ali"


async def test_the_customer_keeps_their_name_across_a_panel_restart(node):
    """Xray stores no display name, so a fresh adapter knows none. The panel's
    own row does — and reporting the UUID as the name would look like an answer
    and overwrite it."""
    fake, api = node
    await api.create_key(name="Ali Rezaei")
    assert (await api.list_keys())[0]["name"] == "Ali Rezaei"

    restarted = XrayAPI("127.0.0.1", fake.port, "vless-in", LINK)
    assert (await restarted.list_keys())[0]["name"] is None, (
        "the adapter invented a name, which the panel would take as authoritative")


def test_a_bare_host_port_still_shows_a_host():
    """An Xray address has no scheme, which urlparse reads as a path with no
    netloc — the servers list showed an empty column."""
    from outline_panel.web.deps import host
    assert host("127.0.0.1:10085") == "127.0.0.1:10085"
    assert host("https://1.2.3.4:1234/secret") == "1.2.3.4:1234"


async def test_a_backend_without_metrics_does_not_500_the_key_list():
    """`MetricsCapable` is a capability the port declares and no call site
    honoured: /api/keys and /api/stats asked every backend for Outline's
    experimental metrics, so one Xray server made both endpoints 500."""
    import os
    import sys
    import tempfile

    import httpx

    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "xr4.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    await deps.db.init()
    await deps.settings.bootstrap()
    async with FakeXray() as fake:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                              base_url="http://panel.example.com")
        await c.post("/api/login", json={"password": "pw"})
        r = await c.post("/api/servers", json={
            "name": "Edge", "kind": "xray", "apiUrl": f"127.0.0.1:{fake.port}",
            "inboundTag": "vless-in", "link": {"address": "e.example.com", "port": 443}})
        sid = r.json()["id"]
        await c.post(f"/api/servers/{sid}/keys",
                     json={"name": "Ali", "limit_gb": 5, "days": 30})

        keys = await c.get("/api/keys")
        assert keys.status_code == 200, keys.text
        assert [k["name"] for k in keys.json()["keys"]] == ["Ali"]
        stats = await c.get("/api/stats")
        assert stats.status_code == 200
        assert stats.json()["available"] is False, "claimed metrics it cannot serve"

        # and the per-server settings screen, which asks the same question
        settings = await c.get(f"/api/servers/{sid}/settings")
        assert settings.status_code == 200
        assert settings.json()["metricsEnabled"] is None
        # toggling something this backend cannot do is refused, not a 500
        r = await c.put(f"/api/servers/{sid}/settings/metrics", json={"enabled": True})
        assert r.status_code == 400 and "does not support" in r.json()["detail"]
        await c.aclose()
    await deps.db.close()
