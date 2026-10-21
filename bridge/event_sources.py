"""Rebuilding a Nostr event's exact bytes on demand.

The bridge doesn't store event content (DESIGN.md, "The storage rule"),
but a relay must be able to hand out any event it holds, byte for byte. So
each row of the relay index carries a ``source``: a small JSON recipe
naming a *kind* and whatever that kind needs to produce the event again.
The row also keeps the event's ``id``, ``pubkey``, ``created_at`` and
``sig``, so rebuilding only has to reproduce ``kind``, ``tags`` and
``content`` and check they hash to the same ``id``.

Source kinds:

- ``inline``: ``{"kind": "inline", "event_kind", "tags", "content"}``.
  Only for events that ARE bookkeeping, with no Matrix content behind them:
  relay lists, the media server list, later follow and mute lists. Their
  tags are references (relay URLs, pubkeys) and their content is empty.
- ``note`` (kind 1) and ``deletion`` (kind 5), version 1: rebuilt from the
  Matrix message or redaction they were made from (``bridge.notes``). If
  that event has been redacted, the event is gone.
- ``profile`` (kind 0), version 1: rebuilt from the Profile Room's
  ``m.room.name`` and ``m.room.topic`` state events, by event ID (an old
  state event can still be fetched after a newer one replaces it). The
  picture and banner URLs, and the ``nip05`` handle, are kept in the
  recipe: they depend on config and on processing done once.

Every converter is a frozen contract. It may depend only on the recipe
and the Matrix events it names, never on the clock or config. Once events
exist under a version it must never change; converting differently means
adding a new version and keeping the old one for as long as its events
exist.

Rebuilding has four outcomes, and they must not be confused:

- ``ok``: the event, exactly as signed.
- ``gone``: the Matrix event behind it was redacted or deleted. The event
  can never be served again.
- ``mismatch``: the source now produces different bytes. For a profile,
  that means the room changed and a new kind 0 is due; anywhere else it is
  a bug. Not served either way.
- ``unavailable``: the homeserver couldn't be asked right now. Nothing may
  be deleted on the strength of a failed lookup.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.nostr.event import compute_id
from bridge.notes import CONVERTERS, event_kind_of, is_redacted, matrix_event_id_of, share_v1
from bridge.repository import EventRow
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

PROFILE_KIND = 0


@dataclass(frozen=True)
class Rebuilt:
    status: str  # "ok" | "gone" | "mismatch" | "unavailable"
    event: dict[str, Any] | None = None


def inline_source(event_kind: int, tags: list[list[str]], content: str = "") -> dict[str, Any]:
    return {"kind": "inline", "event_kind": event_kind, "tags": tags, "content": content}


# -- kind 0, version 1 ---------------------------------------------------------


def profile_content_v1(*, name: str | None, about: str | None, picture: str | None, banner: str | None, nip05: str) -> str:
    """Kind 0 ``content``: JSON with a fixed key order and no whitespace.
    ``name`` goes into both ``name`` and ``display_name`` (NIP-24): clients
    differ in which one they show."""
    profile: dict[str, str] = {}
    if name:
        profile["name"] = name
        profile["display_name"] = name
    if about:
        profile["about"] = about
    if picture:
        profile["picture"] = picture
    if banner:
        profile["banner"] = banner
    profile["nip05"] = nip05
    return json.dumps(profile, ensure_ascii=False, separators=(",", ":"))


def _state_text(event: dict[str, Any] | None, key: str) -> str | None:
    value = ((event or {}).get("content") or {}).get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _fetch(app: FastAPI, room_id: str, event_id: str | None) -> dict[str, Any] | None:
    if not event_id:
        return None
    return await app.state.homeserver.get_event(room_id, event_id, as_user_id=bot_mxid(app.state.config))


def profile_content_v2(
    *, name: str | None, about: str | None, picture: str | None, banner: str | None, nip05: str,
    website: str | None, lud16: str | None,
) -> str:
    """Version 2: v1 plus ``website`` and ``lud16`` (a Lightning address),
    from the Profile Room's ``software.haven.nostr.profile_extra`` state
    (set when an existing key is imported)."""
    profile: dict[str, str] = {}
    if name:
        profile["name"] = name
        profile["display_name"] = name
    if about:
        profile["about"] = about
    if picture:
        profile["picture"] = picture
    if banner:
        profile["banner"] = banner
    if website:
        profile["website"] = website
    if lud16:
        profile["lud16"] = lud16
    profile["nip05"] = nip05
    return json.dumps(profile, ensure_ascii=False, separators=(",", ":"))


async def profile_content_from_source(app: FastAPI, source: dict[str, Any]) -> str:
    """The kind 0 content a ``profile`` recipe produces (v1 or v2). Raises
    ``HomeserverError`` if the homeserver can't be asked."""
    version = source.get("v")
    if version not in (1, 2):
        raise ValueError(f"Unknown profile source version {version!r}")
    room_id = source["room_id"]
    name_event = await _fetch(app, room_id, source.get("name_event"))
    topic_event = await _fetch(app, room_id, source.get("topic_event"))
    if version == 1:
        return profile_content_v1(
            name=_state_text(name_event, "name"),
            about=_state_text(topic_event, "topic"),
            picture=source.get("picture"),
            banner=source.get("banner"),
            nip05=source["nip05"],
        )
    extra_event = await _fetch(app, room_id, source.get("extra_event"))
    return profile_content_v2(
        name=_state_text(name_event, "name"),
        about=_state_text(topic_event, "topic"),
        picture=source.get("picture"),
        banner=source.get("banner"),
        nip05=source["nip05"],
        website=_state_text(extra_event, "website"),
        lud16=_state_text(extra_event, "lud16"),
    )


# -- embedded: someone else's event, carried in its Matrix event ----------------

EMBED_FIELD = "software.haven.nostr.event"


def embedded_source() -> dict[str, Any]:
    return {"kind": "embedded"}


async def _rebuild_embedded(app: FastAPI, row: EventRow) -> "Rebuilt":
    if not row.room_id or not row.matrix_event_id:
        return Rebuilt("mismatch")
    try:
        matrix_event = await _fetch(app, row.room_id, row.matrix_event_id)
    except HomeserverError:
        return Rebuilt("unavailable")
    if matrix_event is None:
        return Rebuilt("gone")
    raw = (matrix_event.get("content") or {}).get(EMBED_FIELD)
    if not isinstance(raw, dict):
        return Rebuilt("gone")  # redacted, or the field was stripped
    if raw.get("id") != row.id:
        return Rebuilt("mismatch")
    return Rebuilt("ok", raw)


# -- rebuilding ----------------------------------------------------------------


async def rebuild(app: FastAPI, row: EventRow) -> Rebuilt:
    try:
        source = json.loads(row.source)
        kind = source.get("kind")
        if kind == "inline":
            tags = [list(tag) for tag in source["tags"]]
            content = source.get("content", "")
            event_kind = int(source["event_kind"])
        elif kind == "profile":
            tags = []
            content = await profile_content_from_source(app, source)
            event_kind = PROFILE_KIND
        elif kind == "embedded":
            return await _rebuild_embedded(app, row)
        elif (kind, source.get("v")) == ("share", 1):
            record = await _fetch(app, source["room_id"], source["event_id"])
            original = await _fetch(app, source["original_room"], source["original_event"])
            if record is None or is_redacted(record) or original is None or is_redacted(original):
                return Rebuilt("gone")
            content, tags = share_v1(record, original, source)
            event_kind = event_kind_of(source)
        elif (kind, source.get("v")) in CONVERTERS:
            matrix_event = await _fetch(app, source["room_id"], matrix_event_id_of(source))
            # A redaction with no reason has empty content in older room
            # versions; it is only gone if it no longer exists.
            if matrix_event is None or (kind != "deletion" and is_redacted(matrix_event)):
                return Rebuilt("gone")
            assert matrix_event is not None
            content, tags = CONVERTERS[(kind, source["v"])](matrix_event, source)
            event_kind = event_kind_of(source)
        else:
            logger.error("Event %s has unknown source kind %r", row.id, kind)
            return Rebuilt("mismatch")
    except HomeserverError:
        logger.info("Could not rebuild %s right now", row.id, exc_info=True)
        return Rebuilt("unavailable")
    except (ValueError, KeyError, TypeError):
        logger.exception("Event %s has a malformed source", row.id)
        return Rebuilt("mismatch")
    if compute_id(row.pubkey, row.created_at, event_kind, tags, content) != row.id:
        return Rebuilt("mismatch")
    return Rebuilt(
        "ok",
        {
            "id": row.id,
            "pubkey": row.pubkey,
            "created_at": row.created_at,
            "kind": event_kind,
            "tags": tags,
            "content": content,
            "sig": row.sig,
        },
    )
