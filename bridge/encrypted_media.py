"""Encrypted media: a file forwarded out of an end-to-end encrypted room.

Such a message is ordinary plaintext, but its file is per-file encrypted
(``content.file``, Matrix's EncryptedFile) and the key is in the message.
Publishing it means decrypting it and keeping a plaintext copy in the media
repository, so it waits for the sender to agree.

This server may run more than one bridge (the ActivityPub and ATProto
siblings), and they share the work rather than each asking and each making
a copy (``encrypted_media.bridges``, in the order they ask):

1. A bridge that would publish such a file and has no copy of it waits its
   turn (``wait_seconds`` times its place in the list), then looks at the
   post's thread: if a bridge before it already asked, or announced a copy,
   it leaves it there. Otherwise it asks: a message in the post's thread
   (``PROMPT_FIELD``) the sender confirms by replying "confirm" or with a
   👍.
2. The bridge that asked decrypts the file, uploads it once, and announces
   the copy in the room (an ``ANNOUNCE_TYPE`` event related to the post),
   then publishes the post itself.
3. Every other bridge, seeing the announcement, checks the copy really is
   the sender's file (re-encrypting it with the post's own key gives the
   ciphertext's hash, so no bridge has to trust another), and publishes the
   post with it.

The copies are remembered as ``media_copies`` rows (``encrypted:<mxc>``):
bookkeeping only, like every other copy.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI

from bridge import confirm
from bridge.homeserver_client import HomeserverError
from bridge.messages import notice
from bridge.tasks import run_in_background
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

# On a bridge's question: {"prompt_for": "<the post's event id>"}.
PROMPT_FIELD = "software.haven.encrypted_media"
# A decrypted copy, announced by the bridge that made it.
ANNOUNCE_TYPE = "software.haven.decrypted_media"
ACTION = "decrypt_media"
_MEDIA_MSGTYPES = ("m.image", "m.video", "m.audio", "m.file")
# The ActivityPub bridge's marker for its question, kept so it knows ours.
_MARKER = "this message is encrypted"


def encrypted_file(content: dict[str, Any]) -> dict[str, Any] | None:
    """The EncryptedFile of a media message that has no plain ``url``."""
    if content.get("msgtype") not in _MEDIA_MSGTYPES or isinstance(content.get("url"), str):
        return None
    file = content.get("file")
    if not isinstance(file, dict) or not isinstance(file.get("url"), str) or not file["url"].startswith("mxc://"):
        return None
    if not isinstance((file.get("key") or {}).get("k"), str) or not isinstance(file.get("iv"), str):
        return None
    if not isinstance((file.get("hashes") or {}).get("sha256"), str):
        return None
    return file


def _source(encrypted_mxc: str) -> str:
    return f"encrypted:{encrypted_mxc}"


def _b64(value: str, *, urlsafe: bool) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded) if urlsafe else base64.b64decode(padded)


def _crypt(file: dict[str, Any], data: bytes) -> bytes:
    """AES-256-CTR with the file's key and IV: the same both ways."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(_b64(file["key"]["k"], urlsafe=True)), modes.CTR(_b64(file["iv"], urlsafe=False)))
    worker = cipher.encryptor()
    return worker.update(data) + worker.finalize()


def _matches(file: dict[str, Any], ciphertext: bytes) -> bool:
    return hashlib.sha256(ciphertext).digest() == _b64(file["hashes"]["sha256"], urlsafe=False)


def _with_copy(content: dict[str, Any], mxc: str) -> dict[str, Any]:
    info = {k: v for k, v in (content.get("info") or {}).items() if not k.startswith("thumbnail_")}
    resolved = {k: v for k, v in content.items() if k != "file"}
    resolved["url"] = mxc
    resolved["info"] = info
    return resolved


async def known_copy(app: FastAPI, file: dict[str, Any]) -> str | None:
    found = await app.state.repository.get_media_copy(_source(file["url"]))
    return found.mxc if found else None


async def resolve(app: FastAPI, event: dict[str, Any]) -> dict[str, Any]:
    """``event`` with its encrypted file swapped for the decrypted copy, when
    one exists (the post's own, or an edit's new content)."""
    content = event.get("content") or {}
    changed = content
    file = encrypted_file(content)
    mxc = await known_copy(app, file) if file else None
    if mxc is not None:
        changed = _with_copy(content, mxc)
    new = content.get("m.new_content")
    file = encrypted_file(new) if isinstance(new, dict) else None
    mxc = await known_copy(app, file) if file else None
    if mxc is not None:
        changed = {**changed, "m.new_content": _with_copy(new, mxc)}
    return {**event, "content": changed} if changed is not content else event


# -- waiting for a turn, and asking ----------------------------------------------------------


def _bridges(app: FastAPI) -> tuple[str, ...]:
    return app.state.config.encrypted_media.bridges


def _rank(app: FastAPI) -> int:
    bridges = _bridges(app)
    bot = bot_mxid(app.state.config)
    return bridges.index(bot) if bot in bridges else 0


async def _related(app: FastAPI, room_id: str, event_id: str, *, ours: bool = False) -> list[dict[str, Any]]:
    """The thread replies and announcements under the post, from the other
    bridges sharing copies (and this one's own, with ``ours``)."""
    others = set(_bridges(app)) - (set() if ours else {bot_mxid(app.state.config)})
    found: list[dict[str, Any]] = []
    for rel_type in (ANNOUNCE_TYPE, "m.thread"):
        try:
            page = await app.state.homeserver.get_relations(room_id, event_id, rel_type, as_user_id=bot_mxid(app.state.config))
        except HomeserverError:
            continue
        found += [e for e in page.get("chunk") or [] if e.get("sender") in others]
    return found


def hold(app: FastAPI, room_id: str, event: dict[str, Any]) -> None:
    """A post that would be published but for its encrypted file: ask about
    it (in turn), or leave it to the bridge that already did. Its handling
    is over; it's published again once there is a copy."""
    run_in_background(app, _hold(app, room_id, event), name=f"encrypted media {event.get('event_id', '')[:12]}")


async def _hold(app: FastAPI, room_id: str, event: dict[str, Any]) -> None:
    event_id = event.get("event_id", "")
    rank = _rank(app)
    if rank:
        await asyncio.sleep(rank * app.state.config.encrypted_media.wait_seconds)
        for related in await _related(app, room_id, event_id):
            if related.get("type") == ANNOUNCE_TYPE:
                await on_announce(app, related)
                return
            asked = ((related.get("content") or {}).get(PROMPT_FIELD) or {}).get("prompt_for")
            if asked == event_id or _MARKER in str((related.get("content") or {}).get("body") or ""):
                return
    await _ask(app, room_id, event)


async def _ask(app: FastAPI, room_id: str, event: dict[str, Any]) -> None:
    sender = event.get("sender", "")
    event_id = event.get("event_id", "")
    try:
        name = (await app.state.homeserver.get_profile(sender)).get("displayname") or sender
    except HomeserverError:
        name = sender
    pill = f'<a href="https://matrix.to/#/{html.escape(sender, quote=True)}">{html.escape(name)}</a>'
    what = ("To post it to your linked networks, the bridges need one decrypted copy of it on your homeserver, "
            "which they all share.")
    how = 'Reply "confirm" to this message, or react with 👍, to upload it.'
    await confirm.ask(
        app, room_id, action=ACTION, user=sender, args={"event_id": event_id},
        plain=f"⚠️{name}, {_MARKER}. {what}\n\n{how}",
        rich=f"<p>⚠️{pill}, {_MARKER}. {html.escape(what)}</p><p>{html.escape(how)}</p>",
        add_instruction=False, reactions=True,
        extra={
            PROMPT_FIELD: {"prompt_for": event_id},
            "m.mentions": {"user_ids": [sender]},
            "m.relates_to": {"rel_type": "m.thread", "event_id": event_id, "is_falling_back": True,
                             "m.in_reply_to": {"event_id": event_id}},
        },
    )


async def maybe_claim_answer(request: Any, event: dict[str, Any]) -> bool:
    """A "confirm" or 👍 answering another listed bridge's question (its own
    bridge acts on it): kept in Matrix, never published as a post or a
    reaction here. Runs after this bridge's own confirmations."""
    from bridge.confirm import _first_line_after_fallback, _reply_target, is_confirm_reaction

    app = request.app
    others = set(_bridges(app)) - {bot_mxid(app.state.config)}
    if not others:
        return False
    content = event.get("content") or {}
    if event.get("type") == "m.reaction":
        relation = content.get("m.relates_to") or {}
        target = relation.get("event_id") if is_confirm_reaction(str(relation.get("key") or "")) else None
    elif event.get("type") == "m.room.message" and _first_line_after_fallback(content).strip().lower() == "confirm":
        target = _reply_target(content)
    else:
        return False
    if not isinstance(target, str):
        return False
    try:
        question = await app.state.homeserver.get_event(event.get("room_id", ""), target, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return False
    asked = (question or {}).get("content") or {}
    return (question or {}).get("sender") in others and (PROMPT_FIELD in asked or _MARKER in str(asked.get("body") or ""))


# -- the copy --------------------------------------------------------------------------------


async def _thread_notice(app: FastAPI, room_id: str, event_id: str, text: str) -> None:
    await notice(app, room_id, text, extra={"m.relates_to": {
        "rel_type": "m.thread", "event_id": event_id, "is_falling_back": True, "m.in_reply_to": {"event_id": event_id}}})


def _media_part(content: dict[str, Any]) -> dict[str, Any]:
    new = content.get("m.new_content")
    return new if isinstance(new, dict) and encrypted_file(new) else content


async def _decrypt(app: FastAPI, content: dict[str, Any], file: dict[str, Any]) -> str | None:
    """The decrypted copy's ``mxc://``: made once, then remembered."""
    from bridge.render import upload_once

    existing = await known_copy(app, file)
    if existing is not None:
        return existing
    try:
        download = await app.state.homeserver.download_media(file["url"])
    except HomeserverError:
        logger.warning("Could not download encrypted file %s", file["url"], exc_info=True)
        return None
    if download is None or not _matches(file, download.content):
        logger.warning("Encrypted file %s is missing or fails its hash check", file["url"])
        return None
    try:
        plaintext = await asyncio.to_thread(_crypt, file, download.content)
    except (KeyError, ValueError):
        logger.warning("Could not decrypt %s", file["url"], exc_info=True)
        return None
    mime = (content.get("info") or {}).get("mimetype") or "application/octet-stream"
    name = str(content.get("body") or "").strip() or "file"
    try:
        return await upload_once(app, plaintext, mime, name, source=_source(file["url"]), as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        logger.warning("Could not upload the decrypted copy of %s", file["url"], exc_info=True)
        return None


async def announce(app: FastAPI, room_id: str, event_id: str, content: dict[str, Any], file: dict[str, Any], mxc: str) -> None:
    info = {k: v for k, v in (content.get("info") or {}).items() if not k.startswith("thumbnail_") and k != "xyz.amorgan.blurhash"}
    try:
        await app.state.homeserver.send_message_event(room_id, {
            "m.relates_to": {"rel_type": ANNOUNCE_TYPE, "event_id": event_id},
            "url": mxc, "encrypted_url": file["url"], "info": info,
        }, as_user_id=bot_mxid(app.state.config), event_type=ANNOUNCE_TYPE)
    except HomeserverError:
        logger.warning("Could not announce the decrypted copy of %s", file["url"], exc_info=True)


async def announce_if_needed(app: FastAPI, room_id: str, event: dict[str, Any]) -> None:
    """A post published with a copy this bridge already had: announced, unless
    another bridge has, so the others don't ask about it."""
    if not _bridges(app):
        return
    content = _media_part(event.get("content") or {})
    file = encrypted_file(content)
    mxc = await known_copy(app, file) if file else None
    if mxc is None:
        return
    if any(e.get("type") == ANNOUNCE_TYPE for e in await _related(app, room_id, event.get("event_id", ""), ours=True)):
        return
    await announce(app, room_id, event.get("event_id", ""), content, file, mxc)


async def _confirmed(app: FastAPI, room_id: str, sender: str, args: dict[str, Any]) -> None:
    event_id = str(args.get("event_id") or "")
    try:
        post = await app.state.homeserver.get_event(room_id, event_id, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return
    if not post or post.get("sender") != sender:
        return
    content = _media_part(post.get("content") or {})
    file = encrypted_file(content)
    if file is None:
        return
    mxc = await _decrypt(app, content, file)
    if mxc is None:
        await _thread_notice(app, room_id, event_id, "Couldn't decrypt/upload that file, so it wasn't posted.")
        return
    if _bridges(app):
        await announce(app, room_id, event_id, content, file, mxc)
    await republish(app, post)
    await _thread_notice(app, room_id, event_id, "✅ Sent.")


confirm.register(ACTION, _confirmed)


async def on_announce(app: FastAPI, event: dict[str, Any]) -> None:
    """Another bridge's decrypted copy: checked against the post's own file,
    remembered, and the post published with it."""
    from bridge.repository import MediaCopy

    sender = event.get("sender", "")
    if sender not in _bridges(app) or sender == bot_mxid(app.state.config):
        return
    content = event.get("content") or {}
    relation = content.get("m.relates_to") or {}
    room_id, event_id, mxc = event.get("room_id", ""), relation.get("event_id"), content.get("url")
    if relation.get("rel_type") != ANNOUNCE_TYPE or not isinstance(event_id, str) or not isinstance(mxc, str):
        return
    try:
        post = await app.state.homeserver.get_event(room_id, event_id, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return
    media = _media_part((post or {}).get("content") or {})
    file = encrypted_file(media)
    if file is None or file["url"] != content.get("encrypted_url"):
        return
    if await known_copy(app, file) is None:
        try:
            download = await app.state.homeserver.download_media(mxc)
        except HomeserverError:
            download = None
        if download is None or not _matches(file, await asyncio.to_thread(_crypt, file, download.content)):
            logger.warning("%s announced a copy of %s that isn't the same file; ignored", sender, file["url"])
            return
        await app.state.repository.put_media_copy(MediaCopy(
            _source(file["url"]), hashlib.sha256(download.content).hexdigest(), mxc,
            (media.get("info") or {}).get("mimetype") or download.content_type or "application/octet-stream",
            len(download.content), datetime.now(timezone.utc).isoformat()))
    await republish(app, post)


async def republish(app: FastAPI, post: dict[str, Any]) -> None:
    """The post handled again, now that its file has a copy: everything that
    decides whether and where it's published applies as before."""
    from bridge.appservice_routes import handle_event

    try:
        await handle_event(SimpleNamespace(app=app), post)
    except Exception:
        logger.exception("Publishing %s with its decrypted file failed", post.get("event_id"))
