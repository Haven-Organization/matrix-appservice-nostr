"""Talking to other Nostr relays: the bridge as a client.

One ``RelayConnection`` per relay URL, shared by everything that publishes
to or subscribes on it. Each connection:

- connects only when something needs it, through ``bridge.safe_fetch``'s
  address check (relay URLs come from strangers' relay lists), and
  reconnects with exponential backoff while subscriptions remain,
- re-sends its subscriptions after every reconnect,
- paces outgoing events (``min_send_interval``), because public relays
  rate-limit by IP and every local ID publishes from the bridge's one
  address, and stops sending for a while after a ``rate-limited:`` reply,
- answers NIP-42 ``AUTH`` challenges, as the pubkey of the event being sent
  or the subscription being opened, when a relay demands it
  (``auth-required:``). Which keys it can sign for is up to the
  ``auth_signer`` the pool is given.

It knows nothing about Matrix or the bridge's IDs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import websockets
from websockets.asyncio.client import ClientConnection, connect

from bridge.nostr.event import Event
from bridge.safe_fetch import USER_AGENT, SafeFetcher, UnsafeAddress, resolve_target

logger = logging.getLogger(__name__)

# Given (pubkey, relay URL, challenge), a signed kind 22242 event, or None
# if the bridge can't sign for that pubkey.
AuthSigner = Callable[[str, str, str], Awaitable[Event | None]]
EventHandler = Callable[[str, str, dict[str, Any]], Any]  # (relay, sub id, raw event)
EoseHandler = Callable[[str, str], Any]  # (relay, sub id)
ClosedHandler = Callable[[str, str, str], Any]  # (relay, sub id, message)

AUTH_KIND = 22242

# Reply prefixes (NIP-01) worth trying again later: the same event may well
# be accepted then. Anything else (blocked, invalid, restricted, pow, mute)
# is the relay's considered "no".
_RETRYABLE_PREFIXES = ("rate-limited:", "error:", "auth-required:")


def normalize_relay_url(url: str) -> str:
    """One spelling per relay: lowercase scheme and host, no default port,
    no trailing slash on an empty path, no fragment."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or (scheme, port) in (("wss", 443), ("ws", 80)) else f"{host}:{port}"
    path = parts.path if parts.path not in ("", "/") else ""
    return urlunsplit((scheme, netloc, path, parts.query, ""))


@dataclass(frozen=True)
class PublishResult:
    relay: str
    accepted: bool
    message: str

    @property
    def retryable(self) -> bool:
        return not self.accepted and (self.message.startswith(_RETRYABLE_PREFIXES) or not self.message)


@dataclass
class Subscription:
    id: str
    filters: list[dict[str, Any]]
    on_event: EventHandler
    on_eose: EoseHandler | None = None
    on_closed: ClosedHandler | None = None
    # Authenticate as this pubkey if the relay refuses the REQ with
    # auth-required:.
    auth_pubkey: str | None = None
    auth_retried: bool = field(default=False, repr=False)


async def _call(handler: Callable[..., Any] | None, *args: Any) -> None:
    if handler is None:
        return
    try:
        result = handler(*args)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("Relay subscription handler failed")


class RelayConnection:
    def __init__(self, pool: "RelayPool", url: str) -> None:
        self.pool = pool
        self.url = url
        self.subscriptions: dict[str, Subscription] = {}
        self.last_error: str | None = None
        self.connected_since: float | None = None
        self._ws: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._unsafe = False
        self._demand = 0
        self._failures = 0
        self._pending_ok: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._cooldown_until = 0.0
        self._cooldown_seconds = 0.0
        self._challenge: str | None = None
        self._challenge_event = asyncio.Event()
        self._authed: set[str] = set()
        self.last_used = time.monotonic()

    # -- lifecycle -------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _wanted(self) -> bool:
        return not self._closing and not self._unsafe and (bool(self.subscriptions) or self._demand > 0)

    def _ensure_running(self) -> None:
        if self._unsafe or self._closing:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"relay {self.url}")

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._fail_pending("error: connection closed")

    def _fail_pending(self, message: str) -> None:
        for future in self._pending_ok.values():
            if not future.done():
                future.set_result((False, message))
        self._pending_ok.clear()

    async def _run(self) -> None:
        while self._wanted():
            try:
                target = await resolve_target(
                    self.url, allow_private=self.pool.allow_private, schemes=("ws", "wss")
                )
                async with connect(
                    self.url,
                    host=target.address,
                    port=target.port,
                    proxy=None,
                    open_timeout=self.pool.connect_timeout,
                    max_size=self.pool.max_message_bytes,
                    user_agent_header=USER_AGENT,
                ) as ws:
                    self._ws = ws
                    self._failures = 0
                    self.last_error = None
                    self.connected_since = time.monotonic()
                    self._connected.set()
                    for subscription in list(self.subscriptions.values()):
                        await self._send_req(subscription)
                    await self._read_loop(ws)
            except UnsafeAddress as exc:
                self.last_error = str(exc)
                self._unsafe = True
                logger.warning("Not connecting to relay %s: %s", self.url, exc)
            except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
                self.last_error = repr(exc)
                logger.debug("Relay %s connection failed: %r", self.url, exc)
            finally:
                self._ws = None
                self._connected.clear()
                self.connected_since = None
                self._challenge = None
                self._challenge_event.clear()
                self._authed.clear()
                self._fail_pending("error: connection lost")
            if not self._wanted():
                break
            self._failures += 1
            delay = min(self.pool.max_backoff, 2 ** min(self._failures, 10)) * (0.75 + random.random() / 2)
            await asyncio.sleep(delay)

    async def _read_loop(self, ws: ClientConnection) -> None:
        async for raw in ws:
            self.last_used = time.monotonic()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, list) or not message or not isinstance(message[0], str):
                continue
            await self._dispatch(message)

    async def _dispatch(self, message: list[Any]) -> None:
        kind = message[0]
        if kind == "EVENT" and len(message) >= 3 and isinstance(message[2], dict):
            subscription = self.subscriptions.get(message[1])
            if subscription is not None:
                await _call(subscription.on_event, self.url, subscription.id, message[2])
        elif kind == "EOSE" and len(message) >= 2:
            subscription = self.subscriptions.get(message[1])
            if subscription is not None:
                await _call(subscription.on_eose, self.url, subscription.id)
        elif kind == "OK" and len(message) >= 3:
            future = self._pending_ok.pop(str(message[1]), None)
            if future is not None and not future.done():
                text = message[3] if len(message) >= 4 and isinstance(message[3], str) else ""
                future.set_result((message[2] is True, text))
        elif kind == "CLOSED" and len(message) >= 2:
            await self._on_closed(str(message[1]), message[2] if len(message) >= 3 and isinstance(message[2], str) else "")
        elif kind == "AUTH" and len(message) >= 2 and isinstance(message[1], str):
            self._challenge = message[1]
            self._authed.clear()
            self._challenge_event.set()
        elif kind == "NOTICE" and len(message) >= 2:
            logger.debug("NOTICE from %s: %s", self.url, message[1])

    async def _on_closed(self, sub_id: str, text: str) -> None:
        subscription = self.subscriptions.get(sub_id)
        if subscription is None:
            return
        if text.startswith("auth-required:") and subscription.auth_pubkey and not subscription.auth_retried:
            subscription.auth_retried = True
            # Not awaited inline: authenticating waits on this same read loop.
            asyncio.create_task(self._auth_and_resubscribe(subscription))
            return
        self.subscriptions.pop(sub_id, None)
        await _call(subscription.on_closed, self.url, sub_id, text)

    async def _auth_and_resubscribe(self, subscription: Subscription) -> None:
        assert subscription.auth_pubkey is not None
        if await self._authenticate(subscription.auth_pubkey, self.pool.publish_timeout):
            if subscription.id in self.subscriptions:
                await self._send_req(subscription)
            return
        self.subscriptions.pop(subscription.id, None)
        await _call(subscription.on_closed, self.url, subscription.id, "auth-required: could not authenticate")

    # -- sending ---------------------------------------------------------

    async def _send(self, message: list[Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("not connected")
        await ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        self.last_used = time.monotonic()

    async def _send_req(self, subscription: Subscription) -> None:
        try:
            await self._send(["REQ", subscription.id, *subscription.filters])
        except (ConnectionError, websockets.exceptions.WebSocketException):
            pass  # re-sent on reconnect

    async def _send_and_wait_ok(self, message: list[Any], event_id: str, timeout: float) -> tuple[bool, str]:
        future = self._pending_ok.get(event_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._pending_ok[event_id] = future
            try:
                await self._send(message)
            except (ConnectionError, websockets.exceptions.WebSocketException) as exc:
                self._pending_ok.pop(event_id, None)
                return False, f"error: {exc!r}"
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            self._pending_ok.pop(event_id, None)
            return False, "error: no reply from relay"

    async def _send_event(self, event: Event, timeout: float) -> tuple[bool, str]:
        async with self._send_lock:
            wait = self._last_send + self.pool.min_send_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send = time.monotonic()
        return await self._send_and_wait_ok(["EVENT", event.to_dict()], event.id, timeout)

    async def _authenticate(self, pubkey: str, timeout: float) -> bool:
        if pubkey in self._authed:
            return True
        if self.pool.auth_signer is None:
            return False
        if self._challenge is None:
            try:
                await asyncio.wait_for(self._challenge_event.wait(), min(timeout, 3.0))
            except asyncio.TimeoutError:
                return False
        challenge = self._challenge
        if challenge is None:
            return False
        auth_event = await self.pool.auth_signer(pubkey, self.url, challenge)
        if auth_event is None:
            return False
        accepted, message = await self._send_and_wait_ok(["AUTH", auth_event.to_dict()], auth_event.id, timeout)
        if accepted:
            self._authed.add(pubkey)
        else:
            logger.info("Relay %s refused AUTH for %s: %s", self.url, pubkey, message)
        return accepted

    async def _wait_connected(self, timeout: float) -> bool:
        """Wait until connected, or until the connection gives up for good
        (an unsafe address), or ``timeout``. Returns whether connected."""
        if self.is_connected:
            return True
        waiter = asyncio.create_task(self._connected.wait())
        runner = self._task
        try:
            await asyncio.wait(
                [waiter] + ([runner] if runner is not None else []),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            waiter.cancel()
        return self.is_connected

    def _cooldown(self) -> None:
        self._cooldown_seconds = min(self.pool.max_backoff, max(5.0, self._cooldown_seconds * 2))
        self._cooldown_until = time.monotonic() + self._cooldown_seconds

    async def publish(self, event: Event, timeout: float) -> PublishResult:
        if self._unsafe:
            return PublishResult(self.url, False, f"blocked: {self.last_error or 'unsafe address'}")
        now = time.monotonic()
        if now < self._cooldown_until:
            return PublishResult(self.url, False, "rate-limited: waiting before sending this relay more")
        deadline = now + timeout
        self._demand += 1
        self._ensure_running()
        try:
            if not await self._wait_connected(timeout):
                if self._unsafe:
                    return PublishResult(self.url, False, f"blocked: {self.last_error or 'unsafe address'}")
                return PublishResult(self.url, False, f"error: could not connect ({self.last_error or 'timed out'})")
            accepted, message = await self._send_event(event, max(0.1, deadline - time.monotonic()))
            if not accepted and message.startswith("auth-required:"):
                if await self._authenticate(event.pubkey, max(0.1, deadline - time.monotonic())):
                    accepted, message = await self._send_event(event, max(0.1, deadline - time.monotonic()))
            if not accepted and message.startswith("rate-limited:"):
                self._cooldown()
            elif accepted:
                self._cooldown_seconds = 0.0
            return PublishResult(self.url, accepted, message)
        finally:
            self._demand -= 1

    # -- subscriptions ---------------------------------------------------

    def subscribe(self, subscription: Subscription) -> None:
        self.subscriptions[subscription.id] = subscription
        self._ensure_running()
        if self.is_connected:
            asyncio.create_task(self._send_req(subscription))

    async def unsubscribe(self, sub_id: str) -> None:
        if self.subscriptions.pop(sub_id, None) is not None and self.is_connected:
            try:
                await self._send(["CLOSE", sub_id])
            except (ConnectionError, websockets.exceptions.WebSocketException):
                pass


class RelayPool:
    def __init__(
        self,
        *,
        allow_private: bool = False,
        auth_signer: AuthSigner | None = None,
        min_send_interval: float = 0.25,
        publish_timeout: float = 15.0,
        connect_timeout: float = 10.0,
        max_backoff: float = 300.0,
        max_message_bytes: int = 4 * 1024 * 1024,
        idle_timeout: float = 300.0,
        fetcher: SafeFetcher | None = None,
    ) -> None:
        self.allow_private = allow_private
        self.auth_signer = auth_signer
        self.min_send_interval = min_send_interval
        self.publish_timeout = publish_timeout
        self.connect_timeout = connect_timeout
        self.max_backoff = max_backoff
        self.max_message_bytes = max_message_bytes
        self.idle_timeout = idle_timeout
        self._fetcher = fetcher
        self._connections: dict[str, RelayConnection] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._janitor: asyncio.Task[None] | None = None

    def connection(self, url: str) -> RelayConnection:
        key = normalize_relay_url(url)
        connection = self._connections.get(key)
        if connection is None:
            connection = RelayConnection(self, key)
            self._connections[key] = connection
            if self._janitor is None:
                self._janitor = asyncio.create_task(self._close_idle_loop(), name="relay pool janitor")
        return connection

    def connections(self) -> list[RelayConnection]:
        return list(self._connections.values())

    async def publish(self, event: Event, relays: list[str], timeout: float | None = None) -> list[PublishResult]:
        """Send ``event`` to every relay in ``relays`` at once and collect
        their answers. Never raises for a relay's failure."""
        urls = list(dict.fromkeys(normalize_relay_url(url) for url in relays))
        return list(
            await asyncio.gather(
                *(self.connection(url).publish(event, timeout or self.publish_timeout) for url in urls)
            )
        )

    def subscribe(
        self,
        url: str,
        filters: list[dict[str, Any]],
        on_event: EventHandler,
        *,
        on_eose: EoseHandler | None = None,
        on_closed: ClosedHandler | None = None,
        auth_pubkey: str | None = None,
        sub_id: str | None = None,
    ) -> str:
        """Open a subscription that survives reconnects until
        ``unsubscribe``. Returns its id."""
        subscription = Subscription(
            id=sub_id or secrets.token_hex(8),
            filters=filters,
            on_event=on_event,
            on_eose=on_eose,
            on_closed=on_closed,
            auth_pubkey=auth_pubkey,
        )
        self.connection(url).subscribe(subscription)
        return subscription.id

    async def query(self, urls: list[str], filters: list[dict[str, Any]], *, timeout: float = 8.0) -> list[dict[str, Any]]:
        """One-off fetch: ask every relay in ``urls`` and collect what they
        send until each says EOSE (or closes, or ``timeout``). Events are
        deduplicated by id; nothing is verified here."""
        urls = list(dict.fromkeys(normalize_relay_url(url) for url in urls))
        if not urls:
            return []
        found: dict[str, dict[str, Any]] = {}
        finished: set[str] = set()
        all_done = asyncio.Event()

        def on_event(relay: str, sub_id: str, event: dict[str, Any]) -> None:
            event_id = event.get("id")
            if isinstance(event_id, str):
                found.setdefault(event_id, event)

        def on_done(relay: str, sub_id: str, *_: Any) -> None:
            finished.add(relay)
            if len(finished) >= len(urls):
                all_done.set()

        subs: list[tuple[str, str]] = []
        for url in urls:
            connection = self.connection(url)
            if connection._unsafe:
                on_done(url, "")
                continue
            sub_id = self.subscribe(url, filters, on_event, on_eose=on_done, on_closed=on_done)
            subs.append((url, sub_id))
        try:
            await asyncio.wait_for(all_done.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            for url, sub_id in subs:
                await self.unsubscribe(url, sub_id)
        return list(found.values())

    async def unsubscribe(self, url: str, sub_id: str) -> None:
        connection = self._connections.get(normalize_relay_url(url))
        if connection is not None:
            await connection.unsubscribe(sub_id)

    async def relay_info(self, url: str) -> dict[str, Any]:
        """The relay's NIP-11 document (cached), or ``{}`` if it has none."""
        key = normalize_relay_url(url)
        if key in self._info:
            return self._info[key]
        info: dict[str, Any] = {}
        if self._fetcher is not None:
            parts = urlsplit(key)
            http_url = urlunsplit(("https" if parts.scheme == "wss" else "http", parts.netloc, parts.path or "/", "", ""))
            try:
                fetched = await self._fetcher.get(http_url, max_bytes=256 * 1024, accept="application/nostr+json")
                if fetched.status == 200:
                    parsed = json.loads(fetched.content)
                    if isinstance(parsed, dict):
                        info = parsed
            except Exception:
                logger.debug("No NIP-11 document from %s", key, exc_info=True)
        self._info[key] = info
        return info

    async def _close_idle_loop(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self.idle_timeout))
            now = time.monotonic()
            for key, connection in list(self._connections.items()):
                if not connection.subscriptions and connection._demand == 0 and now - connection.last_used > self.idle_timeout:
                    self._connections.pop(key, None)
                    await connection.close()

    async def close(self) -> None:
        if self._janitor is not None:
            self._janitor.cancel()
            try:
                await self._janitor
            except asyncio.CancelledError:
                pass
            self._janitor = None
        await asyncio.gather(*(connection.close() for connection in self._connections.values()), return_exceptions=True)
        self._connections.clear()
