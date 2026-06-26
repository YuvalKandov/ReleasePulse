"""Unit tests for perform_check: result classification, fully offline.

httpx.MockTransport supplies canned responses/exceptions; a fake resolver stands
in for DNS. No network, no database.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl

import httpx

from releasepulse.models import Endpoint
from releasepulse.security.ssrf import SsrfResolutionError
from releasepulse.worker.check import perform_check


def _endpoint(url="https://svc.example/health", method="GET", expected_status=200, timeout_sec=5):
    return Endpoint(url=url, method=method, expected_status=expected_status, timeout_sec=timeout_sec)


def _public_resolver(host, port):
    return [ipaddress.ip_address("93.184.216.34")]


def _run(handler, endpoint=None, *, resolver=_public_resolver, app_env="production", allowlist_raw=""):
    endpoint = endpoint or _endpoint()

    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            return await perform_check(
                endpoint, client, app_env=app_env, allowlist_raw=allowlist_raw, resolver=resolver
            )

    return asyncio.run(go())


# --- success / status -----------------------------------------------------

def test_success() -> None:
    result = _run(lambda req: httpx.Response(200))
    assert result.success is True
    assert result.status_code == 200
    assert result.latency_ms is not None
    assert result.error_type is None


def test_custom_expected_status_success() -> None:
    result = _run(lambda req: httpx.Response(204), _endpoint(expected_status=204))
    assert result.success is True


def test_unexpected_status() -> None:
    result = _run(lambda req: httpx.Response(503))
    assert result.success is False
    assert result.error_type == "unexpected_status"
    assert result.status_code == 503
    assert result.latency_ms is not None  # we got a response, latency is meaningful


# --- transport failures ---------------------------------------------------

def test_timeout() -> None:
    def handler(req):
        raise httpx.ReadTimeout("read timed out", request=req)

    result = _run(handler)
    assert result.success is False
    assert result.error_type == "timeout"
    assert result.latency_ms is None


def test_connection_refused() -> None:
    def handler(req):
        raise httpx.ConnectError("connection refused", request=req)

    result = _run(handler)
    assert result.error_type == "connection_refused"


def test_tls_error() -> None:
    def handler(req):
        raise httpx.ConnectError("handshake failed", request=req) from ssl.SSLError("bad cert")

    result = _run(handler)
    assert result.error_type == "tls_error"


def test_dns_error_via_connect() -> None:
    def handler(req):
        raise httpx.ConnectError("name resolution", request=req) from socket.gaierror("nodename")

    result = _run(handler)
    assert result.error_type == "dns_error"


# --- SSRF re-validation at fetch time -------------------------------------

def test_unresolvable_host_is_dns_error() -> None:
    def bad_resolver(host, port):
        raise SsrfResolutionError(f"could not resolve host '{host}'")

    result = _run(lambda req: httpx.Response(200), resolver=bad_resolver)
    assert result.success is False
    assert result.error_type == "dns_error"


def test_ssrf_rebind_to_private_is_blocked() -> None:
    def private_resolver(host, port):
        return [ipaddress.ip_address("10.0.0.5")]

    # Handler would 200, but SSRF must block before the request is made.
    result = _run(lambda req: httpx.Response(200), resolver=private_resolver)
    assert result.success is False
    assert result.error_type == "internal_error"
    assert "ssrf" in (result.error_detail or "").lower() or "non-routable" in (result.error_detail or "")
