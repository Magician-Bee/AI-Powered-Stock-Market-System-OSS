from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse


def validate_public_http_url(value: Any, *, purpose: str = "Network") -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{purpose} URL must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise PermissionError(f"Credentials in {purpose.casefold()} URLs are blocked")
    if url_has_blocked_target(url, resolve_dns=True):
        raise PermissionError(
            f"{purpose} targets on localhost, private, link-local or reserved networks are blocked"
        )
    return url


def url_has_blocked_target(url: str, *, resolve_dns: bool) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in {"data", "blob", "about"}:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return True
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local")):
        return True
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(host)))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(
                    item[4][0]
                    for item in socket.getaddrinfo(
                        host,
                        parsed.port or (443 if parsed.scheme == "https" else 80),
                        type=socket.SOCK_STREAM,
                    )
                )
            except OSError as exc:
                raise RuntimeError(f"Unable to resolve public network host: {host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
            or ip.is_multicast
        ):
            return True
    return False


__all__ = ["url_has_blocked_target", "validate_public_http_url"]
