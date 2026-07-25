"""
Error codes. `detail` stays the English sentence it always was — that is what
keeps old clients, curl and every existing test working — and `code` is added
beside it so the UI can translate.
"""
import os
import re
import sys
import tempfile

import httpx
import pytest

from outline_panel.core import errors


def _builders():
    """The zero-argument error builders defined in this module.

    `dir()` also turns up HTTPException, which errors.py imports — a class, not
    a function, and it has no __code__.
    """
    import inspect
    for name, fn in vars(errors).items():
        if (inspect.isfunction(fn) and not name.startswith("_")
                and fn.__module__ == errors.__name__
                and fn.__code__.co_argcount == 0):
            yield name, fn


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
    deps.reg.servers["s1"] = {"id": "s1", "name": "Tokyo",
                              "api_url": "https://1.2.3.4:1/x",
                              "cert_sha256": None, "api": FakeOutline()}
    await deps.db.add_server("s1", "Tokyo", "https://1.2.3.4:1/x")
    yield appmod.app, deps
    await deps.db.close()


async def _c(application, user="admin", pw="pw", login=True):
    t = httpx.ASGITransport(app=application)
    c = httpx.AsyncClient(transport=t, base_url="http://x")
    if login:
        assert (await c.post("/api/login",
                             json={"username": user, "password": pw})).status_code == 200
    return c


async def test_an_error_carries_a_code_and_the_english_detail(app):
    application, _ = app
    c = await _c(application, login=False)
    r = await c.post("/api/login", json={"username": "admin", "password": "no"})
    body = r.json()
    assert r.status_code == 401
    assert body["code"] == "auth.bad_credentials"
    assert body["detail"] == "Wrong username or password"     # unchanged
    await c.aclose()


async def test_params_travel_with_the_code(app):
    """A translated sentence has to be able to rebuild the numbers itself."""
    application, deps = app
    from outline_panel.core import security
    h, s = security.hash_password("sara-pw")
    aid = await deps.db.add_admin("sara", h, s, caps="keys.create", servers="s1")
    await deps.db.update_admin(aid, credit_enabled=1)
    await deps.db.credit_admin(aid, 1_000, reason="topup")
    pid = await deps.db.add_package("30GB", 30, 30, 25_000)

    c = await _c(application, "sara", "sara-pw")
    r = await c.post("/api/servers/s1/keys", json={"name": "Ali", "package_id": pid})
    body = r.json()
    assert r.status_code == 402 and body["code"] == "credit.insufficient"
    assert body["params"]["package"] == "30GB"
    assert body["params"]["price"] == 25_000 and body["params"]["credit"] == 1_000
    await c.aclose()


async def test_an_uncoded_error_still_answers_with_detail(app):
    """Not every error was converted, and the UI falls back to `detail` — so an
    uncoded one has to keep working rather than 500."""
    application, _ = app
    c = await _c(application)
    # a valid body that the *handler* rejects — "x" would be stopped by the
    # schema first and never reach it
    r = await c.post("/api/admins", json={"username": "reza", "password": "secret1",
                                          "caps": [], "servers": []})
    assert r.status_code == 400
    assert "code" not in r.json() and r.json()["detail"]
    await c.aclose()


def test_every_code_is_unique_and_well_formed():
    codes = [fn().code for _, fn in _builders()]
    assert codes, "no zero-argument error builders found"
    assert len(codes) == len(set(codes)), "duplicate error code"
    for c in codes:
        assert re.fullmatch(r"[a-z]+(\.[a-z_]+)+", c), c


def test_the_dictionary_covers_every_code():
    """A code with no Persian entry silently shows English — fine as a fallback,
    but not for a code that already exists when the dictionary is written."""
    js = open(os.path.join(os.path.dirname(__file__), "..", "src",
                           "outline_panel", "static", "i18n.js")).read()
    for _, fn in _builders():
        code = fn().code
        assert f"'err.{code}'" in js, f"no translation for {code}"


def test_a_panel_error_is_still_an_http_exception():
    """Everything that catches HTTPException — FastAPI included — must keep
    catching these."""
    from fastapi import HTTPException
    e = errors.unknown_server()
    assert isinstance(e, HTTPException)
    assert e.status_code == 404 and e.detail == "Unknown server"
