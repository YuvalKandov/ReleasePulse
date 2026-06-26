"""Unit tests for the SSRF validator.

DNS resolution is injected via a fake resolver, so these tests need no network
and are fully deterministic.
"""

from __future__ import annotations

import ipaddress

import pytest

from releasepulse.security.ssrf import (
    SsrfValidationError,
    is_globally_routable,
    validate_url,
)


def fake_resolver(mapping: dict[str, list[str]]):
    """Build a resolver that returns canned addresses for known hosts."""

    def _resolve(host: str, port: int | None):
        if host not in mapping:
            raise SsrfValidationError(f"could not resolve host '{host}'")
        return [ipaddress.ip_address(addr) for addr in mapping[host]]

    return _resolve


# --- is_globally_routable: the core address classifier --------------------

@pytest.mark.parametrize(
    "addr",
    [
        "8.8.8.8",                  # public IPv4
        "93.184.216.34",            # public IPv4
        "2606:4700:4700::1111",     # public IPv6 (Cloudflare)
    ],
)
def test_public_addresses_are_routable(addr: str) -> None:
    assert is_globally_routable(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.1",                 # private
        "172.16.5.4",               # private
        "192.168.1.1",              # private
        "127.0.0.1",                # loopback
        "169.254.169.254",          # link-local (cloud metadata)
        "224.0.0.1",                # multicast
        "240.0.0.1",                # reserved
        "0.0.0.0",                  # unspecified
        "::1",                      # IPv6 loopback
        "fe80::1",                  # IPv6 link-local
        "fc00::1",                  # IPv6 unique-local (private)
        "::ffff:169.254.169.254",   # IPv4-mapped metadata
    ],
)
def test_non_routable_addresses_are_rejected(addr: str) -> None:
    assert is_globally_routable(ipaddress.ip_address(addr)) is False


# --- validate_url: scheme and host shape ----------------------------------

@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "gopher://x"])
def test_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(SsrfValidationError, match="scheme"):
        validate_url(url, app_env="production", resolver=fake_resolver({}))


def test_rejects_url_without_host() -> None:
    with pytest.raises(SsrfValidationError, match="no host"):
        validate_url("https://", app_env="production", resolver=fake_resolver({}))


def test_rejects_invalid_port() -> None:
    with pytest.raises(SsrfValidationError, match="port"):
        validate_url(
            "http://example.com:notaport",
            app_env="production",
            resolver=fake_resolver({"example.com": ["8.8.8.8"]}),
        )


# --- validate_url: the routable / non-routable decision -------------------

def test_allows_public_https() -> None:
    resolver = fake_resolver({"example.com": ["93.184.216.34"]})
    addrs = validate_url("https://example.com/health", app_env="production", resolver=resolver)
    assert ipaddress.ip_address("93.184.216.34") in addrs


def test_allows_public_http() -> None:
    resolver = fake_resolver({"example.com": ["8.8.8.8"]})
    validate_url("http://example.com/", app_env="production", resolver=resolver)


def test_rejects_private_target() -> None:
    resolver = fake_resolver({"internal": ["10.0.0.5"]})
    with pytest.raises(SsrfValidationError, match="non-routable"):
        validate_url("http://internal/", app_env="production", resolver=resolver)


def test_rejects_cloud_metadata() -> None:
    resolver = fake_resolver({"metadata": ["169.254.169.254"]})
    with pytest.raises(SsrfValidationError, match="non-routable"):
        validate_url("http://metadata/latest/", app_env="production", resolver=resolver)


def test_rejects_when_any_resolved_ip_is_private() -> None:
    # One public and one private record: the private one must veto.
    resolver = fake_resolver({"mixed": ["8.8.8.8", "10.0.0.1"]})
    with pytest.raises(SsrfValidationError, match="non-routable"):
        validate_url("https://mixed/", app_env="production", resolver=resolver)


def test_rejects_unresolvable_host() -> None:
    with pytest.raises(SsrfValidationError, match="resolve"):
        validate_url("https://nope.invalid/", app_env="production", resolver=fake_resolver({}))


# --- validate_url: dev/test allowlist override ----------------------------

def test_dev_allowlist_hostname_permits_private() -> None:
    resolver = fake_resolver({"demo-service": ["172.18.0.5"]})
    addrs = validate_url(
        "http://demo-service:8080/health",
        app_env="dev",
        allowlist_raw="demo-service",
        resolver=resolver,
    )
    assert ipaddress.ip_address("172.18.0.5") in addrs


def test_dev_allowlist_cidr_permits_private() -> None:
    resolver = fake_resolver({"demo-service": ["172.18.0.5"]})
    validate_url(
        "http://demo-service:8080/",
        app_env="dev",
        allowlist_raw="172.18.0.0/16",
        resolver=resolver,
    )


def test_production_ignores_allowlist() -> None:
    # Same allowlist that works in dev must be ignored in production.
    resolver = fake_resolver({"demo-service": ["172.18.0.5"]})
    with pytest.raises(SsrfValidationError, match="non-routable"):
        validate_url(
            "http://demo-service:8080/",
            app_env="production",
            allowlist_raw="demo-service,172.18.0.0/16",
            resolver=resolver,
        )
