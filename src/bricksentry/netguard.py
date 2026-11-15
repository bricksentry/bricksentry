"""Guard for the few outbound HTTP calls bricksentry makes (Slack/Teams webhooks, GitHub API).

The URLs come from your own config or CI environment, but a mistyped or malicious value could
still point at an internal service. So outbound requests must use https, must not target
loopback, private, link-local or other non-public addresses, and never follow redirects.

Set ``BRICKSENTRY_ALLOW_PRIVATE_URLS=1`` when a legitimate target is on a private network
(for example GitHub Enterprise Server or a webhook relay inside your VPC).
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.request
from urllib.parse import urlsplit

ALLOW_PRIVATE_ENV = "BRICKSENTRY_ALLOW_PRIVATE_URLS"


class UnsafeURLError(ValueError):
    """An outbound URL that bricksentry refuses to call."""


def _private_allowed() -> bool:
    return os.environ.get(ALLOW_PRIVATE_ENV, "").strip().lower() in ("1", "true", "yes")


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
                or addr.is_multicast or addr.is_unspecified)


def check_url(url: str, what: str = "URL") -> None:
    """Raise UnsafeURLError if ``url`` is not https or resolves to a non-public address.

    A host that can't be resolved right now is let through: the request itself will then fail
    with a normal connection error, and nothing internal can be reached that way.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeURLError(f"{what} must start with https://")
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"{what} has no host")
    if _private_allowed():
        return
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
        raise UnsafeURLError(f"{what} points at localhost; set {ALLOW_PRIVATE_ENV}=1 if that is intended")
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            candidates = [info[4][0] for info in socket.getaddrinfo(host, parts.port or 443)]
        except (socket.gaierror, UnicodeError, OSError):
            return
    for ip in candidates:
        if not _is_public(ip):
            raise UnsafeURLError(
                f"{what} resolves to a non-public address; set {ALLOW_PRIVATE_ENV}=1 if that is intended")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None  # a redirect becomes an HTTPError instead of a request to a new host


_OPENER = urllib.request.build_opener(_NoRedirect)


def urlopen(req: urllib.request.Request, timeout: float):
    """``urllib.request.urlopen`` without redirects, after ``check_url``."""
    check_url(req.full_url)
    return _OPENER.open(req, timeout=timeout)
