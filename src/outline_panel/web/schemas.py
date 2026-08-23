"""
What the API promises to return.

Not one route declared a `response_model`: every response was a hand-built dict
and the frontend read it by convention, so a renamed field was invisible until
the UI blanked. These models make the contract checkable and put it in the
OpenAPI schema the frontend's types are generated from.

Two rules learned while writing them.

**FastAPI drops what a model does not declare.** A missing field is not a
validation error, it is a silently thinner response — which is precisely why the
golden master went in first. Every model here is checked against
`tests/golden/*.json`.

**Fields that come from Outline are typed loosely on purpose.** `peakDevices`
and `tunnelSec` are forwarded from an experimental upstream endpoint whose shape
varies by server version. Pinning them to `int` would mean a server that answers
with a float turns a working dashboard into a 500. The panel's own values —
ids, limits, timestamps it computes — are typed properly, because those it
controls.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Ok(BaseModel):
    """The envelope every write shares.

    `pending` appears only when some member of a subscription did not reach its
    server: the panel has recorded the intent and a worker is retrying. Saying
    nothing would be claiming an effect that has not happened yet.
    """
    ok: bool = True
    pending: list[dict] | None = None


class LimitOk(Ok):
    """A write that also reports the ceiling it settled on."""
    limit: int | None = None


class KeyOut(BaseModel):
    """One customer as the dashboard's list renders them."""
    id: str
    serverId: str
    serverName: str | None = None
    name: str
    accessUrl: str | None = None
    used: int
    limit: int | None = None
    expiry: int | None = None
    monthlyBytes: int | None = None
    createdTs: int | None = None
    ownerAdminId: int | None = None
    ownerName: str | None = None
    durationDays: int | None = None
    activated: bool
    pending: bool
    disabled: bool
    subToken: str | None = None
    profileUrl: str | None = None
    # forwarded from Outline's experimental metrics — shape varies by version
    lastSeen: Any = None
    peakDevices: Any = None
    tunnelSec: Any = None


class ServerError(BaseModel):
    serverId: str
    serverName: str | None = None
    error: str


class KeyList(BaseModel):
    """Errors travel *beside* the keys, never instead of them: one unreachable
    server must not blank a page that can still show the other four."""
    keys: list[KeyOut]
    errors: list[ServerError]


class ServerOut(BaseModel):
    id: str
    name: str | None = None
    host: str | None = None
    reachable: bool
    serverName: str | None = None
    version: str | None = None


class ServerList(BaseModel):
    servers: list[ServerOut]


class Me(BaseModel):
    """What the signed-in admin may do. The server enforces all of it
    independently — this only spares people buttons that would 403."""
    ok: bool = True
    id: int
    username: str
    isOwner: bool
    caps: list[str]
    servers: list[str]
    creditEnabled: bool
    credit: int
    discountPct: int


class PackageOut(BaseModel):
    id: int
    name: str
    gb: float | None = None
    days: int | None = None
    monthlyGb: float | None = None
    price: int
    basePrice: int
    # only present for an admin who buys from the price list
    affordable: bool | None = None


class PackageList(BaseModel):
    packages: list[PackageOut]
    credit: int
    creditEnabled: bool
    discountPct: int


class SubServer(BaseModel):
    server: str
    used: int
    limit: int | None = None
    disabled: bool
    url: str


class SubInfo(BaseModel):
    """The customer-facing summary. Also read by VPN clients, so the field names
    are a contract with software nobody here controls."""
    name: str
    used: int
    total: int
    unlimited: bool
    expire: int
    # 0 unless the term has not begun; then it is what the countdown will run
    # for once the customer first connects (invariant T6)
    pendingDays: int
    updateInterval: int
    servers: list[SubServer]
