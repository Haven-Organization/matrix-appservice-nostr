"""Games' saved data, kept in Matrix so it follows the player.

A webxdc app saves the way any web page does, in ``localStorage`` (Flash
games under Ruffle keep their SharedObjects there). In the sandbox each
game gets a ``localStorage`` of its own (shim.js), loaded from here when the
game starts and sent back a moment after it changes.

A save is a hidden state event in the player's own Games room:
``software.haven.nostr.webxdc.save`` with the game's id as its state key,
holding the game's keys and values. One too big for an event goes up as a
file, and the event points at it. Only the player and the bridge's bot are
in that room, wherever the game was played.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

SAVE_STATE_TYPE = "software.haven.nostr.webxdc.save"
MAX_SAVE_BYTES = 5 * 1024 * 1024  # what a browser gives a page's localStorage
_INLINE_BYTES = 48 * 1024  # under Matrix's 64 KB event limit, with room to spare


def _clean(data: Any) -> dict[str, str] | None:
    if not isinstance(data, dict):
        return None
    out = {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    return out if len(json.dumps(out).encode()) <= MAX_SAVE_BYTES else None


async def load(app: FastAPI, user: str, game_id: str) -> dict[str, str] | None:
    """The game's saved data for ``user``, or None if there's none yet."""
    from bridge.games_room import _ROOM_KEY

    room = await app.state.repository.get_state(_ROOM_KEY.format(user))
    if not room:
        return None
    try:
        content = await app.state.homeserver.get_room_state(room, SAVE_STATE_TYPE, game_id, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        logger.info("Could not read the save of %s for %s", game_id, user, exc_info=True)
        return None
    if not content:
        return None
    if isinstance(content.get("url"), str):
        download = await app.state.homeserver.download_media(content["url"])
        if download is None:
            return None
        try:
            return _clean(json.loads(download.content))
        except ValueError:
            return None
    return _clean(content.get("data"))


async def store(app: FastAPI, user: str, game_id: str, data: Any) -> bool:
    """Keep the game's saved data for ``user``. False if it's too big or unusable."""
    from bridge.games_room import ensure_games_room

    cleaned = _clean(data)
    if cleaned is None:
        return False
    room, _new = await ensure_games_room(app, user)
    if room is None:
        return False
    bot = bot_mxid(app.state.config)
    encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode()
    content: dict[str, Any] = {"updated": int(time.time() * 1000)}
    try:
        if len(encoded) <= _INLINE_BYTES:
            content["data"] = cleaned
        else:
            content["url"] = await app.state.homeserver.upload_media(encoded, "application/json", f"{game_id}.save.json",
                                                                    as_user_id=bot)
            content["size"] = len(encoded)
        await app.state.homeserver.send_state_event(room, SAVE_STATE_TYPE, content, state_key=game_id, as_user_id=bot)
    except HomeserverError:
        logger.warning("Could not keep the save of %s for %s", game_id, user, exc_info=True)
        return False
    return True
