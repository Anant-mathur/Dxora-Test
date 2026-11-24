"""URL validation and SSRF protection.

Layers:
1. normalize_url: scheme, credentials, port, length, hostname shape.
2. assert_public_host: resolves the hostname and rejects ANY address that is not globally
   routable (loopback, private, link-local incl. cloud metadata 169.254.169.254, CGNAT,
   multicast, reserved, IPv4-mapped IPv6 of those).
3. The extractor calls both again on every redirect hop.

Known limit: DNS can change between this check and the actual connection (DNS rebinding).
For hard guarantees, also block private ranges at the network layer (egress firewall).
"""
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from app.errors import BlockedURLError, HostResolutionError, InvalidURLError

MAX_URL_LENGTH = 2048
ALLOWED_PORTS = {None, 80, 443}


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise InvalidURLError("URL must not be empty.")
    if len(raw) > MAX_URL_LENGTH or any(c.isspace() or ord(c) < 32 for c in raw):
        raise InvalidURLError("URL is too long or contains whitespace/control characters.")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise InvalidURLError("Malformed URL.")
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidURLError("Only http:// and https:// URLs are supported.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise InvalidURLError("URL has no host.")
    if parts.username or parts.password:
        raise InvalidURLError("URLs with embedded credentials are not allowed.")
    if port not in ALLOWED_PORTS:
        raise BlockedURLError("Only ports 80 and 443 are allowed.")
    is_ip = _is_ip_literal(host)
    if not is_ip:
        if "." not in host:
            raise InvalidURLError("Host must be a fully qualified domain name (e.g. example.com).")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise InvalidURLError("Host contains invalid characters.")
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _check_ip(ip_text: str) -> None:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise BlockedURLError("This address is private or internal and cannot be analyzed.")


def resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HostResolutionError(f"Could not resolve the domain '{host}'.")
    return sorted({info[4][0] for info in infos})


def assert_public_host(host: str) -> None:
    """Raise BlockedURLError if the host is, or resolves to, a non-public address."""
    if _is_ip_literal(host):
        _check_ip(host)
        return
    addresses = resolve_host(host)
    if not addresses:
        raise HostResolutionError(f"Could not resolve the domain '{host}'.")
    for address in addresses:  # reject if ANY record is internal
        _check_ip(address)
