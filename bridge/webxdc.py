"""Games: webxdc mini apps over Nostr (NIP-DC). See DESIGN.md, "Games".

- A **game** is a post with a ``.xdc`` attachment: an ``imeta`` with
  ``m application/x-webxdc`` and a ``webxdc`` identifier. The identifier is
  the game; ``register_games`` maps it to the post when the post is stored.
- A **move** is a kind 4932 with the identifier in an ``i`` tag. Moves live
  in Matrix as hidden events (``UPDATE_EVENT_TYPE``, carrying the signed
  event) in a thread under the game post, in the post's home room.
- **Realtime** packets are kind 20932: ephemeral, never stored, signed by a
  key made per player session. They are handed to whoever is playing the
  game here and to the bridge's relay's subscribers.

Players' sessions (``Session``) are the player page's websocket; ``Hub``
keeps them by game, so a move or packet reaches everyone playing it here at
once, without a round trip through a public relay.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.nostr import event as nostr_event
from bridge.nostr import secp256k1
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

MIME = "application/x-webxdc"
UPDATE_KIND = 4932
REALTIME_KIND = 20932
UPDATE_EVENT_TYPE = "software.haven.nostr.webxdc.update"
# On a game post's Matrix event: what the player needs to start it.
GAME_FIELD = "software.haven.nostr.webxdc"
_GAME_KEY = "webxdc:game:{}"
_ACTIVE_KEY = "webxdc:active:{}"
_REALTIME_MAX = 128_000


@dataclass(frozen=True)
class App:
    id: str
    url: str
    sha256: str | None
    name: str


def apps_of(raw: dict[str, Any]) -> list[App]:
    """The webxdc apps a post carries (NIP-DC ``imeta``), with an identifier."""
    found: list[App] = []
    groups: list[dict[str, str]] = []
    if raw.get("kind") == 1063:
        # NIP-94 file metadata (NIP-DC's other form, which Ditto uses): the
        # same fields as flat tags.
        flat: dict[str, str] = {}
        for tag in raw.get("tags") or []:
            if isinstance(tag, list) and len(tag) >= 2 and isinstance(tag[1], str):
                flat.setdefault(tag[0], tag[1].strip())
        groups.append(flat)
    for tag in raw.get("tags") or []:
        if not isinstance(tag, list) or not tag or tag[0] != "imeta":
            continue
        fields: dict[str, str] = {}
        for item in tag[1:]:
            if isinstance(item, str) and " " in item:
                key, _, value = item.partition(" ")
                fields.setdefault(key, value.strip())
        groups.append(fields)
    for fields in groups:
        url, game_id = fields.get("url", ""), fields.get("webxdc", "")
        if fields.get("m") != MIME or not url.startswith(("https://", "http://")) or not game_id or len(game_id) > 128:
            continue
        name = fields.get("alt", "")
        if name.lower().startswith("webxdc app:"):
            name = name.split(":", 1)[1].strip()
        if not name:
            name = url.rsplit("/", 1)[-1].split("?", 1)[0]
            name = name[:-4] if name.lower().endswith(".xdc") else "a game"
        sha = fields.get("x", "")
        found.append(App(game_id, url, sha.lower() if len(sha) == 64 else None, name[:80]))
    return found


def game_line(app_info: App, prefix: str) -> tuple[str, str]:
    """The line a game post shows in Matrix: (plain, html)."""
    import html

    plain = f"🎮 {app_info.name}: reply {prefix}play to open"
    rich = f"🎮 <strong>{html.escape(app_info.name)}</strong>: reply <code>{html.escape(prefix)}play</code> to open"
    return plain, rich


def game_field(app_info: App) -> dict[str, Any]:
    return {"id": app_info.id, "url": app_info.url, "x": app_info.sha256, "name": app_info.name}


async def register_games(app: FastAPI, raw: dict[str, Any]) -> None:
    """Note which game ids this stored post starts (the first post wins)."""
    if not app.state.config.webxdc.enabled:
        return
    repository = app.state.repository
    for game in apps_of(raw):
        key = _GAME_KEY.format(game.id)
        if await repository.get_state(key) is None:
            await repository.set_state(key, raw["id"])


async def game_post(app: FastAPI, game_id: str):
    """The game's post in Matrix (an ``EventRow`` with a room), or None."""
    repository = app.state.repository
    post_id = await repository.get_state(_GAME_KEY.format(game_id))
    row = await repository.get_event(post_id) if post_id else None
    return row if row is not None and row.room_id and row.matrix_event_id else None


async def app_of_post(app: FastAPI, row) -> App | None:
    from bridge.event_sources import rebuild

    rebuilt = await rebuild(app, row)
    if rebuilt.status != "ok" or rebuilt.event is None:
        return None
    games = apps_of(rebuilt.event)
    return games[0] if games else None


async def touch(app: FastAPI, game_id: str) -> None:
    """The game was opened now: follow its moves for ``active_days``."""
    await app.state.repository.set_state(_ACTIVE_KEY.format(game_id), str(int(time.time())))
    feeds = getattr(app.state, "feeds", None)
    if feeds is not None:
        feeds.changed()


async def active_games(app: FastAPI) -> list[str]:
    horizon = time.time() - app.state.config.webxdc.active_days * 86400
    out = []
    for key, value in await app.state.repository.list_state(_ACTIVE_KEY.format("")):
        if value.isdigit() and int(value) >= horizon:
            out.append(key.split(":", 2)[2])
    return out


def _game_id(raw: dict[str, Any]) -> str | None:
    return next((t[1] for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == "i"), None)


# -- moves (kind 4932) -------------------------------------------------------------


async def _check_update(app: FastAPI, raw: dict[str, Any]):
    """(game id, its post, None), or (None, None, why it's refused)."""
    from bridge.inbound import REJECTED, IngestResult

    if not app.state.config.webxdc.enabled:
        return None, None, REJECTED
    game_id = _game_id(raw)
    post = await game_post(app, game_id) if game_id else None
    if post is None:
        return None, None, IngestResult(False, "restricted: not a game this relay knows")
    if len((raw.get("content") or "").encode()) > app.state.config.webxdc.max_update_kb * 1024:
        return None, None, IngestResult(False, "invalid: this update is too large")
    return game_id, post, None


async def receive_update(app: FastAPI, raw: dict[str, Any]):
    """A move from Nostr: handed to whoever is playing the game here at once,
    then stored in Matrix under the game's post."""
    game_id, post, refused = await _check_update(app, raw)
    if refused is not None:
        return refused
    hub(app).deliver_update(game_id, raw)
    return await _store_update(app, raw, game_id, post)


async def _store_update(app: FastAPI, raw: dict[str, Any], game_id: str, post):
    """The move as a hidden event in a thread under the game's post."""
    from bridge.inbound import IngestResult, _store, _thread_root_of
    from bridge import remote as remotes

    repository = app.state.repository
    local = await repository.get_id_by_pubkey(raw["pubkey"])
    if local is not None:
        sender = bot_mxid(app.state.config)
    else:
        ghost = (await remotes.get_or_create_remote(app, raw["pubkey"])).ghost_user_id
        try:
            await remotes.ensure_ghost_in_room(app, ghost, post.room_id)
            sender = ghost
        except HomeserverError:
            sender = bot_mxid(app.state.config)
    root = await _thread_root_of(app, post.room_id, post.matrix_event_id)
    from bridge.event_sources import EMBED_FIELD

    content = {"m.relates_to": {"rel_type": "m.thread", "event_id": root, "is_falling_back": True,
                                "m.in_reply_to": {"event_id": post.matrix_event_id}},
               "game": game_id, EMBED_FIELD: raw}
    try:
        event_id = await app.state.homeserver.send_message_event(
            post.room_id, content, as_user_id=sender, ts=int(raw["created_at"]) * 1000, event_type=UPDATE_EVENT_TYPE
        )
    except HomeserverError:
        return IngestResult(False, "error: could not store this update")
    await _store(app, raw, room_id=post.room_id, matrix_event_id=event_id, scope="public")
    return IngestResult(True, "", stored=True)


async def updates(app: FastAPI, game_id: str) -> list[dict[str, Any]]:
    """Every move of the game this relay has, in serial order."""
    from bridge.relay_server import query_events

    events = await query_events(app, [{"kinds": [UPDATE_KIND], "#i": [game_id]}], limit=100_000)
    return sorted(events, key=lambda e: (e.get("created_at", 0), e.get("id", "")))


async def send_update(app: FastAPI, player, game_id: str, update: Any, descr: str = "") -> dict[str, Any] | None:
    """``sendUpdate()`` from a player here: a kind 4932 signed as their
    identity. It goes out first (to the others playing here, the relay's
    subscribers and the public relays) and is stored in Matrix after, so a
    move never waits on the homeserver. Returns the event, or None."""
    from bridge.publishing import publish
    from bridge.tasks import run_in_background
    from bridge import remote as remotes

    post = await game_post(app, game_id)
    if post is None:
        return None
    # The content is the update's payload; its info, summary and document
    # ride in tags, as NIP-DC and the other clients have it.
    update = update if isinstance(update, dict) else {"payload": update}
    content = json.dumps(update.get("payload"), ensure_ascii=False, separators=(",", ":"))
    if len(content.encode()) > app.state.config.webxdc.max_update_kb * 1024:
        return None
    info = str(update.get("info") or "")[:50]
    tags = [["i", game_id], ["alt", f"Webxdc update: {(descr or info or 'a move')[:60]}"]]
    for name in ("info", "summary", "document"):
        value = str(update.get(name) or "")[:50 if name != "document" else 100]
        if value:
            tags.append([name, value])
    event = await app.state.signer.sign(player.pubkey, kind=UPDATE_KIND, content=content,
                                        created_at=int(time.time()), tags=tags)
    if event is None:
        return None
    raw = event.to_dict()
    game_id, post, refused = await _check_update(app, raw)
    if refused is not None:
        return None
    hub(app).deliver_update(game_id, raw)
    relay_hub = getattr(app.state, "relay_hub", None)
    if relay_hub is not None:
        relay_hub.broadcast(raw, "public")
    author = await app.state.repository.get_remote_account(post.pubkey)
    extra = remotes.write_relays_for(app, author)[:4]

    async def out_then_store() -> None:
        # Both start now; a failed publish is retried from the stored copy.
        await asyncio.gather(publish(app, event, extra_relays=extra), _store_update(app, raw, game_id, post))

    run_in_background(app, out_then_store(), name=f"webxdc update {event.id[:8]}")
    return raw


# -- realtime (kind 20932) -------------------------------------------------------------


async def receive_realtime(app: FastAPI, raw: dict[str, Any]):
    from bridge.inbound import IngestResult

    game_id = _game_id(raw)
    if not app.state.config.webxdc.enabled or not game_id or not hub(app).playing(game_id):
        return IngestResult(False, "restricted: nobody here is playing that game")
    if len(raw.get("content") or "") > _REALTIME_MAX * 4 // 3 + 4:
        return IngestResult(False, "invalid: too large")
    hub(app).deliver_realtime(game_id, raw)
    return IngestResult(True, "")


async def send_realtime(app: FastAPI, session: "Session", data: bytes) -> None:
    """A realtime packet from a player here: signed by the session's own key,
    handed to the others playing here, the relay's subscribers, and the
    game author's relays."""
    if len(data) > _REALTIME_MAX:
        return
    event = nostr_event.build(session.key, kind=REALTIME_KIND, content=base64.b64encode(data).decode(),
                              created_at=int(time.time()), tags=[["i", session.game_id]])
    raw = event.to_dict()
    hub(app).deliver_realtime(session.game_id, raw, skip=session)
    relay_hub = getattr(app.state, "relay_hub", None)
    if relay_hub is not None:
        relay_hub.broadcast_ephemeral(raw)
    if session.relays:
        asyncio.create_task(_quietly(app.state.relay_pool.publish(event, session.relays, timeout=5.0)))


async def _quietly(coro) -> None:
    try:
        await coro
    except Exception:
        logger.debug("A realtime packet didn't go out", exc_info=True)


# -- players here ---------------------------------------------------------------------


@dataclass(eq=False)
class Session:
    """One player page, playing one game."""

    game_id: str
    send: Callable[[dict[str, Any]], Any]
    relays: list[str] = field(default_factory=list)
    key: bytes = field(default_factory=secp256k1.generate_secret_key)
    realtime: bool = False


class Hub:
    def __init__(self) -> None:
        self._sessions: dict[str, set[Session]] = {}

    def add(self, session: Session) -> None:
        self._sessions.setdefault(session.game_id, set()).add(session)

    def remove(self, session: Session) -> None:
        sessions = self._sessions.get(session.game_id)
        if sessions is not None:
            sessions.discard(session)
            if not sessions:
                del self._sessions[session.game_id]

    def playing(self, game_id: str) -> bool:
        return any(s.realtime for s in self._sessions.get(game_id, ()))

    def realtime_games(self) -> list[str]:
        return [g for g, sessions in self._sessions.items() if any(s.realtime for s in sessions)]

    def deliver_update(self, game_id: str, raw: dict[str, Any]) -> None:
        for session in list(self._sessions.get(game_id, ())):
            _fire(session.send({"t": "update", "event": raw}))

    def deliver_realtime(self, game_id: str, raw: dict[str, Any], skip: Session | None = None) -> None:
        for session in list(self._sessions.get(game_id, ())):
            if session is not skip and session.realtime:
                _fire(session.send({"t": "realtime", "b64": raw.get("content") or ""}))


def _fire(result: Any) -> None:
    if asyncio.iscoroutine(result):
        asyncio.ensure_future(result)


def hub(app: FastAPI) -> Hub:
    return app.state.__dict__.setdefault("webxdc_hub", Hub())


def new_game_id() -> str:
    return str(uuid.uuid4())
