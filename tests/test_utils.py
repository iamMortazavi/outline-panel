from outline_panel.core.utils import fmt_bytes, fmt_expiry, gb_to_bytes


def test_gb_to_bytes():
    assert gb_to_bytes(1) == 1024 ** 3
    assert gb_to_bytes(0) == 0
    assert gb_to_bytes(0.5) == 1024 ** 3 // 2


def test_fmt_bytes_none_is_unlimited():
    assert fmt_bytes(None) == "نامحدود"


def test_fmt_bytes_units():
    assert fmt_bytes(0) == "0.00 B"
    assert fmt_bytes(1024) == "1.00 KB"
    assert fmt_bytes(1024 ** 3) == "1.00 GB"


def test_fmt_expiry_none():
    assert fmt_expiry(None) == "بدون انقضا"
    assert fmt_expiry(0) == "بدون انقضا"


def test_fmt_expiry_past():
    assert "منقضی" in fmt_expiry(1)  # epoch-ish past timestamp


def test_fmt_expiry_future():
    import time
    txt = fmt_expiry(int(time.time()) + 5 * 86400)
    assert "مانده" in txt
