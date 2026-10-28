"""Direct messages: NIP-17 private DMs (NIP-44 encrypted, NIP-59 gift
wrapped), and legacy NIP-04 DMs received.

One DM room per (local ID, remote account), created by the bot with the
user and the account's ghost. A DM from a stranger arrives as an invite to
a new room: the user can just decline.

Sending: the owner's message becomes a kind 14 rumor (kind 15 for a file,
encrypted with AES-GCM; the encrypted bytes go into the Matrix media
repository and are served like any other published file), sealed and gift
wrapped twice: to the recipient, sent to their DM relays (kind 10050), and
to the sender, kept for their own Nostr apps.

Storage follows the usual rule: a gift wrap is kept inside the Matrix event
that shows it. A received wrap is in the ghost's message; the sender's own
copy of a sent message is in a hidden ``software.haven.nostr.dm_copy``
event next to it. Both are indexed as ``dm``, readable on the relay only by
their recipient (after NIP-42 auth).
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request

from bridge import remote as remotes
from bridge.event_sources import EMBED_FIELD, embedded_source
from bridge.homeserver_client import HomeserverError
from bridge.inbound import IngestResult, REJECTED, index_tags
from bridge.media import MediaProblem, blob_url
from bridge.messages import invite_to_room, notice
from bridge.nostr import nip04, nip59
from bridge.notes import MEDIA_MSGTYPES, message_body
from bridge.publishing import publish_in_background
from bridge.repository import Blob, EventRow, NostrId, RemoteAccount
from bridge.users import bot_mxid, is_bridge_user

logger = logging.getLogger(__name__)

DM_COPY_EVENT_TYPE = "software.haven.nostr.dm_copy"
FILE_MESSAGE_KIND = 15


# -- rooms ----------------------------------------------------------------------------


async def ensure_dm_room(app: FastAPI, nostr_id: NostrId, remote: RemoteAccount) -> str:
    """The DM room between a local ID and a remote account, created (and
    its owner invited) if there is none. Raises ``HomeserverError``."""
    repository = app.state.repository
    existing = await repository.get_dm_room(nostr_id.id, remote.pubkey)
    if existing is not None:
        return existing
    config = app.state.config
    homeserver = app.state.homeserver
    try:
        label = (await homeserver.get_profile(remote.ghost_user_id)).get("displayname")
    except HomeserverError:
        label = None
    label = label or remote.nip05 or remotes.short_npub(remote.pubkey)
    if len(await repository.list_ids_for_user(nostr_id.matrix_user_id)) > 1:
        label = f"{label} ↔ {nostr_id.name}"
    room_id = await homeserver.create_room(
        as_user_id=bot_mxid(config),
        name=label,
        topic=f"Nostr DMs with {remote.nip05 or remotes.short_npub(remote.pubkey)}, as {nostr_id.name}@{config.bridge.domain}",
        invite=[nostr_id.matrix_user_id, remote.ghost_user_id],
        is_direct=config.bridge.dm_rooms_as_direct,
        power_level_users={nostr_id.matrix_user_id: 99},
    )
    await homeserver.join_room(room_id, as_user_id=remote.ghost_user_id)
    await repository.set_dm_room(room_id, nostr_id.id, remote.pubkey)
    from bridge.room_widget import add_widget_once

    await add_widget_once(app, room_id)
    logger.info("Created DM room %s for %s and %s", room_id, nostr_id.name, remote.pubkey)
    return room_id


# -- receiving ------------------------------------------------------------------------


async def receive_dm(app: FastAPI, raw: dict[str, Any], *, origin: str, ip: str | None) -> IngestResult:
    """A gift wrap (kind 1059) or legacy DM (kind 4) addressed to local IDs."""
    repository = app.state.repository
    recipients = []
    for tag in raw.get("tags") or []:
        if len(tag) >= 2 and tag[0] == "p":
            local = await repository.get_id_by_pubkey(tag[1])
            if local is not None:
                recipients.append(local)
    if not recipients:
        return REJECTED
    stored = False
    for local in recipients:
        secret = await app.state.signer.secret_key(local.pubkey)
        try:
            if raw["kind"] == nip59.GIFT_WRAP_KIND:
                if secret is not None:
                    unwrapped = nip59.unwrap(raw, secret)
                    sender, rumor = unwrapped.sender, unwrapped.rumor
                else:
                    sender, rumor = await _unwrap_via_signer(app, local, raw)
            elif secret is not None:
                sender = raw["pubkey"]
                rumor = {**raw, "content": nip04.decrypt(raw["content"], secret, sender), "kind": 14}
            else:
                continue  # legacy DMs to a remote-signer ID: NIP-04 isn't asked of signers
        except Exception as exc:
            logger.info("Could not open a DM for %s: %s", local.name, exc)
            continue
        guard = getattr(app.state, "spam", None)
        if guard is not None and origin in ("relay", "subscription") and sender != local.pubkey:
            from bridge.inbound import _tier

            verdict = await guard.check_unwrapped_sender(sender, await _tier(app, sender))
            if not verdict.ok:
                return IngestResult(False, verdict.reply)
        if await repository.is_blocked_by_any(sender, [local.id]):
            continue
        if await _deliver(app, raw, rumor, sender, local):
            stored = True
    return IngestResult(True, "", stored=True) if stored else IngestResult(False, "restricted: could not deliver")


async def _deliver(app: FastAPI, raw: dict[str, Any], rumor: dict[str, Any], sender: str, local: NostrId) -> bool:
    repository = app.state.repository
    if sender == local.pubkey:
        return False  # our own sent copy coming back
    if rumor.get("kind") not in (14, FILE_MESSAGE_KIND):
        return False
    if await repository.get_id_by_pubkey(sender) is not None:
        return False  # between two IDs on this server: that's a Matrix DM
    remote = await remotes.get_or_create_remote(app, sender)
    room_id = await ensure_dm_room(app, local, remote)
    content = await _rumor_content(app, rumor, as_user_id=remote.ghost_user_id)
    if content is None:
        return False
    reply_to = next((t[1] for t in rumor.get("tags") or [] if len(t) >= 2 and t[0] == "e"), None)
    if reply_to:
        matrix_reply = await repository.get_mirrored_matrix_event(reply_to, room_id)
        if matrix_reply:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": matrix_reply}}
    content[EMBED_FIELD] = raw
    sender_mxid = remote.ghost_user_id
    try:
        event_id = await app.state.homeserver.send_message_event(
            room_id, content, as_user_id=sender_mxid, ts=int(rumor.get("created_at") or time.time()) * 1000
        )
    except HomeserverError:
        logger.warning("Could not deliver a DM to %s", room_id, exc_info=True)
        return False
    await repository.put_event(
        EventRow(
            id=raw["id"], pubkey=raw["pubkey"], kind=raw["kind"], created_at=int(raw["created_at"]), sig=raw["sig"],
            source=json.dumps(embedded_source()), room_id=room_id, matrix_event_id=event_id, scope="dm",
            tags=index_tags(raw.get("tags")),
        )
    )
    if isinstance(rumor.get("id"), str):
        await repository.put_mirrored_event(event_id, rumor["id"], room_id)
    hub = getattr(app.state, "relay_hub", None)
    if hub is not None:
        hub.broadcast(raw, "dm")
    return True


async def _rumor_content(app: FastAPI, rumor: dict[str, Any], *, as_user_id: str) -> dict[str, Any] | None:
    text = rumor.get("content") or ""
    if rumor.get("kind") != FILE_MESSAGE_KIND:
        return {"msgtype": "m.text", "body": text or " "}
    tags = {t[0]: t[1] for t in rumor.get("tags") or [] if len(t) >= 2}
    try:
        fetched = await app.state.fetcher.get(text, max_bytes=app.state.config.nostr.max_media_mb * 1024 * 1024)
        data = fetched.content
        if tags.get("x") and hashlib.sha256(data).hexdigest() != tags["x"].lower():
            raise ValueError("hash mismatch")
        if tags.get("encryption-algorithm") == "aes-gcm":
            data = AESGCM(bytes.fromhex(tags["decryption-key"])).decrypt(bytes.fromhex(tags["decryption-nonce"]), data, None)
    except Exception as exc:
        logger.info("Could not fetch a DM attachment: %s", exc)
        return {"msgtype": "m.notice", "body": "(an attachment that couldn't be fetched)"}
    mime = tags.get("file-type") or "application/octet-stream"
    try:
        mxc = await app.state.homeserver.upload_media(data, mime, "attachment", as_user_id=as_user_id)
    except HomeserverError:
        return None
    msgtype = {"image": "m.image", "video": "m.video", "audio": "m.audio"}.get(mime.split("/")[0], "m.file")
    return {"msgtype": msgtype, "body": "attachment", "url": mxc, "info": {"mimetype": mime, "size": len(data)}}


async def _unwrap_via_signer(app: FastAPI, local: NostrId, raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Open a gift wrap for an ID whose key is in a remote signer (NIP-46):
    the same checks as ``nip59.unwrap``, with the signer decrypting."""
    from bridge.nostr import event as nostr_event
    from bridge.nostr.event import compute_id

    wrap = nostr_event.verify(raw)
    seal = nostr_event.verify(json.loads(await app.state.signer.nip44_decrypt(local.pubkey, wrap.pubkey, wrap.content)))
    if seal.kind != nip59.SEAL_KIND:
        raise ValueError("not a seal")
    rumor = json.loads(await app.state.signer.nip44_decrypt(local.pubkey, seal.pubkey, seal.content))
    if not isinstance(rumor, dict) or rumor.get("pubkey") != seal.pubkey:
        raise ValueError("the rumor isn't from the seal's signer")
    expected = compute_id(rumor["pubkey"], rumor["created_at"], rumor["kind"], rumor.get("tags") or [], rumor.get("content") or "")
    if rumor.get("id") not in (None, expected):
        raise ValueError("rumor id mismatch")
    rumor["id"] = expected
    return seal.pubkey, rumor


async def _wrap(app: FastAPI, local: NostrId, rumor: dict[str, Any], recipient: str):
    """Seal and gift-wrap a rumor from a local ID to ``recipient``; through
    the remote signer when the bridge doesn't hold the key."""
    secret = await app.state.signer.secret_key(local.pubkey)
    if secret is not None:
        return nip59.wrap_message(rumor, secret, recipient)
    from bridge.signing import SigningUnavailable

    sealed_content = await app.state.signer.nip44_encrypt(local.pubkey, recipient, json.dumps(rumor))
    seal = await app.state.signer.sign(
        local.pubkey, kind=nip59.SEAL_KIND, content=sealed_content,
        created_at=int(time.time()) - secrets.randbelow(2 * 86400), tags=[],
    )
    if seal is None:
        raise SigningUnavailable(app.state.signer.last_error.get(local.pubkey, "The remote signer didn't sign."))
    return nip59.gift_wrap(seal, recipient)


# -- sending --------------------------------------------------------------------------


async def maybe_send_dm(request: Request, event: dict[str, Any]) -> bool:
    """Returns True if this was a message in a DM room."""
    if event.get("type") != "m.room.message":
        return False
    app = request.app
    repository = app.state.repository
    room_id = event.get("room_id", "")
    info = await repository.get_dm_room_info(room_id)
    if info is None:
        return False
    sender = event.get("sender", "")
    if is_bridge_user(app.state.config, sender):
        return True
    local = await repository.get_id(info[0])
    content = event.get("content") or {}
    if local is None or sender != local.matrix_user_id:
        return True
    from bridge.shadow_ids import still_allowed

    if not await still_allowed(app, local, room_id):
        return True  # a third party whose access was taken away: stays in Matrix
    if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
        return True  # edits can't be sent
    from bridge.commands import message_for_a_bot

    if message_for_a_bot(content, app.state.config):
        return False
    try:
        await send_dm(app, local, info[1], event)
    except MediaProblem as exc:
        await notice(app, room_id, f"That wasn't sent: {exc}.", extra={"m.relates_to": {"m.in_reply_to": {"event_id": event.get("event_id", "")}}})
    return True


async def _encrypted_file(app: FastAPI, content: dict[str, Any]) -> list[list[str]] | tuple[str, list[list[str]]]:
    if "file" in content:
        raise MediaProblem("it's end-to-end encrypted, and the bridge can't read encrypted files")
    download = await app.state.homeserver.download_media(content.get("url") or "")
    if download is None:
        raise MediaProblem("the file couldn't be found on the homeserver")
    limit = app.state.config.nostr.max_media_mb * 1024 * 1024
    if len(download.content) > limit:
        raise MediaProblem("it's too large")
    key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    sealed = AESGCM(key).encrypt(nonce, download.content, None)
    mxc = await app.state.homeserver.upload_media(sealed, "application/octet-stream", "file.bin", as_user_id=bot_mxid(app.state.config))
    sha = hashlib.sha256(sealed).hexdigest()
    from datetime import datetime, timezone

    await app.state.repository.put_blob(Blob(sha, mxc, "application/octet-stream", len(sealed), datetime.now(timezone.utc).isoformat()))
    mime = ((content.get("info") or {}).get("mimetype") or download.content_type or "application/octet-stream").split(";")[0]
    url = blob_url(app.state.config.nostr.relay_http_base, sha, "application/octet-stream")
    tags = [
        ["file-type", mime], ["encryption-algorithm", "aes-gcm"], ["decryption-key", key.hex()],
        ["decryption-nonce", nonce.hex()], ["x", sha], ["ox", hashlib.sha256(download.content).hexdigest()],
        ["size", str(len(sealed))],
    ]
    return url, tags


async def send_dm(app: FastAPI, local: NostrId, recipient: str, event: dict[str, Any]) -> None:
    repository = app.state.repository
    content = event.get("content") or {}
    room_id = event.get("room_id", "")
    if await repository.get_id_by_pubkey(recipient) is not None:
        await notice(app, room_id, "That's an ID on this server: message its owner directly in Matrix instead.")
        return
    tags: list[list[str]] = [["p", recipient]]
    kind = 14
    if content.get("msgtype") in MEDIA_MSGTYPES:
        url, file_tags = await _encrypted_file(app, content)
        text, kind = url, FILE_MESSAGE_KIND
        tags += file_tags
    else:
        text = message_body(content)
        if not text.strip():
            return
    reply_to = ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
    if reply_to:
        rumor_id = await repository.get_mirrored_nostr_id(reply_to)
        if rumor_id:
            tags.append(["e", rumor_id, app.state.config.nostr.relay_url, "reply"])
    from bridge.signing import SigningUnavailable

    created_at = int(event.get("origin_server_ts", 0)) // 1000 or int(time.time())
    rumor = nip59.make_rumor(local.pubkey, kind, text, tags, created_at)
    try:
        to_recipient = await _wrap(app, local, rumor, recipient)
        to_self = await _wrap(app, local, rumor, local.pubkey)
    except SigningUnavailable as exc:
        await notice(app, room_id, f"That wasn't sent: {exc}")
        return

    # The sender's copy: kept in Matrix (hidden event), served by the relay.
    copy_id = await app.state.homeserver.send_message_event(
        room_id,
        {EMBED_FIELD: to_self.to_dict(), "m.relates_to": {"rel_type": "m.reference", "event_id": event.get("event_id", "")}},
        as_user_id=bot_mxid(app.state.config),
        event_type=DM_COPY_EVENT_TYPE,
    )
    await repository.put_event(
        EventRow(
            id=to_self.id, pubkey=to_self.pubkey, kind=to_self.kind, created_at=to_self.created_at, sig=to_self.sig,
            source=json.dumps(embedded_source()), room_id=room_id, matrix_event_id=copy_id, scope="dm",
            tags=index_tags([list(t) for t in to_self.tags]),
        )
    )
    await repository.put_mirrored_event(event.get("event_id", ""), rumor["id"], room_id)
    publish_in_background(app, to_self, relays=[app.state.config.nostr.relay_url], scope="dm")

    # The recipient's copy: to their DM relays (or, failing that, inbox).
    remote = await repository.get_remote_account(recipient)
    if remote is not None and not remote.dm_relays:
        remote = await remotes.refresh(app, remote, force=True)
    relays = list(remote.dm_relays) if remote and remote.dm_relays else remotes.inbox_relays_for(app, remote)
    if not relays:
        relays = list(app.state.config.nostr.publish_relays)
    publish_in_background(app, to_recipient, relays=relays, scope="dm")


async def open_dm(app: FastAPI, local: NostrId, pubkey: str) -> str:
    """``[dm``: the DM room with an account, the user (re-)invited."""
    remote = await remotes.get_or_create_remote(app, pubkey)
    room_id = await ensure_dm_room(app, local, remote)
    await invite_to_room(app, room_id, local.matrix_user_id)
    return room_id


def dm_link_html(room_id: str, label: str) -> str:
    return f'<a href="https://matrix.to/#/{html.escape(room_id)}">{html.escape(label)}</a>'
