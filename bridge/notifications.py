"""Each Matrix user's notifications room: a private room with the bot
where new followers, replies, mentions, reactions, reposts and zaps are
reported (from step 6 of the build order on). One per Matrix user, not
per ID: once a user has several IDs, each notification names the ID it
is for.

It is kept apart from Profile Rooms so that anyone else invited into a
Profile Room can't see who is interacting with its owner.
"""

from __future__ import annotations

import html
import logging

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.messages import notice
from bridge.spaces import NOTIFICATIONS_ROOM_SPACE_ORDER, add_room_to_space
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

_EXPLANATION = (
    "I'll tell you here whenever something on Nostr involves one of your IDs: new followers, replies, "
    "mentions, reactions, reposts and zaps. Stay in this room to keep getting them."
)


async def ensure_notifications_room(app: FastAPI, matrix_user_id: str) -> str | None:
    """Get-or-create the user's notifications room. Returns its room ID, or
    ``None`` if it couldn't be created."""
    repository = app.state.repository
    existing = (await repository.get_user_rooms(matrix_user_id)).notifications_room_id
    if existing is not None:
        return existing
    config = app.state.config
    try:
        room_id = await app.state.homeserver.create_room(
            as_user_id=bot_mxid(config),
            name=config.bridge.notifications_room_name,
            invite=[matrix_user_id],
            is_direct=True,
            preset="trusted_private_chat",
            avatar_mxc=config.bridge.notifications_room_avatar_mxc or config.appservice.bot_avatar_mxc,
        )
    except HomeserverError:
        logger.warning("Could not create a notifications room for %s", matrix_user_id, exc_info=True)
        return None
    await repository.set_user_notifications_room(matrix_user_id, room_id)
    await add_room_to_space(app, matrix_user_id, room_id, order=NOTIFICATIONS_ROOM_SPACE_ORDER)
    name = config.bridge.notifications_room_name
    # An ordinary message with a bold header: the first thing in a new room
    # should stand out, not be muted like a notice.
    await notice(
        app,
        room_id,
        f"{name}\n{_EXPLANATION}",
        html_message=f"<p><strong>{html.escape(name)}</strong></p><p>{html.escape(_EXPLANATION)}</p>",
        msgtype="m.text",
    )
    logger.info("Created notifications room %s for %s", room_id, matrix_user_id)
    return room_id
