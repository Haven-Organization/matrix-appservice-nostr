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


async def handle_play(app: FastAPI, room_id: str, sender: str, content: dict[str, Any], argument: str = "") -> None:
    from bridge.commands import reply_target_event_id
    from bridge.interactions import reposted_original

    config = app.state.config
    prefix = config.appservice.command_prefix
    if not config.webxdc.enabled:
        await notice(app, room_id, "Games aren't turned on on this bridge.")
        return
    row = None
    if argument.strip():
        # A link to the game post (or its nevent/note/naddr): brought in
        # first, as [import does, so there's no need to find it in a room.
        from bridge.importing import ImportProblem, fetch_post, import_post, parse_post_reference

        try:
            row = await import_post(app, await fetch_post(app, parse_post_reference(argument)))
        except ImportProblem as exc:
            await notice(app, room_id, str(exc))
            return
    else:
        target = reply_target_event_id(content)
        relates = content.get("m.relates_to") or {}
        if target is None and relates.get("rel_type") == "m.thread":
            target = relates.get("event_id")
        row = await app.state.repository.get_event_by_matrix_event(target) if target else None
    if row is not None and row.kind in (6, 16):
        row = await reposted_original(app, row) or row
    game = await webxdc.app_of_post(app, row) if row is not None else None
    if game is None:
        await notice(app, room_id, f'Reply "{prefix}play" to a game post (one with "🎮" on it), or send '
                                   f'"{prefix}play <link to the game post>", to open that game.')
        return
    opened = await open_game(app, sender, game, row)
    if opened is None:
        await notice(app, room_id, "I couldn't open the game in your Games room just now; try again in a minute.")
        return
    games_room, new = opened
    if room_id == games_room:
        return  # its card is there
    first = " It needs a Matrix client with widgets, such as Element Web or Desktop." if new else ""
    await notice(
        app, room_id, f"Opened {game.name} in your Games room: {games_room}.{first}",
        html_message=f"Opened <strong>{html.escape(game.name)}</strong> in your {room_link_html(games_room, 'Games room')}."
        f"{html.escape(first)}",
    )


async def open_game(app: FastAPI, user: str, game: webxdc.App, row) -> tuple[str, bool] | None:
    """``game`` as the widget of ``user``'s Games room, with its card there
    (posted the first time only). (The room, whether it was just made), or
    None if it couldn't be done."""
    if await webxdc.game_post(app, game.id) is None:
        await webxdc.register_games(app, (await _raw(app, row)) or {})
    games_room, new = await ensure_games_room(app, user)
    if games_room is None:
        return None
    try:
        await show_game(app, games_room, game)
    except HomeserverError:
        logger.warning("Could not set the game widget in %s", games_room, exc_info=True)
        return None
    await invite_to_room(app, games_room, user)
    await webxdc.touch(app, game.id)
    try:
        await ensure_card(app, games_room, game, row)
    except Exception:
        logger.warning("Could not post the card of %s", game.id, exc_info=True)
    return games_room, new


async def _raw(app: FastAPI, row) -> dict[str, Any] | None:
    from bridge.event_sources import rebuild

    rebuilt = await rebuild(app, row)
    return rebuilt.event if rebuilt.status == "ok" else None


# -- the game's card ---------------------------------------------------------------------------

_CARD_KEY = "webxdc:card:{}"  # game -> its cartridge picture
_CARD_IN_ROOM = "webxdc:cardmsg:{}:{}"  # (room, game) -> its card there
_CARD_OF = "webxdc:cardof:{}"  # card -> the game post
_PLAY_REACTION = "webxdc:playreact:{}"  # a user's 🎮 on a card -> the game post
PLAY_KEY = "🎮"
_ICON_MAX_BYTES = 20 * 1024 * 1024


async def ensure_card(app: FastAPI, room_id: str, game: webxdc.App, row) -> str | None:
    """The game's card in ``room_id``: the one already there, or a new one."""
    repository = app.state.repository
    key = _CARD_IN_ROOM.format(room_id, game.id)
    existing = await repository.get_state(key)
    if existing:
        try:
            event = await app.state.homeserver.get_event(room_id, existing, as_user_id=bot_mxid(app.state.config))
        except HomeserverError:
            event = None
        if event and (event.get("content") or {}) and "redacted_because" not in (event.get("unsigned") or {}):
            return existing
    card = await post_card(app, room_id, game, row)
    if card is not None:
        await repository.set_state(key, card)
    return card


async def post_card(app: FastAPI, room_id: str, game: webxdc.App, row) -> str | None:
    """A card for a game: its cartridge picture (as Ditto shows games), name
    and description, shown as its author's post (MSC4144, ``external_url``),
    with the bot's 🎮 on it. Toggling your own 🎮 on it loads the game again
    (``maybe_handle_card_reaction``). It counts as a copy of the game post,
    so other reactions go to the post, and replying "[play" works too."""
    from bridge.inbound import _per_message_profile, _post_event_type, event_url

    config = app.state.config
    raw = await _raw(app, row) or {}
    mxc = await _cartridge(app, game)
    description = _description(raw, game)
    again = f"React {PLAY_KEY} to this to load it again."
    caption = f"🎮 {game.name}" + (f"\n{description}" if description else "") + f"\n\n{again}"
    formatted = (f"<p>🎮 <strong>{html.escape(game.name)}</strong></p>"
                 + (f"<p>{html.escape(description)}</p>".replace("\n", "<br>") if description else "")
                 + f"<p><em>{html.escape(again)}</em></p>")
    content: dict[str, Any] = {"body": caption, "format": "org.matrix.custom.html", "formatted_body": formatted}
    if mxc is not None:
        from bridge.cartridge import HEIGHT, WIDTH

        content.update({"msgtype": "m.image", "url": mxc, "filename": f"{_slug(game.name)}.png",
                        "info": {"mimetype": "image/png", "w": WIDTH, "h": HEIGHT}})
    else:
        content["msgtype"] = "m.text"
    if raw:
        content["external_url"] = event_url(app, raw)
    if config.bridge.msc4144_per_message_profiles:
        local = await app.state.repository.get_id_by_pubkey(row.pubkey)
        content["com.beeper.per_message_profile"] = (
            await _per_message_profile(app, local) if local is not None else await _ghost_profile(app, row.pubkey))
    homeserver = app.state.homeserver
    bot = bot_mxid(config)
    card = await homeserver.send_message_event(room_id, content, as_user_id=bot, event_type=_post_event_type(app))
    repository = app.state.repository
    await repository.put_mirrored_event(card, row.id, room_id)
    await repository.set_state(_CARD_OF.format(card), row.id)
    try:
        await homeserver.send_message_event(
            room_id, {"m.relates_to": {"rel_type": "m.annotation", "event_id": card, "key": PLAY_KEY}},
            as_user_id=bot, event_type="m.reaction")
    except HomeserverError:
        logger.info("Could not put %s on the card in %s", PLAY_KEY, room_id, exc_info=True)
    return card


async def maybe_handle_card_reaction(request, event: dict[str, Any]) -> bool:
    """A user's 🎮 on a game card, added or taken away, loads that game.
    Other reactions on a card fall through (they go to the game post)."""
    from bridge.users import is_bridge_user, is_local_user

    app = request.app
    config = app.state.config
    sender = event.get("sender", "")
    if not config.webxdc.enabled or is_bridge_user(config, sender) or not is_local_user(config, sender):
        return False
    repository = app.state.repository
    if event.get("type") == "m.reaction":
        relates = (event.get("content") or {}).get("m.relates_to") or {}
        if relates.get("rel_type") != "m.annotation" or str(relates.get("key", "")).rstrip("\ufe0f") != PLAY_KEY:
            return False
        card = relates.get("event_id")
        row_id = await repository.get_state(_CARD_OF.format(card)) if isinstance(card, str) else None
        if not row_id:
            return False
        await repository.set_state(_PLAY_REACTION.format(event.get("event_id", "")), row_id)
    elif event.get("type") == "m.room.redaction":
        redacted = event.get("redacts") or (event.get("content") or {}).get("redacts")
        key = _PLAY_REACTION.format(redacted) if isinstance(redacted, str) else None
        row_id = await repository.get_state(key) if key else None
        if not row_id:
            return False
        await repository.delete_state(key)
    else:
        return False
    row = await repository.get_event(row_id)
    game = await webxdc.app_of_post(app, row) if row is not None else None
    if game is not None and await open_game(app, sender, game, row) is None:
        await notice(app, event.get("room_id", ""), "I couldn't open the game just now; try again in a minute.")
    return True


async def _ghost_profile(app: FastAPI, pubkey: str) -> dict[str, Any]:
    from bridge import remote as remotes

    ghost = (await remotes.get_or_create_remote(app, pubkey)).ghost_user_id
    profile: dict[str, Any] = {"id": ghost}
    try:
        found = await app.state.homeserver.get_profile(ghost)
    except HomeserverError:
        found = {}
    if found.get("displayname"):
        profile["displayname"] = found["displayname"]
    if found.get("avatar_url"):
        profile["avatar_url"] = found["avatar_url"]
    return profile


def _description(raw: dict[str, Any], game: webxdc.App) -> str:
    text = str(raw.get("content") or "")
    text = "\n".join(line for line in text.splitlines() if game.url not in line and not line.strip().endswith(".xdc"))
    return text.strip()[:500]


def _slug(name: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:60] or "game"


async def _cartridge(app: FastAPI, game: webxdc.App) -> str | None:
    """The game's cartridge picture in Matrix (made once per game)."""
    from bridge.cartridge import render

    repository = app.state.repository
    key = _CARD_KEY.format(game.id)
    cached = await repository.get_state(key)
    if cached:
        return cached
    picture = render(game.id, await _label(app, game), game.name)
    try:
        mxc = await app.state.homeserver.upload_media(picture, "image/png", f"{_slug(game.name)}.png",
                                                      as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        logger.info("Could not upload the cartridge of %s", game.id, exc_info=True)
        return None
    await repository.set_state(key, mxc)
    return mxc


async def _label(app: FastAPI, game: webxdc.App) -> bytes | None:
    """The game's picture: the post's, or else the icon inside the app."""
    fetcher = app.state.fetcher
    if game.image:
        try:
            return (await fetcher.get(game.image, max_bytes=5 * 1024 * 1024)).content
        except Exception:
            logger.info("Could not fetch the picture of %s", game.id, exc_info=True)
    try:
        import io
        import zipfile

        archive = zipfile.ZipFile(io.BytesIO((await fetcher.get(game.url, max_bytes=_ICON_MAX_BYTES)).content))
        for name in ("icon.png", "icon.jpg"):
            if name in archive.namelist() and archive.getinfo(name).file_size <= 5 * 1024 * 1024:
                return archive.read(name)
    except Exception:
        logger.info("No icon found in %s", game.id, exc_info=True)
    return None
