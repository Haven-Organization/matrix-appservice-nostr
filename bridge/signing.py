"""Which Nostr keys the bridge holds, and signing with them.

The one place secret keys are decrypted (``bridge.key_vault``) and used.
Two kinds of key:

- the bot's own identity, ``_@<bridge.domain>``, created on first start
  and kept in ``server_keys``,
- each ID's key, in ``ids`` (from step 2 of the build order on). An ID
  whose key is held by a remote signer (NIP-46) has none here.

Decrypted keys are not cached: decrypting is cheap next to signing, and
a key that isn't in memory can't leak from it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from bridge.key_vault import KeyVault
from bridge.nostr import event as nostr_event
from bridge.nostr import secp256k1
from bridge.nostr.relay_pool import AUTH_KIND
from bridge.repository import BridgeRepository

BOT_KEY_NAME = "bot"


def server_key_purpose(name: str) -> str:
    return f"server:{name}"


def id_key_purpose(pubkey: str) -> str:
    return f"id:{pubkey}"


@dataclass(frozen=True)
class BotIdentity:
    pubkey: str


class Signer:
    def __init__(self, repository: BridgeRepository, vault: KeyVault) -> None:
        self._repository = repository
        self._vault = vault
        self.bot: BotIdentity | None = None

    async def load_bot_identity(self) -> BotIdentity:
        """The bot's identity, creating its key on first start."""
        candidate = self._vault.encrypt(secp256k1.generate_secret_key(), server_key_purpose(BOT_KEY_NAME))
        stored = await self._repository.get_or_create_server_key(BOT_KEY_NAME, candidate)
        secret = self._vault.decrypt(stored, server_key_purpose(BOT_KEY_NAME))
        self.bot = BotIdentity(pubkey=secp256k1.public_key(secret).hex())
        return self.bot

    async def _secret_for(self, pubkey: str) -> bytes | None:
        if self.bot is not None and pubkey == self.bot.pubkey:
            stored = await self._repository.get_server_key(BOT_KEY_NAME)
            return None if stored is None else self._vault.decrypt(stored, server_key_purpose(BOT_KEY_NAME))
        nostr_id = await self._repository.get_id_by_pubkey(pubkey)
        if nostr_id is None or nostr_id.secret_key is None:
            return None
        return self._vault.decrypt(nostr_id.secret_key, id_key_purpose(pubkey))

    async def can_sign(self, pubkey: str) -> bool:
        return await self._secret_for(pubkey) is not None

    async def sign(
        self,
        pubkey: str,
        *,
        kind: int,
        content: str,
        created_at: int,
        tags: Iterable[Iterable[str]] = (),
    ) -> nostr_event.Event | None:
        """A new event by ``pubkey``, or ``None`` if the bridge doesn't hold
        that key."""
        secret = await self._secret_for(pubkey)
        if secret is None:
            return None
        return nostr_event.build(secret, kind=kind, content=content, created_at=created_at, tags=tags)

    async def auth_event(self, pubkey: str, relay_url: str, challenge: str) -> nostr_event.Event | None:
        """A NIP-42 authentication event, for ``bridge.nostr.relay_pool``."""
        return await self.sign(
            pubkey,
            kind=AUTH_KIND,
            content="",
            created_at=int(time.time()),
            tags=[["relay", relay_url], ["challenge", challenge]],
        )
