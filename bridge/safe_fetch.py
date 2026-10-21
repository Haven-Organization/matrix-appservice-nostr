"""Fetching URLs that strangers chose, safely.

Media in posts, avatars, custom emoji, NIP-05 lookups of remote accounts,
and the relays in other people's relay lists are all addresses picked by
someone on the internet. Without care, any of them could point the bridge
at its own network: the homeserver's admin API, a database, a cloud
metadata endpoint. So every such connection goes through here, which:

- allows only http(s) (or ws(s), for relays),
- resolves the hostname and refuses it unless every address is public
  (not private, loopback, link-local, multicast, reserved, ...),
- then connects to the address it checked, not the hostname, so DNS can't
  answer differently the second time (DNS rebinding); TLS still verifies
  the certificate against the real hostname,
- follows redirects by hand, checking each hop the same way,
- caps time and response size,
- never sends credentials of any kind.

``allow_private`` (``nostr.allow_private_addresses``) switches the address
check off, for local development only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "matrix-appservice-nostr (+https://github.com/Haven-Organization/matrix-appservice-nostr)"
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


class UnsafeAddress(Exception):
    """The URL points somewhere the bridge refuses to connect to."""


class FetchError(Exception):
    """The fetch failed: unreachable, too big, too slow, or too many redirects."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


@dataclass(frozen=True)
class Target:
    """Where to actually connect for a checked URL."""

    scheme: str
    hostname: str
    port: int
    address: str


async def resolve_target(url: str, *, allow_private: bool, schemes: tuple[str, ...] = ("http", "https")) -> Target:
    """Check ``url`` and pick the address to connect to. Raises
    ``UnsafeAddress``."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        raise UnsafeAddress(f"Only {', '.join(schemes)} URLs are allowed")
    hostname = (parts.hostname or "").rstrip(".").lower()
    if not hostname:
        raise UnsafeAddress("URL has no host")
    if parts.username or parts.password:
        raise UnsafeAddress("URLs with credentials are not allowed")
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise UnsafeAddress("Invalid port") from exc
    try:
        ipaddress.ip_address(hostname)
        literal = True
    except ValueError:
        literal = False
    if literal:
        addresses = [hostname]
    else:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeAddress(f"Could not resolve {hostname}") from exc
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not addresses:
            raise UnsafeAddress(f"Could not resolve {hostname}")
    if not allow_private and not all(_is_public(address) for address in addresses):
        raise UnsafeAddress(f"{hostname} does not resolve to a public address")
    return Target(scheme=scheme, hostname=hostname, port=port, address=addresses[0])


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    content: bytes
    content_type: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def _pinned_url(url: str, target: Target) -> str:
    parts = urlsplit(url)
    host = f"[{target.address}]" if ":" in target.address else target.address
    netloc = f"{host}:{target.port}"
    rebuilt = f"{parts.scheme}://{netloc}{parts.path or '/'}"
    if parts.query:
        rebuilt += f"?{parts.query}"
    return rebuilt


def _host_header(target: Target) -> str:
    host = f"[{target.hostname}]" if ":" in target.hostname else target.hostname
    return host if target.port == _DEFAULT_PORTS[target.scheme] else f"{host}:{target.port}"


class SafeFetcher:
    def __init__(self, *, allow_private: bool = False, timeout: float = 15.0) -> None:
        self.allow_private = allow_private
        self._timeout = timeout
        # follow_redirects stays off: every hop is checked by hand.
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        max_bytes: int,
        accept: str | None = None,
        max_redirects: int = 5,
    ) -> Fetched:
        """GET ``url``. Raises ``UnsafeAddress`` or ``FetchError``. A
        non-2xx final status is returned, not raised."""
        current = url
        for _ in range(max_redirects + 1):
            target = await resolve_target(current, allow_private=self.allow_private)
            headers = {"Host": _host_header(target), "User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            if accept:
                headers["Accept"] = accept
            extensions = {"sni_hostname": target.hostname} if target.scheme == "https" else {}
            request = self._client.build_request(
                "GET", _pinned_url(current, target), headers=headers, extensions=extensions
            )
            try:
                response = await asyncio.wait_for(self._client.send(request, stream=True), self._timeout)
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                raise FetchError(f"Could not fetch {current}: {exc!r}") from exc
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("Redirect without a Location")
                    current = urljoin(current, location)
                    continue
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(f"Response too large ({declared} bytes)")
                body = bytearray()
                try:
                    async with asyncio.timeout(self._timeout):
                        async for chunk in response.aiter_raw():
                            body += chunk
                            if len(body) > max_bytes:
                                raise FetchError(f"Response larger than {max_bytes} bytes")
                except (httpx.HTTPError, TimeoutError) as exc:
                    raise FetchError(f"Could not read {current}: {exc!r}") from exc
                return Fetched(
                    url=current,
                    status=response.status_code,
                    content=bytes(body),
                    content_type=response.headers.get("content-type", "application/octet-stream"),
                )
            finally:
                await response.aclose()
        raise FetchError("Too many redirects")
