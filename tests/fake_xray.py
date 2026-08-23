"""
An Xray-core API, faked at the wire.

Not a stub of the adapter's methods — a real h2c server that decodes the real
protobuf and answers with real protobuf, on a real socket. That is the only kind
of fake worth having here: everything interesting in the Xray adapter is the
encoding and the framing, and a fake that spoke Python objects would test none
of it.

What it does *not* prove is that Xray itself agrees with these field numbers and
type names. They are taken verbatim from XTLS/Xray-core's `.proto` files, and
`tests/test_xray_proto.py` pins the bytes — but only a real node closes that
gap, and the adapter says so in its own docstring.
"""

import asyncio
import struct

import h2.config
import h2.connection
import h2.events

from outline_panel.core.xray import proto

ADD_USER = "xray.app.proxyman.command.AddUserOperation"
REMOVE_USER = "xray.app.proxyman.command.RemoveUserOperation"


class FakeXray:
    """Holds users and traffic counters, the way a running Xray would."""

    def __init__(self, tag: str = "vless-in"):
        self.tag = tag
        self.users: dict[str, dict] = {}      # email -> {"id":…, "flow":…}
        self.traffic: dict[str, tuple[int, int]] = {}   # email -> (up, down)
        self.calls: list[str] = []
        self.fail_next: str | None = None
        self._server = None
        self.port = 0

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()

    # ---------------------------------------------------------------- server
    async def _serve(self, reader, writer):
        conn = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        await writer.drain()
        bodies: dict[int, bytearray] = {}
        paths: dict[int, str] = {}
        try:
            while True:
                chunk = await reader.read(65535)
                if not chunk:
                    return
                for event in conn.receive_data(chunk):
                    if isinstance(event, h2.events.RequestReceived):
                        headers = dict(event.headers)
                        paths[event.stream_id] = headers.get(":path", "")
                        bodies[event.stream_id] = bytearray()
                    elif isinstance(event, h2.events.DataReceived):
                        bodies[event.stream_id] += event.data
                        conn.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, h2.events.StreamEnded):
                        self._respond(conn, event.stream_id,
                                      paths.get(event.stream_id, ""),
                                      bytes(bodies.get(event.stream_id, b"")))
                out = conn.data_to_send()
                if out:
                    writer.write(out)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, h2.exceptions.ProtocolError):
            return
        finally:
            writer.close()

    def _respond(self, conn, stream, path, raw):
        self.calls.append(path)
        if self.fail_next and self.fail_next in path:
            self.fail_next = None
            conn.send_headers(stream, [(":status", "200"), ("grpc-status", "14"),
                                       ("grpc-message", "the node is unavailable")],
                              end_stream=True)
            return
        try:
            message = self._handle(path, _unframe(raw))
        except _Status as e:
            conn.send_headers(stream, [(":status", "200"),
                                       ("grpc-status", str(e.code)),
                                       ("grpc-message", e.message)], end_stream=True)
            return
        conn.send_headers(stream, [(":status", "200"),
                                   ("content-type", "application/grpc")])
        conn.send_data(stream, _frame(message))
        conn.send_headers(stream, [("grpc-status", "0")], end_stream=True)

    # -------------------------------------------------------------- handlers
    def _handle(self, path: str, message: bytes) -> bytes:
        if path.endswith("/AlterInbound"):
            return self._alter(message)
        if path.endswith("/GetInboundUsers"):
            return self._get_users(message)
        if path.endswith("/QueryStats"):
            return self._query_stats(message)
        if path.endswith("/GetSysStats"):
            return proto.uint_field(9, 4242) + proto.string_field(10, "25.1.30")
        raise _Status(12, f"unimplemented: {path}")     # 12 = UNIMPLEMENTED

    def _alter(self, message: bytes) -> bytes:
        fields = proto.parse(message)
        if proto.text(fields, 1) != self.tag:
            raise _Status(5, "unknown inbound tag")     # 5 = NOT_FOUND
        op = proto.parse(proto.first(fields, 2, b""))
        kind, payload = proto.text(op, 1), proto.first(op, 2, b"")
        if kind == ADD_USER:
            user = proto.parse(proto.parse(payload).get(1, [b""])[0])
            email = proto.text(user, 2)
            account = proto.parse(proto.first(proto.parse(
                proto.first(user, 3, b"")), 2, b""))
            if email in self.users:
                raise _Status(6, f"User {email} already exists.")  # 6 = ALREADY_EXISTS
            self.users[email] = {"id": proto.text(account, 1),
                                 "flow": proto.text(account, 2)}
            return b""
        if kind == REMOVE_USER:
            email = proto.text(proto.parse(payload), 1)
            if email not in self.users:
                raise _Status(5, f"User {email} not found.")
            del self.users[email]
            return b""
        raise _Status(3, f"unknown operation {kind}")   # 3 = INVALID_ARGUMENT

    def _get_users(self, message: bytes) -> bytes:
        fields = proto.parse(message)
        if proto.text(fields, 1) != self.tag:
            raise _Status(5, "unknown inbound tag")
        wanted = proto.text(fields, 2)
        out = b""
        for email in self.users:
            if wanted and email != wanted:
                continue
            out += proto.bytes_field(1, proto.string_field(2, email))
        return out

    def _query_stats(self, message: bytes) -> bytes:
        pattern = proto.text(proto.parse(message), 1)
        out = b""
        for email, (up, down) in self.traffic.items():
            for direction, value in (("uplink", up), ("downlink", down)):
                name = f"user>>>{email}>>>traffic>>>{direction}"
                if pattern and not name.startswith(pattern):
                    continue
                out += proto.bytes_field(
                    1, proto.string_field(1, name) + proto.uint_field(2, value))
        return out


class _Status(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _frame(message: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(message)) + message


def _unframe(body: bytes) -> bytes:
    if len(body) < 5:
        return b""
    return body[5:5 + struct.unpack(">I", body[1:5])[0]]
