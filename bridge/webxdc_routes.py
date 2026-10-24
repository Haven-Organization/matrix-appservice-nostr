"""The webxdc player and sandbox, over HTTP. See bridge.webxdc and DESIGN.md.

On the bridge's own host:

- ``GET /xdc/play``: the player page (the Games room widget).
- ``GET /xdc/static/<file>``: its scripts.
- ``WS /xdc/ws?game=&session=``: one player's connection for one game.
- ``GET /xdc/app?game=``: the game's ``.xdc``, fetched by the bridge for a
  browser that can't fetch it from its host (no CORS). Never stored.

On the sandbox host (``webxdc.sandbox_domain``), ``SandboxHost`` answers
everything, and serves only ``/__sandbox__/`` (the loader) and ``/sw.js``
(its service worker): a different origin from the bridge and the
homeserver, with nothing on it an app could reach.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from bridge import webxdc

logger = logging.getLogger(__name__)

router = APIRouter()
STATIC = Path(__file__).parent / "static" / "webxdc"
APP_CSP = ("default-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' data: blob:; "
           "connect-src 'self' data: blob:; base-uri 'self'; form-action 'none'")
_SCRIPTS = {"zip.js", "player.js", "shim.js", "hub.js"}


def _enabled(request: Request | WebSocket) -> None:
    if not request.app.state.config.webxdc.enabled:
        raise HTTPException(404, "Games aren't turned on here.")


def sandbox_origin(config: Any) -> str:
    """The sandbox's origin: the bridge's scheme and port, its own host."""
    from urllib.parse import urlsplit

    base = urlsplit(config.nostr.relay_http_base)
    port = f":{base.port}" if base.port else ""
    return f"{base.scheme}://{config.webxdc.sandbox_domain}{port}"


# -- the sandbox host ----------------------------------------------------------------

_RELOAD_ONCE = (b"<!doctype html><meta charset=utf-8><script>if(location.hash.indexOf('xdc-retry')<0)"
                b"{location.hash='xdc-retry';location.reload()}</script>")


class SandboxHost:
    """Requests for the sandbox hostname never reach the bridge's routes."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_sandbox(scope):
            await self._app(scope, receive, send)
            return
        config = scope["app"].state.config
        path = scope.get("path", "")
        if path == "/__sandbox__/" or path == "/__sandbox__":
            body = (STATIC / "sandbox.html").read_bytes()
            # No frame-ancestors: the player is framed by whatever Matrix client
            # the user runs, and frame-ancestors is checked against every frame
            # above. The loader takes an app only from its direct parent's first
            # message, and this origin holds nothing but games' own saved data.
            headers = {
                "content-type": "text/html; charset=utf-8", "cache-control": "no-store",
                # The same as for the app's own files (a blob: page made here, for
                # browsers without service workers, inherits it): its own origin,
                # blob: and data:, and no host at all.
                "content-security-policy": APP_CSP,
            }
        elif path == "/sw.js":
            body = (STATIC / "sw.js").read_bytes()
            headers = {"content-type": "text/javascript", "cache-control": "no-store", "service-worker-allowed": "/"}
        else:
            # A navigation that beat the service worker to control: reload once.
            body = _RELOAD_ONCE
            headers = {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"}
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _is_sandbox(scope: Scope) -> bool:
        app = scope.get("app")
        config = getattr(getattr(app, "state", None), "config", None)
        if config is None or not config.webxdc.enabled:
            return False
        host = next((v.decode() for k, v in scope.get("headers") or [] if k == b"host"), "")
        return host.split(":", 1)[0].lower() == config.webxdc.sandbox_domain


# -- the player ----------------------------------------------------------------------


@router.get("/xdc/play")
async def play_page(request: Request) -> HTMLResponse:
    _enabled(request)
    config = request.app.state.config
    settings = json.dumps({"sandboxOrigin": sandbox_origin(config)})
    page = _PAGE.replace("__SETTINGS__", settings)
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/xdc/static/{name}")
async def static(request: Request, name: str) -> Response:
    _enabled(request)
    if name not in _SCRIPTS:
        raise HTTPException(404)
    return Response((STATIC / name).read_bytes(), media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@router.get("/xdc/app")
async def app_file(request: Request, game: str) -> Response:
    """The game's .xdc for a browser that can't fetch it from its host."""
    from bridge.safe_fetch import FetchError, UnsafeAddress
    from bridge.widget import _session_user

    _enabled(request)
    user = _session_user(request)
    info, _post, local = await webxdc.game_info(request.app, game)
    if info is None:
        raise HTTPException(404, "No such game here.")
    if local is not None:
        # A room's own game: from the homeserver, for the room's members.
        if not await webxdc.in_room(request.app, local["room"], user):
            raise HTTPException(403, "That game is only for the members of its room.")
        download = await request.app.state.homeserver.download_media(info.url)
        if download is None:
            raise HTTPException(502, "Couldn't get the game from the homeserver.")
        return Response(download.content, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
    limit = request.app.state.config.webxdc.max_app_mb * 1024 * 1024
    try:
        fetched = await request.app.state.fetcher.get(info.url, max_bytes=limit)
    except (FetchError, UnsafeAddress) as exc:
        raise HTTPException(502, f"Couldn't fetch the game: {exc}") from exc
    if fetched.status != 200:
        raise HTTPException(502, f"The game's host answered {fetched.status}.")
    return Response(fetched.content, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


@router.websocket("/xdc/ws")
async def player_socket(websocket: WebSocket) -> None:
    from bridge import ids
    from bridge import remote as remotes
    from bridge.nostr import nip19
    from bridge.widget import user_of_session

    app = websocket.app
    await websocket.accept()

    async def say(message: dict[str, Any]) -> None:
        try:
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            pass

    config = app.state.config
    user = user_of_session(websocket.query_params.get("session", ""))
    game_id = websocket.query_params.get("game", "")
    if not config.webxdc.enabled or user is None:
        await say({"t": "error", "error": "Sign-in expired; reload the game."})
        await websocket.close()
        return
    info, post, local = await webxdc.game_info(app, game_id)
    if info is None or (local is not None and not await webxdc.in_room(app, local["room"], user)):
        await say({"t": "error", "error": "That game isn't here any more." if info is None
                   else "That game is only for the members of its room."})
        await websocket.close()
        return
    player = await ids.active_id(app, user)
    if local is not None:
        # A room's own game: players are who they are in Matrix.
        relays: list[str] = []
        try:
            display = (await app.state.homeserver.get_profile(user)).get("displayname")
        except Exception:
            display = None
        me = {"addr": user, "name": str(display or user)}
        moves = await webxdc.local_updates(app, game_id, local)
    else:
        author = await app.state.repository.get_remote_account(post.pubkey)
        relays = list(remotes.write_relays_for(app, author)[:3]) if author else list(config.nostr.publish_relays[:3])
        me = {"addr": nip19.npub_encode(player.pubkey) if player else user,
              "name": f"{player.name}@{config.bridge.domain}" if player else user}
        moves = await webxdc.updates(app, game_id)
    session = webxdc.Session(game_id, say, relays=relays, local=local is not None)
    hub = webxdc.hub(app)
    hub.add(session)
    await webxdc.touch(app, game_id)
    await say({"t": "init", "game": webxdc.game_field(info), "self": me, "updates": moves})
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            kind = message.get("t")
            if kind == "send" and local is not None:
                sent = await webxdc.send_local_update(app, user, game_id, local, message.get("update"),
                                                      str(message.get("descr") or ""))
                await say({"t": "ack", "id": message.get("id"), "ok": sent is not None,
                           **({} if sent else {"error": "That move couldn't be sent."})})
            elif kind == "send":
                if player is None:
                    await say({"t": "ack", "id": message.get("id"), "ok": False,
                               "error": "You need a Nostr ID to play: send [create profile to the bot."})
                    continue
                sent = await webxdc.send_update(app, player, game_id, message.get("update"), str(message.get("descr") or ""))
                await say({"t": "ack", "id": message.get("id"), "ok": sent is not None,
                           **({} if sent else {"error": "That move couldn't be sent."})})
            elif kind == "rt_join":
                session.realtime = True
                app.state.feeds.changed()
                await say({"t": "ack", "id": message.get("id"), "ok": True})
            elif kind == "rt_send" and session.realtime:
                try:
                    data = base64.b64decode(str(message.get("b64") or ""), validate=True)
                except ValueError:
                    continue
                await webxdc.send_realtime(app, session, data)
            elif kind == "rt_leave":
                session.realtime = False
                app.state.feeds.changed()
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        hub.remove(session)
        if session.realtime:
            app.state.feeds.changed()


_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nostr game</title>
<style>
  html, body { margin:0; height:100%; background:#000; color:#ccc; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  #game { position:absolute; inset:0; width:100%; height:100%; border:0; display:block; }
  #status { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; text-align:center; padding:24px; z-index:1; pointer-events:none; }
  #status.err { color:#f97066; }
</style>
</head>
<body>
<div id="status">Loading&hellip;</div>
<script>window.__PLAYER = __SETTINGS__;</script>
<script src="/xdc/static/zip.js"></script>
<script src="/xdc/static/player.js"></script>
</body>
</html>
"""
