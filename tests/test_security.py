from outline_panel.core import security


def test_password_roundtrip():
    h, s = security.hash_password("hunter2")
    assert security.verify_password("hunter2", h, s)
    assert not security.verify_password("wrong", h, s)


def test_password_unique_salt():
    h1, s1 = security.hash_password("same")
    h2, s2 = security.hash_password("same")
    assert s1 != s2 and h1 != h2  # different salt -> different hash


def test_verify_handles_garbage():
    assert not security.verify_password("x", "", "")
    assert not security.verify_password("x", "zzz", "zzz")


def test_totp_now_accepts():
    secret = security.generate_totp_secret()
    code = security.totp_now(secret)
    assert security.verify_totp(secret, code)
    assert len(code) == 6 and code.isdigit()


def test_totp_rejects_wrong():
    secret = security.generate_totp_secret()
    assert not security.verify_totp(secret, "000000") or security.totp_now(secret) == "000000"
    assert not security.verify_totp(secret, "")


def test_totp_provisioning_uri():
    secret = security.generate_totp_secret()
    uri = security.totp_provisioning_uri(secret, "admin")
    assert uri.startswith("otpauth://totp/") and secret in uri
