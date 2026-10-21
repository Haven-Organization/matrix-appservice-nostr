"""NIP-46 remote signing ("Nostr Connect", "bunker"): an ID whose secret key
stays in a signer app (nsec.app, Amber, a hardware signer), which the
bridge asks to sign and to decrypt.

The bridge is the *client*. It has its own throwaway keypair (kept
encrypted like any other key), and talks to the signer through relays:
requests and responses are kind 24133 events, NIP-44 encrypted between the
client key and the signer's key, tagged with the recipient's key.

    bunker://<signer pubkey>?relay=wss://...&relay=wss://...&secret=<optional>

Methods used: ``connect``, ``get_public_key``, ``sign_event``,
``nip44_encrypt``, ``nip44_decrypt``. A signer may answer a request with
``auth_url`` (the user must approve in a browser first): that surfaces as
``AuthUrlNeeded`` so the user can be shown the link.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bridge.nostr import event as nostr_event
from bridge.nostr import nip04, nip44, secp256k1

logger = logging.getLogger(__name__)

REQUEST_KIND = 24133
_HEX64 = frozenset("0123456789abcdef")


class RemoteSignerError(Exception):
    """The signer refused, failed, or didn't answer. Safe to show."""


class AuthUrlNeeded(RemoteSignerError):
    def __init__(self, url: str) -> None:
        super().__init__(f"Approve the bridge in your signer first: {url}")
        self.url = url


@dataclass(frozen=True)
class BunkerUri:
    signer_pubkey: str
    relays: tuple[str, ...]
    secret: str | None


def parse_bunker_uri(uri: str) -> BunkerUri:
    parts = urlsplit(uri.strip())
    if parts.scheme != "bunker":
        raise RemoteSignerError("That isn't a bunker:// address.")
    pubkey = (parts.netloc or parts.path.lstrip("/")).lower()
    if len(pubkey) != 64 or not set(pubkey) <= _HEX64:
        raise RemoteSignerError("The bunker address doesn't name a valid signer key.")
    query = parse_qs(parts.query)
    relays = tuple(r for r in query.get("relay", []) if r.startswith(("wss://", "ws://")))
    if not relays:
        raise RemoteSignerError("The bunker address names no relay to reach the signer on.")
    secret = (query.get("secret") or [None])[0]
    return BunkerUri(pubkey, relays[:4], secret)


class RemoteSigner:
    def __init__(self, pool, *, client_secret: bytes, bunker: BunkerUri, user_pubkey: str | None = None) -> None:
        self._pool = pool
        self._client_secret = client_secret
        self.client_pubkey = secp256k1.public_key(client_secret).hex()
        self.bunker = bunker
        self.user_pubkey = user_pubkey
        self._conversation_key = nip44.get_conversation_key(client_secret, bunker.signer_pubkey)
        self._waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._subscribed = False
        self._lock = asyncio.Lock()

    def _ensure_subscription(self) -> None:
        if self._subscribed:
            return
        for relay in self.bunker.relays:
            self._pool.subscribe(
                relay,
                [{"kinds": [REQUEST_KIND], "#p": [self.client_pubkey], "since": int(time.time()) - 10}],
                self._on_event,
            )
        self._subscribed = True

    def _on_event(self, relay: str, sub_id: str, raw: dict[str, Any]) -> None:
        if raw.get("pubkey") != self.bunker.signer_pubkey:
            return
        try:
            nostr_event.verify(raw)
            try:
                plaintext = nip44.decrypt(raw["content"], self._conversation_key)
            except Exception:
                plaintext = nip04.decrypt(raw["content"], self._client_secret, self.bunker.signer_pubkey)
            message = json.loads(plaintext)
        except Exception:
            logger.debug("Unreadable NIP-46 message from %s", relay, exc_info=True)
            return
        future = self._waiting.get(str(message.get("id")))
        if future is not None and not future.done():
            if message.get("result") == "auth_url" and isinstance(message.get("error"), str):
                future.set_exception(AuthUrlNeeded(message["error"]))
            else:
                future.set_result(message)

    async def request(self, method: str, params: list[str], *, timeout: float = 30.0) -> str:
        self._ensure_subscription()
        request_id = secrets.token_hex(8)
        payload = json.dumps({"id": request_id, "method": method, "params": params})
        event = nostr_event.build(
            self._client_secret,
            kind=REQUEST_KIND,
            content=nip44.encrypt(payload, self._conversation_key),
            created_at=int(time.time()),
            tags=[["p", self.bunker.signer_pubkey]],
        )
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            results = await self._pool.publish(event, list(self.bunker.relays))
            if not any(r.accepted for r in results):
                raise RemoteSignerError("Couldn't reach the signer's relays.")
            message = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RemoteSignerError("The remote signer didn't answer in time. Is it online?") from exc
        finally:
            self._waiting.pop(request_id, None)
        if message.get("error"):
            raise RemoteSignerError(f"The remote signer refused: {message['error']}")
        result = message.get("result")
        if not isinstance(result, str):
            raise RemoteSignerError("The remote signer sent an unexpected answer.")
        return result

    async def connect(self) -> str:
        """Introduce the bridge to the signer, then ask which key it signs
        for. Returns that pubkey."""
        params = [self.bunker.signer_pubkey]
        if self.bunker.secret:
            params.append(self.bunker.secret)
        params.append("sign_event,nip44_encrypt,nip44_decrypt,get_public_key")
        result = await self.request("connect", params, timeout=60.0)
        if self.bunker.secret and result not in ("ack", self.bunker.secret):
            raise RemoteSignerError("The signer's answer didn't match the bunker address's secret.")
        pubkey = (await self.request("get_public_key", [])).lower()
        if len(pubkey) != 64 or not set(pubkey) <= _HEX64:
            raise RemoteSignerError("The signer returned an invalid public key.")
        self.user_pubkey = pubkey
        return pubkey

    async def sign_event(self, *, kind: int, content: str, created_at: int, tags: list[list[str]]) -> nostr_event.Event:
        unsigned = {"kind": kind, "content": content, "tags": tags, "created_at": created_at}
        if self.user_pubkey:
            unsigned["pubkey"] = self.user_pubkey
        async with self._lock:
            result = await self.request("sign_event", [json.dumps(unsigned)])
        try:
            signed = nostr_event.verify(json.loads(result))
        except (ValueError, nostr_event.InvalidEvent) as exc:
            raise RemoteSignerError(f"The signer returned an invalid event ({exc}).") from exc
        if self.user_pubkey and signed.pubkey != self.user_pubkey:
            raise RemoteSignerError("The signer signed with a different key.")
        if (signed.kind, signed.content, signed.created_at) != (kind, content, created_at) or [list(t) for t in signed.tags] != tags:
            raise RemoteSignerError("The signer changed the event it was asked to sign.")
        return signed

    async def nip44_encrypt(self, pubkey: str, plaintext: str) -> str:
        return await self.request("nip44_encrypt", [pubkey, plaintext])

    async def nip44_decrypt(self, pubkey: str, ciphertext: str) -> str:
        return await self.request("nip44_decrypt", [pubkey, ciphertext])
