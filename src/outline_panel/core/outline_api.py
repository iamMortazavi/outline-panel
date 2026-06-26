"""
Outline Management API (Shadowbox) client.

Official docs:
https://github.com/OutlineFoundation/outline-server/tree/master/src/shadowbox#access-keys-management-api

Note: Outline servers use a self-signed TLS certificate. When no certSha256 is
provided we fall back to verify=False; when it is, the cert is pinned instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import socket
import ssl
from urllib.parse import urlparse

import httpx


class OutlineError(Exception):
    """Raised for any error while talking to the Outline API."""


def _norm_fp(fp: str | None) -> str | None:
    """Normalize a cert fingerprint: drop ':'/spaces, lowercase."""
    if not fp:
        return None
    fp = fp.replace(":", "").replace(" ", "").strip().lower()
    return fp or None


def parse_access_config(text: str) -> tuple[str, str | None]:
    """
    Extract apiUrl and (optionally) certSha256 from user input, which may be:
      • a raw URL:  https://1.2.3.4:1234/SecretPath
      • the Outline Manager access config: {"apiUrl":"https://...","certSha256":"..."}
    Returns (url, cert_sha256|None).
    """
    text = (text or "").strip()
    if not text:
        raise OutlineError("API URL is empty.")
    cert = None
    if text.startswith("{"):
        try:
            data = json.loads(text)
            url = data.get("apiUrl", "")
            cert = data.get("certSha256")
        except json.JSONDecodeError:
            m = re.search(r'"apiUrl"\s*:\s*"([^"]+)"', text)
            url = m.group(1) if m else ""
            mc = re.search(r'"certSha256"\s*:\s*"([^"]+)"', text)
            cert = mc.group(1) if mc else None
    else:
        url = text
    url = url.strip().rstrip("/")
    if not re.match(r"^https://", url):
        raise OutlineError("Invalid API URL (must start with https://).")
    return url, _norm_fp(cert)


def _fetch_cert_der(host: str, port: int, timeout: float) -> bytes:
    """Fetch the server's DER certificate via an unverified TLS handshake."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            return ssock.getpeercert(binary_form=True)


class OutlineAPI:
    def __init__(self, api_url: str, cert_sha256: str | None = None,
                 timeout: float = 15.0):
        # api_url looks like: https://1.2.3.4:1234/AbCdEf12345
        self.api_url = api_url.rstrip("/")
        # If certSha256 is given the cert is pinned; otherwise verify=False
        # (the server cert is self-signed). Pinning happens lazily on the first
        # request so the constructor stays sync and non-blocking.
        self.cert_sha256 = _norm_fp(cert_sha256)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _pinned_ssl_context(self) -> ssl.SSLContext:
        parsed = urlparse(self.api_url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        loop = asyncio.get_running_loop()
        der = await loop.run_in_executor(
            None, _fetch_cert_der, host, port, self._timeout
        )
        fp = hashlib.sha256(der).hexdigest()
        if fp != self.cert_sha256:
            raise OutlineError(
                "Server certificate does not match the stored fingerprint (possible MITM)."
            )
        pem = ssl.DER_cert_to_PEM_cert(der)
        ctx = ssl.create_default_context(cadata=pem)
        ctx.check_hostname = False  # cert is issued for an IP; CN may not match
        return ctx

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            verify: ssl.SSLContext | bool
            verify = await self._pinned_ssl_context() if self.cert_sha256 else False
            self._client = httpx.AsyncClient(verify=verify, timeout=self._timeout)
            return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # internals ---------------------------------------------------------
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.api_url}{path}"
        client = await self._get_client()
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise OutlineError(f"Failed to reach the Outline server: {e}") from e
        if resp.status_code >= 400:
            raise OutlineError(
                f"Error response from server ({resp.status_code}): {resp.text[:200]}"
            )
        return resp

    # server ------------------------------------------------------------
    async def get_server_info(self) -> dict:
        resp = await self._request("GET", "/server")
        return resp.json()

    async def rename_server(self, name: str) -> None:
        await self._request("PUT", "/name", json={"name": name})

    async def set_global_data_limit(self, limit_bytes: int) -> None:
        await self._request(
            "PUT", "/server/access-key-data-limit",
            json={"limit": {"bytes": int(limit_bytes)}},
        )

    async def remove_global_data_limit(self) -> None:
        await self._request("DELETE", "/server/access-key-data-limit")

    # metrics -----------------------------------------------------------
    async def get_metrics_enabled(self) -> bool:
        resp = await self._request("GET", "/metrics/enabled")
        return bool(resp.json().get("metricsEnabled"))

    async def set_metrics_enabled(self, enabled: bool) -> None:
        await self._request(
            "PUT", "/metrics/enabled", json={"metricsEnabled": bool(enabled)}
        )

    # access keys (users) -----------------------------------------------
    async def list_keys(self) -> list[dict]:
        resp = await self._request("GET", "/access-keys")
        return resp.json().get("accessKeys", [])

    async def create_key(
        self,
        name: str | None = None,
        limit_bytes: int | None = None,
    ) -> dict:
        body: dict = {}
        if name:
            body["name"] = name
        if limit_bytes is not None:
            body["limit"] = {"bytes": int(limit_bytes)}
        resp = await self._request("POST", "/access-keys", json=body or None)
        return resp.json()

    async def get_key(self, key_id: str) -> dict:
        resp = await self._request("GET", f"/access-keys/{key_id}")
        return resp.json()

    async def rename_key(self, key_id: str, name: str) -> None:
        await self._request(
            "PUT", f"/access-keys/{key_id}/name", json={"name": name}
        )

    async def delete_key(self, key_id: str) -> None:
        await self._request("DELETE", f"/access-keys/{key_id}")

    # data limits -------------------------------------------------------
    async def set_data_limit(self, key_id: str, limit_bytes: int) -> None:
        await self._request(
            "PUT",
            f"/access-keys/{key_id}/data-limit",
            json={"limit": {"bytes": int(limit_bytes)}},
        )

    async def remove_data_limit(self, key_id: str) -> None:
        await self._request("DELETE", f"/access-keys/{key_id}/data-limit")

    # usage -------------------------------------------------------------
    async def get_transfer_metrics(self) -> dict[str, int]:
        """Return a {key_id: bytes_transferred} dict."""
        resp = await self._request("GET", "/metrics/transfer")
        return resp.json().get("bytesTransferredByUserId", {})

    # advanced (experimental) metrics -----------------------------------
    async def get_server_metrics(self, since: str = "30d") -> dict:
        """
        Advanced per-server and per-key metrics from the experimental endpoint:
        tunnelTime, dataTransferred, bandwidth (current/peak), geo locations,
        and per key: last activity and peak simultaneous devices.
        May be unavailable on some versions or when metrics sharing is off.
        """
        resp = await self._request(
            "GET", "/experimental/server/metrics", params={"since": since}
        )
        return resp.json()
