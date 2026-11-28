"""The bridge's room widget: a small web app any widget-capable client
(Element, ...) shows in a room, with the same layout and sections as the
sibling bridges' widgets (the page itself is ``bridge.widget_page``):

- Home: browsing Nostr (``bridge.widget_browse``) and importing posts.
- Profile: the user's IDs, editing their profiles, who they follow.
- Communities: their Concord communities and NIP-29 groups.
- Settings: what the room they're in is for, getting back into their
  rooms, DM filters, and keys: importing an existing one (``nsec``, or
  ``ncryptsec`` with its password), connecting a remote signer
  (``bunker://``), and exporting one. Keys never go through a room
  message; they travel only over HTTPS, between the page and the bridge.
- Admin: third-party access and bans, every ID and bridge room, the
  backstage space, and the bridge's settings (``bridge.bridge_settings``).

Nothing the page's URL says is trusted. The page asks its client for a
Matrix OpenID token, which the homeserver itself vouches for, and trades
it for a session. Every call needs that session and works only for
someone joined to the room the widget is in (any room: Home, Profile,
Communities and Admin aren't about the room). Actions run the same
handlers as the commands, which answer in the room as usual. Admin calls
check admin status on every request.
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
from bridge.widget_page import PAGE as _PAGE

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


async def _profile_fields(app, nostr_id) -> dict[str, str]:
    """An ID's profile as it's published (its kind 0), rebuilt from the
    index: name, about, picture and banner URLs and so on."""
    from bridge.event_sources import rebuild
    from bridge.remote import parse_profile

    row = await app.state.repository.get_latest_event(nostr_id.pubkey, 0)
    if row is None:
        return {}
    try:
        rebuilt = await rebuild(app, row)
    except Exception:
        logger.info("Could not rebuild the profile of %s", nostr_id.name, exc_info=True)
        return {}
    return parse_profile(rebuilt.event) if rebuilt.status == "ok" else {}


def _https(url: str | None) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


async def _room_kind(app, user: str, room_id: str, owned) -> dict[str, Any]:
    """What the room the widget is in is, for the Settings section's "This
    room" card."""
    from bridge import communities

    repository = app.state.repository
    profile = await repository.get_id_for_profile_room(room_id)
    if profile is not None:
        return {"kind": "profile", "id": profile.name, "mine": profile.matrix_user_id == user,
                "number": ids.id_number(owned, profile) if profile.matrix_user_id == user else None}
    remote = await repository.get_remote_account_for_room(room_id)
    if remote is not None:
        following = [i.name for i in owned if remote.pubkey in await repository.list_follows(i.id)]
        blocked = [i.name for i in owned if remote.pubkey in await repository.list_blocks(i.id)]
        try:
            name = (await app.state.homeserver.get_profile(remote.ghost_user_id)).get("displayname")
        except HomeserverError:
            name = None
        return {"kind": "remote", "pubkey": remote.pubkey, "npub": nip19.npub_encode(remote.pubkey),
                "nip05": remote.nip05, "name": name, "following": following, "blocked": blocked}
    dm = await repository.get_dm_room_info(room_id)
    if dm is not None:
        return {"kind": "dm", "pubkey": dm[1], "npub": nip19.npub_encode(dm[1])}
    rooms = await repository.get_user_rooms(user)
    if room_id in (rooms.notifications_room_id, rooms.space_room_id):
        return {"kind": "notifications" if room_id == rooms.notifications_room_id else "space"}
    found = await communities.find(app, user, "", room_id)
    if found is not None:
        kind, target = found
        return {"kind": "community" if kind == "concord" else "group", "type": kind,
                "community_id": target.id if kind == "concord" else target.key,
                "name": target.name or target.id}
    return {"kind": "other"}


@router.get("/widget/api/context")
async def context(request: Request, room_id: str) -> JSONResponse:
    from bridge.shadow_ids import effective_tier

    user = await _member(request, room_id)
    app = request.app
    repository = app.state.repository
    config = app.state.config
    owned = await repository.list_ids_for_user(user)
    active = await ids.active_id(app, user)
    id_list = []
    for n, i in enumerate(owned, start=1):
        fields = await _profile_fields(app, i)
        id_list.append({
            "number": n,
            "name": i.name,
            "handle": f"{i.name}@{config.bridge.domain}",
            "npub": nip19.npub_encode(i.pubkey),
            "pubkey": i.pubkey,
            "active": active is not None and active.id == i.id,
            "key": {"generated": "held by the bridge", "imported": "imported, held by the bridge", "bunker": "in your remote signer"}.get(i.key_origin, i.key_origin),
            "key_origin": i.key_origin,
            "exportable": i.key_origin != "bunker",
            "profile_room": i.profile_room_id,
            "tier": await effective_tier(app, i),
            "followers": await repository.count_followers(i.id),
            "display_name": fields.get("name") or i.name,
            "avatar_url": _https(fields.get("picture")),
        })
    admin = await is_admin(config, app.state.homeserver, user)
    return JSONResponse({
        "user": user, "domain": config.bridge.domain, "prefix": config.appservice.command_prefix,
        "ids": id_list, "room": await _room_kind(app, user, room_id, owned), "admin": admin, "is_admin": admin,
        "has_profile": bool(owned), "max_ids": config.bridge.max_ids_per_user,
        "web_viewer_url": config.nostr.web_viewer_url,
        "relays": _browse().configured_relays(app),
        "backfill_default": config.nostr.backfill_on_follow or 20,
    }, headers={"Cache-Control": "no-store"})


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


# -- admin: third-party access (as matrix-appservice-activitypub's Access tab) ---------


async def _admin(request: Request, room_id: str) -> str:
    """Checked on every admin call, not only by hiding the section: nothing
    stops a request straight at an endpoint."""
    user = await _member(request, room_id)
    if not await is_admin(request.app.state.config, request.app.state.homeserver, user):
        raise HTTPException(403, "Only a bridge admin can do that.")
    return user


@router.get("/widget/api/admin/grants")
async def admin_grants(request: Request, room_id: str) -> JSONResponse:
    """Every grant, as "[allowed" lists them. Checked here as well as by
    hiding the card: nothing stops a request straight at this endpoint."""
    await _admin(request, room_id)
    grants = await request.app.state.repository.list_third_party_allows()
    return JSONResponse({
        "third_party_access": request.app.state.config.bridge.third_party_access,
        "grants": [{"rule_type": g.rule_type, "value": g.value, "tier": g.tier, "granted_by": g.granted_by,
                    "granted_at": g.granted_at} for g in grants],
    }, headers={"Cache-Control": "no-store"})


def _grant_argument(body: dict[str, Any], *, with_tier: bool) -> str:
    from bridge.commands import _ALLOW_RULE_TYPES

    rule_type, value, tier = body.get("rule_type"), body.get("value"), body.get("tier")
    if rule_type not in _ALLOW_RULE_TYPES or not isinstance(value, str) or not value.strip() or " " in value.strip():
        raise HTTPException(400, f"rule_type must be one of {', '.join(_ALLOW_RULE_TYPES)}, and a value is required.")
    argument = f"{rule_type} {value.strip()}"
    if with_tier and rule_type != "room" and isinstance(tier, str) and tier.strip():
        argument += f" {tier.strip()}"
    return argument


@router.post("/widget/api/admin/allow")
async def admin_allow(request: Request) -> JSONResponse:
    """The same as "[allow" (it answers in the room, a homeserver asking
    for its confirm there)."""
    from bridge.commands import _handle_allow

    body = await _body(request)
    user = await _admin(request, body["room_id"])
    await _handle_allow(request.app, body["room_id"], user, _grant_argument(body, with_tier=True))
    return JSONResponse({"ok": True})


@router.post("/widget/api/admin/disallow")
async def admin_disallow(request: Request) -> JSONResponse:
    from bridge.commands import _handle_disallow

    body = await _body(request)
    user = await _admin(request, body["room_id"])
    await _handle_disallow(request.app, body["room_id"], user, _grant_argument(body, with_tier=False))
    return JSONResponse({"ok": True})


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


from bridge.widget_api import router as _more  # noqa: E402  (the rest of the widget's API)

router.include_router(_more)


def _browse():
    from bridge import widget_browse

    return widget_browse
