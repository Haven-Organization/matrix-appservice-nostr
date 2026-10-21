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


async def notify(
    app: FastAPI,
    nostr_id,
    plain: str,
    rich: str,
    *,
    extra: dict | None = None,
    media: dict | None = None,
) -> str | None:
    """Tell the owner of ``nostr_id`` something in their notifications room.
    Once they have more than one ID, the message names the ID it is for.
    ``media`` (a Matrix media message's url/info/msgtype) makes the
    notification that image or video, with the text as its caption.
    Returns the Matrix event ID."""
    room_id = await ensure_notifications_room(app, nostr_id.matrix_user_id)
    if room_id is None:
        return None
    owned = await app.state.repository.list_ids_for_user(nostr_id.matrix_user_id)
    if len(owned) > 1:
        plain = f"{nostr_id.name}: {plain}"
        rich = f"<strong>{html.escape(nostr_id.name)}</strong>: {rich}"
    content: dict = {"msgtype": "m.text", "body": plain, "format": "org.matrix.custom.html", "formatted_body": rich}
    if media:
        content = {**media, "body": plain, "format": "org.matrix.custom.html", "formatted_body": rich}
    if extra:
        content.update(extra)
    try:
        return await app.state.homeserver.send_message_event(room_id, content, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        logger.warning("Could not notify %s", nostr_id.matrix_user_id, exc_info=True)
        return None
