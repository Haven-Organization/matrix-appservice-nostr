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


def media_server_tags(app: FastAPI) -> list[list[str]]:
    return [["server", app.state.config.nostr.relay_http_base]]


async def _room_state(app: FastAPI, room_id: str) -> dict[str, dict[str, Any]]:
    """The room's current state events (with their event IDs), by type,
    for the ones a profile is made of."""
    wanted = {"m.room.name", "m.room.topic", "m.room.avatar", PROFILE_BANNER_STATE_TYPE}
    events = await app.state.homeserver.get_full_state(room_id, as_user_id=bot_mxid(app.state.config))
    return {e["type"]: e for e in events if e.get("type") in wanted and e.get("state_key") == ""}


async def _image_entry(app: FastAPI, event: dict[str, Any] | None, previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """``{"event_id", "mxc", "url"}`` for an avatar/banner state event,
    reusing the previous URL when the image is the same one."""
    mxc = ((event or {}).get("content") or {}).get("url")
    if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
        return None
    if previous and previous.get("mxc") == mxc and previous.get("url"):
        return {"event_id": event["event_id"], "mxc": mxc, "url": previous["url"]}
    published = await publish_matrix_image(app, mxc, max_bytes=PROFILE_IMAGE_MAX_BYTES)
    if published is None:
        logger.info("Profile image %s can't be used; leaving it out", mxc)
        return None
    return {"event_id": event["event_id"], "mxc": mxc, "url": published.url}


async def profile_source(app: FastAPI, nostr_id: NostrId, previous: dict[str, Any] | None) -> dict[str, Any]:
    """The ``profile`` v1 recipe for the ID's kind 0 as things stand."""
    assert nostr_id.profile_room_id is not None
    state = await _room_state(app, nostr_id.profile_room_id)
    previous = previous or {}
    avatar = await _image_entry(app, state.get("m.room.avatar"), previous.get("avatar_image"))
    banner = await _image_entry(app, state.get(PROFILE_BANNER_STATE_TYPE), previous.get("banner_image"))
    return {
        "kind": "profile",
        "v": 1,
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
    if nostr_id.profile_room_id:
        previous = await repository.get_latest_event(nostr_id.pubkey, PROFILE_KIND)
        previous_source = json.loads(previous.source) if previous is not None else None
        source = await profile_source(app, nostr_id, previous_source if (previous_source or {}).get("kind") == "profile" else None)
        content = await profile_content_from_source(app, source)
        if not _unchanged(previous, nostr_id.pubkey, PROFILE_KIND, [], content):
            event = await _store_and_publish(
                app, nostr_id, kind=PROFILE_KIND, tags=[], content=content, source=source,
                previous=previous, label=f"profile of {handle(app, nostr_id)}", room_id=nostr_id.profile_room_id,
            )
            if event is not None:
                published.append(event)
        await _update_bridge_info(app, nostr_id, source)

    for kind, tags, label in (
        (RELAY_LIST_KIND, relay_list_tags(app), "relay list"),
        (DM_RELAY_LIST_KIND, dm_relay_tags(app), "DM relay list"),
        (BLOSSOM_SERVER_LIST_KIND, media_server_tags(app), "media server list"),
    ):
        previous = await repository.get_latest_event(nostr_id.pubkey, kind)
        if _unchanged(previous, nostr_id.pubkey, kind, tags, ""):
            continue
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
