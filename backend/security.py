"""Network-boundary validation shared by model-facing research tools."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit


class UnsafeURLError(ValueError):
    pass


def validate_public_http_url(
    value: str,
    *,
    resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
) -> str:
    """Allow only public HTTP(S) destinations on standard ports."""
    if len(value) > 2_048:
        raise UnsafeURLError("URL exceeds 2048 characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURLError("only absolute HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URL credentials are not allowed")
    expected_port = 443 if parsed.scheme == "https" else 80
    if parsed.port not in {None, expected_port}:
        raise UnsafeURLError("only standard HTTP(S) ports are allowed")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise UnsafeURLError("local destinations are not allowed")
    try:
        records = resolver(hostname, expected_port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError("destination hostname could not be resolved") from exc
    addresses = {record[4][0] for record in records}
    if not addresses:
        raise UnsafeURLError("destination hostname returned no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise UnsafeURLError("private, local, reserved, and metadata addresses are blocked")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
