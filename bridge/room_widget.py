"""Adds the bridge's widget (``bridge.widget``) to rooms, as the sibling
bridges do: on ``[widget``, and whenever the bridge creates a room. Each
room gets it once; a startup pass gives it to rooms made before the widget
existed, without putting it back where people removed it.

Sent under both ``m.widget`` and the legacy ``im.vector.modular.widgets``:
clients vary in which they read.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

WIDGET_STATE_TYPES = ("m.widget", "im.vector.modular.widgets")
WIDGET_NAME = "Nostr Bridge"


def _mark_key(room_id: str) -> str:
    return f"widget_added:{room_id}"


async def add_bridge_widget(app: FastAPI, room_id: str) -> bool:
    """Add the widget to ``room_id`` as the bot. Never raises."""
    config = app.state.config
    widget_id = f"nostr-bridge-{uuid.uuid4().hex[:12]}"
    content = {
        "type": "m.custom",
        "url": (
            f"{config.nostr.relay_http_base}/widget"
            f"?matrix_user_id=$matrix_user_id&matrix_room_id=$matrix_room_id&theme=$theme&widgetId={widget_id}"
        ),
        "name": WIDGET_NAME,
        "data": {"title": WIDGET_NAME},
    }
    if config.appservice.bot_avatar_mxc:
        content["avatar_url"] = config.appservice.bot_avatar_mxc
    ok = True
    for state_type in WIDGET_STATE_TYPES:
        try:
            await app.state.homeserver.send_state_event(
                room_id, state_type, content, state_key=widget_id, as_user_id=bot_mxid(config)
            )
        except HomeserverError as exc:
            ok = False
            logger.info("Could not add the widget (%s) to %s: %s", state_type, room_id, exc)
    if ok:
        await app.state.repository.set_state(_mark_key(room_id), widget_id)
    return ok


async def add_widget_once(app: FastAPI, room_id: str) -> None:
    if await app.state.repository.get_state(_mark_key(room_id)) is None:
        await add_bridge_widget(app, room_id)


async def add_missing_widgets(app: FastAPI) -> None:
    """At startup: every room the bridge made that never had the widget."""
    repository = app.state.repository
    rooms: list[str] = []
    for nostr_id in await repository.list_all_ids():
        if nostr_id.profile_room_id:
            rooms.append(nostr_id.profile_room_id)
        user_rooms = await repository.get_user_rooms(nostr_id.matrix_user_id)
        rooms += [r for r in (user_rooms.space_room_id, user_rooms.notifications_room_id) if r]
    rooms += [r.room_id for r in await repository.list_remote_accounts() if r.room_id]
    added = 0
    for room_id in dict.fromkeys(rooms):
        if await repository.get_state(_mark_key(room_id)) is None and await add_bridge_widget(app, room_id):
            added += 1
    if added:
        logger.info("Added the widget to %d room(s)", added)
