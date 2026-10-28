"""Reposts made in Matrix, published to Nostr.

A repost in Matrix is a record in the reposter's Profile Room carrying
MSC4501's ``org.matrix.msc4501.social.relates_to`` with rel_type
``org.matrix.msc4501.social.repost``, pointing at the original post. Two
things post such records:

- an MSC4501 client (Haven), as the user, alongside a 🔁 reaction on the
  original; with a caption it's a quote-post;
- another bridge's bot (the fediverse or Bluesky bridge), as a "q reposted
  X's post" card when the user reposts through that bridge, shown as the
  user (MSC4144 per-message profile).

In a Profile Room shared with those bridges, the repost reaches Nostr too:

- of a post that's on Nostr: the 🔁 reaction already reposts it (kind 6/16),
  so a bare record adds nothing; a captioned one is a quote (note v2);
- of anything else (a fediverse or Bluesky post, or a Matrix message the
  bot can read): a note saying whose post was reposted, with a link to it,
  its text and media (``notes.share_v2``).

Redacting the record (undoing the repost) deletes the note, as for any post.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request

from bridge.homeserver_client import HomeserverError
from bridge.media import MediaProblem, publish_matrix_media
from bridge.notes import (
    CONVERTERS, MEDIA_MSGTYPES, NOTE_VERSION, SHARE_CONVERTERS, SHARE_VERSION, _PERMALINK_RE, event_kind_of,
    share_caption,
)
from bridge.publishing import NotifyTarget
from bridge.users import bot_mxid, is_other_bot

logger = logging.getLogger(__name__)

RELATES_TO_FIELD = "org.matrix.msc4501.social.relates_to"
REPOST_REL_TYPE = "org.matrix.msc4501.social.repost"
PER_MESSAGE_PROFILE_FIELD = "com.beeper.per_message_profile"


def repost_relation(event: dict[str, Any]) -> dict[str, Any] | None:
    """The MSC4501 repost relation a message carries, if any."""
    if event.get("type") != "m.room.message":
        return None
    relation = (event.get("content") or {}).get(RELATES_TO_FIELD)
    if not isinstance(relation, dict) or relation.get("rel_type") != REPOST_REL_TYPE:
        return None
    if not isinstance(relation.get("event_id"), str) or not isinstance(relation.get("room_id"), str):
        return None
    return relation


async def maybe_publish_share(request: Request, event: dict[str, Any]) -> bool:
    relation = repost_relation(event)
    if relation is None:
        return False
    app = request.app
    config = app.state.config
    repository = app.state.repository
    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    content = event.get("content") or {}
    owner_id = await repository.get_id_for_profile_room(room_id)
    if owner_id is None:
        return is_other_bot(config, sender)  # another bridge's card elsewhere: not ours
    if sender != owner_id.matrix_user_id:
        if not is_other_bot(config, sender):
            return False
        # Another bridge's card: only for a repost by the room's owner.
        shown_as = (content.get(PER_MESSAGE_PROFILE_FIELD) or {}).get("id")
        if shown_as is not None and shown_as != owner_id.matrix_user_id:
            return True
    from bridge import shadow_ids

    if not await shadow_ids.may_post_from_profile_room(app, owner_id) or not await shadow_ids.still_allowed(
        app, owner_id, room_id
    ):
        return True  # a third party's parked room: stays in Matrix
    original_event = relation["event_id"]
    original_room = relation["room_id"]
    nostr_row = await repository.get_event_by_matrix_event(original_event)
    if nostr_row is not None:
        return await _quote_nostr_post(app, owner_id, event, nostr_row)
    try:
        original = await app.state.homeserver.get_event(original_room, original_event, as_user_id=bot_mxid(config))
    except HomeserverError:
        original = None
    if not original or not original.get("content"):
        logger.info("Repost %s: can't read the original %s", event.get("event_id"), original_event)
        return True
    original_content = original.get("content") or {}
    if original.get("sender") == owner_id.matrix_user_id and not original_content.get("external_url"):
        return True  # reposting your own Matrix message: it's already yours
    recipe: dict[str, Any] = {
        "kind": "share",
        "v": SHARE_VERSION,
        "room_id": room_id,
        "event_id": event.get("event_id", ""),
        "original_room": original_room,
        "original_event": original_event,
        "author": await _author_name(app, relation, original),
        "media": None,
    }
    if original_content.get("msgtype") in MEDIA_MSGTYPES:
        try:
            media = await publish_matrix_media(app, original_content)
        except MediaProblem as exc:
            logger.info("Repost %s: leaving the media out (%s)", event.get("event_id"), exc)
        else:
            recipe["media"] = {
                "url": media.url, "m": media.mime_type, "x": media.sha256, "size": media.size,
                "dim": f"{media.width}x{media.height}" if media.width and media.height else None,
            }
    text, tags = SHARE_CONVERTERS[SHARE_VERSION](event, original, recipe)
    await _publish(app, owner_id, event, recipe, text, tags)
    return True


async def _quote_nostr_post(app: FastAPI, author, event: dict[str, Any], quoted) -> bool:
    """A repost of a Nostr post: the 🔁 reaction already reposts it, so only
    a captioned one (a quote-post) publishes anything."""
    content = event.get("content") or {}
    if not share_caption(content):
        return True
    link = _PERMALINK_RE.search(content.get("body") or "")
    recipe: dict[str, Any] = {
        "kind": "note", "v": NOTE_VERSION, "room_id": event.get("room_id", ""), "event_id": event.get("event_id", ""),
        "mentions": [], "p": [quoted.pubkey] if quoted.pubkey != author.pubkey else [], "reply": None,
        "relay_hint": app.state.config.nostr.relay_url, "media": None,
        "quote": {"id": quoted.id, "pubkey": quoted.pubkey, "link": link.group(0) if link else ""},
    }
    text, tags = CONVERTERS[("note", NOTE_VERSION)](event, recipe)
    await _publish(app, author, event, recipe, text, tags)
    return True


async def _author_name(app: FastAPI, relation: dict[str, Any], original: dict[str, Any]) -> str:
    name = relation.get("displayname")
    if isinstance(name, str) and name.strip():
        return name.strip()
    sender = original.get("sender", "")
    try:
        profile = await app.state.homeserver.get_profile(sender)
    except HomeserverError:
        profile = {}
    name = (profile or {}).get("displayname")
    return name.strip() if isinstance(name, str) and name.strip() else sender


async def _publish(app: FastAPI, author, event: dict[str, Any], recipe: dict[str, Any], text: str,
                   tags: list[list[str]]) -> None:
    from bridge.profile_posts import _store_and_send

    if not text:
        return
    room_id = event.get("room_id", "")
    event_id = event.get("event_id", "")
    row = await _store_and_send(
        app, author, kind=event_kind_of(recipe), content=text, tags=tags,
        created_at=int(event.get("origin_server_ts", 0)) // 1000, recipe=recipe, room_id=room_id,
        matrix_event_id=event_id, notify=NotifyTarget(room_id, event_id, "this repost"),
    )
    if row is not None:
        logger.info("Published repost %s from %s as %s", row.id, event_id, author.name)
