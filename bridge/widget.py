"""The bridge's room widget: a small web page any widget-capable client
(Element, ...) shows in a room, as the sibling bridges have.

It does what commands do (switching and creating IDs, following,
blocking, DMs, backfill) and the things that must never go through a room
message: importing an existing key (``nsec``, or ``ncryptsec`` with its
password), connecting a remote signer (``bunker://``), and exporting a key.
Those travel only over HTTPS, between the page and the bridge.

Nothing the page's URL says is trusted. The page asks its client for a
Matrix OpenID token, which the homeserver itself vouches for, and trades
it for a short-lived session. Every action needs that session and works
only in a room the user is joined to. Ordinary actions run the same
handlers as the commands, which answer in the room as usual.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bridge import ids, keys
from bridge.admins import is_admin
from bridge.homeserver_client import HomeserverError
from bridge.nostr import nip19
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

router = APIRouter()

# A widget session lasts while it's used: each use (and an open widget's
# ping every few minutes) pushes its end back, up to a day after the last.
# Sessions are kept in bridge_state, under a hash of the token, so a
# restart doesn't sign anyone out.
_SESSION_TTL = 24 * 3600
_PERSIST_EVERY = 600  # seconds an expiry may move before it's written again
_SESSION_KEY = "widget:session:{}"
_sessions: dict[str, tuple[str, float]] = {}  # token hash -> (user, expires)
_persisted: dict[str, float] = {}
_persist: Any = None  # set by load_sessions: writes (hash, user, expires) or (hash, None, 0) to delete
# scrypt cost an imported ncryptsec may ask for: 2^18 (256 MB). Higher costs
# would let one paste tie up the server's memory.
_MAX_IMPORT_LOG_N = 18


def _hash(session_id: str) -> str:
    import hashlib

    return hashlib.sha256(session_id.encode()).hexdigest()


async def load_sessions(app: Any) -> None:
    """The sessions kept from before a restart, and keeping new ones."""

    repository = app.state.repository
    now = time.time()
    for key, value in await repository.list_state(_SESSION_KEY.format("")):
        digest = key.rsplit(":", 1)[1]
        try:
            user, expires = json.loads(value)
        except (ValueError, TypeError):
            continue
        if expires > now:
            _sessions[digest] = (user, expires)
            _persisted[digest] = expires
        else:
            await repository.delete_state(key)

    def persist(digest: str, user: str | None, expires: float) -> None:
        async def write() -> None:
            try:
                if user is None:
                    await repository.delete_state(_SESSION_KEY.format(digest))
                else:
                    await repository.set_state(_SESSION_KEY.format(digest), json.dumps([user, expires]))
            except Exception:
                logger.info("Could not keep a widget session", exc_info=True)

        try:
            asyncio.get_running_loop().create_task(write())
        except RuntimeError:
            return False  # not on the server's loop: written next time
        return True

    global _persist
    _persist = persist


def _keep(digest: str, user: str, expires: float) -> None:
    _sessions[digest] = (user, expires)
    if _persist is not None and expires - _persisted.get(digest, 0) >= _PERSIST_EVERY:
        if _persist(digest, user, expires):
            _persisted[digest] = expires


def _new_session(user: str) -> str:
    now = time.time()
    for digest, (_u, expires) in list(_sessions.items()):
        if expires < now:
            del _sessions[digest]
            _persisted.pop(digest, None)
            if _persist is not None:
                _persist(digest, None, 0)
    session_id = secrets.token_urlsafe(32)
    _keep(_hash(session_id), user, now + _SESSION_TTL)
    return session_id


def user_of_session(session_id: str) -> str | None:
    """The Matrix user a widget session belongs to, if it's still valid
    (and, being used, it lasts longer)."""
    digest = _hash(session_id or "")
    entry = _sessions.get(digest)
    now = time.time()
    if entry is None or now > entry[1]:
        return None
    _keep(digest, entry[0], now + _SESSION_TTL)
    return entry[0]


def _session_user(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing session; reload the widget.")
    user = user_of_session(auth[7:].strip())
    if user is None:
        raise HTTPException(401, "Session expired; reload the widget.")
    return user


async def _member(request: Request, room_id: str) -> str:
    user = _session_user(request)
    try:
        member = await request.app.state.homeserver.get_room_state(
            room_id, "m.room.member", user, as_user_id=bot_mxid(request.app.state.config)
        )
    except HomeserverError:
        member = None
    if (member or {}).get("membership") != "join":
        raise HTTPException(403, "You need to be in this room to use the widget here.")
    return user


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("room_id"), str):
        raise HTTPException(400, "room_id is required")
    return body


def _rate_limit_keys(request: Request, user: str) -> None:
    """Key imports and exports per user: a password-protected key costs
    real work to open, and nobody needs more than a few tries."""
    store = _attempts(request)
    now = time.monotonic()
    attempts = [t for t in store.get(user, []) if now - t < 600]
    if len(attempts) >= 10:
        raise HTTPException(429, "Too many attempts; wait a few minutes.")
    attempts.append(now)
    store[user] = attempts


def _attempts(request: Request) -> dict[str, list[float]]:
    state = request.app.state
    if not hasattr(state, "key_attempts"):
        state.key_attempts = {}
    return state.key_attempts


async def _own_id(request: Request, user: str, reference: Any):
    if not isinstance(reference, (str, int)):
        raise HTTPException(400, "Which ID?")
    found = await ids.find_own_id(request.app, user, str(reference))
    if found is None:
        raise HTTPException(404, "You don't have that ID.")
    return found


# -- sign-in -----------------------------------------------------------------------


@router.post("/widget/api/session")
async def session(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    token = body.get("access_token") if isinstance(body, dict) else None
    server = body.get("matrix_server_name") if isinstance(body, dict) else None
    config = request.app.state.config
    if not isinstance(token, str) or not token or server != config.homeserver.server_name:
        raise HTTPException(403, f"This widget only works for accounts on {config.homeserver.server_name}.")
    user = await request.app.state.homeserver.verify_openid_token(token)
    if user is None or user.rsplit(":", 1)[-1] != config.homeserver.server_name:
        raise HTTPException(403, "Could not verify who you are with the homeserver.")
    return JSONResponse({"session_id": _new_session(user), "matrix_user_id": user})


@router.get("/widget/api/ping")
async def ping(request: Request) -> dict[str, bool]:
    """An open widget checking in, so its session lasts while it's open."""
    _session_user(request)
    return {"ok": True}


# -- what the page shows --------------------------------------------------------------


@router.get("/widget/api/context")
async def context(request: Request, room_id: str) -> JSONResponse:
    user = await _member(request, room_id)
    app = request.app
    repository = app.state.repository
    config = app.state.config
    owned = await repository.list_ids_for_user(user)
    active = await ids.active_id(app, user)
    id_list = [
        {
            "number": n,
            "name": i.name,
            "handle": f"{i.name}@{config.bridge.domain}",
            "npub": nip19.npub_encode(i.pubkey),
            "active": active is not None and active.id == i.id,
            "key": {"generated": "held by the bridge", "imported": "imported, held by the bridge", "bunker": "in your remote signer"}.get(i.key_origin, i.key_origin),
            "exportable": i.key_origin != "bunker",
            "profile_room": i.profile_room_id,
            "followers": await repository.count_followers(i.id),
        }
        for n, i in enumerate(owned, start=1)
    ]
    room: dict[str, Any] = {"kind": "other"}
    profile = await repository.get_id_for_profile_room(room_id)
    remote = await repository.get_remote_account_for_room(room_id)
    dm = await repository.get_dm_room_info(room_id)
    rooms = await repository.get_user_rooms(user)
    if profile is not None:
        room = {"kind": "profile", "id": profile.name, "mine": profile.matrix_user_id == user}
    elif remote is not None:
        following = [i.name for i in owned if remote.pubkey in await repository.list_follows(i.id)]
        blocked = [i.name for i in owned if remote.pubkey in await repository.list_blocks(i.id)]
        try:
            name = (await app.state.homeserver.get_profile(remote.ghost_user_id)).get("displayname")
        except HomeserverError:
            name = None
        room = {
            "kind": "remote", "npub": nip19.npub_encode(remote.pubkey), "nip05": remote.nip05, "name": name,
            "following": following, "blocked": blocked,
        }
    elif dm is not None:
        room = {"kind": "dm"}
    elif room_id in (rooms.notifications_room_id, rooms.space_room_id):
        room = {"kind": "notifications" if room_id == rooms.notifications_room_id else "space"}
    return JSONResponse({
        "user": user, "domain": config.bridge.domain, "prefix": config.appservice.command_prefix,
        "ids": id_list, "room": room, "admin": await is_admin(config, app.state.homeserver, user),
        "max_ids": config.bridge.max_ids_per_user,
    })


# -- ordinary actions (the command handlers answer in the room) -----------------------


async def _command(request: Request, handler, arguments) -> JSONResponse:
    body = await _body(request)
    user = await _member(request, body["room_id"])
    await handler(request.app, body["room_id"], user, *arguments(body))
    return JSONResponse({"ok": True})


@router.post("/widget/api/switch")
async def switch(request: Request) -> JSONResponse:
    from bridge.commands import _handle_id

    return await _command(request, _handle_id, lambda b: [str(b.get("id", "")) or "list"])


@router.post("/widget/api/create")
async def create(request: Request) -> JSONResponse:
    from bridge.commands import _handle_create

    return await _command(request, _handle_create, lambda b: [f"profile {b.get('name') or ''}".strip()])


@router.post("/widget/api/follow")
async def follow(request: Request) -> JSONResponse:
    from bridge.commands import _handle_follow

    return await _command(request, _handle_follow, lambda b: [str(b.get("account", ""))])


@router.post("/widget/api/unfollow")
async def unfollow(request: Request) -> JSONResponse:
    from bridge.commands import _handle_unfollow

    return await _command(request, _handle_unfollow, lambda b: [str(b.get("account", ""))])


@router.post("/widget/api/block")
async def block(request: Request) -> JSONResponse:
    from bridge.commands import _handle_block

    body = await _body(request)
    user = await _member(request, body["room_id"])
    await _handle_block(request.app, body["room_id"], user, str(body.get("account", "")), blocked=bool(body.get("block", True)))
    return JSONResponse({"ok": True})


@router.post("/widget/api/backfill")
async def backfill(request: Request) -> JSONResponse:
    from bridge.commands import _handle_backfill

    return await _command(request, _handle_backfill, lambda b: [str(b.get("count", "20"))])


@router.post("/widget/api/dm")
async def dm(request: Request) -> JSONResponse:
    from bridge.commands import _handle_dm

    return await _command(request, _handle_dm, lambda b: [str(b.get("account", ""))])


# -- keys ------------------------------------------------------------------------------


def _check_ncryptsec_cost(key: str) -> None:
    if key.strip().startswith("ncryptsec1"):
        try:
            _hrp, data = nip19.bech32_decode(key.strip())
        except nip19.Nip19Error as exc:
            raise HTTPException(400, f"That key couldn't be read: {exc}") from exc
        if len(data) > 1 and data[1] > _MAX_IMPORT_LOG_N:
            raise HTTPException(400, "That key's password protection is stronger than this server allows importing; "
                                     "export it again with a lower strength (log_n 16).")


async def _material(request: Request, body: dict[str, Any]) -> keys.KeyMaterial:
    if body.get("uri"):
        return await keys.from_bunker(request.app, str(body["uri"]))
    key = str(body.get("key") or "")
    _check_ncryptsec_cost(key)
    secret = await asyncio.to_thread(keys.decode_secret, key, body.get("password") or None)
    return keys.from_secret(secret)


@router.post("/widget/api/key/import")
async def import_key(request: Request) -> JSONResponse:
    """Create an ID from an existing key, or give an ID a different key.
    ``key`` (nsec/ncryptsec/hex, with ``password``) or ``uri`` (bunker://)."""
    from bridge.commands import _finish_creating

    body = await _body(request)
    user = await _member(request, body["room_id"])
    _rate_limit_keys(request, user)
    app = request.app
    config = app.state.config
    try:
        material = await _material(request, body)
    except keys.KeyProblem as exc:
        from bridge.nostr.nip46 import AuthUrlNeeded

        cause = exc.__cause__
        if isinstance(cause, AuthUrlNeeded):
            return JSONResponse({"ok": False, "auth_url": cause.url, "message": str(exc)})
        raise HTTPException(400, str(exc)) from exc
    if body.get("mode") == "replace":
        target = await _own_id(request, user, body.get("id"))
        if str(body.get("confirm", "")).strip().lower() != target.name:
            raise HTTPException(400, f'Type "{target.name}" to confirm replacing its key.')
        try:
            updated = await keys.replace_key(app, target, material)
        except keys.KeyProblem as exc:
            raise HTTPException(400, str(exc)) from exc
        message = f"{updated.name}@{config.bridge.domain} now uses {nip19.npub_encode(updated.pubkey)}."
    else:
        name = ids.normalize_requested(str(body.get("name") or ""), config.bridge.domain)
        if not name:
            raise HTTPException(400, "Give the new ID a name.")
        limit = await ids.limit_problem(app, user, is_admin=await is_admin(config, app.state.homeserver, user))
        if limit is not None:
            raise HTTPException(400, limit.message)
        try:
            created = await keys.create_with_key(app, user, name, material)
        except (ids.IdRejected, keys.KeyProblem) as exc:
            raise HTTPException(400, str(exc)) from exc
        profile_event = await keys.existing_profile(app, material.pubkey)
        await _finish_creating(app, body["room_id"], user, created, profile_event=profile_event)
        updated = await app.state.repository.get_id(created.id) or created
        message = f"Created {updated.name}@{config.bridge.domain} with your key."
    follows = bool(body.get("import_follows"))
    posts = min(int(body.get("import_posts") or 0), 100)
    if follows or posts:
        from bridge.messages import notice
        from bridge.tasks import run_in_background

        async def bring_in() -> None:
            summary = await keys.import_history(app, updated, follows=follows, posts=posts)
            await notice(app, body["room_id"], f"{updated.name}@{config.bridge.domain}: {summary}.")

        run_in_background(app, bring_in(), name=f"import history {updated.id}")
        message += " Bringing in your follows and posts in the background."
    return JSONResponse({"ok": True, "message": message})


@router.post("/widget/api/key/export")
async def export_key(request: Request) -> JSONResponse:
    body = await _body(request)
    user = await _member(request, body["room_id"])
    _rate_limit_keys(request, user)
    target = await _own_id(request, user, body.get("id"))
    password = str(body.get("password") or "") or None
    try:
        exported = await keys.export_key(request.app, target, password)
    except keys.KeyProblem as exc:
        raise HTTPException(400, str(exc)) from exc
    logger.info("%s exported the key of %s", user, target.name)
    return JSONResponse({"ok": True, "key": exported, "encrypted": bool(password)})


@router.get("/widget")
async def page() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store, must-revalidate"})


_PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nostr Bridge</title>
<style>
  :root { --bg:#f6f4fa; --card:#fff; --border:#e3dcef; --text:#14101c; --muted:#655c75; --accent:#662482; --accent-text:#fff;
          --danger:#c4271b; --danger-bg:#fdf0ef; --ok-bg:#ecfdf3; --ok-text:#067647; }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#141018; --card:#1d1724; --border:#33293f;
          --text:#ece6f3; --muted:#a397b3; --accent:#b77ad6; --accent-text:#141018; --danger:#f97066; --danger-bg:#2b1618; --ok-bg:#0f2a1d; --ok-text:#47cd89; } }
  :root[data-theme="dark"] { --bg:#141018; --card:#1d1724; --border:#33293f; --text:#ece6f3; --muted:#a397b3; --accent:#b77ad6;
          --accent-text:#141018; --danger:#f97066; --danger-bg:#2b1618; --ok-bg:#0f2a1d; --ok-text:#47cd89; }
  * { box-sizing: border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--text); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; margin-bottom:12px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:0 0 10px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px; align-items:center; }
  button { font:inherit; font-weight:600; border:1px solid var(--border); background:var(--card); color:var(--text); border-radius:8px; padding:7px 12px; cursor:pointer; }
  button.primary { background:var(--accent); color:var(--accent-text); border-color:var(--accent); }
  button.danger { color:var(--danger); border-color:var(--danger); }
  button:disabled { opacity:.5; cursor:default; }
  input, select { font:inherit; border:1px solid var(--border); background:var(--bg); color:var(--text); border-radius:8px; padding:7px 10px; flex:1; min-width:120px; }
  .item { display:flex; justify-content:space-between; gap:8px; align-items:center; padding:8px 10px; border:1px solid var(--border); border-radius:8px; margin-bottom:6px; }
  .item div { overflow-wrap:anywhere; }
  .muted { color:var(--muted); font-size:12.5px; }
  .badge { font-size:11px; padding:2px 8px; border-radius:999px; background:var(--accent); color:var(--accent-text); font-weight:600; margin-left:6px; }
  .warn { background:var(--danger-bg); color:var(--danger); border-radius:8px; padding:8px 10px; font-size:13px; margin-bottom:8px; }
  .secret { font-family:ui-monospace,monospace; word-break:break-all; background:var(--bg); padding:8px; border-radius:8px; border:1px solid var(--border); }
  .toast { position:fixed; left:16px; right:16px; bottom:16px; padding:10px 14px; border-radius:10px; font-weight:600; display:none; }
  .toast.ok { background:var(--ok-bg); color:var(--ok-text); display:block; } .toast.err { background:var(--danger-bg); color:var(--danger); display:block; }
  details summary { cursor:pointer; font-weight:600; margin-bottom:8px; }
  a { color:var(--accent); }
</style>
</head>
<body>
<div id="app"><p class="muted">Connecting&hellip;</p></div>
<div id="toast" class="toast"></div>
<script>
(function () {
  "use strict";
  const params = new URLSearchParams(location.search);
  const widgetId = params.get("widgetId") || "";
  const roomId = params.get("matrix_room_id") || "";
  const theme = params.get("theme") || "";
  if (theme.indexOf("dark") !== -1) document.documentElement.setAttribute("data-theme", "dark");
  else if (theme.indexOf("light") !== -1) document.documentElement.setAttribute("data-theme", "light");

  let counter = 0; const pending = {}; let openIdResolve = null;
  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || typeof msg !== "object") return;
    if (msg.widgetId && widgetId && msg.widgetId !== widgetId) return;
    if (msg.api === "fromWidget" && msg.requestId && pending[msg.requestId] && "response" in msg) {
      const resolve = pending[msg.requestId]; delete pending[msg.requestId]; resolve(msg.response); return;
    }
    if (msg.api === "toWidget" && msg.action === "capabilities") {
      // Signing in needs the client around it, so no pop-out button.
      window.parent.postMessage(Object.assign({}, msg, { response: { capabilities: ["io.element.requires_client"] } }), "*"); return;
    }
    if (msg.api === "toWidget" && msg.action === "openid_credentials") {
      window.parent.postMessage(Object.assign({}, msg, { response: {} }), "*");
      if (openIdResolve) { const r = openIdResolve; openIdResolve = null; r(msg.data); } return;
    }
    if (msg.api === "toWidget" && msg.requestId) window.parent.postMessage(Object.assign({}, msg, { response: {} }), "*");
  });
  function fromWidget(action, data) {
    return new Promise((resolve) => {
      const requestId = "req-" + (counter++) + "-" + Date.now();
      pending[requestId] = resolve;
      window.parent.postMessage({ api: "fromWidget", widgetId, requestId, action, data: data || {} }, "*");
    });
  }
  async function openId() {
    const first = await fromWidget("get_openid", {});
    if (first && first.state === "allowed") return first;
    if (first && first.state === "blocked") throw new Error("Your client refused to tell the widget who you are.");
    return new Promise((resolve, reject) => { openIdResolve = resolve;
      setTimeout(() => { if (openIdResolve) { openIdResolve = null; reject(new Error("Timed out waiting for approval.")); } }, 60000); });
  }
  fromWidget("content_loaded", {});

  let session = null;
  async function api(path, body, method) {
    const headers = { "Content-Type": "application/json" };
    if (session) headers["Authorization"] = "Bearer " + session;
    const res = await fetch(path, { method: method || (body ? "POST" : "GET"), headers, body: body ? JSON.stringify(body) : undefined });
    let data = {}; try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error(data.detail || ("Request failed (" + res.status + ")"));
    return data;
  }
  function toast(text, ok) { const n = document.getElementById("toast"); n.textContent = text; n.className = "toast " + (ok ? "ok" : "err");
    clearTimeout(toast.t); toast.t = setTimeout(() => { n.className = "toast"; }, 5000); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c])); }
  async function act(path, body, button, message) {
    if (button) button.disabled = true;
    try { const r = await api(path, Object.assign({ room_id: roomId }, body)); toast(r.message || message || "Done.", true); await load(); return r; }
    catch (e) { toast(e.message, false); } finally { if (button) button.disabled = false; }
  }

  let ctx = null;
  async function load() { ctx = await api("/widget/api/context?room_id=" + encodeURIComponent(roomId)); render(); }

  function idOptions() { return ctx.ids.map((i) => `<option value="${i.number}">${esc(i.handle)}</option>`).join(""); }

  function render() {
    const app = document.getElementById("app");
    let out = "";
    if (ctx.room.kind === "remote") {
      const r = ctx.room;
      out += `<div class="card"><h2>${esc(r.name || r.nip05 || "This account")}</h2>
        <p class="muted">${esc(r.nip05 || "")} ${esc(r.npub)}</p>
        <p class="muted">${r.following.length ? "Followed by your " + esc(r.following.join(", ")) : "Not followed by your IDs."}</p>
        <div class="row"><button class="primary" id="follow">Follow as active ID</button><button id="unfollow">Unfollow</button>
        <button id="dm">DM</button><button id="block">${r.blocked.length ? "Unblock" : "Block"}</button></div>
        <div class="row"><input id="count" type="number" value="20" min="1" max="100"><button id="backfill">Bring in older posts</button></div></div>`;
    }
    out += `<div class="card"><h2>Your Nostr IDs</h2>`;
    if (!ctx.ids.length) out += `<p class="muted">You don't have a Nostr ID yet.</p>`;
    for (const i of ctx.ids) {
      out += `<div class="item"><div><strong>${esc(i.handle)}</strong>${i.active ? '<span class="badge">active</span>' : ""}
        <div class="muted">${esc(i.npub)} · key ${esc(i.key)} · ${i.followers} known followers</div></div>
        ${i.active ? "" : `<button data-switch="${i.number}">Switch</button>`}</div>`;
    }
    out += `<div class="row"><input id="newname" placeholder="new ID name, e.g. ${esc(ctx.user.slice(1).split(":")[0])}">
      <button class="primary" id="create">Create ID</button></div></div>`;

    out += `<div class="card"><h2>Bring your own key</h2>
      <p class="muted">Use a Nostr key you already have, so your existing followers come with you. Keys go straight to the bridge over HTTPS and are stored encrypted; never paste one into a chat message.</p>
      <details><summary>Import an nsec or ncryptsec</summary>
        <div class="row"><input id="key" type="password" placeholder="nsec1… or ncryptsec1… or hex" autocomplete="off"></div>
        <div class="row"><input id="password" type="password" placeholder="password (only for ncryptsec)" autocomplete="off"></div>
        ${modeFields("imp")}
        <div class="row"><button class="primary" id="import">Import key</button></div>
      </details>
      <details><summary>Connect a remote signer (NIP-46)</summary>
        <p class="muted">Your key stays in your signer app (nsec.app, Amber, …). Paste its bunker:// address. Posting and DMs need the signer to be online.</p>
        <div class="row"><input id="uri" placeholder="bunker://…" autocomplete="off"></div>
        ${modeFields("sig")}
        <div class="row"><button class="primary" id="signer">Connect signer</button></div>
        <div id="authurl"></div>
      </details></div>`;

    if (ctx.ids.some((i) => i.exportable)) {
      out += `<div class="card"><h2>Export a key</h2>
        <div class="warn">Anyone with this key <em>is</em> that Nostr identity. Only export it to put it in your own Nostr app or signer.</div>
        <div class="row"><select id="exportid">${ctx.ids.filter((i) => i.exportable).map((i) => `<option value="${i.number}">${esc(i.handle)}</option>`).join("")}</select></div>
        <div class="row"><input id="exportpw" type="password" placeholder="password to protect it (recommended)" autocomplete="off"></div>
        <div class="row"><button class="danger" id="export">Show key</button></div><div id="exported"></div></div>`;
    }
    app.innerHTML = out;
    bind();
  }

  function modeFields(p) {
    return `<div class="row"><select id="${p}mode"><option value="new">as a new ID</option>${ctx.ids.length ? '<option value="replace">replacing an ID\'s key</option>' : ""}</select></div>
      <div class="row" id="${p}new"><input id="${p}name" placeholder="name for the new ID"></div>
      <div class="row" id="${p}replace" style="display:none"><select id="${p}id">${idOptions()}</select>
        <input id="${p}confirm" placeholder="type the ID's name to confirm"></div>
      <div class="warn" id="${p}warn" style="display:none">Replacing a key loses the followers of the old key: they follow the key, not the handle.</div>
      <div class="row"><label><input type="checkbox" id="${p}follows" checked style="flex:none;min-width:0"> bring in who this key follows</label></div>
      <div class="row"><label><input type="checkbox" id="${p}posts" checked style="flex:none;min-width:0"> bring in its latest posts</label></div>`;
  }

  function modeBody(p) {
    const mode = document.getElementById(p + "mode").value;
    const body = { mode, import_follows: document.getElementById(p + "follows").checked, import_posts: document.getElementById(p + "posts").checked ? 20 : 0 };
    if (mode === "new") body.name = document.getElementById(p + "name").value;
    else { body.id = document.getElementById(p + "id").value; body.confirm = document.getElementById(p + "confirm").value; }
    return body;
  }

  function bind() {
    const on = (id, fn) => { const n = document.getElementById(id); if (n) n.onclick = () => fn(n); };
    document.querySelectorAll("[data-switch]").forEach((b) => { b.onclick = () => act("/widget/api/switch", { id: b.dataset.switch }, b); });
    on("create", (b) => act("/widget/api/create", { name: document.getElementById("newname").value }, b));
    if (ctx.room.kind === "remote") {
      const account = ctx.room.npub;
      on("follow", (b) => act("/widget/api/follow", { account }, b));
      on("unfollow", (b) => act("/widget/api/unfollow", { account }, b));
      on("dm", (b) => act("/widget/api/dm", { account }, b));
      on("block", (b) => act("/widget/api/block", { account, block: !ctx.room.blocked.length }, b));
      on("backfill", (b) => act("/widget/api/backfill", { count: document.getElementById("count").value }, b));
    }
    for (const p of ["imp", "sig"]) {
      const sel = document.getElementById(p + "mode");
      if (sel) sel.onchange = () => { const r = sel.value === "replace";
        document.getElementById(p + "new").style.display = r ? "none" : "";
        document.getElementById(p + "replace").style.display = r ? "" : "none";
        document.getElementById(p + "warn").style.display = r ? "" : "none"; };
    }
    on("import", async (b) => {
      const body = Object.assign(modeBody("imp"), { key: document.getElementById("key").value, password: document.getElementById("password").value });
      await act("/widget/api/key/import", body, b);
      document.getElementById("key") && (document.getElementById("key").value = "");
    });
    on("signer", async (b) => {
      b.disabled = true;
      try {
        const r = await api("/widget/api/key/import", Object.assign(modeBody("sig"), { room_id: roomId, uri: document.getElementById("uri").value }));
        if (r.auth_url) { document.getElementById("authurl").innerHTML = `<div class="warn">Approve the bridge in your signer: <a href="${esc(r.auth_url)}" target="_blank" rel="noopener">${esc(r.auth_url)}</a>, then press Connect again.</div>`; }
        else { toast(r.message, true); await load(); }
      } catch (e) { toast(e.message, false); } finally { b.disabled = false; }
    });
    on("export", async (b) => {
      b.disabled = true;
      try {
        const r = await api("/widget/api/key/export", { room_id: roomId, id: document.getElementById("exportid").value, password: document.getElementById("exportpw").value });
        document.getElementById("exported").innerHTML = `<p class="muted">${r.encrypted ? "Protected with your password (NIP-49):" : "Unprotected secret key:"}</p><div class="secret">${esc(r.key)}</div>`;
      } catch (e) { toast(e.message, false); } finally { b.disabled = false; }
    });
  }

  (async () => {
    try {
      if (window.parent === window) throw new Error("Open this from its room in your Matrix client: it signs in through the client, so it can't work in a window of its own.");
      const creds = await openId();
      const s = await api("/widget/api/session", { access_token: creds.access_token, matrix_server_name: creds.matrix_server_name });
      session = s.session_id;
      // Checking in, so the session lasts while the widget is open.
      setInterval(() => { api("/widget/api/ping").catch(() => {}); }, 5 * 60 * 1000);
      await load();
    } catch (e) { document.getElementById("app").innerHTML = `<div class="card"><p>${esc(e.message)}</p></div>`; }
  })();
})();
</script>
</body>
</html>
"""
