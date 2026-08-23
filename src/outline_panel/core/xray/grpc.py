"""
gRPC over cleartext HTTP/2, by hand.

Xray's API inbound speaks h2c — HTTP/2 with no TLS, on the assumption you reach
it over localhost, a WireGuard link or an SSH tunnel rather than the open
internet. httpx cannot do that: it only reaches HTTP/2 by ALPN negotiation
during a TLS handshake, and against a cleartext port it sends HTTP/1.1 and gets
"invalid HTTP/2 preamble" back. That was checked before this file was written,
not assumed.

So this drives `h2` directly. It is more code than a client library call and it
buys two things worth having: h2c with prior knowledge, and access to the HTTP/2
trailers, which is where gRPC actually puts its status code — httpx does not
expose them at all.

**One connection per call, on purpose.** A long-lived multiplexed connection
would save a millisecond against a host that is usually localhost, and cost
reconnect logic, stream-id bookkeeping and a class of stale-connection bugs that
only appear in production. The panel makes a handful of these calls per
operation.
"""

from __future__ import annotations

import asyncio
import struct

import h2.config
import h2.connection
import h2.events

# gRPC's own framing, inside the HTTP/2 body: a compression flag, then a
# big-endian length, then the message.
_COMPRESSED, _HEADER = 0, 5


class GrpcError(Exception):
    """A call that did not return OK.

    `status` is the numeric gRPC status when the server sent one — 5 is
    NOT_FOUND, 3 INVALID_ARGUMENT, 16 UNAUTHENTICATED — and None when the
    connection failed before any status existed.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def frame(message: bytes) -> bytes:
    return bytes([_COMPRESSED]) + struct.pack(">I", len(message)) + message


def unframe(body: bytes) -> bytes:
    """The first message out of a response body.

    Unary calls carry exactly one. An empty body is not an error at this layer:
    several Xray responses are genuinely empty messages (`AlterInboundResponse`
    has no fields), and telling that apart from a failure is the status code's
    job, not this function's.
    """
    if len(body) < _HEADER:
        return b""
    size = struct.unpack(">I", body[1:_HEADER])[0]
    return body[_HEADER:_HEADER + size]


async def unary(host: str, port: int, path: str, message: bytes,
                timeout: float = 10.0) -> bytes:
    """One unary call. Returns the response message, or raises GrpcError."""
    try:
        return await asyncio.wait_for(
            _call(host, port, path, message), timeout)
    except TimeoutError as e:
        raise GrpcError(f"timed out after {timeout:g}s calling {path}") from e
    except (OSError, ConnectionError) as e:
        raise GrpcError(f"could not reach the Xray API at {host}:{port}: {e}") from e


async def _call(host: str, port: int, path: str, message: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        conn = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))
        conn.initiate_connection()
        stream = conn.get_next_available_stream_id()
        conn.send_headers(stream, [
            (":method", "POST"),
            (":path", path),
            (":scheme", "http"),
            (":authority", f"{host}:{port}"),
            ("te", "trailers"),                       # required by gRPC
            ("content-type", "application/grpc+proto"),
            ("user-agent", "outline-panel"),
        ])
        conn.send_data(stream, frame(message), end_stream=True)
        writer.write(conn.data_to_send())
        await writer.drain()

        body = bytearray()
        headers: dict[str, str] = {}
        trailers: dict[str, str] = {}
        seen_headers = False
        while True:
            chunk = await reader.read(65535)
            if not chunk:
                raise GrpcError(f"the Xray API closed the connection during {path}")
            for event in conn.receive_data(chunk):
                if isinstance(event, h2.events.ResponseReceived):
                    # The first HEADERS frame. A gRPC error raised before any
                    # message arrives is a "Trailers-Only" response, which is
                    # this same frame carrying grpc-status — hence one dict for
                    # both and a check that looks in each.
                    headers = {k.lower(): v for k, v in event.headers}
                    seen_headers = True
                elif isinstance(event, h2.events.TrailersReceived):
                    trailers = {k.lower(): v for k, v in event.headers}
                elif isinstance(event, h2.events.DataReceived):
                    body += event.data
                    conn.acknowledge_received_data(
                        event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    _raise_for_status(path, headers, trailers, seen_headers)
                    return unframe(bytes(body))
                elif isinstance(event, h2.events.ConnectionTerminated):
                    raise GrpcError(
                        f"the Xray API terminated the connection during {path}")
            out = conn.data_to_send()
            if out:
                writer.write(out)
                await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass


def _raise_for_status(path, headers: dict, trailers: dict, seen_headers: bool) -> None:
    if not seen_headers:
        raise GrpcError(f"no response headers from the Xray API for {path}")
    http = headers.get(":status", "")
    if http and http != "200":
        raise GrpcError(f"the Xray API answered HTTP {http} for {path}")
    raw = trailers.get("grpc-status", headers.get("grpc-status"))
    if raw in (None, "", "0"):
        return
    detail = trailers.get("grpc-message") or headers.get("grpc-message") or ""
    try:
        code = int(raw)
    except ValueError:
        code = None
    raise GrpcError(f"{path} failed: {detail or 'gRPC status ' + str(raw)}", code)
