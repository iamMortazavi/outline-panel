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


class PackageRow(BaseModel):
    """A package straight out of the table, which is what create/edit return —
    snake_case, unlike the `PackageOut` the picker reads."""
    id: int
    name: str
    gb: float | None = None
    days: int | None = None
    monthly_gb: float | None = None
    price: int
    created_ts: int | None = None


class SubServer(BaseModel):
    server: str
    used: int
    limit: int | None = None
    disabled: bool
    url: str


class PerServerStats(BaseModel):
    id: str
    name: str | None = None
    available: bool
    tunnelSec: Any = 0
    dataBytes: Any = 0
    bwCurrent: Any = 0
    bwPeak: Any = 0
    bwTs: Any = None
    locations: list[dict] = []


class Stats(BaseModel):
    """Rolled up across the servers this admin can see. Everything numeric here
    is forwarded from Outline's experimental endpoint, so it is typed loosely
    for the same reason `KeyOut.peakDevices` is."""
    available: bool
    serverCount: int
    tunnelSec: Any = 0
    dataBytes: Any = 0
    bwCurrent: Any = 0
    bwPeak: Any = 0
    bwTs: Any = None
    locations: list[dict] = []
    perServer: list[PerServerStats] = []


# ------------------------------------------------------------------- admins
class AdminOut(BaseModel):
    """An admin as the owner's screen sees them — never the password hash."""
    id: int
    username: str
    isOwner: bool
    caps: list[str]
    servers: list[str]
    disabled: bool
    createdTs: int | None = None
    creditEnabled: bool
    credit: int
    discountPct: int
    telegramId: int | None = None


class AdminList(BaseModel):
    admins: list[AdminOut]
    caps: list[str]
    servers: list[dict]


class LedgerOut(BaseModel):
    """One movement of credit. Rows are returned as stored, so the field names
    are the column names — `package_name` and `price_before_discount` are
    snapshots taken at the time of sale and never join back to `packages`."""
    id: int
    admin_id: int
    delta: int
    balance_after: int
    reason: str
    package_id: int | None = None
    package_name: str | None = None
    price_before_discount: int | None = None
    server_id: str | None = None
    key_id: str | None = None
    note: str | None = None
    created_ts: int | None = None


class Ledger(BaseModel):
    entries: list[LedgerOut]


class CreditOk(BaseModel):
    ok: bool = True
    credit: int


# ------------------------------------------------------------------- servers
class ServerCreated(BaseModel):
    ok: bool = True
    id: str


class ServerSettings(BaseModel):
    """Reads through to the Outline server, so every field but `id` and `label`
    is None when it cannot be reached — the screen degrades rather than 502s."""
    id: str
    label: str | None = None
    host: str | None = None
    name: str | None = None
    version: str | None = None
    metricsEnabled: bool | None = None
    globalLimit: int | None = None


class ServerUptime(BaseModel):
    id: str
    name: str | None = None
    failingNow: int
    probes: int
    ok: int | None = None
    uptimePct: float | None = None
    avgLatencyMs: int | None = None


class HealthSummary(BaseModel):
    days: int
    servers: list[ServerUptime]


class HealthProbe(BaseModel):
    id: int
    server_id: str
    ts: int
    reachable: int
    latency_ms: int | None = None
    error: str | None = None


class HealthHistory(BaseModel):
    history: list[HealthProbe]


# -------------------------------------------------------------------- audit
class AuditEntry(BaseModel):
    id: int
    ts: int
    actor_admin_id: int | None = None
    actor_name: str | None = None
    action: str
    target: str | None = None
    status: int | None = None
    ip: str | None = None
    detail: str | None = None


class AuditPage(BaseModel):
    entries: list[AuditEntry]
    # None when the page came back short: there is nothing more to ask for.
    nextBeforeId: int | None = None


# --------------------------------------------------------------- convergence
class PendingOp(BaseModel):
    id: int
    serverId: str
    keyId: str
    attempts: int
    lastError: str | None = None
    nextTryTs: int
    createdTs: int


class PendingList(BaseModel):
    depth: int
    pending: list[PendingOp]


class DriftReport(BaseModel):
    enabled: bool
    differences: list[dict]
    wouldSuspend: int


class Reconciled(BaseModel):
    differences: list[dict]
    queued: int


# ----------------------------------------------------------------- settings
class PanelSettings(BaseModel):
    """Values plus the spec that describes them, so the settings screen is
    generated rather than written — a new knob is a row in KNOBS and nothing
    else. `values` is keyed by knob name, which is why it stays a dict."""
    values: dict[str, int | str]
    spec: list[dict]


class PanelSaved(BaseModel):
    ok: bool = True
    values: dict[str, int | str]


class ProfileHost(BaseModel):
    baseUrl: str
    host: str


class OwnerSettings(BaseModel):
    totpEnabled: bool


class BotStatus(BaseModel):
    configured: bool
    enabled: bool
    running: bool
    username: str | None = None
    adminIds: list[int]
    webappUrl: str


class BotTested(BaseModel):
    ok: bool = True
    username: str | None = None


class UsernameOk(BaseModel):
    ok: bool = True
    username: str


# --------------------------------------------------------------- two-factor
class TotpState(BaseModel):
    enabled: bool
    pending: bool


class TotpEnrolment(BaseModel):
    """The secret and the otpauth:// URI an authenticator app scans."""
    secret: str
    uri: str


# -------------------------------------------------------------------- backup
class Snapshot(BaseModel):
    name: str
    bytes: int
    ts: int
    # only on a snapshot taken twice inside one second
    skipped: bool | None = None


class SnapshotList(BaseModel):
    enabled: bool
    dir: str
    everyHours: int
    keep: int
    lastTs: int
    snapshots: list[Snapshot]


class Restored(BaseModel):
    ok: bool = True
    servers: int
    keys: int


# ---------------------------------------------------------------- key writes
class KeyCreated(BaseModel):
    """What creating a customer hands back. `servers`/`serverErrors` appear only
    when the sale put them on more than one server."""
    id: str
    serverId: str
    name: str
    subToken: str | None = None
    profileUrl: str | None = None
    accessUrl: str | None = None
    limit: int | None = None
    monthlyBytes: int | None = None
    createdTs: int | None = None
    durationDays: int | None = None
    pending: bool
    servers: list[str] | None = None
    serverErrors: list[dict] | None = None


class Rotated(BaseModel):
    ok: bool = True
    id: str
    previousId: str
    accessUrl: str | None = None
    limit: int | None = None
    carriedUsed: int
    subToken: str | None = None


class SubMember(BaseModel):
    serverId: str
    serverName: str | None = None
    keyId: str
    name: str | None = None


class SubServerChoice(BaseModel):
    id: str
    name: str | None = None
    included: bool


class SubInfoAdmin(BaseModel):
    """A subscription as the panel sees it — which servers the customer is on,
    and which they could be put on. Not the customer-facing `SubInfo`."""
    token: str
    profileUrl: str | None = None
    path: str
    members: list[SubMember]
    servers: list[SubServerChoice]


class OwnerSet(BaseModel):
    ok: bool = True
    ownerAdminId: int | None = None


class BulkResult(BaseModel):
    """Partial success is the normal outcome, not an error: one unreachable
    server must not sink the other fourteen."""
    ok: bool = True
    action: str
    server: str
    done: list[dict]
    failed: list[dict]


# ------------------------------------------------------------------ mini app
class TmaMe(BaseModel):
    username: str
    isOwner: bool
    caps: list[str]
    creditEnabled: bool
    credit: int


class TmaUser(BaseModel):
    id: int | None = None
    name: str | None = None


class TmaBootstrap(BaseModel):
    servers: list[dict]
    user: TmaUser
    me: TmaMe


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
