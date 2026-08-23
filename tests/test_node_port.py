"""
Conformance: anything the registry hands out must be a usable backend.

A second backend (VLESS+Reality via Xray or sing-box) is the point of having a
port at all. These tests are what a new adapter is developed against — it should
be possible to write one, point this file at it, and know what is still missing
from a list rather than from an AttributeError in production.

The Xray adapter is here because this suite is what it was developed against —
the port was written first and the adapter had to satisfy it, rather than the
port being widened afterwards to fit whatever the adapter happened to do.

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


def _xray():
    return importlib.import_module("outline_panel.core.xray.api")


def _adapters():
    return [_outline().OutlineAPI("https://1.2.3.4:1/x"),
            _xray().XrayAPI("127.0.0.1", 10085, "vless-in"),
            FakeOutline()]


ADAPTER_IDS = ["outline", "xray", "fake"]


@pytest.fixture(params=[0, 1, 2], ids=ADAPTER_IDS)
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


def test_the_registry_builds_every_adapter_in_one_place():
    """Whatever `reg.get()` returns is used as a backend without checking, so
    the factory is the one place this can go wrong.

    It used to construct `OutlineAPI` inline in two methods. A second backend
    turned that into a branch — and the rule is that the branch stays single:
    a third literal somewhere else is a backend that misses whatever the
    factory learns next.
    """
    import inspect as _inspect

    from outline_panel.web import registry
    source = _inspect.getsource(registry)
    assert "def build(" in source, "the adapter factory is gone"
    # every construction lives inside build(); the methods call it
    factory = source[source.index("def build("):source.index("class Registry")]
    assert "OutlineAPI(" in factory and "XrayAPI(" in factory
    outside = source.replace(factory, "")
    for adapter in ("OutlineAPI(", "XrayAPI("):
        assert adapter not in outside, (
            f"{adapter} is constructed outside build() — put it in the factory")


def test_an_unknown_backend_is_refused_rather_than_guessed():
    """A row with a kind nobody implements must not quietly become an Outline
    client pointed at an Xray port."""
    from outline_panel.web import registry
    with pytest.raises(ValueError):
        registry.build({"id": "x", "api_url": "https://h:1/p", "kind": "wireguard"})


def test_a_row_from_before_the_column_existed_is_an_outline_server():
    """Migration 010 backfills `kind`, but a hand-edited row or an old backup
    may have none. Outline is the only thing this panel could talk to before,
    so that is what a missing kind means."""
    from outline_panel.web import registry
    client = registry.build({"id": "x", "api_url": "https://1.2.3.4:1/p"})
    assert type(client).__name__ == "OutlineAPI"


def test_a_backend_says_whether_it_can_cap_a_key_itself():
    """The one place the two backends genuinely differ in capability, and the
    panel has to know: Outline enforces a data limit server-side, Xray cannot.
    An adapter that stays quiet is read as "yes" — the optimistic answer, and
    the one where customers never stop at their allowance.
    """
    outline = _outline().OutlineAPI("https://1.2.3.4:1/x")
    xray = _xray().XrayAPI("127.0.0.1", 10085, "vless-in")
    assert getattr(outline, "enforces_data_limit", True) is True
    assert xray.enforces_data_limit is False


def test_the_adapters_do_not_share_an_implementation():
    """A second adapter that inherits from the first is not a second adapter —
    it is the first one with the parts nobody exercised yet."""
    outline_cls = type(_outline().OutlineAPI("https://1.2.3.4:1/x"))
    xray_cls = type(_xray().XrayAPI("h", 1, "t"))
    assert not issubclass(xray_cls, outline_cls)
    assert not issubclass(outline_cls, xray_cls)
