"""``[play`` and the Games room: where a user's webxdc games open.

``[play``, sent as a reply to a game post in any room, opens that game in
the sender's Games room: a room per Matrix user, made the first time, filed
in their space, and re-invited to if they left. The room has one game
widget at a time; ``[play`` on another game replaces it (the game's state is
on Nostr, so nothing is lost). The widget is centred
(``io.element.widgets.layout``). Games need a client with widgets.
"""

from __future__ import annotations

import html
import logging
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI

from bridge import webxdc
from bridge.homeserver_client import HomeserverError
from bridge.messages import invite_to_room, notice, room_link_html
from bridge.room_widget import WIDGET_STATE_TYPES
from bridge.spaces import add_room_to_space
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

GAME_WIDGET_ID = "nostr-game"
LAYOUT_STATE_TYPE = "io.element.widgets.layout"
GAMES_ROOM_NAME = "Nostr Games"
_ROOM_KEY = "webxdc:room:{}"
_EXPLANATION = (
    "Games open here, one at a time, as a widget. Games need a Matrix client that supports widgets well, "
    "such as Element Web or Element Desktop; most mobile clients don't. Reply \"{prefix}play\" to a game "
    "post anywhere to open it here. Moves are posted on Nostr as your Active identity."
)


async def ensure_games_room(app: FastAPI, matrix_user_id: str) -> tuple[str | None, bool]:
    """(the user's Games room, whether it was just made)."""
    repository = app.state.repository
    key = _ROOM_KEY.format(matrix_user_id)
    existing = await repository.get_state(key)
    if existing:
        return existing, False
    config = app.state.config
    try:
        room_id = await app.state.homeserver.create_room(
            as_user_id=bot_mxid(config), name=GAMES_ROOM_NAME, invite=[matrix_user_id], preset="private_chat",
            avatar_mxc=config.appservice.bot_avatar_mxc, power_level_users={matrix_user_id: 50},
        )
    except HomeserverError:
        logger.warning("Could not create a Games room for %s", matrix_user_id, exc_info=True)
        return None, False
    await repository.set_state(key, room_id)
    await add_room_to_space(app, matrix_user_id, room_id)
    prefix = config.appservice.command_prefix
    text = _EXPLANATION.format(prefix=prefix)
    await notice(app, room_id, f"{GAMES_ROOM_NAME}\n{text}",
                 html_message=f"<p><strong>{GAMES_ROOM_NAME}</strong></p><p>{html.escape(text)}</p>", msgtype="m.text")
    logger.info("Created Games room %s for %s", room_id, matrix_user_id)
    return room_id, True


def player_url(app: FastAPI, game: webxdc.App) -> str:
    base = app.state.config.nostr.relay_http_base
    return (f"{base}/xdc/play?game={quote(game.id, safe='')}"
            "&matrix_user_id=$matrix_user_id&matrix_room_id=$matrix_room_id&theme=$theme"
            f"&widgetId={GAME_WIDGET_ID}")


async def show_game(app: FastAPI, room_id: str, game: webxdc.App) -> None:
    """The room's game widget, now ``game``, centred."""
    config = app.state.config
    bot = bot_mxid(config)
    content: dict[str, Any] = {
        "type": "m.custom", "url": player_url(app, game), "name": f"🎮 {game.name}",
        "data": {"title": game.name, "game": game.id},
    }
    if config.appservice.bot_avatar_mxc:
        content["avatar_url"] = config.appservice.bot_avatar_mxc
    homeserver = app.state.homeserver
    for state_type in WIDGET_STATE_TYPES:
        await homeserver.send_state_event(room_id, state_type, content, state_key=GAME_WIDGET_ID, as_user_id=bot)
    try:
        await homeserver.send_state_event(
            room_id, LAYOUT_STATE_TYPE,
            {"widgets": {GAME_WIDGET_ID: {"container": "center", "index": 0, "width": 100, "height": 100}}},
            state_key="", as_user_id=bot,
        )
    except HomeserverError:
        logger.info("Could not lay the game out in %s", room_id, exc_info=True)


async def handle_play(app: FastAPI, room_id: str, sender: str, content: dict[str, Any]) -> None:
    from bridge.commands import reply_target_event_id
    from bridge.interactions import reposted_original

    config = app.state.config
    prefix = config.appservice.command_prefix
    if not config.webxdc.enabled:
        await notice(app, room_id, "Games aren't turned on on this bridge.")
        return
    target = reply_target_event_id(content)
    relates = content.get("m.relates_to") or {}
    if target is None and relates.get("rel_type") == "m.thread":
        target = relates.get("event_id")
    row = await app.state.repository.get_event_by_matrix_event(target) if target else None
    if row is not None and row.kind in (6, 16):
        row = await reposted_original(app, row) or row
    game = await webxdc.app_of_post(app, row) if row is not None else None
    if game is None:
        await notice(app, room_id, f'Reply "{prefix}play" to a game post (one with "🎮" on it) to open that game.')
        return
    if await webxdc.game_post(app, game.id) is None:
        await webxdc.register_games(app, (await _raw(app, row)) or {})
    games_room, new = await ensure_games_room(app, sender)
    if games_room is None:
        await notice(app, room_id, "I couldn't make your Games room just now; try again in a minute.")
        return
    try:
        await show_game(app, games_room, game)
    except HomeserverError:
        logger.warning("Could not set the game widget in %s", games_room, exc_info=True)
        await notice(app, room_id, "I couldn't open the game in your Games room just now; try again in a minute.")
        return
    await invite_to_room(app, games_room, sender)
    await webxdc.touch(app, game.id)
    first = " It needs a Matrix client with widgets, such as Element Web or Desktop." if new else ""
    await notice(
        app, room_id, f"Opened {game.name} in your Games room: {games_room}.{first}",
        html_message=f"Opened <strong>{html.escape(game.name)}</strong> in your {room_link_html(games_room, 'Games room')}."
        f"{html.escape(first)}",
    )


async def _raw(app: FastAPI, row) -> dict[str, Any] | None:
    from bridge.event_sources import rebuild

    rebuilt = await rebuild(app, row)
    return rebuilt.event if rebuilt.status == "ok" else None
