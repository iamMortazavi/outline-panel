"""
The protobuf encoding, pinned byte by byte.

This layer is hand-written, which is defensible for five small messages and
indefensible without tests that check the actual bytes. Each expectation below
is derived from the protobuf spec and the field numbers in XTLS/Xray-core's own
`.proto` files — not from running the encoder and writing down what it said.
"""

import pytest

from outline_panel.core.xray import proto


# ------------------------------------------------------------------ varints
@pytest.mark.parametrize("value,expected", [
    (0, b"\x00"),
    (1, b"\x01"),
    (127, b"\x7f"),
    (128, b"\x80\x01"),           # 0x80 continuation, then the high bits
    (300, b"\xac\x02"),           # the example from the protobuf documentation
    (16384, b"\x80\x80\x01"),
])
def test_varint_matches_the_spec(value, expected):
    assert proto.varint(value) == expected


def test_a_varint_round_trips_through_the_parser():
    for value in (0, 1, 127, 128, 300, 2**31, 2**53, 2**63 - 1):
        assert proto.parse(proto.tag(1, proto.VARINT) + proto.varint(value))[1][0] == value


# -------------------------------------------------------------------- fields
def test_a_string_field_is_tag_length_bytes():
    # field 2, wire type 2 -> key 0x12; "ab" is two bytes
    assert proto.string_field(2, "ab") == b"\x12\x02ab"


def test_proto3_leaves_default_values_off_the_wire():
    """Not an optimisation — Xray's own encoder omits them, and a decoder that
    saw an explicit zero where it expected absence would still be correct, but
    the bytes would not match what a real client sends."""
    assert proto.string_field(1, "") == b""
    assert proto.uint_field(1, 0) == b""
    assert proto.bool_field(2, False) == b""
    assert proto.bytes_field(3, b"") == b""


def test_a_true_bool_is_a_varint_one():
    assert proto.bool_field(2, True) == b"\x10\x01"


def test_utf8_survives_the_round_trip():
    encoded = proto.string_field(1, "کاربر ۱")
    assert proto.text(proto.parse(encoded), 1) == "کاربر ۱"


# --------------------------------------------------------------- the messages
def test_a_typed_message_carries_its_full_name():
    """`TypedMessage{ type = 1 (string), value = 2 (bytes) }`. The type string
    is how Xray decides what the payload is, and the one thing the wire cannot
    validate for us."""
    got = proto.typed_message("xray.proxy.vless.Account", b"\x01\x02")
    fields = proto.parse(got)
    assert proto.text(fields, 1) == "xray.proxy.vless.Account"
    assert proto.first(fields, 2) == b"\x01\x02"


def test_an_add_user_operation_decodes_back_to_its_parts():
    """AlterInboundRequest{tag=1, operation=2} wrapping
    AddUserOperation{user=1} wrapping User{level=1, email=2, account=3}."""
    account = (proto.string_field(1, "66ad4540-b58c-4ad2-9926-ea63445a9b57")
               + proto.string_field(2, "xtls-rprx-vision")
               + proto.string_field(3, "none"))
    user = (proto.uint_field(1, 0)
            + proto.string_field(2, "customer-1")
            + proto.bytes_field(3, proto.typed_message("xray.proxy.vless.Account",
                                                       account)))
    request = (proto.string_field(1, "vless-in")
               + proto.bytes_field(2, proto.typed_message(
                   "xray.app.proxyman.command.AddUserOperation",
                   proto.bytes_field(1, user))))

    outer = proto.parse(request)
    assert proto.text(outer, 1) == "vless-in"
    operation = proto.parse(proto.first(outer, 2))
    assert proto.text(operation, 1) == "xray.app.proxyman.command.AddUserOperation"
    inner = proto.parse(proto.parse(proto.first(operation, 2)).get(1)[0])
    assert proto.text(inner, 2) == "customer-1"
    acct = proto.parse(proto.first(proto.parse(proto.first(inner, 3)), 2))
    assert proto.text(acct, 1) == "66ad4540-b58c-4ad2-9926-ea63445a9b57"
    assert proto.text(acct, 2) == "xtls-rprx-vision"


def test_a_repeated_field_keeps_every_value():
    """`QueryStatsResponse{ stat = 1 (repeated Stat) }` — collapsing repeats to
    the last one would silently report a single customer's traffic."""
    body = (proto.bytes_field(1, proto.string_field(1, "a") + proto.uint_field(2, 5))
            + proto.bytes_field(1, proto.string_field(1, "b") + proto.uint_field(2, 7)))
    stats = proto.parse(body)[1]
    assert len(stats) == 2
    assert [proto.text(proto.parse(s), 1) for s in stats] == ["a", "b"]


def test_unknown_fields_are_carried_rather_than_fatal():
    """Protobuf's forward-compatibility rule, and the reason a newer Xray does
    not break this: a field number we have never heard of decodes and is
    ignored, instead of throwing."""
    body = proto.string_field(2, "known") + proto.uint_field(99, 1234)
    fields = proto.parse(body)
    assert proto.text(fields, 2) == "known"
    assert fields[99] == [1234]


def test_a_truncated_message_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        proto.parse(b"\x12\x10short")


def test_an_int64_that_wrapped_reads_as_negative():
    """Traffic counters are unsigned on the wire. A value past 2^63 is a wrapped
    negative, and reporting it as ~18 exabytes would blow past every quota."""
    assert proto.signed((1 << 64) - 1) == -1
    assert proto.signed(1024) == 1024
