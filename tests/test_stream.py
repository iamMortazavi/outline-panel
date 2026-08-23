"""
The live stream: one sampler for the panel, not one poll loop per tab.

The two things worth proving are the reason it exists and the reason it is
dangerous. The reason it exists: adding tabs must not add load on the Outline
servers. The reason it is dangerous: it is a long-lived connection carrying
customer data, so scoping and revocation have to hold on it exactly as they do
on a REST request — a stream that leaked one reseller's customers to another
would be invariant A-2 breaking, faster.
"""

import asyncio
import contextlib
import json
import os
import sys
import tempfile

import httpx
import pytest

from test_features import FakeOutline

GB = 1024 ** 3


@pytest.fixture
async def panel():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "stream.db")
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
    from outline_panel.web.stream import hub
    await hub.stop()
    await deps.db.close()


async def _login(application, user="admin", pw="pw"):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                          base_url="http://panel.example.com", timeout=10)
    r = await c.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return c


class SSE:
    """Drive an SSE endpoint over ASGI directly.

    Not through httpx: its `ASGITransport` is not a real transport and buffers a
    streaming response, so a stream that emits one small frame and then waits —
    which is exactly what a live view does — never arrives. Driving the app
    ourselves is also the more precise test: it sees the frames in the order and
    at the moment the server actually produced them.
    """

    def __init__(self, app, cookie: str):
        self.app = app
        self.cookie = cookie
        self.frames: list[tuple[str, str]] = []   # (event name, data)
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._buf = ""
        self._got = asyncio.Event()
        self._done = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "path": "/api/stream", "raw_path": b"/api/stream",
            "query_string": b"", "root_path": "", "scheme": "http",
            "client": ("127.0.0.1", 5000), "server": ("panel.example.com", 80),
            "headers": [(b"host", b"panel.example.com"),
                        (b"accept", b"text/event-stream"),
                        (b"cookie", f"outline_session={self.cookie}".encode())],
        }

        async def receive():
            # A browser sends nothing until it goes away; mimic that, so the
            # server's disconnect handling is exercised on __aexit__.
            await self._done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.headers = {k.decode().lower(): v.decode()
                                for k, v in message["headers"]}
            elif message["type"] == "http.response.body":
                self._absorb(message.get("body", b""))

        self._task = asyncio.create_task(self.app(scope, receive, send))
        return self

    def _absorb(self, chunk: bytes) -> None:
        self._buf += chunk.decode()
        while "\n\n" in self._buf:
            frame, self._buf = self._buf.split("\n\n", 1)
            name, data = None, None
            for line in frame.splitlines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
                elif line.startswith(":"):
                    name = "comment"
            if name:
                self.frames.append((name, data or ""))
                self._got.set()

    async def next_frame(self, name: str, timeout: float = 15.0):
        """Wait for the next frame with this event name and return its data."""
        seen = 0
        async def wait():
            nonlocal seen
            while True:
                for ev, data in self.frames[seen:]:
                    seen += 1
                    if ev == name:
                        return data
                self._got.clear()
                await self._got.wait()
        return await asyncio.wait_for(wait(), timeout)

    async def snapshot(self, timeout: float = 15.0) -> dict:
        return json.loads(await self.next_frame("snapshot", timeout))

    async def __aexit__(self, *exc):
        self._done.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


async def _first_snapshot(client, timeout=15.0):
    """Open a stream as this client, read one snapshot, and close it."""
    async with SSE(client._transport.app, client.cookies.get("outline_session")) as sse:
        assert sse.status is None or sse.status == 200
        snap = await sse.snapshot(timeout)
        assert sse.status == 200
        assert sse.headers["content-type"].startswith("text/event-stream")
        assert sse.headers["cache-control"] == "no-store"
        assert sse.headers["x-accel-buffering"] == "no"
        return snap


# ------------------------------------------------------------------- content
async def test_the_stream_sends_what_the_rest_route_sends(panel):
    """One shaping function, so the live view cannot drift from /api/keys.

    If these two ever disagree, the panel shows one thing on load and another a
    few seconds later, which reads as data loss.
    """
    application, deps, fakes = panel
    c = await _login(application)
    await c.post("/api/servers/s1/keys", json={"name": "Ali", "limit_gb": 10, "days": 30})

    snapshot = await _first_snapshot(c)
    rest = (await c.get("/api/keys")).json()
    assert [k["id"] for k in snapshot["keys"]] == [k["id"] for k in rest["keys"]]
    assert snapshot["keys"][0]["name"] == "Ali"
    assert set(snapshot) == {"keys", "errors", "stats"}
    assert snapshot["stats"]["serverCount"] == 2
    await c.aclose()


async def test_a_reseller_only_sees_their_own_customers(panel):
    """The snapshot is taken as the owner — that is the only way to take it
    once. Filtering per subscriber is therefore the whole security boundary."""
    application, deps, fakes = panel
    owner = await _login(application)
    r = await owner.post("/api/admins", json={
        "username": "sara", "password": "sara-pw",
        "caps": ["keys.view", "keys.create"], "servers": ["s1"]})
    sara_id = r.json()["id"]
    mine = (await owner.post("/api/servers/s1/keys",
                             json={"name": "owners", "limit_gb": 5, "days": 0})).json()["id"]
    sara = await _login(application, "sara", "sara-pw")
    hers = (await sara.post("/api/servers/s1/keys",
                            json={"name": "saras", "limit_gb": 5, "days": 0})).json()["id"]
    assert (await deps.db.get_key("s1", hers))["owner_admin_id"] == sara_id

    snapshot = await _first_snapshot(sara)
    ids = [k["id"] for k in snapshot["keys"]]
    assert ids == [hers], f"a reseller's stream showed {ids}"
    assert mine not in ids
    # ...and only the servers she was granted
    assert [p["id"] for p in snapshot["stats"]["perServer"]] == ["s1"]
    assert snapshot["stats"]["serverCount"] == 1
    await sara.aclose()
    await owner.aclose()


async def test_an_unreachable_server_arrives_as_an_error_not_a_gap(panel):
    """Same rule as the REST route: errors travel beside the keys. A server
    that is down must not silently shorten the customer list."""
    application, deps, fakes = panel
    c = await _login(application)
    await c.post("/api/servers/s1/keys", json={"name": "Ali", "limit_gb": 1, "days": 0})

    class Dead(FakeOutline):
        async def list_keys(self):
            from outline_panel.core.outline_api import OutlineError
            raise OutlineError("connection refused")
    deps.reg.servers["s2"]["api"] = Dead()

    snapshot = await _first_snapshot(c)
    assert [e["serverId"] for e in snapshot["errors"]] == ["s2"]
    assert [k["name"] for k in snapshot["keys"]] == ["Ali"]
    await c.aclose()


# ------------------------------------------------------------------- access
async def test_the_stream_needs_a_session(panel):
    application, deps, fakes = panel
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                             base_url="http://panel.example.com")
    r = await anon.get("/api/stream")
    assert r.status_code == 401
    await anon.aclose()


async def test_revoking_an_admin_closes_their_stream(panel):
    """A REST request re-reads the admin row every time, which is what makes
    revocation instant. A stream can be open for hours, so it has to re-check on
    every tick or disabling someone would leave them watching."""
    application, deps, fakes = panel
    owner = await _login(application)
    r = await owner.post("/api/admins", json={
        "username": "sara", "password": "sara-pw", "caps": ["keys.view"],
        "servers": ["s1"]})
    sara_id = r.json()["id"]
    sara = await _login(application, "sara", "sara-pw")

    async with SSE(application, sara.cookies.get("outline_session")) as sse:
        await sse.next_frame("hello")
        await deps.db.update_admin(sara_id, disabled=1)
        await sse.next_frame("bye")
        names = [n for n, _ in sse.frames]
    assert names[-1] == "bye", f"the stream stayed open after revocation: {names}"
    await sara.aclose()
    await owner.aclose()


# ------------------------------------------------------------------- sampler
async def test_the_sampler_stops_when_the_last_tab_closes(panel):
    """Nobody watching means nobody talking to the Outline servers. Without
    this the panel keeps polling every server forever after the last tab is
    shut, which is the load the stream exists to remove."""
    from outline_panel.web.stream import hub
    application, deps, fakes = panel
    c = await _login(application)
    await _first_snapshot(c)
    # the context manager in _first_snapshot has exited, so the tab is gone
    for _ in range(50):
        if hub.subscribers == 0:
            break
        await asyncio.sleep(0.05)
    assert hub.subscribers == 0
    assert hub._task is None, "the sampler is still running with nobody watching"
    await c.aclose()


async def test_one_sample_serves_every_tab(panel):
    """The whole point. Ten tabs used to mean ten fan-outs to every server; now
    a tick costs one call per server no matter how many people are watching."""
    from outline_panel.web.stream import hub
    application, deps, fakes = panel
    c = await _login(application)
    await c.post("/api/servers/s1/keys", json={"name": "Ali", "limit_gb": 1, "days": 0})

    calls = {"n": 0}
    real = fakes["s1"].list_keys

    async def counting():
        calls["n"] += 1
        return await real()
    fakes["s1"].list_keys = counting

    snapshot = await hub.sample()
    assert calls["n"] == 1
    # every subscriber is served from that one reading
    for admin in ({"is_owner": 1, "id": None}, {"is_owner": 1, "id": None}):
        from outline_panel.web.stream import visible_to
        assert len(visible_to(admin, snapshot)["keys"]) == 1
    assert calls["n"] == 1, "serving a subscriber went back to the server"
    await c.aclose()
