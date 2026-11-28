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


async def send_bridge_info(app: FastAPI, *, room_id: str, pubkey: str) -> None:
    """Tag ``room_id`` as bridged to the Nostr key ``pubkey``. Best-effort,
    and a no-op where the state already says this."""
    config = app.state.config
    bot = bot_mxid(config)
    # The channel is only its ID: a name or avatar here would go stale,
    # and keeping it current costs state events on every profile edit.
    channel: dict = {"id": pubkey}
    protocol: dict = {"id": PROTOCOL_ID, "displayname": PROTOCOL_DISPLAY_NAME}
    if config.appservice.bot_avatar_mxc:
        protocol["avatar_url"] = config.appservice.bot_avatar_mxc
    content = {"bridgebot": bot, "creator": bot, "protocol": protocol, "channel": channel}
    state_key = f"{PROTOCOL_ID}://{pubkey}"
    homeserver = app.state.homeserver
    for event_type in _EVENT_TYPES:
        try:
            if _same(await homeserver.get_room_state(room_id, event_type, state_key, as_user_id=bot), content):
                continue
            await homeserver.send_state_event(room_id, event_type, content, as_user_id=bot, state_key=state_key)
        except HomeserverError:
            logger.info("Could not set %s in %s", event_type, room_id, exc_info=True)


def _same(current: dict | None, content: dict) -> bool:
    """Whether ``current`` already says what ``content`` does. A channel
    name or avatar left by older versions doesn't count as a difference:
    rewriting every room just to drop them would be the noise they cost."""
    if not current:
        return False
    channel = {k: v for k, v in (current.get("channel") or {}).items() if k not in ("displayname", "avatar_url")}
    return {**current, "channel": channel} == content
