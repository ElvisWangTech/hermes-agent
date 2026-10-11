"""Where an internal proxy is allowed to send a request.

Names must be listed. IP addresses must be loopback, private, or link-local
unless they are also listed. One hop: a request that already carries the hop
header is never forwarded again.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

HOP_HEADER = "X-Hermes-Cluster-Hop"
TOKEN_HEADER = "X-Hermes-Cluster-Token"
PROFILE_HEADER = "X-Hermes-Cluster-Profile"


def address_allowed(url: str, allow_hosts: tuple[str, ...] | list[str]) -> bool:
    """True when ``url`` is an internal HTTP target this deployment may dial."""
    parts = urlsplit(url or "")
    if parts.scheme not in {"http", "https"} or parts.username or not parts.hostname:
        return False
    host = parts.hostname.lower().rstrip(".")
    allowed = {item.strip().lower() for item in allow_hosts if item and item.strip()}
    if host in allowed:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def same_destination(left: str, right: str) -> bool:
    """Host and port equality, ignoring path."""
    a, b = urlsplit(left or ""), urlsplit(right or "")
    if not a.hostname or not b.hostname:
        return False
    return a.hostname.lower() == b.hostname.lower() and _port(a) == _port(b)


def join_owner_url(owner_base: str, path: str, query: str = "") -> str:
    parts = urlsplit(owner_base)
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _port(parts) -> int:
    if parts.port:
        return parts.port
    return 443 if parts.scheme == "https" else 80
