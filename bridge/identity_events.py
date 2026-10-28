"""The events that make up a Nostr ID's public identity, kept in step with
Matrix and config:

- kind 0, the profile: built from the Profile Room (name, topic, avatar,
  banner) plus the ID's NIP-05 handle,
- kind 10002 (NIP-65), where the ID writes and reads: the bridge's relay
  for both, plus ``nostr.publish_relays`` for writing,
- kind 10050 (NIP-17), where to send it DMs: the bridge's relay only,
- kind 10063 (BUD-03), where its media is: the bridge's media URL base.

``sync_identity`` works out what each of these should be now, and signs
and publishes only the ones that differ from what was last published. It
runs when an ID is created or renamed, when its Profile Room's name,
topic, avatar or banner changes, and for every ID at startup (which
catches a changed ``bridge.domain``, relay or media URL in the config).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import FastAPI

from bridge.bridge_info import send_bridge_info
from bridge.event_sources import PROFILE_KIND, inline_source, profile_content_from_source
from bridge.homeserver_client import HomeserverError
from bridge.media import PROFILE_IMAGE_MAX_BYTES, publish_matrix_image
from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE

PROFILE_EXTRA_STATE_TYPE = "software.haven.nostr.profile_extra"
from bridge.nostr.event import Event, compute_id
from bridge.repository import EventRow, NostrId
from bridge.publishing import publish_in_background
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

RELAY_LIST_KIND = 10002
DM_RELAY_LIST_KIND = 10050
BLOSSOM_SERVER_LIST_KIND = 10063


def handle(app: FastAPI, nostr_id: NostrId) -> str:
    return f"{nostr_id.name}@{app.state.config.bridge.domain}"


# -- what the events should be now ---------------------------------------------


def relay_list_tags(app: FastAPI) -> list[list[str]]:
    nostr = app.state.config.nostr
    tags = [["r", nostr.relay_url]]
    tags += [["r", url, "write"] for url in nostr.publish_relays if url != nostr.relay_url]
    return tags


def dm_relay_tags(app: FastAPI) -> list[list[str]]:
    return [["relay", app.state.config.nostr.relay_url]]


PIN_LIST_KIND = 10001


async def pin_list_tags(app: FastAPI, nostr_id: NostrId) -> list[list[str]]:
    """NIP-51 pinned notes: the ID's own posts pinned in its Profile Room
    (``m.room.pinned_events``), in pin order. Pins of anything else (a
    notice, someone's reply) are passed over."""
    assert nostr_id.profile_room_id is not None
    try:
        state = await app.state.homeserver.get_room_state(
            nostr_id.profile_room_id, "m.room.pinned_events", as_user_id=bot_mxid(app.state.config)
        )
    except HomeserverError:
        return []
    tags = []
    for event_id in (state or {}).get("pinned") or []:
        if not isinstance(event_id, str):
            continue
        row = await app.state.repository.get_event_by_matrix_event(event_id, include_copies=False)
        if row is not None and row.pubkey == nostr_id.pubkey and row.kind in (1, 20, 21, 22, 30023):
            tags.append(["e", row.id])
    return tags


def media_server_tags(app: FastAPI) -> list[list[str]]:
    return [["server", app.state.config.nostr.relay_http_base]]


async def _room_state(app: FastAPI, room_id: str) -> dict[str, dict[str, Any]]:
    """The room's current state events (with their event IDs), by type,
    for the ones a profile is made of."""
    wanted = {"m.room.name", "m.room.topic", "m.room.avatar", PROFILE_BANNER_STATE_TYPE, PROFILE_EXTRA_STATE_TYPE}
    events = await app.state.homeserver.get_full_state(room_id, as_user_id=bot_mxid(app.state.config))
    return {e["type"]: e for e in events if e.get("type") in wanted and e.get("state_key") == ""}


async def _mxc_entry(app: FastAPI, mxc: Any, previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """``{"mxc", "url"}`` for a profile image, reusing the previous URL
    when the image is the same one."""
    if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
        return None
    if previous and previous.get("mxc") == mxc and previous.get("url"):
        return {"mxc": mxc, "url": previous["url"]}
    published = await publish_matrix_image(app, mxc, max_bytes=PROFILE_IMAGE_MAX_BYTES)
    if published is None:
        logger.info("Profile image %s can't be used; leaving it out", mxc)
        return None
    return {"mxc": mxc, "url": published.url}


async def _image_entry(app: FastAPI, event: dict[str, Any] | None, previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """``{"event_id", "mxc", "url"}`` for an avatar/banner state event."""
    entry = await _mxc_entry(app, ((event or {}).get("content") or {}).get("url"), previous)
    return {"event_id": event["event_id"], **entry} if entry is not None and event is not None else None


async def mirrored_profile_source(app: FastAPI, nostr_id: NostrId, previous: dict[str, Any] | None) -> dict[str, Any]:
    """The ``matrix_profile`` v1 recipe for a shadow ID's kind 0: its
    owner's Matrix display name and avatar (``bridge.shadow_ids``). There's
    no room event to rebuild these from, so the recipe holds them, as it
    holds the picture URL and handle of a ``profile`` one."""
    user = nostr_id.matrix_user_id
    try:
        profile = await app.state.homeserver.get_profile(user)
    except HomeserverError:
        profile = {}
    avatar = await _mxc_entry(app, profile.get("avatar_url"), (previous or {}).get("avatar_image"))
    name = profile.get("displayname")
    return {
        "kind": "matrix_profile",
        "v": 1,
        "name": name if isinstance(name, str) and name.strip() else user,
        "about": f"{user} is a Matrix user.",
        "picture": (avatar or {}).get("url"),
        "nip05": handle(app, nostr_id),
        "avatar_image": avatar,
    }


async def profile_source(app: FastAPI, nostr_id: NostrId, previous: dict[str, Any] | None) -> dict[str, Any]:
    """The ``profile`` v1 recipe for the ID's kind 0 as things stand."""
    assert nostr_id.profile_room_id is not None
    state = await _room_state(app, nostr_id.profile_room_id)
    previous = previous or {}
    avatar = await _image_entry(app, state.get("m.room.avatar"), previous.get("avatar_image"))
    banner = await _image_entry(app, state.get(PROFILE_BANNER_STATE_TYPE), previous.get("banner_image"))
    extra = state.get(PROFILE_EXTRA_STATE_TYPE)
    return {
        "kind": "profile",
        # v2 only when the room has extra fields: v1 events keep rebuilding as v1.
        "v": 2 if extra else 1,
        **({"extra_event": extra["event_id"]} if extra else {}),
        "room_id": nostr_id.profile_room_id,
        "name_event": (state.get("m.room.name") or {}).get("event_id"),
        "topic_event": (state.get("m.room.topic") or {}).get("event_id"),
        # What the converter reads: the published URLs.
        "picture": (avatar or {}).get("url"),
        "banner": (banner or {}).get("url"),
        "nip05": handle(app, nostr_id),
        # Where those came from, so an unchanged image isn't processed again.
        "avatar_image": avatar,
        "banner_image": banner,
    }


# -- signing, storing and publishing --------------------------------------------


async def _store_and_publish(
    app: FastAPI,
    nostr_id: NostrId,
    *,
    kind: int,
    tags: list[list[str]],
    content: str,
    source: dict[str, Any],
    previous: EventRow | None,
    label: str,
    room_id: str | None = None,
) -> Event | None:
    # A replaceable event only replaces one with an older created_at.
    created_at = int(time.time())
    if previous is not None and created_at <= previous.created_at:
        created_at = previous.created_at + 1
    event = await app.state.signer.sign(nostr_id.pubkey, kind=kind, content=content, created_at=created_at, tags=tags)
    if event is None:
        logger.warning("Can't sign %s for %s: the bridge doesn't hold its key", label, handle(app, nostr_id))
        return None
    await app.state.repository.put_event(
        EventRow(
            id=event.id,
            pubkey=event.pubkey,
            kind=kind,
            created_at=created_at,
            sig=event.sig,
            source=json.dumps(source, ensure_ascii=False, separators=(",", ":")),
            room_id=room_id,
        ),
        replace=True,
    )
    logger.info("Signed %s (%s)", label, event.id)
    publish_in_background(app, event)
    return event


def _unchanged(previous: EventRow | None, pubkey: str, kind: int, tags: list[list[str]], content: str) -> bool:
    """Whether what was last published already says this: rebuilt with the
    old timestamp, it would be the very same event."""
    return previous is not None and compute_id(pubkey, previous.created_at, kind, tags, content) == previous.id


def _lock_for(app: FastAPI, id_number: int) -> asyncio.Lock:
    locks: dict[int, asyncio.Lock] = app.state.identity_locks
    return locks.setdefault(id_number, asyncio.Lock())


async def sync_identity(app: FastAPI, nostr_id: NostrId) -> list[Event]:
    """Bring the ID's kind 0, 10002, 10050 and 10063 up to date. Returns
    the events newly signed. Raises ``HomeserverError`` if the Profile
    Room can't be read.

    One sync per ID at a time: a new Profile Room produces several state
    events at once, and two syncs racing would each sign a profile."""
    async with _lock_for(app, nostr_id.id):
        # Re-read: a rename or room change may have landed while waiting.
        current = await app.state.repository.get_id(nostr_id.id)
        return await _sync_identity(app, current) if current is not None else []


async def _sync_identity(app: FastAPI, nostr_id: NostrId) -> list[Event]:
    repository = app.state.repository
    published: list[Event] = []
    from bridge.shadow_ids import mirrors_matrix

    mirrored = await mirrors_matrix(app, nostr_id)
    if mirrored or nostr_id.profile_room_id:
        previous = await repository.get_latest_event(nostr_id.pubkey, PROFILE_KIND)
        previous_source = json.loads(previous.source) if previous is not None else None
        if mirrored:
            # A shadow ID (or a parked one): its owner's Matrix profile.
            source = await mirrored_profile_source(
                app, nostr_id, previous_source if (previous_source or {}).get("kind") == "matrix_profile" else None
            )
        else:
            source = await profile_source(
                app, nostr_id, previous_source if (previous_source or {}).get("kind") == "profile" else None
            )
        content = await profile_content_from_source(app, source)
        if not _unchanged(previous, nostr_id.pubkey, PROFILE_KIND, [], content):
            event = await _store_and_publish(
                app, nostr_id, kind=PROFILE_KIND, tags=[], content=content, source=source,
                previous=previous, label=f"profile of {handle(app, nostr_id)}",
                room_id=None if mirrored else nostr_id.profile_room_id,
            )
            if event is not None:
                published.append(event)
        if not mirrored:
            await _update_bridge_info(app, nostr_id, source)

    lists = [
        (RELAY_LIST_KIND, relay_list_tags(app), "relay list"),
        (DM_RELAY_LIST_KIND, dm_relay_tags(app), "DM relay list"),
        (BLOSSOM_SERVER_LIST_KIND, media_server_tags(app), "media server list"),
    ]
    if nostr_id.profile_room_id:
        lists.append((PIN_LIST_KIND, await pin_list_tags(app, nostr_id), "pinned posts"))
    for kind, tags, label in lists:
        previous = await repository.get_latest_event(nostr_id.pubkey, kind)
        if _unchanged(previous, nostr_id.pubkey, kind, tags, ""):
            continue
        if kind == PIN_LIST_KIND and previous is None and not tags:
            continue  # nothing pinned yet, and never was: nothing to say
        event = await _store_and_publish(
            app, nostr_id, kind=kind, tags=tags, content="", source=inline_source(kind, tags),
            previous=previous, label=f"{label} of {handle(app, nostr_id)}",
        )
        if event is not None:
            published.append(event)
    return published


async def _update_bridge_info(app: FastAPI, nostr_id: NostrId, source: dict[str, Any]) -> None:
    assert nostr_id.profile_room_id is not None
    try:
        name = await app.state.homeserver.get_room_state(
            nostr_id.profile_room_id, "m.room.name", as_user_id=bot_mxid(app.state.config)
        )
    except HomeserverError:
        name = None
    await send_bridge_info(
        app,
        room_id=nostr_id.profile_room_id,
        pubkey=nostr_id.pubkey,
        display_name=(name or {}).get("name") or handle(app, nostr_id),
        avatar_mxc=(source.get("avatar_image") or {}).get("mxc"),
    )


async def sync_all_identities(app: FastAPI) -> None:
    """Startup pass: every ID's identity events, brought up to date."""
    for nostr_id in await app.state.repository.list_all_ids():
        try:
            await sync_identity(app, nostr_id)
        except Exception:
            logger.warning("Could not sync identity events for %s", handle(app, nostr_id), exc_info=True)
