"""SSRF protection for user-supplied endpoint URLs.

The worker fetches URLs that users register, which makes registration a
server-side request forgery vector. Before we store (or, later, fetch) a URL we
resolve its DNS and reject any address that is not globally routable, so a URL
can't point our worker at internal services or cloud metadata
(169.254.169.254, link-local, loopback, private ranges, ...).

In 'dev' mode an explicit allowlist of hostnames/CIDRs is additionally permitted
so the Docker Compose demo can target private hosts. Production never consults it.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, "int | None"], list[IpAddress]]

ALLOWED_SCHEMES = ("http", "https")


class SsrfValidationError(ValueError):
    """Raised when a URL is rejected as unsafe to register or fetch."""


def is_globally_routable(ip: IpAddress) -> bool:
    """True only if `ip` is a public, globally-routable address.

    Rejects the full set from the spec: private, loopback, link-local,
    multicast, reserved, unspecified. IPv4-mapped IPv6 addresses are evaluated
    by their embedded IPv4, so ::ffff:169.254.169.254 is caught as link-local.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_host(host: str, port: int | None) -> list[IpAddress]:
    """Resolve a hostname to every A/AAAA address it points to."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfValidationError(f"could not resolve host '{host}'") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _parse_allowlist(raw: str) -> tuple[set[str], list[IpNetwork]]:
    """Split a comma-separated allowlist into hostnames and IP networks.

    An entry that parses as an IP/CIDR becomes a network; anything else is
    treated as a hostname (e.g. a Compose service name like 'demo-service').
    """
    hostnames: set[str] = set()
    networks: list[IpNetwork] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            hostnames.add(item.lower())
    return hostnames, networks


def validate_url(
    url: str,
    *,
    app_env: str,
    allowlist_raw: str = "",
    resolver: Resolver = resolve_host,
) -> list[IpAddress]:
    """Validate a user-supplied URL, returning its resolved addresses.

    Raises SsrfValidationError if the URL is unsafe to register or fetch.
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise SsrfValidationError(
            f"unsupported URL scheme '{parts.scheme}'; only http and https are allowed"
        )

    host = parts.hostname
    if not host:
        raise SsrfValidationError("URL has no host")

    try:
        port = parts.port
    except ValueError as exc:
        raise SsrfValidationError("invalid port in URL") from exc

    dev_mode = app_env == "dev"
    allow_hostnames, allow_networks = (
        _parse_allowlist(allowlist_raw) if dev_mode else (set(), [])
    )

    # A dev-allowlisted hostname (e.g. the Compose service name) is trusted
    # outright; we still resolve it so a non-existent host is rejected.
    if dev_mode and host.lower() in allow_hostnames:
        return resolver(host, port)

    addresses = resolver(host, port)
    if not addresses:
        raise SsrfValidationError(f"could not resolve host '{host}'")

    for ip in addresses:
        if is_globally_routable(ip):
            continue
        if dev_mode and any(ip in net for net in allow_networks):
            continue
        raise SsrfValidationError(
            f"host '{host}' resolves to non-routable address {ip}"
        )
    return addresses
