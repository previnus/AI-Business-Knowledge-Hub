import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

WEB_LIMIT = 5 * 1024 * 1024
TIME_LIMIT = 12
IPV6_UNICAST = ipaddress.IPv6Network("2000::/3")
AZURE_WIRE_SERVER = ipaddress.IPv4Address("168.63.129.16")


class InvalidSource(Exception):
    pass


class SourceUnavailable(Exception):
    pass


def public_url(value: str, allowed_hosts: set[str]) -> str:
    if not value or len(value) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in value) or "\\" in value:
        raise InvalidSource("invalid_url")
    try:
        url = urlsplit(value)
        host = (url.hostname or "").rstrip(".")
        if (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.port not in (None, 443)
            or host not in allowed_hosts
            or any(not label or not label.replace("-", "").isalnum() for label in host.split("."))
        ):
            raise InvalidSource("host_not_allowed")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return urlunsplit(("https", host, url.path or "/", url.query, ""))
        raise InvalidSource("host_not_allowed")
    except ValueError:
        raise InvalidSource("invalid_url") from None


def resolve_public(host: str) -> str:
    addresses = {
        ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(
        not ip.is_global
        or ip.is_multicast
        or ip == AZURE_WIRE_SERVER
        or (ip.version == 6 and ip not in IPV6_UNICAST)
        or getattr(ip, "sixtofour", None) is not None
        or getattr(ip, "teredo", None) is not None
        for ip in addresses
    ):
        raise InvalidSource("non_public_address")
    return str(min(addresses, key=lambda ip: (ip.version, int(ip))))


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def fetch_html(value: str, allowed_hosts: set[str]) -> tuple[str, bytes]:
    deadline = time.monotonic() + TIME_LIMIT

    def remaining() -> float:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise SourceUnavailable("website_timeout")
        return timeout

    try:
        for _ in range(4):
            url = public_url(value, allowed_hosts)
            parsed = urlsplit(url)
            address = resolve_public(parsed.hostname)
            connection = PinnedHTTPS(parsed.hostname, address, remaining())
            try:
                target = urlunsplit(("", "", parsed.path, parsed.query, ""))
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Accept": "text/html, application/xhtml+xml",
                        "Accept-Encoding": "identity",
                        "User-Agent": "CompanyKnowledgeHub/1.0",
                    },
                )
                sock = connection.sock
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise InvalidSource("invalid_redirect")
                    if any(ord(c) < 33 or ord(c) > 126 for c in location) or "\\" in location:
                        raise InvalidSource("invalid_redirect")
                    value = urljoin(url, location)
                    continue
                if response.status in (408, 429) or response.status >= 500:
                    raise SourceUnavailable("website_unavailable")
                if response.status != 200:
                    raise InvalidSource("website_rejected")
                mime = response.getheader("Content-Type", "").split(";")[0].strip().lower()
                if mime not in ("text/html", "application/xhtml+xml"):
                    raise InvalidSource("expected_html")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise InvalidSource("unsupported_encoding")
                if response.length is not None and response.length > WEB_LIMIT:
                    raise InvalidSource("website_too_large")
                body = bytearray()
                while len(body) <= WEB_LIMIT:
                    sock.settimeout(remaining())
                    block = response.read1(min(64 * 1024, WEB_LIMIT + 1 - len(body)))
                    if not block:
                        if response.length:
                            raise SourceUnavailable("website_unavailable")
                        return url, bytes(body)
                    body.extend(block)
                raise InvalidSource("website_too_large")
            finally:
                connection.close()
        raise InvalidSource("too_many_redirects")
    except ssl.SSLCertVerificationError:
        raise InvalidSource("website_tls_error") from None
    except (OSError, http.client.HTTPException):
        raise SourceUnavailable("website_unavailable") from None
