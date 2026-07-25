"""
Metrics and request-scoped logging. Two questions had no answer before: which
request a log line belongs to, and whether Outline is slow right now.
"""
import json
import logging
import os
import sys
import tempfile

import httpx
import pytest

from outline_panel.core import metrics


@pytest.fixture(autouse=True)
def clean():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture
async def app():
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "w.db")
    os.environ["ADMIN_PASSWORD"] = "pw"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ.pop("OUTLINE_API_URL", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    sys.path.insert(0, os.path.dirname(__file__))
    from outline_panel.web import app as appmod
    from outline_panel.web import deps
    from test_features import FakeOutline
    await deps.db.init()
    await deps.settings.bootstrap()
    fake = FakeOutline()
    deps.reg.servers["s1"] = {"id": "s1", "name": "Tokyo",
                              "api_url": "https://1.2.3.4:1/x",
                              "cert_sha256": None, "api": fake}
    await deps.db.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    # the fixture reloads every outline_panel module, so the metrics registry
    # the app writes to is a *different* object from the one imported at the top
    # of this file — hand the live one back rather than reaching for the stale
    from outline_panel.core import metrics as live
    live.reset()
    yield appmod.app, deps, fake, live
    await deps.db.close()


async def _login(application, user="admin", pw="pw"):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://x")
    assert (await c.post("/api/login", json={"username": user, "password": pw})).status_code == 200
    return c


def test_the_exposition_format_is_parseable():
    metrics.inc("outline_panel_keys_created_total", {"server": "s1"})
    metrics.inc("outline_panel_keys_created_total", {"server": "s1"})
    metrics.observe("outline_panel_keys", 42)
    text = metrics.render()
    assert "# TYPE outline_panel_keys_created_total counter" in text
    assert 'outline_panel_keys_created_total{server="s1"} 2' in text
    assert "outline_panel_keys 42" in text
    # every line is either a comment or `name[{labels}] value`
    for line in text.strip().splitlines():
        assert line.startswith("#") or len(line.rsplit(" ", 1)) == 2


def test_label_values_are_escaped():
    """A server name with a quote in it must not break the format."""
    metrics.inc("outline_panel_outline_calls_total", {"server": 'a"b\\c', "outcome": "ok"})
    line = [x for x in metrics.render().splitlines() if x.startswith("outline_panel_outline")][0]
    assert '\\"' in line and "\\\\" in line


async def test_requests_are_counted_and_tagged(app):
    application, deps, _, metrics = app
    c = await _login(application)
    r = await c.get("/api/keys")
    assert r.headers.get("X-Request-ID")
    text = metrics.render()
    assert 'outline_panel_http_requests_total{method="GET",status="200"}' in text
    assert "outline_panel_http_request_seconds" in text
    await c.aclose()


async def test_an_upstream_request_id_is_kept(app):
    """So a trace survives a reverse proxy."""
    application, deps, _, metrics = app
    c = await _login(application)
    r = await c.get("/api/keys", headers={"X-Request-ID": "trace-me-123"})
    assert r.headers["X-Request-ID"] == "trace-me-123"
    long = await c.get("/api/keys", headers={"X-Request-ID": "x" * 500})
    assert len(long.headers["X-Request-ID"]) <= 64   # it lands in every log line
    await c.aclose()


async def test_outline_calls_are_measured(app):
    """The one place that can answer 'is the upstream failing'."""
    application, deps, fake, metrics = app
    import httpx as _httpx

    from outline_panel.core.outline_api import OutlineAPI, OutlineError

    api = OutlineAPI("https://198.51.100.9:1/x")

    class Boom:
        async def request(self, *a, **kw):
            raise _httpx.ConnectError("refused")

    api._client = Boom()
    with pytest.raises(OutlineError):
        await api.get_server_info()
    text = metrics.render()
    assert 'outcome="unreachable"' in text
    assert 'server="198.51.100.9:1"' in text


async def test_money_and_keys_are_counted(app):
    application, deps, _, metrics = app
    aid = await deps.db.add_admin("sara", "h", "s", caps="keys.create", servers="s1")
    await deps.db.update_admin(aid, credit_enabled=1)
    await deps.db.credit_admin(aid, 10_000, reason="topup")
    await deps.db.charge(aid, 2_500, reason="purchase")
    text = metrics.render()
    assert "outline_panel_credit_added_total 10000" in text
    assert "outline_panel_credit_charged_total 2500" in text

    c = await _login(application)
    await c.post("/api/servers/s1/keys", json={"name": "Ali", "limit_gb": 1})
    assert 'outline_panel_keys_created_total{server="s1"} 1' in metrics.render()
    await c.aclose()


async def test_metrics_is_not_public(app):
    """The labels carry server hostnames and the values carry revenue."""
    application, deps, _, metrics = app
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as anon:
        assert (await anon.get("/metrics")).status_code == 401

    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    await deps.db.add_admin("sara2", h, s, caps="keys.view", servers="s1")
    sub = await _login(application, "sara2", "sara-pw")
    assert (await sub.get("/metrics")).status_code == 401
    await sub.aclose()

    owner = await _login(application)
    r = await owner.get("/metrics")
    assert r.status_code == 200 and "outline_panel_uptime_seconds" in r.text
    await owner.aclose()


async def test_a_scraper_can_use_a_token(app):
    application, deps, _, metrics = app
    await deps.settings.set("metrics_token", "s3cret-token")
    t = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
        ok = await c.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200
        bad = await c.get("/metrics", headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401


async def test_state_gauges_are_sampled_at_scrape_time(app):
    """Sampled, not incremented: a query cannot drift from reality."""
    application, deps, _, metrics = app
    await deps.db.add_key("s1", "9", "Ali", None, None)
    c = await _login(application)
    text = (await c.get("/metrics")).text
    assert "outline_panel_keys 1" in text
    assert "outline_panel_servers 1" in text
    assert "outline_panel_credit_drift 0" in text
    await c.aclose()


def test_json_logging_carries_the_request_id():
    from outline_panel.web.observability import JsonFormatter, request_id
    token = request_id.set("abc123")
    rec = logging.LogRecord("web.access", logging.INFO, "f.py", 1,
                            "%s %s -> %d", ("POST", "/api/x", 200), None)
    rec.status = 200
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["request_id"] == "abc123"
    assert payload["msg"] == "POST /api/x -> 200"
    assert payload["status"] == 200 and payload["level"] == "INFO"
    request_id.reset(token)
