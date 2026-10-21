"""Remote Nostr accounts in Matrix: ghost users, Remote User Rooms, and
what's known about where each account publishes.

Every remote pubkey the bridge deals with gets a ghost, ``@nostr_<hex>``,
whose display name and avatar follow the account's latest profile
(kind 0). The NIP-05 handle in that profile is only shown once verified:
the bridge fetches it and checks it points back to the same key, as
NIP-05 requires. With ``bridge.msc4503_external_handle`` set, the ghost
also carries its handle (or npub) in an MSC4503 profile field, as the
sibling bridges' ghosts do.

A **Remote User Room** is created the first time an account is followed,
or one of its posts needs a home in Matrix (a repost, the start of a
conversation). One room per account, shared by every local follower:
an MSC4501 profile room whose name, topic and avatar mirror the profile,
with the latest kind 0 kept in ``software.haven.nostr.profile`` state
(the raw event, so the relay can serve it).

Where an account publishes (its NIP-65 relay list, kind 10002) and where
it takes DMs (kind 10050) are cached as bookkeeping, refreshed now and
then from ``nostr.discovery_relays``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI

from bridge.bridge_info import send_bridge_info
from bridge.event_sources import EMBED_FIELD, embedded_source
from bridge.homeserver_client import HomeserverError
from bridge.media import PROFILE_IMAGE_MAX_BYTES, prepare_image
from bridge.messages import invite_to_room
from bridge.msc4501 import (
    EXTERNAL_HANDLE_FIELD,
    PROFILE_BANNER_STATE_TYPE,
    SOCIAL_PROFILE_ROOM_TYPE,
    SOCIAL_PROFILE_USER_ID_POWER_LEVEL,
    SOCIAL_PROFILE_USER_ID_STATE_TYPE,
    external_handle_value,
)
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19
from bridge.nostr.nip05 import parse_identifier
from bridge.nostr.relay_pool import normalize_relay_url
from bridge.repository import EventRow, RemoteAccount
from bridge.safe_fetch import FetchError, UnsafeAddress
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

PROFILE_STATE_TYPE = "software.haven.nostr.profile"
SOCIAL_PROFILE_ROOM_FIELD = "org.matrix.msc4501.social.profile_room"
RELAY_LIST_KIND = 10002
DM_RELAY_LIST_KIND = 10050
_REFRESH_EVERY = timedelta(hours=6)
_MAX_RELAYS = 8
_AVATAR_DOWNLOAD_MAX = 8 * 1024 * 1024


def ghost_user_id(app: FastAPI, pubkey: str) -> str:
    config = app.state.config
    return f"@{config.appservice.user_prefix}{pubkey}:{config.homeserver.server_name}"


def pubkey_of_ghost(app: FastAPI, user_id: str) -> str | None:
    config = app.state.config
    prefix = f"@{config.appservice.user_prefix}"
    suffix = f":{config.homeserver.server_name}"
    if not user_id.startswith(prefix) or not user_id.endswith(suffix):
        return None
    candidate = user_id[len(prefix):-len(suffix)]
    return candidate if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate) else None


def short_npub(pubkey: str) -> str:
    npub = nip19.npub_encode(pubkey)
    return f"{npub[:12]}…{npub[-6:]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- profiles ---------------------------------------------------------------------


def parse_profile(raw: dict[str, Any] | None) -> dict[str, str]:
    """The fields of a kind 0 the bridge uses, as clean strings."""
    if not raw:
        return {}
    try:
        data = json.loads(raw.get("content") or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, sources in (
        ("name", ("display_name", "displayName", "name")),
        ("about", ("about",)),
        ("picture", ("picture", "image")),
        ("banner", ("banner",)),
        ("nip05", ("nip05",)),
        ("lud16", ("lud16",)),
        ("website", ("website",)),
    ):
        for source in sources:
            value = data.get(source)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
                break
    if "name" in out:
        out["name"] = out["name"][:100]
    if "about" in out:
        out["about"] = out["about"][:2000]
    return out


def display_name(remote: RemoteAccount, profile: dict[str, str]) -> str:
    return profile.get("name") or remote.nip05 or short_npub(remote.pubkey)


async def verify_nip05(app: FastAPI, identifier: str, pubkey: str) -> str | None:
    """``identifier`` if its domain says it belongs to ``pubkey``, else None.
    Redirects are not followed, as NIP-05 requires."""
    parsed = parse_identifier(identifier)
    if parsed is None:
        return None
    url = f"https://{parsed.domain}/.well-known/nostr.json?name={parsed.name}"
    try:
        fetched = await app.state.fetcher.get(url, max_bytes=256 * 1024, accept="application/json", max_redirects=0)
    except (FetchError, UnsafeAddress):
        return None
    if fetched.status != 200:
        return None
    try:
        names = json.loads(fetched.content).get("names") or {}
    except (ValueError, AttributeError):
        return None
    return str(parsed) if isinstance(names, dict) and names.get(parsed.name) == pubkey else None


async def _fetch_image(app: FastAPI, url: str) -> tuple[bytes, str] | None:
    try:
        fetched = await app.state.fetcher.get(url, max_bytes=_AVATAR_DOWNLOAD_MAX, accept="image/*")
    except (FetchError, UnsafeAddress):
        return None
    if fetched.status != 200:
        return None
    image = await prepare_image(fetched.content, max_bytes=PROFILE_IMAGE_MAX_BYTES)
    return (image.data, image.mime_type) if image is not None else None


async def _upload_image(app: FastAPI, url: str, ghost: str) -> str | None:
    image = await _fetch_image(app, url)
    if image is None:
        return None
    data, mime = image
    try:
        return await app.state.homeserver.upload_media(data, mime, "avatar", as_user_id=ghost)
    except HomeserverError:
        logger.info("Could not store an avatar from %s", url, exc_info=True)
        return None


# -- ghosts -------------------------------------------------------------------------


async def _register_ghost(app: FastAPI, ghost: str) -> None:
    try:
        await app.state.homeserver.register_appservice_user(ghost[1:].split(":", 1)[0])
    except HomeserverError as exc:
        if exc.errcode != "M_USER_IN_USE":
            raise


async def get_or_create_remote(app: FastAPI, pubkey: str) -> RemoteAccount:
    """The account's record and ghost, created (and its profile fetched) on
    first sight. Raises ``HomeserverError`` if the ghost can't be made."""
    repository = app.state.repository
    remote = await repository.get_remote_account(pubkey)
    if remote is not None:
        return remote
    ghost = ghost_user_id(app, pubkey)
    await _register_ghost(app, ghost)
    remote = RemoteAccount(pubkey=pubkey, ghost_user_id=ghost)
    await repository.save_remote_account(remote)
    try:
        await app.state.homeserver.set_display_name(ghost, short_npub(pubkey))
    except HomeserverError:
        pass
    return await refresh(app, remote, force=True)


async def refresh(app: FastAPI, remote: RemoteAccount, *, force: bool = False, hints: tuple[str, ...] = ()) -> RemoteAccount:
    """Fetch the account's latest profile and relay lists and apply them,
    unless that was done recently (``force`` to do it anyway)."""
    if not force and remote.checked_at and datetime.fromisoformat(remote.checked_at) > _now() - _REFRESH_EVERY:
        return remote
    config = app.state.config
    relays = [*hints, *remote.write_relays, *config.nostr.discovery_relays, *config.nostr.fallback_relays[:2]]
    events = await app.state.relay_pool.query(
        relays, [{"authors": [remote.pubkey], "kinds": [0, RELAY_LIST_KIND, DM_RELAY_LIST_KIND]}], timeout=8.0
    )
    newest: dict[int, dict[str, Any]] = {}
    for raw in events:
        try:
            event = nostr_event.verify(raw)
        except nostr_event.InvalidEvent:
            continue
        if event.pubkey != remote.pubkey:
            continue
        if event.kind not in newest or event.created_at > newest[event.kind]["created_at"]:
            newest[event.kind] = raw
    remote = replace(remote, checked_at=_now().isoformat())
    for kind in (RELAY_LIST_KIND, DM_RELAY_LIST_KIND):
        if kind in newest:
            remote = apply_relay_list(remote, newest[kind])
    await app.state.repository.save_remote_account(remote)
    if 0 in newest:
        remote = await apply_profile(app, remote, newest[0])
    return remote


def _relay_urls(values: list[str]) -> tuple[str, ...]:
    urls = []
    for value in values:
        if isinstance(value, str) and value.startswith(("wss://", "ws://")):
            urls.append(normalize_relay_url(value))
    return tuple(dict.fromkeys(urls))[:_MAX_RELAYS]


def apply_relay_list(remote: RemoteAccount, raw: dict[str, Any]) -> RemoteAccount:
    """Cache a kind 10002 (NIP-65) or 10050 (NIP-17) list, if newer."""
    created = int(raw.get("created_at") or 0)
    tags = [t for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2]
    if raw.get("kind") == DM_RELAY_LIST_KIND:
        return replace(remote, dm_relays=_relay_urls([t[1] for t in tags if t[0] == "relay"]))
    if created < remote.relays_at:
        return remote
    write = _relay_urls([t[1] for t in tags if t[0] == "r" and (len(t) < 3 or t[2] == "write")])
    read = _relay_urls([t[1] for t in tags if t[0] == "r" and (len(t) < 3 or t[2] == "read")])
    return replace(remote, write_relays=write, read_relays=read, relays_at=created)


async def apply_profile(app: FastAPI, remote: RemoteAccount, raw: dict[str, Any]) -> RemoteAccount:
    """Apply a verified kind 0 to the ghost and the account's room, if it
    is newer than the one already applied."""
    created = int(raw.get("created_at") or 0)
    if created <= remote.profile_at:
        return remote
    config = app.state.config
    homeserver = app.state.homeserver
    profile = parse_profile(raw)
    nip05 = await verify_nip05(app, profile["nip05"], remote.pubkey) if profile.get("nip05") else None
    remote = replace(remote, nip05=nip05, profile_at=created)
    name = display_name(remote, profile)
    ghost = remote.ghost_user_id

    avatar_mxc = remote.avatar_mxc
    picture = profile.get("picture")
    if picture != remote.avatar_url:
        avatar_mxc = await _upload_image(app, picture, ghost) if picture else None
        remote = replace(remote, avatar_url=picture, avatar_mxc=avatar_mxc)
    try:
        current = await homeserver.get_profile(ghost)
    except HomeserverError:
        current = {}
    try:
        if current.get("displayname") != name:
            await homeserver.set_display_name(ghost, name)
        if avatar_mxc and current.get("avatar_url") != avatar_mxc:
            await homeserver.set_avatar_url(ghost, avatar_mxc)
    except HomeserverError:
        logger.info("Could not update the profile of %s", ghost, exc_info=True)
    if config.bridge.msc4503_external_handle in ("profile", "both"):
        try:
            await homeserver.set_profile_field(
                ghost, EXTERNAL_HANDLE_FIELD, external_handle_value(config, pubkey=remote.pubkey, nip05=nip05)
            )
        except HomeserverError:
            logger.debug("Homeserver won't take %s on %s (needs MSC4133)", EXTERNAL_HANDLE_FIELD, ghost)
    await app.state.repository.save_remote_account(remote)
    if remote.room_id:
        await _dress_room(app, remote, raw, profile, name, avatar_mxc)
    return remote


async def _dress_room(
    app: FastAPI, remote: RemoteAccount, raw: dict[str, Any], profile: dict[str, str], name: str, avatar_mxc: str | None
) -> None:
    """Make the Remote User Room match the profile, and keep the raw kind 0
    in its state (the relay serves it from there)."""
    assert remote.room_id is not None
    homeserver = app.state.homeserver
    ghost = remote.ghost_user_id
    room = remote.room_id

    async def set_state(event_type: str, content: dict[str, Any]) -> str | None:
        try:
            if await homeserver.get_room_state(room, event_type, as_user_id=ghost) == content:
                return None
            return await homeserver.send_state_event(room, event_type, content, as_user_id=ghost)
        except HomeserverError:
            logger.info("Could not set %s in %s", event_type, room, exc_info=True)
            return None

    await set_state("m.room.name", {"name": name})
    await set_state("m.room.topic", {"topic": profile.get("about", "")})
    if avatar_mxc:
        await set_state("m.room.avatar", {"url": avatar_mxc})
    banner = profile.get("banner")
    if banner:
        banner_mxc = await _upload_image(app, banner, ghost)
        if banner_mxc:
            await set_state(PROFILE_BANNER_STATE_TYPE, {"url": banner_mxc})
    state_event_id = await set_state(PROFILE_STATE_TYPE, {EMBED_FIELD: raw})
    if state_event_id:
        await app.state.repository.put_event(
            EventRow(
                id=raw["id"], pubkey=remote.pubkey, kind=0, created_at=int(raw["created_at"]), sig=raw["sig"],
                source=json.dumps(embedded_source()), room_id=room, matrix_event_id=state_event_id, scope="followed",
            ),
            replace=True,
        )
    await send_bridge_info(app, room_id=room, pubkey=remote.pubkey, display_name=name, avatar_mxc=avatar_mxc)


# -- rooms ----------------------------------------------------------------------------


async def ensure_room(app: FastAPI, remote: RemoteAccount) -> RemoteAccount:
    """The account's Remote User Room, created if it has none yet. Raises
    ``HomeserverError`` if it can't be made."""
    if remote.room_id:
        return remote
    config = app.state.config
    homeserver = app.state.homeserver
    ghost = remote.ghost_user_id
    lock: asyncio.Lock = app.state.remote_locks.setdefault(remote.pubkey, asyncio.Lock())
    async with lock:
        fresh = await app.state.repository.get_remote_account(remote.pubkey)
        if fresh is not None and fresh.room_id:
            return fresh
        try:
            current = await homeserver.get_profile(ghost)
        except HomeserverError:
            current = {}
        room_id = await homeserver.create_room(
            as_user_id=bot_mxid(config),
            name=current.get("displayname") or short_npub(remote.pubkey),
            invite=[ghost],
            avatar_mxc=remote.avatar_mxc,
            room_type=SOCIAL_PROFILE_ROOM_TYPE,
            # The ghost whose room this is sits at the bot's level, as in the
            # sibling bridges; followers stay at the default.
            power_level_users={ghost: 100},
            power_level_events={SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
            join_rule=config.bridge.ghost_room_join_rule,
            history_visibility="world_readable" if config.bridge.world_readable_remote_rooms else None,
            initial_state=[{"type": SOCIAL_PROFILE_USER_ID_STATE_TYPE, "state_key": "", "content": {"user_id": ghost}}],
        )
        await homeserver.join_room(room_id, as_user_id=ghost)
        remote = replace(fresh or remote, room_id=room_id)
        await app.state.repository.save_remote_account(remote)
    logger.info("Created Remote User Room %s for %s", room_id, remote.pubkey)
    if config.bridge.set_msc4501_profile_room:
        try:
            await homeserver.set_profile_field(ghost, SOCIAL_PROFILE_ROOM_FIELD, room_id)
        except HomeserverError:
            logger.debug("Homeserver won't take %s (needs MSC4133)", SOCIAL_PROFILE_ROOM_FIELD)
    # Re-apply the profile now there's a room to dress.
    events = await app.state.relay_pool.query(
        [*remote.write_relays, *config.nostr.discovery_relays], [{"authors": [remote.pubkey], "kinds": [0], "limit": 1}]
    )
    latest = max((e for e in events if e.get("pubkey") == remote.pubkey), key=lambda e: e.get("created_at", 0), default=None)
    if latest is not None:
        try:
            nostr_event.verify(latest)
            remote = await apply_profile(app, replace(remote, profile_at=0), latest)
        except nostr_event.InvalidEvent:
            pass
    else:
        await send_bridge_info(app, room_id=room_id, pubkey=remote.pubkey, display_name=None, avatar_mxc=remote.avatar_mxc)
    return remote


async def ensure_ghost_in_room(app: FastAPI, ghost: str, room_id: str) -> None:
    """Get a ghost into a room (a Profile Room it replies in, say): the bot
    invites, the ghost joins."""
    homeserver = app.state.homeserver
    try:
        members = await homeserver.get_joined_members(room_id, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        members = []
    if ghost in members:
        return
    await invite_to_room(app, room_id, ghost)
    await homeserver.join_room(room_id, as_user_id=ghost)


def write_relays_for(app: FastAPI, remote: RemoteAccount | None) -> list[str]:
    """Where to look for an account's posts."""
    if remote is not None and remote.write_relays:
        return list(remote.write_relays)
    return list(app.state.config.nostr.fallback_relays)


def inbox_relays_for(app: FastAPI, remote: RemoteAccount | None) -> list[str]:
    """Where to send events meant for an account (its NIP-65 read relays)."""
    if remote is not None and remote.read_relays:
        return list(remote.read_relays)
    return []
