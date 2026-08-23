"""
Just enough protobuf to talk to Xray.

The messages are taken verbatim from Xray-core's own `.proto` files:

    AlterInboundRequest   { tag = 1 (string), operation = 2 (TypedMessage) }
    TypedMessage          { type = 1 (string), value = 2 (bytes) }
    AddUserOperation      { user = 1 (User) }
    RemoveUserOperation   { email = 1 (string) }
    User                  { level = 1 (uint32), email = 2 (string),
                            account = 3 (TypedMessage) }
    vless.Account         { id = 1 (string), flow = 2 (string),
                            encryption = 3 (string) }
    QueryStatsRequest     { pattern = 1 (string), reset = 2 (bool) }
    QueryStatsResponse    { stat = 1 (repeated Stat) }
    Stat                  { name = 1 (string), value = 2 (int64) }
    GetInboundUserRequest { tag = 1 (string), email = 2 (string) }
    GetInboundUserResponse{ users = 1 (repeated User) }

Only two wire types appear: varint (0) for the numbers and the bool, and
length-delimited (2) for every string, bytes and embedded message. That is the
whole reason this file can exist instead of a code generator.

Unknown fields decode into the same dict and are ignored, which is what keeps a
newer Xray from breaking this: protobuf's own forward-compatibility rule.
"""

from __future__ import annotations

VARINT, LENGTH = 0, 2


# ------------------------------------------------------------------ encoding
def varint(value: int) -> bytes:
    """Base-128, little-endian, high bit as the continuation flag."""
    if value < 0:
        # int64 negatives are sign-extended to ten bytes. No field here is ever
        # negative, but a silent wrong answer is worse than a loud one.
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def uint_field(field: int, value: int) -> bytes:
    """Omitted when zero: proto3 does not put default values on the wire, and
    Xray's own encoder does not either."""
    return b"" if not value else tag(field, VARINT) + varint(value)


def bool_field(field: int, value: bool) -> bytes:
    return b"" if not value else tag(field, VARINT) + varint(1)


def bytes_field(field: int, value: bytes) -> bytes:
    return b"" if not value else tag(field, LENGTH) + varint(len(value)) + value


def string_field(field: int, value: str) -> bytes:
    return bytes_field(field, (value or "").encode())


def typed_message(type_name: str, payload: bytes) -> bytes:
    """A `TypedMessage`: Xray's way of carrying a message whose type is only
    known at runtime. The type string is the protobuf full name, and getting it
    wrong is the one mistake this layer cannot detect."""
    return string_field(1, type_name) + bytes_field(2, payload)


# ------------------------------------------------------------------ decoding
def parse(buf: bytes) -> dict[int, list]:
    """Field number -> list of values, varints as ints and the rest as bytes.

    A list per field because protobuf repeats rather than replaces, and because
    a `repeated` field is indistinguishable from a scalar until you know the
    schema. Callers know theirs.
    """
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _varint_at(buf, i)
        field, wire = key >> 3, key & 0x07
        if wire == VARINT:
            value, i = _varint_at(buf, i)
        elif wire == LENGTH:
            size, i = _varint_at(buf, i)
            # Slicing past the end returns what is there rather than raising, so
            # a truncated response would decode into a short string and be used.
            # A message that is cut off is a failure, not a shorter message.
            if i + size > len(buf):
                raise ValueError(
                    f"truncated field {field}: wanted {size} bytes, "
                    f"{len(buf) - i} left")
            value, i = buf[i:i + size], i + size
        elif wire == 5:                       # fixed32
            if i + 4 > len(buf):
                raise ValueError(f"truncated fixed32 in field {field}")
            value, i = buf[i:i + 4], i + 4
        elif wire == 1:                       # fixed64
            if i + 8 > len(buf):
                raise ValueError(f"truncated fixed64 in field {field}")
            value, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out.setdefault(field, []).append(value)
    return out


def _varint_at(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def first(fields: dict[int, list], number: int, default=None):
    got = fields.get(number)
    return got[0] if got else default


def text(fields: dict[int, list], number: int, default: str = "") -> str:
    got = first(fields, number)
    return got.decode(errors="replace") if isinstance(got, bytes) else default


def signed(value: int) -> int:
    """A protobuf int64 comes off the wire unsigned; traffic counters are
    always positive, but a wrapped value would read as astronomically large
    rather than as the small negative it is."""
    return value - (1 << 64) if value >= 1 << 63 else value
