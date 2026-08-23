"""
Xray-core behind `ports.node.NodePort`.

Two things about Xray do not line up with the port, and pretending otherwise is
how an adapter looks finished and is not. Both are handled here, in the open:

**Xray does not enforce a data limit.** Outline caps a key server-side; Xray has
no equivalent — a user is either in the inbound or not. So `set_data_limit(k, 0)`
*removes the user*, which really does stop traffic and is what the port asks of
a zero limit. A non-zero limit only ensures the user is present, and the ceiling
becomes the panel's to enforce. That is why this adapter reports
`enforces_data_limit = False`, and why the scheduler grew a pass that suspends a
customer over their allowance when their node cannot do it itself. Without that
pass a customer on Xray would run past their quota forever.

**Xray's traffic counters restart with the process.** The panel's whole
time-and-quota model rests on usage being cumulative and never resetting:
activation is "usage went above zero", and a quota reset is "raise the ceiling to
current usage plus the allowance". Xray's counters are cumulative only since it
last started. This carries a per-key baseline forward when it sees the number go
backwards, so an Xray restart does not hand every customer their allowance back.

*Known limitation, stated rather than hidden:* the baseline lives in this
object. If Xray and the panel restart together, the delta since the panel last
read is lost and those bytes are not billed. Making it survive that means
persisting a baseline per key, which needs storage this layer does not have —
worth doing before anyone runs Xray at scale, and dishonest to leave unsaid.
"""

from __future__ import annotations

import uuid as _uuid
from urllib.parse import quote, urlencode

from ..outline_api import OutlineError
from . import proto
from .grpc import GrpcError, unary

HANDLER = "/xray.app.proxyman.command.HandlerService"
STATS = "/xray.app.stats.command.StatsService"

# The protobuf full names Xray matches on. A typo here is the one mistake the
# encoding layer cannot catch: the call succeeds and the operation is ignored.
ADD_USER = "xray.app.proxyman.command.AddUserOperation"
REMOVE_USER = "xray.app.proxyman.command.RemoveUserOperation"
VLESS_ACCOUNT = "xray.proxy.vless.Account"


class XrayAPI:
    """One Xray inbound, as a place to put customers.

    `OutlineError` is raised rather than a type of this module's own: the
    executor branches on it, and on its `status` being 404, to tell "already
    gone" from "the server is down". A second exception type would mean every
    caller learning about a second backend, which is the opposite of a port.
    """

    #: This backend cannot cap a key itself — see the module docstring.
    enforces_data_limit = False

    def __init__(self, api_host: str, api_port: int, inbound_tag: str,
                 link: dict | None = None, timeout: float = 10.0):
        self.api_host = api_host
        self.api_port = int(api_port)
        self.inbound_tag = inbound_tag
        # What a customer's vless:// URL is built from: the address and port
        # they connect to, plus the Reality/TLS parameters. Not the API address,
        # which is usually a tunnel the customer cannot reach.
        self.link = dict(link or {})
        self._timeout = timeout
        # key id -> (last raw reading, carried total). See the docstring.
        self._usage_baseline: dict[str, tuple[int, int]] = {}
        self._names: dict[str, str] = {}

    # ------------------------------------------------------------- plumbing
    async def _call(self, path: str, message: bytes) -> bytes:
        try:
            return await unary(self.api_host, self.api_port, path, message,
                               self._timeout)
        except GrpcError as e:
            # 5 is NOT_FOUND. The executor reads 404 as "the state you asked
            # for is already true", which is exactly what it means here.
            raise OutlineError(str(e), status=404 if e.status == 5 else None) from e

    async def _alter(self, operation_type: str, operation: bytes) -> None:
        request = (proto.string_field(1, self.inbound_tag)
                   + proto.bytes_field(2, proto.typed_message(operation_type,
                                                              operation)))
        await self._call(f"{HANDLER}/AlterInbound", request)

    def _account(self, key_id: str) -> bytes:
        account = (proto.string_field(1, key_id)
                   + proto.string_field(2, self.link.get("flow", ""))
                   + proto.string_field(3, self.link.get("encryption", "none")))
        # `email` is the handle Xray files a user under, and the panel's key id
        # is exactly that: unique, stable, already the primary key here. Xray
        # keeps no per-user statistics at all for a user without one.
        return (proto.uint_field(1, 0)
                + proto.string_field(2, key_id)
                + proto.bytes_field(3, proto.typed_message(VLESS_ACCOUNT, account)))

    def access_url(self, key_id: str, name: str | None = None) -> str:
        """The `vless://` line a client imports.

        Built here rather than by the panel because only the adapter knows what
        its backend speaks — the panel treats `accessUrl` as opaque, which is
        the whole reason a second protocol fits behind the same port.
        """
        cfg = self.link
        params = {"type": cfg.get("network", "tcp"),
                  "security": cfg.get("security", "reality")}
        for key in ("sni", "fp", "pbk", "sid", "spx", "flow", "path", "host"):
            if cfg.get(key):
                params[key] = cfg[key]
        host = cfg.get("address") or self.api_host
        port = cfg.get("port") or 443
        label = quote(name or self._names.get(key_id) or key_id)
        return f"vless://{key_id}@{host}:{port}?{urlencode(params)}#{label}"

    def _shape(self, key_id: str) -> dict:
        """One key in the shape every caller of the port expects.

        `name` is None rather than the key id when this process has not been
        told one: Xray stores no display name, so after a panel restart the only
        thing that knows the customer's name is the panel's own row. Returning
        the UUID here would look like an answer and quietly overwrite it.
        """
        name = self._names.get(key_id)
        return {"id": key_id, "name": name,
                "accessUrl": self.access_url(key_id, name),
                "dataLimit": {}}

    # ---------------------------------------------------------------- keys
    async def list_keys(self) -> list[dict]:
        request = proto.string_field(1, self.inbound_tag)
        fields = proto.parse(await self._call(f"{HANDLER}/GetInboundUsers", request))
        out = []
        for raw in fields.get(1, []):
            user = proto.parse(raw)
            email = proto.text(user, 2)
            if email:
                out.append(self._shape(email))
        return out

    async def get_key(self, key_id: str) -> dict:
        request = (proto.string_field(1, self.inbound_tag)
                   + proto.string_field(2, key_id))
        fields = proto.parse(await self._call(f"{HANDLER}/GetInboundUsers", request))
        for raw in fields.get(1, []):
            if proto.text(proto.parse(raw), 2) == key_id:
                return self._shape(key_id)
        raise OutlineError(f"no such user on this Xray inbound: {key_id}", status=404)

    async def create_key(self, name: str | None = None,
                         limit_bytes: int | None = None) -> dict:
        """Add a user to the inbound.

        The UUID *is* the key id, the VLESS account id and the stats handle, so
        there is one identifier rather than three that have to be kept in step.

        `limit_bytes` is remembered by the panel, not by Xray — see the module
        docstring. A limit of zero is the exception and means blocked, so the
        user is simply not added.
        """
        key_id = str(_uuid.uuid4())
        if name:
            self._names[key_id] = name
        if limit_bytes == 0:
            return {**self._shape(key_id), "dataLimit": {"bytes": 0}}
        await self._alter(ADD_USER, proto.bytes_field(1, self._account(key_id)))
        return self._shape(key_id)

    async def rename_key(self, key_id: str, name: str) -> None:
        """Xray has no notion of a display name — the panel is where names live.

        Remembered here anyway so the label on a freshly built `vless://` URL is
        the customer's name rather than a UUID.
        """
        self._names[key_id] = name

    async def delete_key(self, key_id: str) -> None:
        await self._alter(REMOVE_USER, proto.string_field(1, key_id))

    # --------------------------------------------------------------- limits
    async def set_data_limit(self, key_id: str, limit_bytes: int) -> None:
        """Zero means blocked, and blocked means gone from the inbound.

        Anything else only guarantees the user is present: Xray will not stop
        them at a byte count, so the ceiling is enforced by the scheduler
        instead (see `enforces_data_limit`).
        """
        if int(limit_bytes) <= 0:
            try:
                await self.delete_key(key_id)
            except OutlineError as e:
                # Already gone is the state being asked for. The scheduler
                # re-applies this every pass for a customer over their
                # allowance, and an error each time would be noise that trains
                # people to ignore the log.
                if e.status != 404:
                    raise
            return
        await self.remove_data_limit(key_id)

    async def remove_data_limit(self, key_id: str) -> None:
        """Make sure the customer is connectable.

        Adding a user that is already there is what Xray answers ALREADY_EXISTS
        to, and that is the state being asked for, so it is success.
        """
        try:
            await self._alter(ADD_USER, proto.bytes_field(1, self._account(key_id)))
        except OutlineError as e:
            if "exists" not in str(e).lower():
                raise

    # ---------------------------------------------------------------- usage
    async def get_transfer_metrics(self) -> dict[str, int]:
        """`{key_id: bytes}`, uplink plus downlink, made cumulative.

        Xray reports per-direction counters named
        `user>>><email>>>>traffic>>>uplink|downlink`, and only for users that
        have an email — which is why the key id is one.
        """
        request = proto.string_field(1, "user>>>") + proto.bool_field(2, False)
        fields = proto.parse(await self._call(f"{STATS}/QueryStats", request))
        raw: dict[str, int] = {}
        for entry in fields.get(1, []):
            stat = proto.parse(entry)
            name = proto.text(stat, 1)
            value = proto.signed(proto.first(stat, 2, 0) or 0)
            parts = name.split(">>>")
            if len(parts) >= 4 and parts[0] == "user":
                raw[parts[1]] = raw.get(parts[1], 0) + max(0, value)
        return {kid: self._cumulative(kid, total) for kid, total in raw.items()}

    def _cumulative(self, key_id: str, reading: int) -> int:
        """Carry a baseline forward when Xray's counter goes backwards.

        A counter that only ever grows is load-bearing here: the scheduler reads
        "usage above zero" as the customer's first connection, and a quota reset
        as current usage plus the allowance. A restart that silently returned
        everyone to zero would hand every customer their month back.
        """
        last, carried = self._usage_baseline.get(key_id, (0, 0))
        if reading < last:              # the counter restarted with the process
            carried += last
        self._usage_baseline[key_id] = (reading, carried)
        return carried + reading

    # --------------------------------------------------------------- server
    async def get_server_info(self) -> dict:
        """Also the reachability probe — the health checker calls exactly this."""
        fields = proto.parse(await self._call(f"{STATS}/GetSysStats", b""))
        return {"name": f"Xray · {self.inbound_tag}",
                "version": proto.text(fields, 10) or "xray",
                "uptime": proto.first(fields, 9, 0)}

    async def close(self) -> None:
        """Nothing to release: every call opens and closes its own connection."""
        return None
