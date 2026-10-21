"""Publishing from Profile Rooms: Matrix messages become Nostr notes, and
redacting one deletes the note.

What gets published (DESIGN.md, "Profile Rooms: who posts"):

- The owner's text, emote and media messages, as the room's ID. A reply
  or thread message answering one of the ID's own posts becomes a NIP-10
  reply to it; a reply to anything else is simply a post.
- Another local user's reply to a post here, as *their* active ID (they're
  answering on Nostr, not posting on the owner's behalf). Their other
  messages are conversation and stay in Matrix.
- Not edits (Nostr notes are immutable), commands, notices, or anything
  from the bot or a ghost.

Each note goes into the index with a recipe (``bridge.notes``) and no
content, then out to the public relays (``bridge.publishing``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request

from bridge import ids
from bridge.commands import message_addresses_bot
from bridge.media import MediaProblem, publish_matrix_media
from bridge.messages import notice
from bridge.notes import DELETION_KIND, MEDIA_MSGTYPES, NOTE_KIND, POSTABLE_MSGTYPES, deletion_v1, note_v1, pills
from bridge.publishing import NotifyTarget, publish_in_background
from bridge.repository import EventRow, NostrId
from bridge.users import is_bridge_user, is_local_user

logger = logging.getLogger(__name__)

_INDEXED_TAG_LETTERS = frozenset("epaqkd")


def _relates_to(content: dict[str, Any]) -> dict[str, Any]:
    relates = content.get("m.relates_to")
    return relates if isinstance(relates, dict) else {}


def _is_edit(content: dict[str, Any]) -> bool:
    return _relates_to(content).get("rel_type") == "m.replace" or "m.new_content" in content


def _reply_targets(content: dict[str, Any]) -> list[str]:
    """What a message answers, most specific first: the event it replies
    to, then its thread's root."""
    relates = _relates_to(content)
    targets = []
    in_reply_to = (relates.get("m.in_reply_to") or {}).get("event_id")
    if isinstance(in_reply_to, str):
        targets.append(in_reply_to)
    if relates.get("rel_type") == "m.thread" and isinstance(relates.get("event_id"), str):
        targets.append(relates["event_id"])
    return targets


async def _replied_post(app: FastAPI, content: dict[str, Any]) -> EventRow | None:
    """The note (in the index) this message answers, if any."""
    repository = app.state.repository
    for target in _reply_targets(content):
        row = await repository.get_event_by_matrix_event(target)
        if row is not None and row.kind == NOTE_KIND:
            return row
    return None


def _reply_recipe(parent: EventRow) -> dict[str, str]:
    source = json.loads(parent.source)
    parent_reply = source.get("reply") if source.get("kind") == "note" else None
    if parent_reply:
        root, root_pubkey = parent_reply["root"], parent_reply["root_pubkey"]
    else:
        root, root_pubkey = parent.id, parent.pubkey
    return {"root": root, "root_pubkey": root_pubkey, "parent": parent.id, "parent_pubkey": parent.pubkey}


async def _mentions(app: FastAPI, content: dict[str, Any]) -> list[dict[str, str]]:
    """The message's pills of local users who have a Nostr ID, with the
    key each pointed at now: the user's active ID. (Ghosts of remote
    accounts join this list once following exists.)"""
    config = app.state.config
    found: dict[str, str] = {}
    for user_id, _label in pills(content):
        if user_id in found or not is_local_user(config, user_id):
            continue
        active = await ids.active_id(app, user_id)
        if active is not None:
            found[user_id] = active.pubkey
    return [{"user_id": user, "pubkey": pubkey} for user, pubkey in found.items()]


def _index_tags(tags: list[list[str]]) -> tuple[tuple[str, str], ...]:
    return tuple(
        dict.fromkeys((tag[0], tag[1]) for tag in tags if len(tag) >= 2 and tag[0] in _INDEXED_TAG_LETTERS)
    )


async def _store_and_send(
    app: FastAPI, author: NostrId, *, kind: int, content: str, tags: list[list[str]], created_at: int,
    recipe: dict[str, Any], room_id: str, matrix_event_id: str, notify: NotifyTarget | None,
) -> EventRow | None:
    event = await app.state.signer.sign(author.pubkey, kind=kind, content=content, created_at=created_at, tags=tags)
    if event is None:
        logger.warning("Can't sign for %s: the bridge doesn't hold its key", author.name)
        return None
    row = EventRow(
        id=event.id,
        pubkey=event.pubkey,
        kind=kind,
        created_at=created_at,
        sig=event.sig,
        source=json.dumps(recipe, ensure_ascii=False, separators=(",", ":")),
        room_id=room_id,
        matrix_event_id=matrix_event_id,
        tags=_index_tags(tags),
    )
    await app.state.repository.put_event(row)
    publish_in_background(app, event, notify=notify)
    return row


async def maybe_publish_message(request: Request, event: dict[str, Any]) -> bool:
    """Returns True if this was a message in a Profile Room (published or
    deliberately not)."""
    if event.get("type") != "m.room.message":
        return False
    app = request.app
    config = app.state.config
    room_id = event.get("room_id", "")
    profile_id = await app.state.repository.get_id_for_profile_room(room_id)
    if profile_id is None:
        return False
    sender = event.get("sender", "")
    content = event.get("content") or {}
    if is_bridge_user(config, sender) or not is_local_user(config, sender):
        return True
    if _is_edit(content) or message_addresses_bot(content, config):
        return True
    if content.get("msgtype") not in POSTABLE_MSGTYPES:
        return True

    parent = await _replied_post(app, content)
    if sender == profile_id.matrix_user_id:
        author: NostrId | None = profile_id
    elif parent is not None:
        # Someone else answering a post here does so as themselves.
        author = await ids.active_id(app, sender)
    else:
        author = None  # conversation, not a post
    if author is None:
        return True

    event_id = event.get("event_id", "")
    recipe: dict[str, Any] = {
        "kind": "note",
        "v": 1,
        "room_id": room_id,
        "event_id": event_id,
        "mentions": await _mentions(app, content),
        "p": [],
        "reply": _reply_recipe(parent) if parent is not None else None,
        "relay_hint": config.nostr.relay_url,
        "media": None,
    }
    p_tags: list[str] = []
    if recipe["reply"]:
        p_tags += [recipe["reply"]["parent_pubkey"], recipe["reply"]["root_pubkey"]]
    p_tags += [m["pubkey"] for m in recipe["mentions"]]
    recipe["p"] = [pubkey for pubkey in dict.fromkeys(p_tags) if pubkey != author.pubkey]

    if content.get("msgtype") in MEDIA_MSGTYPES:
        try:
            media = await publish_matrix_media(app, content)
        except MediaProblem as exc:
            await notice(
                app,
                room_id,
                f"That wasn't published on Nostr: {exc}.",
                extra={"m.relates_to": {"m.in_reply_to": {"event_id": event_id}}},
            )
            return True
        recipe["media"] = {
            "url": media.url,
            "m": media.mime_type,
            "x": media.sha256,
            "size": media.size,
            "dim": f"{media.width}x{media.height}" if media.width and media.height else None,
        }

    text, tags = note_v1(event, recipe)
    if not text:
        return True
    created_at = int(event.get("origin_server_ts", 0)) // 1000
    row = await _store_and_send(
        app, author, kind=NOTE_KIND, content=text, tags=tags, created_at=created_at, recipe=recipe,
        room_id=room_id, matrix_event_id=event_id,
        notify=NotifyTarget(room_id, event_id, "this post" if parent is None else "this reply"),
    )
    if row is not None:
        logger.info("Published %s from %s as %s@%s", row.id, event_id, author.name, config.bridge.domain)
    return True


async def maybe_handle_redaction(request: Request, event: dict[str, Any]) -> bool:
    """Returns True if a published note's Matrix event was redacted: a
    NIP-09 deletion is signed by its author and sent out, and the note
    leaves the index (it can never be rebuilt again)."""
    if event.get("type") != "m.room.redaction":
        return False
    target = event.get("redacts") or (event.get("content") or {}).get("redacts")
    if not isinstance(target, str):
        return False
    app = request.app
    repository = app.state.repository
    row = await repository.get_event_by_matrix_event(target)
    if row is None or row.kind == DELETION_KIND:
        return False
    author = await repository.get_id_by_pubkey(row.pubkey)
    await repository.delete_event(row.id)
    if author is None:
        return True
    room_id = event.get("room_id", "")
    recipe = {
        "kind": "deletion",
        "v": 1,
        "room_id": room_id,
        "redaction_event": event.get("event_id", ""),
        "targets": [{"id": row.id, "kind": row.kind}],
    }
    content, tags = deletion_v1(event, recipe)
    deletion = await _store_and_send(
        app, author, kind=DELETION_KIND, content=content, tags=tags,
        created_at=int(event.get("origin_server_ts", 0)) // 1000, recipe=recipe, room_id=room_id,
        matrix_event_id=event.get("event_id", ""), notify=None,
    )
    if deletion is not None:
        logger.info("Deleted %s (redaction %s)", row.id, event.get("event_id"))
    return True
