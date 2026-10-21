"""The bridge's own Nostr relay (``nostr.relay_url``): NIP-01 over a
WebSocket at ``/``, its NIP-11 document at the same address, and NIP-42
authentication.

Reads are answered from the index (``bridge.repository.query_events``):
each matching event is rebuilt from Matrix (``bridge.event_sources``) and
sent if it still checks out. A small in-memory cache keeps popular events
from hitting the homeserver every time. Who may read what:

- local IDs' events and what's addressed to them: anyone,
- gift wraps: only a connection authenticated as their recipient,
- followed accounts' posts: only a connection authenticated as a local ID
  following them, and only with ``nostr.serve_followed_to_local_clients``.

Writes go through ``bridge.inbound.ingest``, which stores an event only if
it has a home in Matrix.

Hashtag and full-text search aren't supported: a filter only ``#t`` could
narrow (or any ``search``) is closed with ``error:``. A ``#t`` alongside
something the index can narrow on is applied to the results in memory.

New events are pushed to open subscriptions as they're stored
(``RelayHub.broadcast``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
import time
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.websockets import WebSocketState

from bridge.event_sources import rebuild
from bridge.nostr import event as nostr_event
from bridge.nostr.relay_pool import AUTH_KIND, normalize_relay_url

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_MESSAGE_BYTES = 256 * 1024
MAX_FILTERS = 10
_NARROWING = ("ids", "authors", "#e", "#p", "#a", "#q", "#E", "#P", "#A", "#d")
_CACHE_SIZE = 5000
_AUTH_WINDOW = 600
SUPPORTED_NIPS = [1, 2, 9, 10, 11, 13, 17, 18, 22, 25, 27, 40, 42, 44, 45, 59, 65, 70, 86, 88]


def matches(flt: dict[str, Any], event: dict[str, Any]) -> bool:
    """NIP-01 filter matching, in memory."""
    if "ids" in flt and event.get("id") not in flt["ids"]:
        return False
    if "authors" in flt and event.get("pubkey") not in flt["authors"]:
        return False
    if "kinds" in flt and event.get("kind") not in flt["kinds"]:
        return False
    created = event.get("created_at", 0)
    if isinstance(flt.get("since"), int) and created < flt["since"]:
        return False
    if isinstance(flt.get("until"), int) and created > flt["until"]:
        return False
    for key, values in flt.items():
        if key.startswith("#") and len(key) == 2:
            letter = key[1]
            if not any(len(t) >= 2 and t[0] == letter and t[1] in values for t in event.get("tags") or []):
                return False
    return True


def _valid_filter(flt: Any) -> str | None:
    if not isinstance(flt, dict):
        return "invalid: a filter must be an object"
    for key in ("ids", "authors"):
        if key in flt and (not isinstance(flt[key], list) or not all(isinstance(v, str) and len(v) == 64 for v in flt[key])):
            return f"invalid: {key} must be a list of 64-character hex strings"
    if "kinds" in flt and (not isinstance(flt["kinds"], list) or not all(isinstance(k, int) and not isinstance(k, bool) for k in flt["kinds"])):
        return "invalid: kinds must be a list of integers"
    for key in ("since", "until", "limit"):
        if key in flt and (not isinstance(flt[key], int) or isinstance(flt[key], bool)):
            return f"invalid: {key} must be an integer"
    for key, values in flt.items():
        if key.startswith("#") and (len(key) != 2 or not isinstance(values, list) or not all(isinstance(v, str) for v in values)):
            return f"invalid: {key} must be a list of strings"
    if "search" in flt:
        return "error: hashtag and full-text search aren't supported by this relay"
    if "#t" in flt and not any(key in flt for key in _NARROWING):
        return "error: hashtag and full-text search aren't supported by this relay"
    return None


class EventCache:
    def __init__(self, size: int = _CACHE_SIZE) -> None:
        self._events: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._size = size

    def get(self, event_id: str) -> dict[str, Any] | None:
        event = self._events.get(event_id)
        if event is not None:
            self._events.move_to_end(event_id)
        return event

    def put(self, event: dict[str, Any]) -> None:
        self._events[event["id"]] = event
        self._events.move_to_end(event["id"])
        while len(self._events) > self._size:
            self._events.popitem(last=False)

    def drop(self, event_id: str) -> None:
        self._events.pop(event_id, None)


class Connection:
    def __init__(self, websocket: WebSocket, ip: str | None) -> None:
        self.websocket = websocket
        self.ip = ip
        self.challenge = secrets.token_hex(16)
        self.authed: set[str] = set()
        self.subscriptions: dict[str, list[dict[str, Any]]] = {}
        self.lock = asyncio.Lock()

    async def send(self, message: list[Any]) -> None:
        if self.websocket.client_state != WebSocketState.CONNECTED:
            return
        async with self.lock:
            try:
                await self.websocket.send_text(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            except Exception:
                pass


class RelayHub:
    """Open connections, so stored events reach live subscriptions."""

    def __init__(self, app) -> None:
        self.app = app
        self.connections: set[Connection] = set()
        self.cache = EventCache()

    async def readers(self, connection: Connection) -> tuple[frozenset[str], frozenset[str]]:
        """(DM readers, followed readers): the local pubkeys this
        connection may read gift wraps for and followed posts as."""
        repository = self.app.state.repository
        local = [p for p in connection.authed if await repository.get_id_by_pubkey(p) is not None]
        followed = frozenset(local) if self.app.state.config.nostr.serve_followed_to_local_clients else frozenset()
        return frozenset(connection.authed), followed

    def broadcast(self, event: dict[str, Any], scope: str) -> None:
        self.cache.put(event)
        for connection in list(self.connections):
            asyncio.create_task(self._deliver(connection, event, scope))

    async def _deliver(self, connection: Connection, event: dict[str, Any], scope: str) -> None:
        if scope == "dm":
            recipients = {t[1] for t in event.get("tags") or [] if len(t) >= 2 and t[0] == "p"}
            if not recipients & connection.authed:
                return
        elif scope == "followed":
            if not self.app.state.config.nostr.serve_followed_to_local_clients:
                return
            followers = await self.app.state.repository.list_followers_of_remote(event["pubkey"])
            authed_ids = {
                i.id for p in connection.authed if (i := await self.app.state.repository.get_id_by_pubkey(p)) is not None
            }
            if not authed_ids & set(followers):
                return
        for sub_id, filters in list(connection.subscriptions.items()):
            if any(matches(f, event) for f in filters):
                await connection.send(["EVENT", sub_id, event])


def _client_ip(app, websocket: WebSocket | Request) -> str | None:
    peer = websocket.client.host if websocket.client else None
    trusted = app.state.config.bridge.trusted_proxies
    forwarded = websocket.headers.get("x-forwarded-for")
    if peer in trusted and forwarded:
        candidate = forwarded.split(",")[-1].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            return peer
    return peer


def relay_info(app) -> dict[str, Any]:
    config = app.state.config
    nostr = config.nostr
    return {
        "name": f"{config.bridge.domain} relay",
        "description": (
            f"The Nostr relay of {config.bridge.domain}'s Matrix server (matrix-appservice-nostr). It serves "
            f"{config.bridge.domain} users' events and takes events addressed to them."
        ),
        "pubkey": app.state.bot_identity.pubkey,
        "software": "https://github.com/Haven-Organization/matrix-appservice-nostr",
        "version": "0.1",
        "supported_nips": SUPPORTED_NIPS,
        "limitation": {
            "max_message_length": MAX_MESSAGE_BYTES,
            "max_subscriptions": nostr.relay_max_subscriptions,
            "max_filters": MAX_FILTERS,
            "max_limit": nostr.relay_max_limit,
            "auth_required": False,
            "payment_required": False,
            "restricted_writes": True,
        },
    }


@router.get("/")
async def info(request: Request):
    if "application/nostr+json" in request.headers.get("accept", ""):
        return JSONResponse(relay_info(request.app), media_type="application/nostr+json")
    return PlainTextResponse(
        f"This is a Nostr relay. Connect a Nostr client to {request.app.state.config.nostr.relay_url}\n"
    )


@router.websocket("/")
async def relay(websocket: WebSocket) -> None:
    app = websocket.app
    ip = _client_ip(app, websocket)
    guard = getattr(app.state, "spam", None)
    if guard is not None and not (await guard.check_connection(ip)).ok:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    connection = Connection(websocket, ip)
    hub: RelayHub = app.state.relay_hub
    hub.connections.add(connection)
    await connection.send(["AUTH", connection.challenge])
    try:
        while True:
            text = await websocket.receive_text()
            if len(text.encode("utf-8", errors="ignore")) > MAX_MESSAGE_BYTES:
                await connection.send(["NOTICE", "error: message too large"])
                continue
            try:
                message = json.loads(text)
            except ValueError:
                await connection.send(["NOTICE", "error: could not parse message"])
                continue
            if not isinstance(message, list) or not message or not isinstance(message[0], str):
                await connection.send(["NOTICE", "error: messages must be JSON arrays"])
                continue
            await _handle(app, connection, message)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Relay connection failed")
    finally:
        hub.connections.discard(connection)


async def _handle(app, connection: Connection, message: list[Any]) -> None:
    verb = message[0]
    if verb == "EVENT" and len(message) >= 2:
        await _on_event(app, connection, message[1])
    elif verb == "REQ" and len(message) >= 2 and isinstance(message[1], str):
        await _on_req(app, connection, message[1], message[2:], count=False)
    elif verb == "COUNT" and len(message) >= 2 and isinstance(message[1], str):
        await _on_req(app, connection, message[1], message[2:], count=True)
    elif verb == "CLOSE" and len(message) >= 2:
        connection.subscriptions.pop(str(message[1]), None)
    elif verb == "AUTH" and len(message) >= 2:
        await _on_auth(app, connection, message[1])
    else:
        await connection.send(["NOTICE", f"error: unknown or malformed {verb!r} message"])


async def _on_event(app, connection: Connection, raw: Any) -> None:
    from bridge.inbound import ingest

    event_id = raw.get("id") if isinstance(raw, dict) else None
    result = await ingest(app, raw, origin="relay", ip=connection.ip, authed=frozenset(connection.authed))
    await connection.send(["OK", event_id if isinstance(event_id, str) else "", result.ok, result.message])


async def _on_auth(app, connection: Connection, raw: Any) -> None:
    event_id = raw.get("id", "") if isinstance(raw, dict) else ""
    try:
        event = await asyncio.to_thread(nostr_event.verify, raw)
    except nostr_event.InvalidEvent as exc:
        await connection.send(["OK", event_id, False, f"invalid: {exc}"])
        return
    relay_tag = event.first_tag_value("relay") or ""
    ours = normalize_relay_url(app.state.config.nostr.relay_url)
    if (
        event.kind != AUTH_KIND
        or event.first_tag_value("challenge") != connection.challenge
        or (relay_tag and normalize_relay_url(relay_tag) != ours)
        or abs(event.created_at - time.time()) > _AUTH_WINDOW
    ):
        await connection.send(["OK", event.id, False, "invalid: bad authentication event"])
        return
    connection.authed.add(event.pubkey)
    await connection.send(["OK", event.id, True, ""])


async def _on_req(app, connection: Connection, sub_id: str, filters: list[Any], *, count: bool) -> None:
    config = app.state.config
    guard = getattr(app.state, "spam", None)
    if guard is not None:
        verdict = await guard.check_req(connection.ip)
        if not verdict.ok:
            await connection.send(["CLOSED", sub_id, verdict.reply])
            return
    if not filters or len(filters) > MAX_FILTERS:
        await connection.send(["CLOSED", sub_id, f"invalid: between 1 and {MAX_FILTERS} filters"])
        return
    for flt in filters:
        problem = _valid_filter(flt)
        if problem:
            await connection.send(["CLOSED", sub_id, problem])
            return
    if not count and sub_id not in connection.subscriptions and len(connection.subscriptions) >= config.nostr.relay_max_subscriptions:
        await connection.send(["CLOSED", sub_id, "rate-limited: too many open subscriptions"])
        return

    hub: RelayHub = app.state.relay_hub
    dm_readers, followed_readers = await hub.readers(connection)
    repository = app.state.repository
    now = int(time.time())
    max_limit = config.nostr.relay_max_limit
    if count:
        total = 0
        for flt in filters:
            index_filter = {k: v for k, v in flt.items() if k != "#t"}
            total += await repository.query_events(
                index_filter, limit=max_limit, now=now, dm_readers=dm_readers, followed_readers=followed_readers, count_only=True
            )
        await connection.send(["COUNT", sub_id, {"count": total}])
        return

    connection.subscriptions[sub_id] = filters
    sent: set[str] = set()
    for flt in filters:
        limit = min(int(flt.get("limit", max_limit)), max_limit)
        if limit <= 0:
            continue
        index_filter = {k: v for k, v in flt.items() if k != "#t"}
        rows = await repository.query_events(
            index_filter, limit=limit * (3 if "#t" in flt else 1), now=now,
            dm_readers=dm_readers, followed_readers=followed_readers,
        )
        events = await _materialize(app, [row for row in rows if row.id not in sent])
        delivered = 0
        for event in events:
            if delivered >= limit:
                break
            if "#t" in flt and not matches({"#t": flt["#t"]}, event):
                continue
            sent.add(event["id"])
            delivered += 1
            await connection.send(["EVENT", sub_id, event])
    await connection.send(["EOSE", sub_id])


async def _materialize(app, rows) -> list[dict[str, Any]]:
    """The events for index rows, rebuilt from Matrix (or from cache).
    Gone events are dropped from the index; unavailable ones are skipped."""
    hub: RelayHub = app.state.relay_hub
    semaphore = asyncio.Semaphore(8)

    async def one(row):
        cached = hub.cache.get(row.id)
        if cached is not None:
            return cached
        async with semaphore:
            result = await rebuild(app, row)
        if result.status == "ok" and result.event is not None:
            hub.cache.put(result.event)
            return result.event
        if result.status == "gone":
            await app.state.repository.delete_event(row.id)
        elif result.status == "mismatch":
            logger.warning("Event %s no longer rebuilds to its id; not serving it", row.id)
        return None

    events = await asyncio.gather(*(one(row) for row in rows))
    return [event for event in events if event is not None]


# -- NIP-86: relay management -------------------------------------------------------
#
# JSON-RPC over HTTP at the relay's own address, so admins can moderate from
# Nostr tools too. Each request carries a NIP-98 authorization (kind 27235)
# naming this URL, the POST method and the body's hash, signed by an ID
# whose owner is a bridge admin.

NIP98_KIND = 27235
NIP86_METHODS = [
    "supportedmethods", "banpubkey", "unbanpubkey", "listbannedpubkeys",
    "blockip", "unblockip", "listblockedips", "banevent", "listbannedevents",
]


def _rpc(result: Any = None, error: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"result": result}
    if error:
        body["error"] = error
    return JSONResponse(body, media_type="application/nostr+json+rpc")


@router.post("/")
async def management(request: Request):
    import hashlib

    from bridge.admins import is_admin
    from bridge.nostr.http_auth import AuthError, parse

    app = request.app
    if "application/nostr+json+rpc" not in request.headers.get("content-type", ""):
        return JSONResponse({"error": "use Content-Type: application/nostr+json+rpc"}, status_code=415)
    body = await request.body()
    try:
        auth = parse(request.headers.get("authorization"), kind=NIP98_KIND, max_age=120)
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    ours = {app.state.config.nostr.relay_http_base, app.state.config.nostr.relay_http_base + "/",
            app.state.config.nostr.relay_url, app.state.config.nostr.relay_url.rstrip("/")}
    if (
        auth.first_tag_value("u") not in ours
        or (auth.first_tag_value("method") or "").upper() != "POST"
        or auth.first_tag_value("payload") != hashlib.sha256(body).hexdigest()
    ):
        return JSONResponse({"error": "the authorization doesn't match this request"}, status_code=401)
    signer = await app.state.repository.get_id_by_pubkey(auth.pubkey)
    if signer is None or not await is_admin(app.state.config, app.state.homeserver, signer.matrix_user_id):
        return JSONResponse({"error": "only this relay's admins can do that"}, status_code=403)
    try:
        request_body = json.loads(body)
        method = str(request_body.get("method"))
        params = request_body.get("params") or []
    except (ValueError, AttributeError):
        return _rpc(error="malformed request")
    guard = app.state.spam
    reason = str(params[1]) if len(params) > 1 else f"via NIP-86 by {signer.name}"
    if method == "supportedmethods":
        return _rpc(NIP86_METHODS)
    if method in ("banpubkey", "unbanpubkey", "blockip", "unblockip", "banevent"):
        kind = "pubkey" if "pubkey" in method else "event" if method == "banevent" else "ip"
        key = _nip86_key(kind, params[0] if params else None)
        if key is None:
            return _rpc(error=f"the first parameter must be {'an IP address' if kind == 'ip' else 'a hex ' + kind + ' id' if kind == 'event' else 'a hex pubkey'}")
        if method.startswith("un"):
            await guard.unban(kind, key)
            return _rpc(True)
        # NIP-86 has no duration: these last until lifted.
        await guard.ban(kind, key, None, reason)
        if kind == "event":
            await _remove_event(app, key)
        return _rpc(True)
    if method in ("listbannedpubkeys", "listblockedips", "listbannedevents"):
        kind, field = {"listbannedpubkeys": ("pubkey", "pubkey"), "listblockedips": ("ip", "ip"),
                       "listbannedevents": ("event", "id")}[method]
        return _rpc([{field: b.key, "reason": b.reason} for b in await guard.list_bans() if b.kind == kind])
    return _rpc(error=f"unsupported method {method!r}")


def _nip86_key(kind: str, value: Any) -> str | None:
    """A NIP-86 parameter as a ban key: a 64-hex pubkey or event id, or an IP."""
    import ipaddress

    if not isinstance(value, str):
        return None
    value = value.strip()
    if kind == "ip":
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return None
    try:
        return value.lower() if len(value) == 64 and bytes.fromhex(value) else None
    except ValueError:
        return None


async def _remove_event(app, event_id: str) -> None:
    """Take an event off the relay by redacting its Matrix home (and copies)."""
    from bridge.homeserver_client import HomeserverError
    from bridge.users import bot_mxid

    repository = app.state.repository
    row = await repository.get_event(event_id)
    if row is None:
        return
    places = [(row.room_id, row.matrix_event_id)] if row.room_id and row.matrix_event_id else []
    places += await repository.list_mirrored_copies(row.id)
    for room_id, matrix_event in places:
        try:
            await app.state.homeserver.redact_event(room_id, matrix_event, as_user_id=bot_mxid(app.state.config), reason="Removed by a relay admin")
        except HomeserverError:
            logger.info("Could not redact %s", matrix_event, exc_info=True)
    await repository.delete_event(row.id)
    app.state.relay_hub.cache.drop(row.id)
