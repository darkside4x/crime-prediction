"""Fail-closed outbound media endpoint validation.

Camera URLs are configuration, but they still cross a server-side request
boundary.  Resolve them before invoking ffmpeg and reject every address class
that could reach the host, VPC, container network, or instance metadata.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import SplitResult, urlsplit

from .errors import VideoPipelineError

AddressResolver = Callable[[str, int], Iterable[str]]

_ALLOWED_PORTS = {
    "https": frozenset({443}),
    "rtsp": frozenset({554, 8554}),
    "rtsps": frozenset({322, 554, 8554}),
}
_DEFAULT_PORTS = {"https": 443, "rtsp": 554, "rtsps": 322}


def _system_resolver(hostname: str, port: int) -> set[str]:
    return {
        str(item[4][0])
        for item in socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    }


def validate_public_media_url(
    value: str,
    *,
    allowed_schemes: set[str] | frozenset[str],
    resolver: AddressResolver | None = None,
) -> SplitResult:
    """Return a parsed URL only when every resolved address is public.

    Validation is deliberately stricter than ordinary URL parsing: userinfo,
    fragments, non-allowlisted ports, unresolved hosts, IPv4-mapped private
    IPv6 addresses, and any non-global address fail closed.
    """

    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if (
            scheme not in allowed_schemes
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("URL shape")
        port = parsed.port or _DEFAULT_PORTS[scheme]
        if port not in _ALLOWED_PORTS[scheme]:
            raise ValueError("port")
        normalized_host = hostname.rstrip(".").lower()
        if not normalized_host or normalized_host == "localhost":
            raise ValueError("hostname")
        addresses = set((resolver or _system_resolver)(normalized_host, port))
        if not addresses:
            raise ValueError("unresolved")
        for address in addresses:
            parsed_address = ipaddress.ip_address(address)
            if isinstance(parsed_address, ipaddress.IPv6Address):
                mapped = parsed_address.ipv4_mapped
                if mapped is not None:
                    parsed_address = mapped
            if not parsed_address.is_global:
                raise ValueError("non-public address")
    except (OSError, TypeError, ValueError, KeyError) as error:
        raise VideoPipelineError(
            "camera_endpoint_invalid",
            "Camera endpoint must resolve only to an approved public network target",
        ) from error
    return parsed


__all__ = ["AddressResolver", "validate_public_media_url"]
