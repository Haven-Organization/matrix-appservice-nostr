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


def client_key_purpose(id_number: int) -> str:
    return f"client:{id_number}"


class SigningUnavailable(Exception):
    """A remote signer couldn't sign or decrypt; the message is safe to show."""


class Signer:
    def __init__(self, repository: BridgeRepository, vault: KeyVault) -> None:
        self._repository = repository
        self._vault = vault
        self.bot: BotIdentity | None = None
        # The relay pool, for remote signers (set once the pool exists).
        self.pool = None
        self._remotes: dict[tuple[int, str], object] = {}
        self.last_error: dict[str, str] = {}

    async def remote_signer(self, nostr_id):
        """The NIP-46 client for an ID whose key is in a signer app."""
        from bridge.nostr.nip46 import RemoteSigner, parse_bunker_uri

        if nostr_id is None or not nostr_id.bunker_uri or not nostr_id.client_key or self.pool is None:
            return None
        key = (nostr_id.id, nostr_id.pubkey)
        signer = self._remotes.get(key)
        if signer is None:
            client_secret = self._vault.decrypt(nostr_id.client_key, client_key_purpose(nostr_id.id))
            signer = RemoteSigner(
                self.pool, client_secret=client_secret, bunker=parse_bunker_uri(nostr_id.bunker_uri), user_pubkey=nostr_id.pubkey
            )
            self._remotes[key] = signer
        return signer

    async def _remote_for_pubkey(self, pubkey: str):
        return await self.remote_signer(await self._repository.get_id_by_pubkey(pubkey))

    async def nip44_encrypt(self, pubkey: str, other: str, plaintext: str) -> str:
        from bridge.nostr import nip44
        from bridge.nostr.nip46 import RemoteSignerError

        secret = await self._secret_for(pubkey)
        if secret is not None:
            return nip44.encrypt(plaintext, nip44.get_conversation_key(secret, other))
        remote = await self._remote_for_pubkey(pubkey)
        if remote is None:
            raise SigningUnavailable("The bridge doesn't hold this ID's key.")
        try:
            return await remote.nip44_encrypt(other, plaintext)
        except RemoteSignerError as exc:
            raise SigningUnavailable(str(exc)) from exc

    async def nip44_decrypt(self, pubkey: str, other: str, ciphertext: str) -> str:
        from bridge.nostr import nip44
        from bridge.nostr.nip46 import RemoteSignerError

        secret = await self._secret_for(pubkey)
        if secret is not None:
            return nip44.decrypt(ciphertext, nip44.get_conversation_key(secret, other))
        remote = await self._remote_for_pubkey(pubkey)
        if remote is None:
            raise SigningUnavailable("The bridge doesn't hold this ID's key.")
        try:
            return await remote.nip44_decrypt(other, ciphertext)
        except RemoteSignerError as exc:
            raise SigningUnavailable(str(exc)) from exc

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

    async def secret_key(self, pubkey: str) -> bytes | None:
        """The decrypted secret key of a local ID (for opening DMs), or
        ``None`` if the bridge doesn't hold it."""
        return await self._secret_for(pubkey)

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
        """A new event by ``pubkey``, or ``None`` if the bridge can't sign
        for it (no key; or a remote signer that refused or didn't answer, in
        which case ``last_error[pubkey]`` says why)."""
        from bridge.nostr.nip46 import RemoteSignerError

        secret = await self._secret_for(pubkey)
        tag_list = [list(tag) for tag in tags]
        if secret is not None:
            return nostr_event.build(secret, kind=kind, content=content, created_at=created_at, tags=tag_list)
        remote = await self._remote_for_pubkey(pubkey)
        if remote is None:
            return None
        try:
            return await remote.sign_event(kind=kind, content=content, created_at=created_at, tags=tag_list)
        except RemoteSignerError as exc:
            self.last_error[pubkey] = str(exc)
            return None

    async def auth_event(self, pubkey: str, relay_url: str, challenge: str) -> nostr_event.Event | None:
        """A NIP-42 authentication event, for ``bridge.nostr.relay_pool``."""
        return await self.sign(
            pubkey,
            kind=AUTH_KIND,
            content="",
            created_at=int(time.time()),
            tags=[["relay", relay_url], ["challenge", challenge]],
        )
