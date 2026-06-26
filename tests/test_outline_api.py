import pytest

from outline_panel.core.outline_api import OutlineError, _norm_fp, parse_access_config


def test_norm_fp():
    assert _norm_fp("AB:cd:EF") == "abcdef"
    assert _norm_fp("  ab cd ") == "abcd"
    assert _norm_fp(None) is None
    assert _norm_fp("") is None


def test_parse_raw_url():
    url, cert = parse_access_config("https://1.2.3.4:1234/Secret/")
    assert url == "https://1.2.3.4:1234/Secret"
    assert cert is None


def test_parse_json_config_with_cert():
    text = '{"apiUrl":"https://1.2.3.4:1234/Secret","certSha256":"AABBCC"}'
    url, cert = parse_access_config(text)
    assert url == "https://1.2.3.4:1234/Secret"
    assert cert == "aabbcc"


def test_parse_rejects_http():
    with pytest.raises(OutlineError):
        parse_access_config("http://1.2.3.4:1234/Secret")


def test_parse_rejects_empty():
    with pytest.raises(OutlineError):
        parse_access_config("")
