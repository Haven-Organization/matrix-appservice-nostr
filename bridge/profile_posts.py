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
from bridge.commands import message_for_a_bot
from bridge.media import MediaProblem, publish_matrix_media
from bridge.messages import notice
from bridge.inbound import index_tags
from bridge.interactions import acting_id, extra_relays_for, find_quote, notify_local_author, reply_context
from bridge.notes import (
    CONVERTERS,
    DELETION_KIND,
    MEDIA_MSGTYPES,
    POSTABLE_MSGTYPES,
    deletion_v1,
    event_kind_of,
    message_body,
    pills,
)
from bridge.remote import pubkey_of_ghost
from bridge.publishing import NotifyTarget, publish_in_background
from bridge.repository import EventRow, NostrId
from bridge.homeserver_client import HomeserverError
from bridge.users import bot_mxid, is_bridge_user, is_local_user

logger = logging.getLogger(__name__)

# What a reply in Matrix can answer on Nostr.
REPLYABLE_KINDS = frozenset({1, 20, 21, 22, 1111, 30023})


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
    """The Nostr post (in the index) this message answers, if any."""
    repository = app.state.repository
    for target in _reply_targets(content):
        row = await repository.get_event_by_matrix_event(target)
        if row is not None and row.kind in REPLYABLE_KINDS:
            return row
    return None


async def _replies_to_own_message(app: FastAPI, room_id: str, content: dict[str, Any], sender: str) -> bool:
    """True for a message that isn't a reply, or answers ``sender``'s own
    message. False if it answers anyone else's, or what it answers can't be
    found."""
    targets = _reply_targets(content)
    if not targets:
        return True
    try:
        target = await app.state.homeserver.get_event(room_id, targets[0], as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return False
    return bool(target) and target.get("sender") == sender


async def _mentions(app: FastAPI, content: dict[str, Any]) -> list[dict[str, str]]:
    """The message's pills of Nostr accounts: local users with an ID (the
    key their active ID has now) and ghosts of remote accounts."""
    config = app.state.config
    found: dict[str, str] = {}
    for user_id, _label in pills(content):
        if user_id in found:
            continue
        ghost_key = pubkey_of_ghost(app, user_id)
        if ghost_key is not None:
            found[user_id] = ghost_key
            continue
        if not is_local_user(config, user_id):
            continue
        active = await ids.active_id(app, user_id)
        if active is not None:
            found[user_id] = active.pubkey
    return [{"user_id": user, "pubkey": pubkey} for user, pubkey in found.items()]


async def _store_and_send(
    app: FastAPI, author: NostrId, *, kind: int, content: str, tags: list[list[str]], created_at: int,
    recipe: dict[str, Any], room_id: str, matrix_event_id: str, notify: NotifyTarget | None,
    extra_relays: list[str] | None = None,
) -> EventRow | None:
    event = await app.state.signer.sign(author.pubkey, kind=kind, content=content, created_at=created_at, tags=tags)
    if event is None:
        reason = app.state.signer.last_error.pop(author.pubkey, "the bridge doesn't hold this ID's key")
        logger.warning("Can't sign for %s: %s", author.name, reason)
        if notify is not None:
            await notice(
                app, notify.room_id, f"That wasn't published on Nostr: {reason}",
                extra={"m.relates_to": {"m.in_reply_to": {"event_id": notify.event_id}}},
            )
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
        tags=index_tags(tags),
    )
    await app.state.repository.put_event(row)
    publish_in_background(app, event, notify=notify, extra_relays=extra_relays)
    return row


async def maybe_publish_message(request: Request, event: dict[str, Any]) -> bool:
    """Returns True if this message was a post or reply (published or
    deliberately not): anything in a Profile Room, or a reply to a Nostr
    post in any room."""
    if event.get("type") != "m.room.message":
        return False
    app = request.app
    config = app.state.config
    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    content = event.get("content") or {}
    repository = app.state.repository
    profile_id = await repository.get_id_for_profile_room(room_id)
    if is_bridge_user(config, sender) or not is_local_user(config, sender):
        return profile_id is not None
    if _is_edit(content) or message_for_a_bot(content, config):
        return profile_id is not None
    if content.get("msgtype") not in POSTABLE_MSGTYPES:
        return profile_id is not None

    parent = await _replied_post(app, content)
    if profile_id is not None and sender == profile_id.matrix_user_id:
        author: NostrId | None = profile_id
    elif parent is not None:
        # Answering a post anywhere else is done as the replier's own ID.
        author = await acting_id(app, sender, room_id)
    else:
        return profile_id is not None  # conversation, not a post
    if author is None:
        return True
    if parent is None and not await _replies_to_own_message(app, room_id, content, sender):
        # Answering something that isn't on Nostr and isn't the owner's own:
        # another bridge's post or bot (a room shared with other bridges), or
        # someone in the room. That's conversation, not a Nostr post.
        return True

    event_id = event.get("event_id", "")
    mentions = await _mentions(app, content)
    recipe: dict[str, Any] = {
        "kind": "note",
        "v": 1,
        "room_id": room_id,
        "event_id": event_id,
        "mentions": mentions,
        "p": [],
        "reply": None,
        "relay_hint": config.nostr.relay_url,
        "media": None,
    }
    p_tags: list[str] = []
    if parent is not None:
        context = await reply_context(app, parent, author.pubkey)
        p_tags += context["p"]
        if "comment" in context:
            recipe["kind"] = "comment"
            recipe["comment"] = context["comment"]
        else:
            recipe["reply"] = context["reply"]
    p_tags += [m["pubkey"] for m in mentions]
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

    if recipe["kind"] == "note":
        quote = await find_quote(app, message_body(content))
        if quote is not None:
            recipe["v"] = 2
            recipe["quote"] = quote
            if quote["pubkey"] != author.pubkey and quote["pubkey"] not in recipe["p"]:
                recipe["p"].append(quote["pubkey"])

    converter = CONVERTERS[(recipe["kind"], recipe["v"])]
    text, tags = converter(event, recipe)
    if not text:
        return True
    created_at = int(event.get("origin_server_ts", 0)) // 1000
    row = await _store_and_send(
        app, author, kind=event_kind_of(recipe), content=text, tags=tags, created_at=created_at, recipe=recipe,
        room_id=room_id, matrix_event_id=event_id,
        notify=NotifyTarget(room_id, event_id, "this post" if parent is None else "this reply"),
        extra_relays=await extra_relays_for(app, recipe["p"]),
    )
    if row is not None:
        logger.info("Published %s from %s as %s@%s", row.id, event_id, author.name, config.bridge.domain)
        if parent is not None:
            await notify_local_author(app, author, parent, "reply", theirs=(room_id, event_id))
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
    row = await repository.get_event_by_matrix_event(target, include_copies=False)
    if row is None or row.kind == DELETION_KIND:
        return False
    author = await repository.get_id_by_pubkey(row.pubkey)
    await repository.delete_event(row.id)
    hub = getattr(app.state, "relay_hub", None)
    if hub is not None:
        hub.cache.drop(row.id)
    if author is None:
        # Someone else's event, removed in Matrix (a moderator, or the
        # Profile Room's owner): it leaves the relay, and counts against
        # its author in the spam rules.
        guard = getattr(app.state, "spam", None)
        if guard is not None and row.room_id and await repository.get_id_for_profile_room(row.room_id) is not None:
            await guard.record_report(row.pubkey, 1)
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
