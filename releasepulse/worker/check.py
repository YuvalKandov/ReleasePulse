"""Perform a single HTTP health check and classify the result.

Pure of the database and the scheduler: given an endpoint and an httpx client,
return a CheckResult. SSRF is re-validated here (at fetch time) because DNS can
be rebound between registration and now. Failures are mapped to the normalized
error_type taxonomy: timeout | dns_error | connection_refused | tls_error |
unexpected_status | internal_error.
"""

from __future__ import annotations

import ssl
import socket
import time
from dataclasses import dataclass

import httpx

from releasepulse.models import Endpoint
from releasepulse.security.ssrf import (
    Resolver,
    SsrfResolutionError,
    SsrfValidationError,
    resolve_host,
    validate_url,
)


@dataclass(frozen=True)
class CheckResult:
    success: bool
    status_code: int | None
    latency_ms: int | None
    error_type: str | None
    error_detail: str | None


def _detail(exc: Exception) -> str:
    return str(exc)[:500] or exc.__class__.__name__


def _classify_connect_error(exc: httpx.ConnectError) -> str:
    cause = exc.__cause__
    if isinstance(cause, ssl.SSLError):
        return "tls_error"
    if isinstance(cause, socket.gaierror):
        return "dns_error"
    return "connection_refused"


async def perform_check(
    endpoint: Endpoint,
    client: httpx.AsyncClient,
    *,
    app_env: str = "production",
    allowlist_raw: str = "",
    resolver: Resolver = resolve_host,
) -> CheckResult:
    # Re-validate against SSRF right before fetching (DNS may have changed).
    try:
        validate_url(endpoint.url, app_env=app_env, allowlist_raw=allowlist_raw, resolver=resolver)
    except SsrfResolutionError as exc:
        return CheckResult(False, None, None, "dns_error", _detail(exc))
    except SsrfValidationError as exc:
        # A routable-policy rejection (e.g. DNS rebind to a private address).
        return CheckResult(False, None, None, "internal_error", _detail(exc))

    start = time.perf_counter()
    try:
        response = await client.request(
            endpoint.method, endpoint.url, timeout=endpoint.timeout_sec
        )
    except httpx.TimeoutException as exc:
        return CheckResult(False, None, None, "timeout", _detail(exc))
    except httpx.ConnectError as exc:
        return CheckResult(False, None, None, _classify_connect_error(exc), _detail(exc))
    except httpx.HTTPError as exc:
        return CheckResult(False, None, None, "internal_error", _detail(exc))

    latency_ms = round((time.perf_counter() - start) * 1000)
    if response.status_code == endpoint.expected_status:
        return CheckResult(True, response.status_code, latency_ms, None, None)
    return CheckResult(
        False,
        response.status_code,
        latency_ms,
        "unexpected_status",
        f"expected {endpoint.expected_status}, got {response.status_code}",
    )
