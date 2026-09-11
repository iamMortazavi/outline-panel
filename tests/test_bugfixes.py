"""
Bugs found by reading the code after the fan-out work, each reproduced first.

Three of them were states the panel could reach and then report incorrectly —
a config still handed out for a server that was gone, a customer owned by an
admin who no longer existed, a setting written by a request that answered 400.
"""
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from test_hardening import _login, _mk_sub, app  # noqa: E402, F401  (fixtures)


async def _customer_on(deps, fakes, sids, token="tok", name="Ali"):
    """One customer with a config on each of `sids`, under one subscription."""
    for sid in sids:
        key = await fakes[sid].create_key(name=name)
        await deps.db.add_key(sid, key["id"], name, None, None)
        await deps.db.set_sub_token(sid, key["id"], token)


def _configs(raw_body: str) -> list[str]:
    return base64.b64decode(raw_body).decode().splitlines()


# ------------------------------------------------------------------- servers
async def test_deleting_a_server_stops_its_config_being_handed_out(app):
    """Removing a server drops its key rows, but the cached subscription
    summary kept serving the config — so a customer went on being handed a
    working config for a server this panel no longer manages, for as long as
    the cache lasted. Unlinking a server already invalidated; deleting one is
    the same change of membership and did not.
    """
    application, deps, fakes = app
    await _customer_on(deps, fakes, ("s1", "s2"))
    c = await _login(application, "admin", "pw")

    r = await c.get("/sub/tok?format=raw")
    assert len(_configs(r.text)) == 2          # fill the cache

    assert (await c.delete("/api/servers/s2")).status_code == 200

    r = await c.get("/sub/tok?format=raw")
    assert len(_configs(r.text)) == 1, "the deleted server is still being served"
    await c.aclose()


async def test_the_server_list_survives_a_server_vanishing_mid_request(app):
    """Every other fan-out tolerates a server disappearing between listing the
    ids and asking about them; this one dereferenced None and 500'd."""
    from outline_panel.web.routers.servers import _server_info

    info = await _server_info("never-existed")
    assert info["id"] == "never-existed" and info["reachable"] is False


# -------------------------------------------------------------------- admins
async def test_deleting_an_admin_hands_their_customers_back_to_the_owner(app):
    """Their keys kept pointing at an id with no row behind it. Nothing crashed
    — the owner sees every key and the name fell back to theirs — so the panel
    showed the users as the owner's while the database disagreed, and a filter
    by that admin still offered them under a person who was gone."""
    application, deps, fakes = app
    aid = await _mk_sub(deps, "sara", servers="s1")
    key = await fakes["s1"].create_key(name="Sara's customer")
    await deps.db.add_key("s1", key["id"], "Sara's customer", None, None,
                          owner_admin_id=aid)

    c = await _login(application, "admin", "pw")
    assert (await c.delete(f"/api/admins/{aid}")).status_code == 200

    row = await deps.db.get_key("s1", key["id"])
    assert row["owner_admin_id"] is None, "key still owned by a deleted admin"

    # and the customer is still there, still working — deleting the reseller
    # must not delete the people who bought from them
    listed = (await c.get("/api/keys")).json()["keys"]
    assert [k["name"] for k in listed] == ["Sara's customer"]
    assert listed[0]["ownerAdminId"] is None
    await c.aclose()


async def test_deleting_an_admin_leaves_other_admins_customers_alone(app):
    """The UPDATE is keyed on the deleted admin — nobody else's keys move."""
    application, deps, fakes = app
    gone = await _mk_sub(deps, "sara", servers="s1")
    stays = await _mk_sub(deps, "reza", servers="s1")
    for owner, name in ((gone, "sara-cust"), (stays, "reza-cust")):
        key = await fakes["s1"].create_key(name=name)
        await deps.db.add_key("s1", key["id"], name, None, None, owner_admin_id=owner)

    c = await _login(application, "admin", "pw")
    assert (await c.delete(f"/api/admins/{gone}")).status_code == 200

    owners = {k["name"]: k["ownerAdminId"]
              for k in (await c.get("/api/keys")).json()["keys"]}
    assert owners == {"sara-cust": None, "reza-cust": stays}
    await c.aclose()


# ------------------------------------------------------------------ settings
async def test_a_rejected_settings_write_changes_nothing(app):
    """The loop validated and wrote one key at a time and stopped at the first
    bad one, so a form with a typo in it answered 400 *after* moving the
    billing cycle — leaving the panel in a state nobody asked for and the
    settings screen still showing the old value."""
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    before = (await c.get("/api/settings/panel")).json()["values"]

    r = await c.put("/api/settings/panel",
                    json={"cycle_days": 7, "not_a_real_setting": 1})
    assert r.status_code == 400

    after = (await c.get("/api/settings/panel")).json()["values"]
    assert after == before, "a rejected write still applied part of the form"
    assert await deps.settings.num("cycle_days") == before["cycle_days"]
    await c.aclose()


@pytest.mark.parametrize("bad", [
    {"cycle_days": 7, "metrics_ttl": 999999},      # out of range
    {"cycle_days": 7, "login_window": "abc"},      # not a number
    {"cycle_days": 7, "currency": ""},             # empty string knob
])
async def test_every_kind_of_bad_value_rejects_the_whole_form(app, bad):
    application, _, _ = app
    c = await _login(application, "admin", "pw")
    before = (await c.get("/api/settings/panel")).json()["values"]
    assert (await c.put("/api/settings/panel", json=bad)).status_code == 400
    assert (await c.get("/api/settings/panel")).json()["values"] == before
    await c.aclose()


async def test_a_valid_settings_write_still_applies_in_full(app):
    """The guard must not have turned the endpoint into a no-op."""
    application, deps, _ = app
    c = await _login(application, "admin", "pw")
    r = await c.put("/api/settings/panel",
                    json={"cycle_days": 7, "metrics_ttl": 30, "currency": "USD"})
    assert r.status_code == 200
    assert await deps.settings.num("cycle_days") == 7
    assert await deps.settings.num("metrics_ttl") == 30
    assert await deps.settings.text("currency") == "USD"
    await c.aclose()


# ------------------------------------------------------- the unknown-key guard
# `ensure_local` refuses an id Outline does not have — otherwise an invented id
# got a local row, a customer link, and (once anything mirrored it onto a second
# server) a real Outline key built from the invented row. Nothing exercised that
# guard: the test double answered KeyError for an unknown id, which no
# `except OutlineError` catches, so the path was never reached. The double now
# 404s the way the real API does.
@pytest.mark.parametrize("call", [
    ("put", "/api/servers/s1/keys/999999/name", {"name": "x"}),
    ("put", "/api/servers/s1/keys/999999/limit", {"limit_gb": 5}),
    ("put", "/api/servers/s1/keys/999999/monthly", {"monthly_gb": 5}),
    ("post", "/api/servers/s1/keys/999999/disable", None),
    ("post", "/api/servers/s1/keys/999999/enable", None),
    ("post", "/api/servers/s1/keys/999999/sub", None),
])
async def test_an_id_outline_does_not_have_is_a_404(app, call):
    application, deps, _ = app
    method, path, body = call
    c = await _login(application, "admin", "pw")
    r = await getattr(c, method)(path, **({"json": body} if body else {}))
    assert r.status_code == 404, f"{method.upper()} {path} -> {r.status_code} {r.text}"
    assert await deps.db.get_key("s1", "999999") is None, "an invented id got a row"
    await c.aclose()


async def test_renaming_checks_the_key_before_renaming_it_upstream(app):
    """It called Outline first, so a key the panel had never seen was renamed
    on the server before anything established that it was real."""
    application, deps, fakes = app
    c = await _login(application, "admin", "pw")
    assert (await c.put("/api/servers/s1/keys/424242/name",
                        json={"name": "ghost"})).status_code == 404
    assert "424242" not in fakes["s1"].keys
    await c.aclose()


async def test_enable_adopts_the_key_the_way_disable_does(app):
    """The two halves of one switch must not disagree about adoption."""
    application, deps, fakes = app
    key = await fakes["s1"].create_key(name="made-in-manager")
    c = await _login(application, "admin", "pw")
    assert (await c.post(f"/api/servers/s1/keys/{key['id']}/enable")).status_code == 200
    row = await deps.db.get_key("s1", key["id"])
    assert row is not None and row["sub_token"], "enable left the key unadopted"
    await c.aclose()
