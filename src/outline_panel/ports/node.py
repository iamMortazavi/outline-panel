"""
What the panel needs from a VPN server, and nothing more.

`OutlineAPI` satisfies this today. The reason it is written down as a protocol
now rather than when a second backend arrives is timing: every call site moved
during the A1 fix, so naming the surface cost almost nothing here and would have
meant editing the same code twice later.

The split matters. `NodePort` is what the panel genuinely cannot work without —
if a backend cannot do these, it cannot host a customer. Everything else is a
capability, checked at the call site, because an Xray node has no equivalent of
Outline's experimental metrics endpoint and a panel that assumed one would
simply be broken against it rather than degraded.

**Structural typing on purpose.** Nothing has to inherit from these; a class
that has the methods satisfies them. So `OutlineAPI` did not change to adopt the
port, and a test double does not have to import anything to be one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NodePort(Protocol):
    """The minimum a backend must do to hold customers.

    Every method raises `OutlineError` on failure — the name is historical, but
    the contract (a `status` of the upstream HTTP code, or None when the server
    could not be reached at all) is what the executor branches on, so a second
    adapter must raise the same type. A 404 in particular has to be
    distinguishable: "the key is already gone" is success for a delete.
    """

    async def list_keys(self) -> list[dict]:
        """Every key on this node: `[{"id": str, "name": str|None,
        "accessUrl": str, "dataLimit": {"bytes": int}|{}}]`."""

    async def get_key(self, key_id: str) -> dict:
        """One key, same shape. Raises with status 404 when it does not exist."""

    async def create_key(self, name: str | None = None,
                         limit_bytes: int | None = None) -> dict:
        """Make one. Must return at least `id` and `accessUrl`.

        `accessUrl` is what reaches the customer, so a non-Outline adapter
        returns whatever its clients understand — `vless://…` rather than
        `ss://…`. The panel treats it as opaque except when labelling it.
        """

    async def rename_key(self, key_id: str, name: str) -> None: ...

    async def delete_key(self, key_id: str) -> None:
        """Remove it. Raising with status 404 is acceptable and is read as
        success by the executor: the key being gone is the state asked for."""

    async def set_data_limit(self, key_id: str, limit_bytes: int) -> None:
        """Cap this key's cumulative transfer. A limit of 0 is how the panel
        suspends someone, so it must actually stop traffic rather than being
        treated as "no limit"."""

    async def remove_data_limit(self, key_id: str) -> None:
        """Back to unlimited. Not the same as a very large limit."""

    async def get_transfer_metrics(self) -> dict[str, int]:
        """`{key_id: bytes_transferred}`, cumulative and never reset.

        The panel's whole time-and-quota model rests on this being cumulative:
        activation is "usage went above zero", and a quota reset is "raise the
        ceiling to current usage plus the allowance". A backend that zeroes its
        counters would need `reset_usage` rethought, not just an adapter.
        """

    async def get_server_info(self) -> dict:
        """`{"name": str, "version": str, ...}`. Also the reachability probe —
        the health checker calls exactly this."""

    async def close(self) -> None:
        """Release connections. Called when a server is removed or replaced."""


@runtime_checkable
class MetricsCapable(Protocol):
    """Per-key last-seen, peak devices, bandwidth, geography.

    Outline's experimental endpoint, and off by default even there. The
    dashboard degrades to "advanced stats are off" without it, which is why this
    is a capability and not part of `NodePort`.
    """

    async def get_metrics_enabled(self) -> bool: ...
    async def set_metrics_enabled(self, enabled: bool) -> None: ...
    async def get_server_metrics_cached(self, since: str = "30d",
                                        ttl: float = 15.0) -> dict: ...


@runtime_checkable
class ServerAdminCapable(Protocol):
    """Settings that belong to the node rather than to a key."""

    async def rename_server(self, name: str) -> None: ...
    async def set_global_data_limit(self, limit_bytes: int) -> None: ...
    async def remove_global_data_limit(self) -> None: ...


#: Every method `NodePort` requires. Used by the conformance check in the tests,
#: so a new adapter finds out what it is missing from a list rather than from a
#: production AttributeError.
REQUIRED = (
    "list_keys", "get_key", "create_key", "rename_key", "delete_key",
    "set_data_limit", "remove_data_limit", "get_transfer_metrics",
    "get_server_info", "close",
)
