"""The Nostr Games widget: an all-purpose place to find games and open them.

Every Games room has it, next to the game widget, and "[play" on its own
puts it back. It signs in like the other widgets (the client's OpenID
token, traded for a bridge session) and offers:

- **Open**: a link to a game post, or its nevent/naddr, brought in as
  "[play <link>" does.
- **On Nostr**: the games announced on Nostr, as Ditto's and poster.place's
  galleries list them: NIP-94 files (kind 1063) of any webxdc type, and
  notes, polls, comments and issues tagged "#webxdc", from
  ``webxdc.catalog_relays`` and the bridge's own relays. Signatures are
  checked, and a game announced more than once is credited to its first
  announcement. Kept for 15 minutes.
- **In this room**: game posts and cards in the room's recent timeline,
  its pinned events and widgets, and .xdc files uploaded to it (a room's
  own games, played by its members without Nostr).

A game picked in the widget plays right there (the player in a frame of
its own, signed in with the widget's session), with a way back to the
list; its card goes in the room (bridge.games_room). Opening is for anyone
who may change the room's widgets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, Response

from bridge import games_room, webxdc
from bridge.homeserver_client import HomeserverError
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

router = APIRouter()

CATALOG_SECONDS = 15 * 60
CATALOG_LIMIT = 300
POST_KINDS = [1, 1068, 1111, 1621]
_TIMELINE_EVENTS = 1000
_PINNED = 50
_render = asyncio.Semaphore(3)


def _enabled(request: Request) -> None:
    if not request.app.state.config.webxdc.enabled:
        raise HTTPException(404, "Games aren't turned on here.")


def _user(request: Request) -> str:
    from bridge.widget import _session_user

    _enabled(request)
    return _session_user(request)


# -- on Nostr ---------------------------------------------------------------------------------


def _catalog_state(app) -> dict[str, Any]:
    return app.state.__dict__.setdefault("games_catalog", {"at": 0.0, "items": [], "raw": {}, "apps": {}, "lock": None})


async def catalog(app, *, refresh: bool = False) -> list[dict[str, Any]]:
    state = _catalog_state(app)
    if state["lock"] is None:
        state["lock"] = asyncio.Lock()
    async with state["lock"]:
        if not refresh and state["items"] and time.time() - state["at"] < CATALOG_SECONDS:
            return state["items"]
        await _load_catalog(app, state)
        return state["items"]


async def _load_catalog(app, state: dict[str, Any]) -> None:
    from bridge.nostr.relay_pool import normalize_relay_url
    from bridge.relay_server import query_events

    config = app.state.config
    relays = list(dict.fromkeys(normalize_relay_url(r) for r in [*config.webxdc.catalog_relays, *config.nostr.publish_relays]))
    filters = [{"kinds": [1063], "#m": sorted(webxdc.MIMES), "limit": CATALOG_LIMIT},
               {"kinds": POST_KINDS, "#t": ["webxdc"], "limit": CATALOG_LIMIT}]
    found = await app.state.relay_pool.query(relays, filters, timeout=10.0)
    try:
        found += await query_events(app, filters, limit=CATALOG_LIMIT)
    except Exception:
        logger.info("Could not read the bridge's own relay for games", exc_info=True)
    games: dict[str, dict[str, Any]] = {}
    for raw in {e.get("id"): e for e in found if isinstance(e, dict)}.values():
        try:
            nostr_event.verify(raw)
        except (nostr_event.InvalidEvent, ValueError, KeyError, TypeError):
            continue
        for game in webxdc.apps_of(raw):
            entry = games.get(game.id)
            if entry is None:
                games[game.id] = {"game": game, "raw": raw, "newest": raw["created_at"]}
                continue
            entry["newest"] = max(entry["newest"], raw["created_at"])
            if raw["created_at"] < entry["raw"]["created_at"]:
                # The first announcement is credited (and its file used), as
                # poster.place does: a later copy can't take a game over.
                image = entry["game"].image
                entry["game"], entry["raw"] = game, raw
                if game.image is None and image:
                    entry["game"] = webxdc.App(game.id, game.url, game.sha256, game.name, image)
            elif entry["game"].image is None and game.image:
                old = entry["game"]
                entry["game"] = webxdc.App(old.id, old.url, old.sha256, old.name, game.image)
    names = await _names(app, relays, sorted({e["raw"]["pubkey"] for e in games.values()}))
    items = []
    for game_id, entry in sorted(games.items(), key=lambda kv: -kv[1]["newest"]):
        raw, game = entry["raw"], entry["game"]
        items.append({
            "id": game_id, "name": game.name, "description": games_room._description(raw, game),
            "author": {"pubkey": raw["pubkey"], "name": names.get(raw["pubkey"]) or nip19.npub_encode(raw["pubkey"])[:16] + "…"},
            "at": raw["created_at"], "local": False,
        })
    state.update(at=time.time(), items=items, raw={k: v["raw"] for k, v in games.items()},
                 apps={k: v["game"] for k, v in games.items()})
    logger.info("Games on Nostr: %d", len(items))


async def _names(app, relays: list[str], pubkeys: list[str]) -> dict[str, str]:
    if not pubkeys:
        return {}
    config = app.state.config
    profiles = await app.state.relay_pool.query(
        list(dict.fromkeys([*relays, *config.nostr.discovery_relays])), [{"kinds": [0], "authors": pubkeys}], timeout=8.0)
    newest: dict[str, dict[str, Any]] = {}
    for raw in profiles:
        if raw.get("pubkey") in pubkeys and raw.get("created_at", 0) > newest.get(raw["pubkey"], {}).get("created_at", -1):
            newest[raw["pubkey"]] = raw
    out = {}
    for pubkey, raw in newest.items():
        try:
            profile = json.loads(raw.get("content") or "{}")
        except ValueError:
            continue
        name = profile.get("display_name") or profile.get("name") if isinstance(profile, dict) else None
        if isinstance(name, str):
            # Custom emoji (NIP-30) can't be drawn here: one-letter ones (a
            # name spelled in emoji letters) become the letter, others go.
            name = re.sub(r":([A-Za-z0-9_+-]+):", lambda m: m.group(1) if len(m.group(1)) == 1 else "", name).strip()
            if name:
                out[pubkey] = name[:60]
    return out


# -- in a room --------------------------------------------------------------------------------


async def room_games(app, room_id: str) -> list[dict[str, Any]]:
    """The games in a room: pinned ones, those in its widgets, and game
    posts, cards and .xdc files in its recent timeline (newest first)."""
    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    items: dict[str, dict[str, Any]] = {}

    async def add(event: dict[str, Any], source: str) -> None:
        content = event.get("content") or {}
        field = content.get(webxdc.GAME_FIELD)
        if isinstance(field, dict) and isinstance(field.get("id"), str):
            game_id, name, local = field["id"], str(field.get("name") or "a game"), False
        elif webxdc.is_xdc_file(content):
            game = await webxdc.register_local(app, room_id, event)
            if game is None:
                return
            game_id, name, local = game.id, game.name, True
        else:
            return
        if game_id not in items and not local:
            # A card or post: the game itself says what it's called now, and
            # whether it's a room's own.
            game, _row, where = await webxdc.game_info(app, game_id)
            if game is not None:
                name, local = game.name, where is not None
        item = items.setdefault(game_id, {"id": game_id, "name": name, "local": local, "sources": []})
        if source not in item["sources"]:
            item["sources"].append(source)

    try:
        pinned = ((await homeserver.get_room_state(room_id, "m.room.pinned_events", as_user_id=bot)) or {}).get("pinned") or []
        for event_id in [p for p in pinned if isinstance(p, str)][:_PINNED]:
            event = await homeserver.get_event(room_id, event_id, as_user_id=bot)
            if event:
                await add(event, "pinned")
        for state in await homeserver.get_full_state(room_id, as_user_id=bot):
            data = (state.get("content") or {}).get("data") or {}
            if state.get("type") in games_room.WIDGET_STATE_TYPES and isinstance(data.get("game"), str):
                game, _row, _local = await webxdc.game_info(app, data["game"])
                if game is not None:
                    await add({"content": {webxdc.GAME_FIELD: webxdc.game_field(game)}}, "widget")
        token, seen = None, 0
        while seen < _TIMELINE_EVENTS:
            page = await homeserver.get_messages(room_id, as_user_id=bot, limit=100, from_token=token)
            chunk = page.get("chunk") or []
            for event in chunk:
                await add(event, "timeline")
            seen += len(chunk)
            token = page.get("end")
            if not token or not chunk:
                break
    except HomeserverError:
        logger.info("Could not read the games in %s", room_id, exc_info=True)
    return list(items.values())


# -- opening ----------------------------------------------------------------------------------


async def _resolve(app, game_id: str):
    """(the game, its Nostr post row, where it is if it's a room's own)."""
    from bridge.importing import ImportProblem, import_post

    game, row, local = await webxdc.game_info(app, game_id)
    if game is not None:
        return game, row, local
    raw = _catalog_state(app)["raw"].get(game_id)
    if raw is None:
        return None, None, None
    try:
        row = await import_post(app, raw)
    except ImportProblem:
        return None, None, None
    game = await webxdc.app_of_post(app, row) if row is not None else None
    return game, row, None


# -- routes -----------------------------------------------------------------------------------


@router.get("/xdc/hub")
async def hub_page(request: Request) -> HTMLResponse:
    _enabled(request)
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})


@router.get("/xdc/api/catalog")
async def catalog_route(request: Request, refresh: bool = False) -> dict[str, Any]:
    _user(request)
    return {"games": await catalog(request.app, refresh=refresh)}


@router.get("/xdc/api/room")
async def room_route(request: Request, room_id: str) -> dict[str, Any]:
    user = _user(request)
    if not await webxdc.in_room(request.app, room_id, user):
        raise HTTPException(403, "The bridge's bot needs to be in this room, and you too, to see its games.")
    return {"games": await room_games(request.app, room_id),
            "can_open": await games_room.can_set_widgets(request.app, room_id, user)}


@router.post("/xdc/api/open")
async def open_route(request: Request) -> dict[str, Any]:
    from bridge.importing import ImportProblem, fetch_post, import_post, parse_post_reference

    app = request.app
    user = _user(request)
    body = await request.json()
    room_id = str(body.get("room_id") or "")
    if not await games_room.can_set_widgets(app, room_id, user):
        raise HTTPException(403, "Only those who can change this room's widgets can open games here.")
    link, game_id = str(body.get("link") or "").strip(), str(body.get("game") or "")
    local = None
    if link:
        try:
            row = await import_post(app, await fetch_post(app, parse_post_reference(link)))
        except ImportProblem as exc:
            raise HTTPException(400, str(exc)) from exc
        if row is not None and row.kind in (6, 16):
            from bridge.interactions import reposted_original

            row = await reposted_original(app, row) or row
        game = await webxdc.app_of_post(app, row) if row is not None else None
        if game is None:
            raise HTTPException(400, "That post doesn't have a game in it.")
    else:
        game, row, local = await _resolve(app, game_id)
        if game is None:
            raise HTTPException(404, "That game couldn't be found.")
    if body.get("in_hub"):
        # Picked in the widget, which plays it itself: only its card here.
        await games_room.game_opened(app, room_id, game, row, local)
    elif not await games_room.open_in_room(app, room_id, game, row, local, user=user):
        raise HTTPException(502, "The game couldn't be opened just now; try again in a minute.")
    return {"message": f"Opened {game.name}.", "game": game.id, "name": game.name}


# -- open widgets, told what to play ---------------------------------------------------------

_PENDING_SECONDS = 600


def _live(app) -> dict[str, list[tuple[str, Any]]]:
    return app.state.__dict__.setdefault("games_hub_live", {})  # room -> [(user, send)]


def _pending(app) -> dict[tuple[str, str], tuple[dict[str, Any], float]]:
    return app.state.__dict__.setdefault("games_hub_pending", {})  # (room, user) -> (message, when)


def announce(app, room_id: str, user: str | None, game: webxdc.App, tab: str) -> None:
    """Tell ``user``'s open Nostr Games widgets in ``room_id`` to play
    ``game`` (anyone's, with no user). It's also kept until a widget says it
    has it, so a widget opened in the next few minutes plays it if none was
    open (or the one that was was closing)."""
    message = {"t": "play", "game": game.id, "name": game.name, "tab": tab if tab in ("nostr", "room") else "nostr"}
    if user is not None:
        _pending(app)[(room_id, user)] = (message, time.time())
    for who, send in list(_live(app).get(room_id, [])):
        if user is None or who == user:
            asyncio.ensure_future(send(message))


@router.websocket("/xdc/hub/ws")
async def hub_socket(websocket: WebSocket) -> None:
    from fastapi import WebSocketDisconnect

    from bridge.widget import user_of_session

    app = websocket.app
    await websocket.accept()
    user = user_of_session(websocket.query_params.get("session", ""))
    room_id = websocket.query_params.get("room_id", "")
    if not app.state.config.webxdc.enabled or user is None:
        await websocket.send_json({"t": "error", "error": "Session expired."})
        await websocket.close()
        return
    if not await webxdc.in_room(app, room_id, user):
        await websocket.send_json({"t": "error", "error": "Not in that room."})
        await websocket.close()
        return

    async def send(message: dict[str, Any]) -> None:
        try:
            await websocket.send_json(message)
        except Exception:
            pass

    entry = (user, send)
    _live(app).setdefault(room_id, []).append(entry)
    waiting = _pending(app).pop((room_id, user), None)
    if waiting is not None and time.time() - waiting[1] < _PENDING_SECONDS:
        await send(waiting[0])
    try:
        while True:
            text = await websocket.receive_text()  # pings, and "got it" for a game it was told to play
            if text.startswith("got:"):
                waiting = _pending(app).get((room_id, user))
                if waiting is not None and waiting[0]["game"] == text[4:]:
                    _pending(app).pop((room_id, user), None)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        listeners = _live(app).get(room_id, [])
        if entry in listeners:
            listeners.remove(entry)
        if not listeners:
            _live(app).pop(room_id, None)


@router.post("/xdc/api/close")
async def close_route(request: Request) -> dict[str, Any]:
    """Back to the list from a game the widget was opened into ([play, a
    card's 🎮): the widget points at the list again, so it doesn't reopen
    that game the next time the client loads it."""
    app = request.app
    user = _user(request)
    room_id = str((await request.json()).get("room_id") or "")
    if not await games_room.can_set_widgets(app, room_id, user):
        raise HTTPException(403, "Only those who can change this room's widgets can do that.")
    await games_room.ensure_hub(app, room_id)
    return {"ok": True}


@router.get("/xdc/api/cartridge")
async def cartridge_route(request: Request, game: str) -> Response:
    """A game's cartridge picture (made once, kept in Matrix)."""
    app = request.app
    user = _user(request)
    info, _row, local = await webxdc.game_info(app, game)
    if info is None:
        info = _catalog_state(app)["apps"].get(game)
    if info is None:
        raise HTTPException(404, "No such game here.")
    if local is not None and not await webxdc.in_room(app, local["room"], user):
        raise HTTPException(403, "That game is only for the members of its room.")
    async with _render:
        mxc = await games_room._cartridge(app, info)
    download = await app.state.homeserver.download_media(mxc) if mxc else None
    if download is None:
        raise HTTPException(502, "The picture couldn't be made just now.")
    return Response(download.content, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nostr Games</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --text:#17191c; --muted:#6b7280; --line:#e3e6ea; --accent:#8e2fe0; --accent-text:#fff; --err:#d93b3b; }
  :root[data-theme="dark"] { --bg:#15191e; --card:#1f252c; --text:#e7e9ec; --muted:#9aa3ad; --line:#2d353e; --accent:#a855f7; --err:#f97066; }
  * { box-sizing:border-box; }
  html, body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { position:sticky; top:0; z-index:2; background:var(--bg); padding:12px 16px 8px; border-bottom:1px solid var(--line); }
  form { display:flex; gap:8px; }
  input { flex:1; min-width:0; padding:8px 10px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--text); font:inherit; }
  button { padding:8px 14px; border-radius:8px; border:0; background:var(--accent); color:var(--accent-text); font:inherit; font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; color:var(--muted); border:1px solid var(--line); font-weight:500; }
  button:disabled { opacity:.6; cursor:default; }
  nav { display:flex; gap:6px; margin-top:10px; align-items:center; }
  nav button { background:transparent; color:var(--muted); border:1px solid transparent; font-weight:600; }
  nav button.on { color:var(--text); border-color:var(--line); background:var(--card); }
  nav .grow { flex:1; }
  nav input { flex:0 1 200px; padding:6px 10px; }
  main { padding:14px 16px 24px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:14px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:10px; cursor:pointer; display:flex; flex-direction:column; gap:6px; transition:transform .08s; }
  .tile:hover { transform:translateY(-2px); border-color:var(--accent); }
  .tile .pic { aspect-ratio:480/560; width:100%; border-radius:8px; background:var(--bg); object-fit:contain; }
  .tile .name { font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tile .by, .tile .desc { color:var(--muted); font-size:12px; overflow:hidden; display:-webkit-box; -webkit-box-orient:vertical; }
  .tile .by { -webkit-line-clamp:1; }
  .tile .desc { -webkit-line-clamp:2; }
  .badge { display:inline-block; font-size:11px; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:0 7px; margin-right:4px; }
  .note { color:var(--muted); padding:24px 4px; text-align:center; }
  #playing { position:fixed; inset:0; display:flex; flex-direction:column; background:#000; z-index:5; }
  #playing[hidden] { display:none; }
  #playing .bar { display:flex; align-items:center; gap:10px; padding:6px 10px; background:var(--bg); border-bottom:1px solid var(--line); }
  #playing .bar button { padding:4px 10px; }
  #title { font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #player { flex:1; width:100%; border:0; background:#000; }
  .toast { position:fixed; left:50%; bottom:16px; transform:translateX(-50%); background:var(--card); border:1px solid var(--line); padding:8px 14px; border-radius:10px; display:none; box-shadow:0 4px 18px rgba(0,0,0,.18); }
  .toast.show { display:block; }
  .toast.err { color:var(--err); }
</style>
</head>
<body>
<header>
  <form id="open"><input id="link" placeholder="Paste a link, nevent or naddr to a game post" autocomplete="off"><button>Open</button></form>
  <nav>
    <button data-tab="nostr" class="on">On Nostr</button>
    <button data-tab="room">In this room</button>
    <span class="grow"></span>
    <input id="filter" placeholder="Search" autocomplete="off">
    <button id="refresh" class="ghost" title="Look again">↻</button>
  </nav>
</header>
<main><div id="list" class="note">Signing in&hellip;</div></main>
<div id="playing" hidden>
  <div class="bar"><button id="back" class="ghost">← All games</button><span id="title"></span></div>
  <iframe id="player" allow="autoplay; fullscreen; clipboard-read; clipboard-write" allowfullscreen></iframe>
</div>
<div id="toast" class="toast"></div>
<script src="/xdc/static/hub.js"></script>
</body>
</html>
"""
