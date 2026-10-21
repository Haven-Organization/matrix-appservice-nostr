"""The per-Matrix-user space (``m.space``) holding every room the bridge
manages for them: the Profile Room of each of their IDs, their
notifications room, and (later) the rooms of accounts they follow.

One space per Matrix user, created the first time it's needed. The bot is
its creator and the user sits one level below (99), as in a Profile Room,
so the bot can always re-invite them.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

_SPACE_ROOM_TYPE = "m.space"

# m.space.child's `order`: children with one sort before every child
# without, in this order. Profile Rooms come first ("0-<n>", by ID
# number), then the notifications room.
NOTIFICATIONS_ROOM_SPACE_ORDER = "1"


def profile_room_space_order(id_number: int) -> str:
    return f"0-{id_number:06d}"


async def ensure_user_space(app: FastAPI, matrix_user_id: str) -> str | None:
    """Get-or-create the user's space. Returns its room ID, or ``None`` if
    it couldn't be created (best-effort, like the rest of the bridge's room
    bookkeeping)."""
    repository = app.state.repository
    existing = (await repository.get_user_rooms(matrix_user_id)).space_room_id
    if existing is not None:
        return existing
    config = app.state.config
    try:
        room_id = await app.state.homeserver.create_room(
            as_user_id=bot_mxid(config),
            name=config.bridge.space_name,
            invite=[matrix_user_id],
            avatar_mxc=config.bridge.space_avatar_mxc or config.appservice.bot_avatar_mxc,
            room_type=_SPACE_ROOM_TYPE,
            power_level_users={matrix_user_id: 99},
        )
    except HomeserverError:
        logger.warning("Could not create a space for %s", matrix_user_id, exc_info=True)
        return None
    await repository.set_user_space(matrix_user_id, room_id)
    from bridge.room_widget import add_widget_once

    await add_widget_once(app, room_id)
    logger.info("Created space %s for %s", room_id, matrix_user_id)
    return room_id


async def add_room_to_space(app: FastAPI, matrix_user_id: str, child_room_id: str, *, order: str | None = None) -> None:
    """Add ``child_room_id`` to the user's space, creating the space first
    if needed. Idempotent."""
    space_room_id = await ensure_user_space(app, matrix_user_id)
    if space_room_id is None:
        return
    homeserver = app.state.homeserver
    config = app.state.config
    bot = bot_mxid(config)
    via = [config.homeserver.server_name]
    content: dict = {"via": via}
    if order is not None:
        content["order"] = order
    try:
        await homeserver.send_state_event(space_room_id, "m.space.child", content, as_user_id=bot, state_key=child_room_id)
    except HomeserverError:
        logger.info("Could not add %s to space %s", child_room_id, space_room_id, exc_info=True)
        return
    # The child's pointer back to its space, which some clients use for
    # breadcrumbs. Not what makes it appear in the space.
    try:
        await homeserver.send_state_event(
            child_room_id, "m.space.parent", {"via": via, "canonical": True}, as_user_id=bot, state_key=space_room_id
        )
    except HomeserverError:
        logger.info("Could not set m.space.parent on %s", child_room_id, exc_info=True)


async def remove_room_from_space(app: FastAPI, matrix_user_id: str, child_room_id: str) -> None:
    """Take a room out of the user's space (an ``m.space.child`` with no
    ``via`` means "not a child"). A no-op if they have no space."""
    space_room_id = (await app.state.repository.get_user_rooms(matrix_user_id)).space_room_id
    if space_room_id is None:
        return
    try:
        await app.state.homeserver.send_state_event(
            space_room_id, "m.space.child", {}, as_user_id=bot_mxid(app.state.config), state_key=child_room_id
        )
    except HomeserverError:
        logger.info("Could not remove %s from space %s", child_room_id, space_room_id, exc_info=True)
