import asyncio

import pytest

from outline_panel.core.outline_api import (
    OutlineAPI,
    OutlineError,
    _norm_fp,
    parse_access_config,
)


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


async def test_metrics_cache_dedupes_calls():
    """get_server_metrics_cached fetches once within the TTL and coalesces
    concurrent callers into a single upstream request."""
    api = OutlineAPI("https://1.2.3.4:1/x")
    calls = 0

    async def fake(since="30d"):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)  # yield so concurrent callers overlap
        return {"n": calls}

    api.get_server_metrics = fake  # type: ignore[method-assign]

    # concurrent callers share one fetch (single-flight)
    a, b = await asyncio.gather(
        api.get_server_metrics_cached("30d", ttl=60),
        api.get_server_metrics_cached("30d", ttl=60),
    )
    assert a == b == {"n": 1}
    assert calls == 1

    # subsequent call within TTL is served from cache
    c = await api.get_server_metrics_cached("30d", ttl=60)
    assert c == {"n": 1} and calls == 1

    # expired TTL re-fetches
    d = await api.get_server_metrics_cached("30d", ttl=0)
    assert d == {"n": 2} and calls == 2
    await api.close()


async def test_metrics_cache_propagates_errors():
    api = OutlineAPI("https://1.2.3.4:1/x")

    async def boom(since="30d"):
        raise OutlineError("down")

    api.get_server_metrics = boom  # type: ignore[method-assign]
    with pytest.raises(OutlineError):
        await api.get_server_metrics_cached("30d")
    # a failed fetch is not cached — the next call retries
    with pytest.raises(OutlineError):
        await api.get_server_metrics_cached("30d")
    await api.close()
