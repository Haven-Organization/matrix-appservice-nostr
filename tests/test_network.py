"""The guarded fetcher and the outgoing relay pool, against local servers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from websockets.asyncio.server import serve

from bridge.nostr import event as nostr_event
from bridge.nostr import secp256k1
from bridge.nostr.relay_pool import RelayPool, normalize_relay_url
from bridge.safe_fetch import FetchError, SafeFetcher, UnsafeAddress, resolve_target

# -- address guard -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
    ],
)
async def test_private_addresses_refused(url: str) -> None:
    with pytest.raises(UnsafeAddress):
        await resolve_target(url, allow_private=False)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.org/", "http://user:pw@example.org/", "http:///x"])
async def test_bad_urls_refused(url: str) -> None:
    with pytest.raises(UnsafeAddress):
        await resolve_target(url, allow_private=True)


async def test_public_literal_allowed() -> None:
    target = await resolve_target("https://1.1.1.1/x", allow_private=False)
    assert (target.address, target.port, target.hostname) == ("1.1.1.1", 443, "1.1.1.1")


async def test_relay_scheme_check() -> None:
    with pytest.raises(UnsafeAddress):
        await resolve_target("https://relay.example/", allow_private=True, schemes=("ws", "wss"))
    with pytest.raises(UnsafeAddress):
        await resolve_target("ws://127.0.0.1:7777", allow_private=False, schemes=("ws", "wss"))


# -- fetcher, against a tiny local HTTP server -------------------------------


async def _http_server(routes: dict[str, tuple[int, dict[str, str], bytes]]):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split(b" ", 2)[1].decode()
        status, headers, body = routes.get(path, (404, {}, b"nope"))
        head = f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        writer.write(head.encode() + b"\r\n" + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_fetch_follows_checked_redirects() -> None:
    server, port = await _http_server(
        {
            "/start": (302, {"Location": "/end"}, b""),
            "/end": (200, {"Content-Type": "text/plain"}, b"done"),
            "/loop": (302, {"Location": "/loop"}, b""),
            "/big": (200, {}, b"x" * 2000),
        }
    )
    fetcher = SafeFetcher(allow_private=True)
    try:
        fetched = await fetcher.get(f"http://127.0.0.1:{port}/start", max_bytes=100)
        assert (fetched.status, fetched.text, fetched.url) == (200, "done", f"http://127.0.0.1:{port}/end")
        with pytest.raises(FetchError, match="redirects"):
            await fetcher.get(f"http://127.0.0.1:{port}/loop", max_bytes=100)
        with pytest.raises(FetchError, match="large"):
            await fetcher.get(f"http://127.0.0.1:{port}/big", max_bytes=100)
    finally:
        await fetcher.aclose()
        server.close()


async def test_fetch_refuses_private_by_default() -> None:
    server, port = await _http_server({"/": (200, {}, b"secret")})
    fetcher = SafeFetcher(allow_private=False)
    try:
        with pytest.raises(UnsafeAddress):
            await fetcher.get(f"http://127.0.0.1:{port}/", max_bytes=100)
    finally:
        await fetcher.aclose()
        server.close()


# -- relay pool, against a fake relay -----------------------------------------


class FakeRelay:
    """Enough of a relay to exercise the pool: stores events, answers REQs
    by author, and can require NIP-42 auth, rate-limit, or hang up."""

    def __init__(self, *, require_auth: bool = False, rate_limit_after: int | None = None) -> None:
        self.require_auth = require_auth
        self.rate_limit_after = rate_limit_after
        self.events: list[dict[str, Any]] = []
        self.reqs: list[list[Any]] = []
        self.connections = 0
        self.received = 0
        self.authed_pubkeys: list[str] = []
        self._sockets: list[Any] = []

    async def handler(self, ws) -> None:
        self.connections += 1
        self._sockets.append(ws)
        challenge = f"challenge-{self.connections}"
        authed: set[str] = set()
        await ws.send(json.dumps(["AUTH", challenge]))
        async for raw in ws:
            message = json.loads(raw)
            if message[0] == "AUTH":
                auth = nostr_event.verify(message[1])
                ok = auth.kind == 22242 and ["challenge", challenge] in [list(t) for t in auth.tags]
                if ok:
                    authed.add(auth.pubkey)
                    self.authed_pubkeys.append(auth.pubkey)
                await ws.send(json.dumps(["OK", auth.id, ok, "" if ok else "invalid: bad auth"]))
            elif message[0] == "EVENT":
                event = message[1]
                self.received += 1
                if self.rate_limit_after is not None and self.received > self.rate_limit_after:
                    await ws.send(json.dumps(["OK", event["id"], False, "rate-limited: slow down"]))
                elif self.require_auth and event["pubkey"] not in authed:
                    await ws.send(json.dumps(["OK", event["id"], False, "auth-required: who are you"]))
                else:
                    nostr_event.verify(event)
                    self.events.append(event)
                    await ws.send(json.dumps(["OK", event["id"], True, ""]))
            elif message[0] == "REQ":
                self.reqs.append(message)
                sub_id, filters = message[1], message[2:]
                for event in self.events:
                    if any(event["pubkey"] in f.get("authors", []) for f in filters):
                        await ws.send(json.dumps(["EVENT", sub_id, event]))
                await ws.send(json.dumps(["EOSE", sub_id]))

    async def hang_up_everyone(self) -> None:
        for ws in self._sockets:
            await ws.close()
        self._sockets.clear()


async def _start(relay: FakeRelay):
    server = await serve(relay.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


def _note(secret: bytes, text: str) -> nostr_event.Event:
    return nostr_event.build(secret, kind=1, content=text, created_at=1700000000)


def test_normalize_relay_url() -> None:
    assert normalize_relay_url("WSS://Relay.Example.org/") == "wss://relay.example.org"
    assert normalize_relay_url("wss://relay.example.org:443") == "wss://relay.example.org"
    assert normalize_relay_url("wss://poster.place/relay") == "wss://poster.place/relay"
    assert normalize_relay_url("ws://host:7777/#x") == "ws://host:7777"


async def test_publish_and_subscribe() -> None:
    relay = FakeRelay()
    server, url = await _start(relay)
    pool = RelayPool(allow_private=True, min_send_interval=0)
    secret = secp256k1.generate_secret_key()
    try:
        note = _note(secret, "hello")
        [result] = await pool.publish(note, [url])
        assert result.accepted and relay.events == [note.to_dict()]

        received: list[dict] = []
        eose = asyncio.Event()
        pool.subscribe(url, [{"authors": [note.pubkey]}], lambda r, s, e: received.append(e), on_eose=lambda r, s: eose.set())
        await asyncio.wait_for(eose.wait(), 5)
        assert received == [note.to_dict()]
    finally:
        await pool.close()
        server.close()


async def test_publish_to_unreachable_relay_reports_failure() -> None:
    pool = RelayPool(allow_private=True, connect_timeout=1)
    try:
        [result] = await pool.publish(_note(secp256k1.generate_secret_key(), "x"), ["ws://127.0.0.1:1"], timeout=2)
        assert not result.accepted and result.retryable
    finally:
        await pool.close()


async def test_private_relay_refused_without_flag() -> None:
    relay = FakeRelay()
    server, url = await _start(relay)
    pool = RelayPool(allow_private=False)
    try:
        [result] = await pool.publish(_note(secp256k1.generate_secret_key(), "x"), [url], timeout=2)
        assert not result.accepted and result.message.startswith("blocked:") and not result.retryable
        assert relay.connections == 0
    finally:
        await pool.close()
        server.close()


async def test_auth_required_is_answered() -> None:
    relay = FakeRelay(require_auth=True)
    server, url = await _start(relay)
    secret = secp256k1.generate_secret_key()
    pubkey = secp256k1.public_key(secret).hex()

    async def signer(for_pubkey: str, relay_url: str, challenge: str):
        if for_pubkey != pubkey:
            return None
        return nostr_event.build(
            secret, kind=22242, content="", created_at=1700000000, tags=[["relay", relay_url], ["challenge", challenge]]
        )

    pool = RelayPool(allow_private=True, auth_signer=signer, min_send_interval=0)
    try:
        [result] = await pool.publish(_note(secret, "needs auth"), [url])
        assert result.accepted, result.message
        assert relay.authed_pubkeys == [pubkey]
        # Someone we can't sign for stays refused.
        [other] = await pool.publish(_note(secp256k1.generate_secret_key(), "stranger"), [url])
        assert not other.accepted and other.message.startswith("auth-required:")
    finally:
        await pool.close()
        server.close()


async def test_rate_limited_relay_gets_a_rest() -> None:
    relay = FakeRelay(rate_limit_after=1)
    server, url = await _start(relay)
    pool = RelayPool(allow_private=True, min_send_interval=0)
    secret = secp256k1.generate_secret_key()
    try:
        assert (await pool.publish(_note(secret, "1"), [url]))[0].accepted
        limited = (await pool.publish(_note(secret, "2"), [url]))[0]
        assert limited.message.startswith("rate-limited:") and limited.retryable
        before = relay.received
        resting = (await pool.publish(_note(secret, "3"), [url]))[0]
        assert resting.message.startswith("rate-limited:")
        assert relay.received == before  # not even sent
    finally:
        await pool.close()
        server.close()


async def test_subscriptions_survive_reconnect() -> None:
    relay = FakeRelay()
    server, url = await _start(relay)
    pool = RelayPool(allow_private=True, max_backoff=0.2)
    try:
        eoses = asyncio.Queue()
        pool.subscribe(url, [{"authors": ["aa" * 32]}], lambda r, s, e: None, on_eose=lambda r, s: eoses.put_nowait(s))
        first = await asyncio.wait_for(eoses.get(), 5)
        await relay.hang_up_everyone()
        second = await asyncio.wait_for(eoses.get(), 10)
        assert first == second
        assert relay.connections == 2 and len(relay.reqs) == 2
    finally:
        await pool.close()
        server.close()


async def test_relay_info_uses_fetcher() -> None:
    body = json.dumps({"name": "fake", "limitation": {"max_subscriptions": 5}}).encode()
    server, port = await _http_server({"/": (200, {"Content-Type": "application/nostr+json"}, body)})
    fetcher = SafeFetcher(allow_private=True)
    pool = RelayPool(allow_private=True, fetcher=fetcher)
    try:
        info = await pool.relay_info(f"ws://127.0.0.1:{port}")
        assert info["limitation"]["max_subscriptions"] == 5
    finally:
        await pool.close()
        await fetcher.aclose()
        server.close()
