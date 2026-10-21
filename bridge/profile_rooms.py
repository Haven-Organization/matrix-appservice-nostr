"""Profile Rooms, and the other rooms every user of the bridge has.

Each Nostr ID has a Profile Room: a room the bot creates and administers,
whose name, topic, avatar and banner are the ID's Nostr profile, and (from
step 3 of the build order) whose owner's messages are its posts. It is an
MSC4501 profile room owned by the user, as in the sibling bridges.

``ensure_user_rooms`` makes sure a user has their space, their
notifications room, and a Profile Room for every ID, re-inviting them to
any they've left. Running ``[create profile`` again for an ID you already
have goes through it, which makes that the way back into your rooms.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.messages import invite_to_room
from bridge.msc4501 import (
    SOCIAL_PROFILE_ROOM_TYPE,
    SOCIAL_PROFILE_USER_ID_POWER_LEVEL,
    SOCIAL_PROFILE_USER_ID_STATE_TYPE,
)
from bridge.notifications import ensure_notifications_room
from bridge.repository import NostrId
from bridge.spaces import add_room_to_space, ensure_user_space, profile_room_space_order
from bridge.users import bot_mxid, localpart

logger = logging.getLogger(__name__)


async def default_profile_name(app: FastAPI, nostr_id: NostrId, *, first: bool) -> str:
    """A new Profile Room's name, which is the ID's Nostr display name: the
    user's Matrix display name for their first ID, the ID's name for any
    later one (a second identity is usually meant to look different)."""
    if first:
        try:
            profile = await app.state.homeserver.get_profile(nostr_id.matrix_user_id)
        except HomeserverError:
            profile = {}
        name = profile.get("displayname")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return localpart(nostr_id.matrix_user_id)
    return nostr_id.name


async def create_profile_room(
    app: FastAPI, nostr_id: NostrId, *, first: bool, profile: dict[str, dict] | None = None
) -> NostrId:
    """Create the ID's Profile Room and record it. ``profile`` (room state
    contents by type) starts it with an existing profile instead of the
    defaults. Raises ``HomeserverError`` if the room can't be made."""
    config = app.state.config
    owner = nostr_id.matrix_user_id
    avatar_mxc = None
    if first and not profile:
        try:
            avatar = (await app.state.homeserver.get_profile(owner)).get("avatar_url")
            avatar_mxc = avatar if isinstance(avatar, str) and avatar.startswith("mxc://") else None
        except HomeserverError:
            pass
    room_id = await app.state.homeserver.create_room(
        as_user_id=bot_mxid(config),
        name=None if profile and "m.room.name" in profile else await default_profile_name(app, nostr_id, first=first),
        invite=[owner],
        avatar_mxc=avatar_mxc,
        room_type=SOCIAL_PROFILE_ROOM_TYPE,
        # One below the bot: the owner can change the name, topic and
        # avatar and moderate the room, while the bot can always still act.
        power_level_users={owner: 99},
        # Only the bot may say whose profile this is (MSC4501's guidance).
        power_level_events={SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
        join_rule=config.bridge.local_profile_room_join_rule,
        initial_state=[
            {"type": SOCIAL_PROFILE_USER_ID_STATE_TYPE, "state_key": "", "content": {"user_id": owner}},
            *({"type": t, "state_key": "", "content": c} for t, c in (profile or {}).items()),
        ],
    )
    await app.state.repository.set_profile_room(nostr_id.id, room_id)
    from bridge.room_widget import add_widget_once

    await add_widget_once(app, room_id)
    logger.info("Created Profile Room %s for %s@%s", room_id, nostr_id.name, config.bridge.domain)
    updated = await app.state.repository.get_id(nostr_id.id)
    assert updated is not None
    return updated


async def ensure_user_rooms(app: FastAPI, matrix_user_id: str) -> None:
    """The user's space, notifications room, and every ID's Profile Room
    (created if missing), with the user invited to each and each filed in
    the space."""
    repository = app.state.repository
    space = await ensure_user_space(app, matrix_user_id)
    notifications = await ensure_notifications_room(app, matrix_user_id)
    ids = await repository.list_ids_for_user(matrix_user_id)
    for number, nostr_id in enumerate(ids, start=1):
        if nostr_id.profile_room_id is None:
            if nostr_id.room_unlinked:
                continue  # unlinked on purpose: [link or [create room gives it one
            try:
                nostr_id = await create_profile_room(app, nostr_id, first=number == 1)
            except HomeserverError:
                logger.warning("Could not create the Profile Room of %s", nostr_id.name, exc_info=True)
                continue
        assert nostr_id.profile_room_id is not None
        await add_room_to_space(app, matrix_user_id, nostr_id.profile_room_id, order=profile_room_space_order(nostr_id.id))
        await invite_to_room(app, nostr_id.profile_room_id, matrix_user_id)
    for room_id in (space, notifications):
        if room_id:
            await invite_to_room(app, room_id, matrix_user_id)
