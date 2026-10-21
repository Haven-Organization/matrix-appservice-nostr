"""Bridge-info state events: marking a room as bridged, and to what.

Every Profile Room carries a state event naming the bridge, the protocol
and the Nostr identity the room represents, so bridge-aware clients can
show that. Same shape as the sibling bridges'. Sent under both ``m.bridge``
(MSC2346) and the legacy ``uk.half-shot.bridge``, which Element's room
settings still read.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

_EVENT_TYPES = ("m.bridge", "uk.half-shot.bridge")
PROTOCOL_ID = "nostr"
PROTOCOL_DISPLAY_NAME = "Nostr"


async def send_bridge_info(
    app: FastAPI, *, room_id: str, pubkey: str, display_name: str | None, avatar_mxc: str | None
) -> None:
    """Tag ``room_id`` as bridged to the Nostr key ``pubkey``. Best-effort,
    and a no-op where the state already says this."""
    config = app.state.config
    bot = bot_mxid(config)
    channel: dict = {"id": pubkey}
    if display_name:
        channel["displayname"] = display_name
    if avatar_mxc:
        channel["avatar_url"] = avatar_mxc
    protocol: dict = {"id": PROTOCOL_ID, "displayname": PROTOCOL_DISPLAY_NAME}
    if config.appservice.bot_avatar_mxc:
        protocol["avatar_url"] = config.appservice.bot_avatar_mxc
    content = {"bridgebot": bot, "creator": bot, "protocol": protocol, "channel": channel}
    state_key = f"{PROTOCOL_ID}://{pubkey}"
    homeserver = app.state.homeserver
    for event_type in _EVENT_TYPES:
        try:
            if await homeserver.get_room_state(room_id, event_type, state_key, as_user_id=bot) == content:
                continue
            await homeserver.send_state_event(room_id, event_type, content, as_user_id=bot, state_key=state_key)
        except HomeserverError:
            logger.info("Could not set %s in %s", event_type, room_id, exc_info=True)
