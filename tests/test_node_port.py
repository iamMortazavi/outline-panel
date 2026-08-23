"""
Conformance: anything the registry hands out must be a usable backend.

A second backend (VLESS+Reality via Xray or sing-box) is the point of having a
port at all. These tests are what a new adapter is developed against — it should
be possible to write one, point this file at it, and know what is still missing
from a list rather than from an AttributeError in production.

The test double is included deliberately. `FakeOutline` drifting from the real
client is not hypothetical: it was missing `get_metrics_enabled` entirely, so
`/api/servers/{sid}/settings` 500'd under test on an error the real client
cannot raise, and it tracked data limits in a side dict so it reported one thing
to the reconciler and another to the panel. Both were found by writing this.
"""

import importlib
import inspect

import pytest

from test_features import FakeOutline


# Imported at call time, never at module scope. The app fixtures elsewhere in
# this suite drop every `outline_panel` module and re-import it, so a class
# captured here would be a different object from the one the code under test
# raises — and `pytest.raises` would miss it. This bit us once already.
def _port():
    return importlib.import_module("outline_panel.ports.node")


def _outline():
    return importlib.import_module("outline_panel.core.outline_api")


def _adapters():
    return [_outline().OutlineAPI("https://1.2.3.4:1/x"), FakeOutline()]


ADAPTER_IDS = ["outline", "fake"]


@pytest.fixture(params=[0, 1], ids=ADAPTER_IDS)
def adapter(request):
    return _adapters()[request.param]


def test_the_required_surface_is_complete(adapter):
    missing = [name for name in _port().REQUIRED if not callable(getattr(adapter, name, None))]
    assert not missing, (
        f"{type(adapter).__name__} cannot hold customers — missing: {missing}")


def test_every_required_method_is_awaitable(adapter):
    """The panel awaits all of these. A synchronous one would not fail at import
    time, it would fail the first time a customer was created."""
    sync = [name for name in _port().REQUIRED
            if not inspect.iscoroutinefunction(getattr(adapter, name))]
    assert not sync, f"{type(adapter).__name__}: not async — {sync}"


def test_it_registers_as_a_node(adapter):
    assert isinstance(adapter, _port().NodePort)


def test_capabilities_are_all_or_nothing(adapter):
    """Half a capability is worse than none: the call site checks once and then
    uses the whole group, so an adapter with `get_metrics_enabled` but no
    `set_metrics_enabled` would pass the check and then break on the toggle."""
    port = _port()
    for cap in (port.MetricsCapable, port.ServerAdminCapable):
        names = [n for n in dir(cap) if not n.startswith("_")]
        present = [n for n in names if callable(getattr(adapter, n, None))]
        assert not present or len(present) == len(names), (
            f"{type(adapter).__name__} implements part of {cap.__name__}: "
            f"has {present}, needs {names}")


def test_create_key_returns_what_the_panel_stores():
    """`id` and `accessUrl` are the two fields every caller uses — the id keys
    every row and the URL is what reaches the customer."""
    import asyncio
    fake = FakeOutline()
    key = asyncio.run(fake.create_key(name="x", limit_bytes=1024))
    assert isinstance(key.get("id"), str) and key["id"]
    assert isinstance(key.get("accessUrl"), str) and "://" in key["accessUrl"]


def test_a_missing_key_is_a_404_not_a_crash():
    """The executor reads `OutlineError.status` to tell "already gone" from "the
    server is down", and treats 404 on a delete as success. An adapter that
    raises something else turns a completed deletion into a stuck outbox row."""
    import asyncio
    fake = FakeOutline()
    with pytest.raises(_outline().OutlineError) as caught:
        asyncio.run(fake.get_key("nope"))
    assert caught.value.status == 404


def test_the_registry_only_ever_hands_out_nodes():
    """Whatever `reg.get()` returns is used as a backend without checking, so
    the type it constructs is the one place this can go wrong."""
    import inspect as _inspect

    from outline_panel.web import registry
    source = _inspect.getsource(registry)
    assert "OutlineAPI(" in source, "the registry stopped building a known adapter"
    # A second backend arrives here: the registry picks an adapter per server
    # row. Until then there is one, and this records that it is deliberate.
    assert source.count("OutlineAPI(") == 2, (
        "the registry builds adapters in more than the two known places — a "
        "second backend belongs behind one factory, not a third literal")
