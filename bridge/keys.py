"""Bringing your own key, and taking it with you (DESIGN.md, "Bringing your
own key").

Three ways an ID can get a key other than a freshly generated one, in
order of preference:

1. A remote signer (NIP-46, ``bunker://``): the key stays in the user's
   signer app; the bridge only ever holds its own client key for talking
   to it.
2. An imported ``nsec`` or password-protected ``ncryptsec`` (NIP-49), given
   through the widget over HTTPS, never in a room message (a homeserver
   stores those unencrypted).
3. Never a chat message: ``bridge.secret_guard`` redacts an ``nsec`` pasted
   into a room the bot can see.

A key can make a new ID, or replace the key of an existing one. Replacing
loses the old key's followers (they follow the key, not the handle), which
the widget says plainly before doing it.

Exporting gives back an ``nsec``, or an ``ncryptsec`` with a password, in
the widget only.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from bridge import ids
from bridge.event_sources import EMBED_FIELD
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19, nip49, secp256k1
from bridge.nostr.nip46 import RemoteSigner, RemoteSignerError, parse_bunker_uri
from bridge.repository import NostrId
from bridge.signing import client_key_purpose, id_key_purpose
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

KEY_ORIGIN_IMPORTED = "imported"
KEY_ORIGIN_BUNKER = "bunker"
PROFILE_EXTRA_STATE_TYPE = "software.haven.nostr.profile_extra"
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class KeyProblem(Exception):
    """Safe to show the user."""


@dataclass(frozen=True)
class KeyMaterial:
    pubkey: str
    secret: bytes | None = None
    bunker_uri: str | None = None
    client_secret: bytes | None = None


def decode_secret(text: str, password: str | None) -> bytes:
    """An ``nsec``, ``ncryptsec`` (with ``password``) or hex secret key."""
    text = text.strip()
    try:
        if text.startswith("nsec1"):
            secret = nip19.nsec_decode(text)
        elif text.startswith("ncryptsec1"):
            if not password:
                raise KeyProblem("That key is password-protected: give its password too.")
            secret = nip49.decrypt(text, password)
        elif _HEX_RE.match(text.lower()):
            secret = bytes.fromhex(text.lower())
        else:
            raise KeyProblem("Paste an nsec1…, an ncryptsec1… (with its password), or a 64-character hex key.")
        secp256k1.public_key(secret)
    except (nip19.Nip19Error, nip49.Nip49Error, secp256k1.InvalidKey, ValueError) as exc:
        if isinstance(exc, KeyProblem):
            raise
        raise KeyProblem(f"That key couldn't be read: {exc}") from exc
    return secret


def from_secret(secret: bytes) -> KeyMaterial:
    return KeyMaterial(pubkey=secp256k1.public_key(secret).hex(), secret=secret)


async def from_bunker(app: FastAPI, uri: str) -> KeyMaterial:
    """Connect to a remote signer and learn the key it signs for. Raises
    ``KeyProblem`` (including when the signer wants the user to approve the
    bridge first: the message carries the link)."""
    try:
        bunker = parse_bunker_uri(uri)
        client_secret = secp256k1.generate_secret_key()
        signer = RemoteSigner(app.state.relay_pool, client_secret=client_secret, bunker=bunker)
        pubkey = await signer.connect()
    except RemoteSignerError as exc:
        raise KeyProblem(str(exc)) from exc
    return KeyMaterial(pubkey=pubkey, bunker_uri=uri.strip(), client_secret=client_secret)


async def _stored_fields(app: FastAPI, material: KeyMaterial, id_number: int) -> dict[str, Any]:
    vault = app.state.vault
    if material.secret is not None:
        return {
            "secret_key": vault.encrypt(material.secret, id_key_purpose(material.pubkey)),
            "key_origin": KEY_ORIGIN_IMPORTED, "bunker_uri": None, "client_key": None,
        }
    assert material.client_secret is not None
    return {
        "secret_key": None, "key_origin": KEY_ORIGIN_BUNKER, "bunker_uri": material.bunker_uri,
        "client_key": vault.encrypt(material.client_secret, client_key_purpose(id_number)),
    }


async def existing_profile(app: FastAPI, pubkey: str) -> dict[str, Any] | None:
    """The key's latest kind 0 out on the network, verified, or ``None``."""
    config = app.state.config
    found = await app.state.relay_pool.query(
        [*config.nostr.discovery_relays, *config.nostr.fallback_relays], [{"authors": [pubkey], "kinds": [0]}]
    )
    newest = None
    for raw in found:
        try:
            nostr_event.verify(raw)
        except nostr_event.InvalidEvent:
            continue
        if raw["pubkey"] == pubkey and (newest is None or raw["created_at"] > newest["created_at"]):
            newest = raw
    return newest


async def seed_profile_room(app: FastAPI, nostr_id: NostrId, profile_event: dict[str, Any] | None) -> None:
    """Start an imported ID's Profile Room from the profile the key already
    has: name, about and picture as the room's own, and the fields the room
    has no place for (website, lightning address) in a state event the
    profile is built from (profile v2)."""
    if profile_event is None or not nostr_id.profile_room_id:
        return
    from bridge.remote import _fetch_image, parse_profile

    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    room = nostr_id.profile_room_id
    fields = parse_profile(profile_event)
    if fields.get("name"):
        await homeserver.send_state_event(room, "m.room.name", {"name": fields["name"]}, as_user_id=bot)
    if fields.get("about"):
        await homeserver.send_state_event(room, "m.room.topic", {"topic": fields["about"]}, as_user_id=bot)
    if fields.get("picture"):
        image = await _fetch_image(app, fields["picture"])
        if image is not None:
            mxc = await homeserver.upload_media(image[0], image[1], "avatar", as_user_id=bot)
            await homeserver.send_state_event(room, "m.room.avatar", {"url": mxc}, as_user_id=bot)
    extra = {k: fields[k] for k in ("website", "lud16") if fields.get(k)}
    if extra:
        await homeserver.send_state_event(room, PROFILE_EXTRA_STATE_TYPE, extra, as_user_id=bot)


async def create_with_key(app: FastAPI, sender: str, name: str, material: KeyMaterial) -> NostrId:
    """A new ID with an existing key. Raises ``ids.IdRejected`` or
    ``KeyProblem``."""
    rejection = await ids.check_new_name(app, name, sender)
    if rejection is not None:
        raise ids.IdRejected(rejection)
    repository = app.state.repository
    if await repository.get_id_by_pubkey(material.pubkey) is not None:
        raise KeyProblem("That key already belongs to an ID on this server.")
    from datetime import datetime, timezone

    stored = await _stored_fields(app, material, 0)
    created = await repository.insert_id(
        name=name, matrix_user_id=sender, pubkey=material.pubkey, secret_key=stored["secret_key"],
        key_origin=stored["key_origin"], created_at=datetime.now(timezone.utc).isoformat(), bunker_uri=stored["bunker_uri"],
    )
    if created is None:
        raise KeyProblem("That name or key was taken just now.")
    if material.client_secret is not None:
        # The client key is bound to the ID's number, known only now.
        stored = await _stored_fields(app, material, created.id)
        await repository.replace_key(created.id, pubkey=material.pubkey, secret_key=None, key_origin=KEY_ORIGIN_BUNKER,
                                     bunker_uri=material.bunker_uri, client_key=stored["client_key"])
        created = await repository.get_id(created.id)
        assert created is not None
    if await repository.get_active_id(sender) is None:
        await repository.set_active_id(sender, created.id)
    logger.info("Created Nostr ID %s with an existing key (%s)", name, stored["key_origin"])
    return created


async def replace_key(app: FastAPI, nostr_id: NostrId, material: KeyMaterial) -> NostrId:
    """Give an existing ID a different key, and republish everything that
    makes up its identity under it. Raises ``KeyProblem``."""
    repository = app.state.repository
    if material.pubkey == nostr_id.pubkey:
        raise KeyProblem("That's already this ID's key.")
    stored = await _stored_fields(app, material, nostr_id.id)
    if not await repository.replace_key(nostr_id.id, pubkey=material.pubkey, **stored):
        raise KeyProblem("That key already belongs to another ID on this server.")
    updated = await repository.get_id(nostr_id.id)
    assert updated is not None
    from bridge.follows import FOLLOW_LIST_KIND, MUTE_LIST_KIND, publish_list
    from bridge.identity_events import sync_identity

    await sync_identity(app, updated)
    await publish_list(app, updated, FOLLOW_LIST_KIND)
    if await repository.list_blocks(updated.id):
        await publish_list(app, updated, MUTE_LIST_KIND)
    app.state.feeds.changed()
    logger.info("Replaced the key of %s (now %s, %s)", nostr_id.name, material.pubkey, stored["key_origin"])
    return updated


async def export_key(app: FastAPI, nostr_id: NostrId, password: str | None) -> str:
    secret = await app.state.signer.secret_key(nostr_id.pubkey)
    if secret is None:
        raise KeyProblem("This ID's key is in a remote signer, not the bridge: export it from there.")
    if password:
        import asyncio

        return await asyncio.to_thread(nip49.encrypt, secret, password)
    return nip19.nsec_encode(secret)


async def import_history(app: FastAPI, nostr_id: NostrId, *, follows: bool, posts: int) -> str:
    """Bring an imported key's existing follows and recent posts here.
    Returns a summary for the user."""
    from bridge.follows import Reference, follow
    from bridge.inbound import ingest

    config = app.state.config
    relays = [*config.nostr.discovery_relays, *config.nostr.fallback_relays]
    summary = []
    if follows:
        lists = await app.state.relay_pool.query(relays, [{"authors": [nostr_id.pubkey], "kinds": [3]}])
        newest = max((e for e in lists if e.get("pubkey") == nostr_id.pubkey), key=lambda e: e.get("created_at", 0), default=None)
        count = 0
        if newest is not None:
            nostr_event.verify(newest)
            for tag in newest.get("tags") or []:
                if len(tag) >= 2 and tag[0] == "p" and _HEX_RE.match(tag[1]) and count < 500:
                    hints = (tag[2],) if len(tag) >= 3 and tag[2].startswith("wss://") else ()
                    try:
                        await follow(app, nostr_id, Reference(tag[1], hints), backfill=False)
                        count += 1
                    except Exception:
                        logger.info("Could not follow %s while importing", tag[1], exc_info=True)
        summary.append(f"followed {count} accounts")
    if posts:
        found = await app.state.relay_pool.query(relays, [{"authors": [nostr_id.pubkey], "kinds": [1], "limit": posts}])
        mirrored = 0
        for raw in sorted((e for e in found if e.get("pubkey") == nostr_id.pubkey), key=lambda e: e.get("created_at", 0))[-posts:]:
            result = await ingest(app, raw, origin="backfill")
            mirrored += int(result.stored)
        summary.append(f"brought in {mirrored} posts")
    return ", ".join(summary)


def embedded(raw: dict[str, Any]) -> dict[str, Any]:
    return {EMBED_FIELD: raw}
