"""Sending things into Matrix as the bot."""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)


async def notice(
    app: FastAPI,
    room_id: str,
    message: str,
    *,
    html_message: str | None = None,
    extra: dict[str, Any] | None = None,
    msgtype: str = "m.notice",
) -> str | None:
    """Send a message as the bot (an ``m.notice`` unless ``msgtype`` says
    otherwise). ``message`` is the plain-text form; ``html_message`` adds a
    formatted one; ``extra`` adds content fields. Returns the event ID, or
    ``None`` if it couldn't be sent."""
    content: dict[str, Any] = {"msgtype": msgtype, "body": message}
    if html_message is not None:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = html_message
    if extra:
        content.update(extra)
    try:
        return await app.state.homeserver.send_message_event(room_id, content, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        logger.warning("Failed to send a message to %s", room_id, exc_info=True)
        return None


def room_link_html(room_id: str, label: str | None = None) -> str:
    return f'<a href="https://matrix.to/#/{html.escape(room_id)}">{html.escape(label or room_id)}</a>'


async def invite_to_room(app: FastAPI, room_id: str, user_id: str) -> None:
    """Invite ``user_id`` unless they're already in the room. Best-effort."""
    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    try:
        if user_id not in await homeserver.get_joined_members(room_id, as_user_id=bot):
            await homeserver.invite_user(room_id, user_id, as_user_id=bot)
    except HomeserverError as exc:
        # M_FORBIDDEN here is typically "already invited".
        logger.info("Invite of %s to %s: %s", user_id, room_id, exc)
