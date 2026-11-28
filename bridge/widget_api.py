"""The widget's API beyond sign-in, IDs, keys and grants (``bridge.widget``):
browsing and importing (Home), editing profiles (Profile), communities,
room recovery and DM filters (Settings), and the Admin section's
identities, rooms, backstage space, bans and settings.

Every call needs a widget session and membership of the room the widget
is in (``room_id``, as a query parameter or in the body); admin calls also
need a bridge admin, checked on every request. Reads answer with
``Cache-Control: no-store``. Actions that have a command run its handler,
which answers in the room as the command would.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from bridge import ids
from bridge.homeserver_client import HomeserverError
from bridge.nostr import nip19
from bridge.users import bot_mxid
from bridge.widget import _admin, _body, _member, _own_id, _profile_fields, _https

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _json(data: Any) -> JSONResponse:
    return JSONResponse(data, headers=_NO_STORE)


async def _chosen_id(request: Request, user: str, reference: Any):
    """The ID a call names, or the user's Active one."""
    if reference not in (None, ""):
        return await _own_id(request, user, reference)
    active = await ids.active_id(request.app, user)
    if active is None:
        raise HTTPException(404, "You don't have a Nostr ID yet.")
    return active


def _until(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(0, int(value))
    except ValueError:
        raise HTTPException(400, "until must be a number.") from None


def _pubkey(value: str) -> str:
    value = (value or "").strip()
    try:
        if value.startswith("npub1"):
            return nip19.npub_decode(value)
        if value.startswith("nprofile1"):
            decoded = nip19.decode(value)
            if isinstance(decoded, nip19.Profile):
                return decoded.pubkey
        if len(value) == 64:
            return bytes.fromhex(value).hex()
    except (nip19.Nip19Error, ValueError):
        pass
    raise HTTPException(400, "That isn't a Nostr account key.")


# -- Home: browsing ----------------------------------------------------------------------------


@router.get("/widget/api/browse/feed")
async def browse_feed(request: Request, room_id: str, source: str = "global", relay: str = "",
                      until: str | None = None) -> JSONResponse:
    from bridge import widget_browse

    user = await _member(request, room_id)
    if source not in ("following", "global", "relay"):
        raise HTTPException(400, "source must be following, global or relay.")
    active = await ids.active_id(request.app, user)
    try:
        data = await widget_browse.feed(request.app, source=source, relay=relay, until=_until(until), active=active)
    except widget_browse.BrowseProblem as exc:
        raise HTTPException(400, str(exc)) from exc
    return _json(data)


@router.get("/widget/api/browse/thread")
async def browse_thread(request: Request, room_id: str, ref: str) -> JSONResponse:
    from bridge import widget_browse

    user = await _member(request, room_id)
    try:
        data = await widget_browse.thread(request.app, ref, active=await ids.active_id(request.app, user))
    except widget_browse.BrowseProblem as exc:
        raise HTTPException(404, str(exc)) from exc
    return _json(data)


@router.get("/widget/api/browse/account")
async def browse_account(request: Request, room_id: str, pubkey: str) -> JSONResponse:
    from bridge import widget_browse

    user = await _member(request, room_id)
    return _json(await widget_browse.account(request.app, _pubkey(pubkey), active=await ids.active_id(request.app, user)))


@router.get("/widget/api/browse/account/posts")
async def browse_account_posts(request: Request, room_id: str, pubkey: str, until: str | None = None) -> JSONResponse:
    from bridge import widget_browse

    user = await _member(request, room_id)
    return _json(await widget_browse.account_posts(request.app, _pubkey(pubkey), _until(until),
                                                   active=await ids.active_id(request.app, user)))


@router.get("/widget/api/browse/search")
async def browse_search(request: Request, room_id: str, q: str = "") -> JSONResponse:
    from bridge import widget_browse

    user = await _member(request, room_id)
    return _json(await widget_browse.search(request.app, q[:300], active=await ids.active_id(request.app, user)))


@router.post("/widget/api/import")
async def import_post(request: Request) -> JSONResponse:
    """Bring one post into Matrix, as "[import" does, and say where it is,
    so the post's card can link to it straight away."""
    from bridge.importing import ImportProblem, fetch_post, import_post as bring_in, parse_post_reference
    from bridge.messages import invite_to_room

    body = await _body(request)
    user = await _member(request, body["room_id"])
    reference = body.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        raise HTTPException(400, "Which post?")
    app = request.app
    if len(reference.strip()) == 64 and all(c in "0123456789abcdef" for c in reference.strip()):
        reference = nip19.note_encode(reference.strip())
    try:
        raw = await fetch_post(app, parse_post_reference(reference))
        before = await app.state.repository.get_event(raw["id"])
        row = await bring_in(app, raw)
    except ImportProblem as exc:
        raise HTTPException(400, str(exc)) from exc
    await invite_to_room(app, row.room_id, user)
    return JSONResponse({"ok": True, "room_id": row.room_id, "event_id": row.matrix_event_id,
                         "already": before is not None and bool(before.matrix_event_id)})


# -- Profile -----------------------------------------------------------------------------------


@router.get("/widget/api/profile")
async def own_profile(request: Request, room_id: str, id: str = "") -> JSONResponse:
    from bridge.identity_events import PROFILE_EXTRA_STATE_TYPE

    user = await _member(request, room_id)
    app = request.app
    repository = app.state.repository
    config = app.state.config
    nostr_id = await _chosen_id(request, user, id)
    fields = await _profile_fields(app, nostr_id)
    editable: dict[str, str] = {}
    if nostr_id.profile_room_id:
        bot = bot_mxid(config)
        homeserver = app.state.homeserver
        try:
            name = await homeserver.get_room_state(nostr_id.profile_room_id, "m.room.name", as_user_id=bot) or {}
            topic = await homeserver.get_room_state(nostr_id.profile_room_id, "m.room.topic", as_user_id=bot) or {}
            extra = await homeserver.get_room_state(nostr_id.profile_room_id, PROFILE_EXTRA_STATE_TYPE, as_user_id=bot) or {}
        except HomeserverError:
            name, topic, extra = {}, {}, {}
        editable = {"name": str(name.get("name") or ""), "about": str(topic.get("topic") or ""),
                    "website": str(extra.get("website") or ""), "lud16": str(extra.get("lud16") or "")}
    owned = await repository.list_ids_for_user(user)
    return _json({
        "number": ids.id_number(owned, nostr_id), "name": nostr_id.name,
        "handle": f"{nostr_id.name}@{config.bridge.domain}", "pubkey": nostr_id.pubkey,
        "npub": nip19.npub_encode(nostr_id.pubkey),
        "display_name": fields.get("name") or nostr_id.name, "about": fields.get("about") or "",
        "avatar_url": _https(fields.get("picture")), "banner_url": _https(fields.get("banner")),
        "website": fields.get("website") or "", "lud16": fields.get("lud16") or "",
        "room_id": nostr_id.profile_room_id, "editable": bool(nostr_id.profile_room_id), "fields": editable,
        "followers_count": await repository.count_followers(nostr_id.id),
        "following_count": len(await repository.list_follows(nostr_id.id)),
        "public_url": config.nostr.web_viewer_url + nip19.npub_encode(nostr_id.pubkey),
    })


async def _sync(app, nostr_id) -> None:
    from bridge.identity_events import sync_identity

    try:
        await sync_identity(app, await app.state.repository.get_id(nostr_id.id) or nostr_id)
    except HomeserverError:
        logger.info("Could not publish the profile of %s yet", nostr_id.name, exc_info=True)


@router.post("/widget/api/profile/update")
async def update_profile(request: Request) -> JSONResponse:
    """Change an ID's profile: its Profile Room's name and topic, and the
    state its website and lightning address are kept in, then publish it
    (as editing the room does)."""
    from bridge.identity_events import PROFILE_EXTRA_STATE_TYPE

    body = await _body(request)
    user = await _member(request, body["room_id"])
    app = request.app
    nostr_id = await _chosen_id(request, user, body.get("id"))
    if not nostr_id.profile_room_id:
        raise HTTPException(400, "This ID has no Profile Room, so its profile follows your Matrix profile.")
    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    room = nostr_id.profile_room_id
    texts = {}
    for key, limit in (("name", 100), ("about", 2000), ("website", 300), ("lud16", 200)):
        value = body.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise HTTPException(400, f"{key} must be text.")
        texts[key] = value.strip()[:limit]
    if texts.get("website") and not texts["website"].startswith(("https://", "http://")):
        raise HTTPException(400, "The website must be an http(s):// link.")
    try:
        if "name" in texts:
            await homeserver.send_state_event(room, "m.room.name", {"name": texts["name"]}, as_user_id=bot)
        if "about" in texts:
            await homeserver.send_state_event(room, "m.room.topic", {"topic": texts["about"]}, as_user_id=bot)
        if "website" in texts or "lud16" in texts:
            extra = dict(await homeserver.get_room_state(room, PROFILE_EXTRA_STATE_TYPE, as_user_id=bot) or {})
            for key in ("website", "lud16"):
                if key in texts:
                    if texts[key]:
                        extra[key] = texts[key]
                    else:
                        extra.pop(key, None)
            await homeserver.send_state_event(room, PROFILE_EXTRA_STATE_TYPE, extra, as_user_id=bot)
    except HomeserverError as exc:
        raise HTTPException(502, "The homeserver refused that change; try again.") from exc
    await _sync(app, nostr_id)
    return JSONResponse({"ok": True})


@router.post("/widget/api/profile/image")
async def profile_image(request: Request, room_id: str, which: str, id: str = "") -> JSONResponse:
    """A new avatar or banner for an ID, sent as the request body."""
    from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE

    user = await _member(request, room_id)
    app = request.app
    if which not in ("avatar", "banner"):
        raise HTTPException(400, "which must be avatar or banner.")
    nostr_id = await _chosen_id(request, user, id)
    if not nostr_id.profile_room_id:
        raise HTTPException(400, "This ID has no Profile Room, so its profile follows your Matrix profile.")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(400, "That isn't an image.")
    data = await request.body()
    limit = min(_MAX_IMAGE_BYTES, app.state.config.nostr.max_media_mb * 1024 * 1024)
    if not data or len(data) > limit:
        raise HTTPException(400, f"Images can be at most {limit // (1024 * 1024)} MB.")
    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    try:
        mxc = await homeserver.upload_media(data, content_type, which, as_user_id=bot)
        state_type = "m.room.avatar" if which == "avatar" else PROFILE_BANNER_STATE_TYPE
        await homeserver.send_state_event(nostr_id.profile_room_id, state_type, {"url": mxc}, as_user_id=bot)
    except HomeserverError as exc:
        raise HTTPException(502, "The homeserver refused that image; try again.") from exc
    await _sync(app, nostr_id)
    return JSONResponse({"ok": True})


@router.get("/widget/api/following")
async def following(request: Request, room_id: str, id: str = "") -> JSONResponse:
    """Who an ID follows (the Active one by default), with what's known of
    each: names and avatars from the accounts' Matrix ghosts."""
    user = await _member(request, room_id)
    app = request.app
    repository = app.state.repository
    nostr_id = await _chosen_id(request, user, id)
    out = []
    for pubkey in list(await repository.list_follows(nostr_id.id))[:1000]:
        remote = await repository.get_remote_account(pubkey)
        local = await repository.get_id_by_pubkey(pubkey)
        name = None
        if remote is not None:
            try:
                name = (await app.state.homeserver.get_profile(remote.ghost_user_id)).get("displayname")
            except HomeserverError:
                name = None
        out.append({
            "pubkey": pubkey, "npub": nip19.npub_encode(pubkey),
            "display_name": name or (f"{local.name}@{app.state.config.bridge.domain}" if local else None)
            or (remote.nip05 if remote else None) or nip19.npub_encode(pubkey)[:16] + "…",
            "handle": (remote.nip05 if remote else None) or nip19.npub_encode(pubkey),
            "avatar_url": _https(remote.avatar_url) if remote else None,
            "room_id": remote.room_id if remote else (local.profile_room_id if local else None),
        })
    return _json({"id": nostr_id.name, "following": out})


@router.post("/widget/api/import_follows")
async def import_follows(request: Request) -> JSONResponse:
    """Follow, here, everyone an ID's key already follows on Nostr (its
    kind 3 list), in the background; a summary goes to the room."""
    from bridge import keys
    from bridge.messages import notice
    from bridge.tasks import run_in_background

    body = await _body(request)
    user = await _member(request, body["room_id"])
    app = request.app
    nostr_id = await _chosen_id(request, user, body.get("id"))
    handle = f"{nostr_id.name}@{app.state.config.bridge.domain}"

    async def bring_in() -> None:
        summary = await keys.import_history(app, nostr_id, follows=True, posts=0)
        await notice(app, body["room_id"], f"{handle}: {summary}.")

    run_in_background(app, bring_in(), name=f"import follows {nostr_id.id}")
    return JSONResponse({"ok": True, "message": f"Bringing in who {handle} follows; a summary will follow in the room."})


@router.post("/widget/api/rename")
async def rename(request: Request) -> JSONResponse:
    from bridge.commands import _handle_rename

    body = await _body(request)
    user = await _member(request, body["room_id"])
    new_name = str(body.get("new_name") or "").strip()
    if not new_name or " " in new_name:
        raise HTTPException(400, "Give the new name, without spaces.")
    await _handle_rename(request.app, body["room_id"], user, f"{body.get('id', '')} {new_name}")
    return JSONResponse({"ok": True})


@router.post("/widget/api/unlink")
async def unlink(request: Request) -> JSONResponse:
    from bridge.room_links import handle_unlink

    body = await _body(request)
    user = await _member(request, body["room_id"])
    await handle_unlink(request.app, body["room_id"], user, str(body.get("id") or ""))
    return JSONResponse({"ok": True})


@router.post("/widget/api/delete_profile")
async def delete_profile(request: Request) -> JSONResponse:
    """As "[delete profile", which asks for a confirmation in the room."""
    from bridge.id_deletion import handle_delete

    body = await _body(request)
    user = await _member(request, body["room_id"])
    await handle_delete(request.app, body["room_id"], user, f"profile {body.get('id') or ''}".strip())
    return JSONResponse({"ok": True})


# -- Settings ----------------------------------------------------------------------------------


_RECOVERY_KINDS = ("profile", "notifications", "space")


async def _recovery_room(app, user: str, kind: str) -> str | None:
    if kind == "profile":
        active = await ids.active_id(app, user)
        return active.profile_room_id if active is not None else None
    rooms = await app.state.repository.get_user_rooms(user)
    if kind == "notifications":
        return rooms.notifications_room_id
    if kind == "space":
        return rooms.space_room_id
    raise HTTPException(400, "Unknown room kind.")


@router.get("/widget/api/recovery_rooms")
async def recovery_rooms(request: Request, room_id: str) -> JSONResponse:
    """The Active ID's Profile Room, the notifications room and the space:
    whether each exists, and whether the user is in it right now."""
    user = await _member(request, room_id)
    app = request.app
    bot = bot_mxid(app.state.config)
    out = {}
    for kind in _RECOVERY_KINDS:
        room = await _recovery_room(app, user, kind)
        if room is None:
            out[kind] = {"room_id": None, "status": "not_created"}
            continue
        try:
            members = await app.state.homeserver.get_joined_members(room, as_user_id=bot)
        except HomeserverError:
            members = []
        out[kind] = {"room_id": room, "status": "open" if user in members else "rejoin"}
    return _json(out)


@router.post("/widget/api/rejoin_room")
async def rejoin_room(request: Request) -> JSONResponse:
    """Invite the user (only ever the caller) back into one of those rooms."""
    body = await _body(request)
    user = await _member(request, body["room_id"])
    app = request.app
    room = await _recovery_room(app, user, str(body.get("kind") or ""))
    if room is None:
        raise HTTPException(404, "That room hasn't been made yet.")
    try:
        await app.state.homeserver.invite_user(room, user, as_user_id=bot_mxid(app.state.config))
    except HomeserverError as exc:
        raise HTTPException(502, "The homeserver refused the invite; try again.") from exc
    return JSONResponse({"ok": True})


@router.get("/widget/api/dm_filters")
async def get_dm_filters(request: Request, room_id: str) -> JSONResponse:
    from bridge import dm_filters

    user = await _member(request, room_id)
    settings = await dm_filters.load(request.app, user)
    return _json({"keywords": list(settings.keywords), "languages": list(settings.languages),
                  "require_name": settings.require_name, "domains": list(settings.domains),
                  "filtered": settings.filtered})


@router.post("/widget/api/dm_filters")
async def set_dm_filters(request: Request) -> JSONResponse:
    """Replace the user's DM request filters (as "[dmfilter" changes them)."""
    from dataclasses import replace

    from bridge import dm_filters

    body = await _body(request)
    user = await _member(request, body["room_id"])
    current = await dm_filters.load(request.app, user)

    def strings(key: str) -> list[str]:
        value = body.get(key) or []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise HTTPException(400, f"{key} must be a list of text.")
        return [v.strip() for v in value if v.strip()]

    updated = dm_filters.DmFilter(filtered=current.filtered, require_name=bool(body.get("require_name")))
    for word in strings("keywords"):
        updated, problem = dm_filters.update(updated, f"keyword add {word}")
        if problem:
            raise HTTPException(400, problem)
    for domain in strings("domains"):
        updated, problem = dm_filters.update(updated, f"domain add {domain}")
        if problem:
            raise HTTPException(400, f"{domain}: {problem}")
    languages = strings("languages")
    if languages:
        updated, problem = dm_filters.update(updated, "language " + " ".join(languages))
        if problem:
            raise HTTPException(400, problem)
    await dm_filters.save(request.app, user, replace(updated, filtered=current.filtered))
    return JSONResponse({"ok": True})


# -- Communities -------------------------------------------------------------------------------


@router.get("/widget/api/communities")
async def communities_list(request: Request, room_id: str) -> JSONResponse:
    from bridge import communities

    user = await _member(request, room_id)
    return _json(await communities.list_for(request.app, user))


@router.post("/widget/api/communities/join")
async def communities_join(request: Request) -> JSONResponse:
    """As "[join": a Concord invite link (or a Direct Invite's community
    name), or a NIP-29 group reference. Answers in the room."""
    from bridge import concord, groups

    body = await _body(request)
    user = await _member(request, body["room_id"])
    link = str(body.get("link") or "").strip()
    if not link:
        raise HTTPException(400, "Paste an invite link or a group reference.")
    reference = groups.parse_reference(link)
    if reference is not None:
        await groups.handle_join(request.app, body["room_id"], user, reference)
    else:
        await concord.handle_join(request.app, body["room_id"], user, link)
    return JSONResponse({"ok": True})


@router.post("/widget/api/communities/create")
async def communities_create(request: Request) -> JSONResponse:
    from bridge.concord_owner import create

    body = await _body(request)
    user = await _member(request, body["room_id"])
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the community a name.")
    await create(request.app, body["room_id"], user, name)
    return JSONResponse({"ok": True})


@router.post("/widget/api/communities/leave")
async def communities_leave(request: Request) -> JSONResponse:
    """Leave a Concord community or NIP-29 group, with every ID of the
    user's in it (``bridge.communities``)."""
    from bridge import communities, concord

    body = await _body(request)
    user = await _member(request, body["room_id"])
    target_id = str(body.get("id") or "")
    found = await communities.find(request.app, user, target_id) if target_id else None
    if found is None or found[0] != body.get("type"):
        raise HTTPException(404, "You're not in that community.")
    try:
        message = await communities.leave(request.app, user, *found)
    except concord.ConcordProblem as exc:
        raise HTTPException(400, f"Couldn't leave it: {exc}.") from exc
    return JSONResponse({"ok": True, "message": message})


async def _owner_action(request: Request, action, argument_key: str) -> JSONResponse:
    body = await _body(request)
    user = await _member(request, body["room_id"])
    community_id = str(body.get("community") or "")
    if not community_id:
        raise HTTPException(400, "Which community?")
    await action(request.app, body["room_id"], user, str(body.get(argument_key) or "").strip(), community_id=community_id)
    return JSONResponse({"ok": True})


@router.post("/widget/api/communities/concord/link")
async def concord_link(request: Request) -> JSONResponse:
    from bridge.concord_owner import make_link

    return await _owner_action(request, make_link, "label")


@router.post("/widget/api/communities/concord/revoke")
async def concord_revoke(request: Request) -> JSONResponse:
    from bridge.concord_owner import revoke

    return await _owner_action(request, revoke, "number")


@router.post("/widget/api/communities/concord/channel")
async def concord_channel(request: Request) -> JSONResponse:
    """Make the room the widget is in a channel of the community, as
    "[concord channel" run there does."""
    from bridge.concord_owner import add_channel

    return await _owner_action(request, add_channel, "name")


@router.post("/widget/api/communities/concord/invite")
async def concord_invite(request: Request) -> JSONResponse:
    from bridge.concord_owner import invite

    return await _owner_action(request, invite, "who")


# -- Admin -------------------------------------------------------------------------------------


@router.get("/widget/api/admin/identities")
async def admin_identities(request: Request, room_id: str) -> JSONResponse:
    """Every Nostr ID on this bridge. Never includes key material."""
    from bridge.shadow_ids import effective_tier

    await _admin(request, room_id)
    app = request.app
    domain = app.state.config.bridge.domain
    out = []
    for nostr_id in await app.state.repository.list_all_ids():
        out.append({
            "name": nostr_id.name, "handle": f"{nostr_id.name}@{domain}", "npub": nip19.npub_encode(nostr_id.pubkey),
            "owner_matrix_id": nostr_id.matrix_user_id, "room_id": nostr_id.profile_room_id,
            "kind": "profile" if nostr_id.profile_room_id else "shadow", "tier": await effective_tier(app, nostr_id),
            "key_origin": nostr_id.key_origin,
            "is_third_party": nostr_id.matrix_user_id.rsplit(":", 1)[-1] != app.state.config.homeserver.server_name,
        })
    return _json({"identities": out})


@router.get("/widget/api/admin/rooms")
async def admin_rooms(request: Request, room_id: str) -> JSONResponse:
    """Every Matrix room the bridge keeps for something on Nostr."""
    from bridge import concord, groups

    await _admin(request, room_id)
    app = request.app
    repository = app.state.repository
    domain = app.state.config.bridge.domain
    rooms: list[dict[str, Any]] = []
    users: set[str] = set()
    for nostr_id in await repository.list_all_ids():
        users.add(nostr_id.matrix_user_id)
        if nostr_id.profile_room_id:
            rooms.append({"room_id": nostr_id.profile_room_id, "kind": "Profile", "maps_to": f"{nostr_id.name}@{domain}",
                          "owner_matrix_id": nostr_id.matrix_user_id})
    for user in sorted(users):
        user_rooms = await repository.get_user_rooms(user)
        if user_rooms.space_room_id:
            rooms.append({"room_id": user_rooms.space_room_id, "kind": "Space", "maps_to": "personal space",
                          "owner_matrix_id": user})
        if user_rooms.notifications_room_id:
            rooms.append({"room_id": user_rooms.notifications_room_id, "kind": "Notifications",
                          "maps_to": "notifications", "owner_matrix_id": user})
    for remote in await repository.list_remote_accounts():
        if remote.room_id:
            rooms.append({"room_id": remote.room_id, "kind": "Remote User", "maps_to": remote.nip05 or nip19.npub_encode(remote.pubkey),
                          "owner_matrix_id": None})
    for community in await concord.all_communities(app):
        if community.space:
            rooms.append({"room_id": community.space, "kind": "Concord space", "maps_to": community.name, "owner_matrix_id": None})
        for info in community.channels.values():
            if info.get("room") and not info.get("deleted"):
                rooms.append({"room_id": info["room"], "kind": "Concord channel",
                              "maps_to": f"#{info.get('name') or 'channel'} in {community.name}", "owner_matrix_id": None})
    for group in await groups.all_groups(app):
        if group.room:
            rooms.append({"room_id": group.room, "kind": "NIP-29 group", "maps_to": f"{group.name or group.id} on {group.host}",
                          "owner_matrix_id": None})
    for key, value in await repository.list_state("nip29:space:"):
        rooms.append({"room_id": value, "kind": "NIP-29 relay space", "maps_to": key[len("nip29:space:"):],
                      "owner_matrix_id": None})
    return _json({"rooms": rooms})


@router.get("/widget/api/admin/backstage_space")
async def admin_backstage_space(request: Request, room_id: str) -> JSONResponse:
    """The backstage space (``bridge.backstage_space``): made on first look
    while the feature is on."""
    from bridge.backstage_space import _STATE_KEY, ensure_backstage_space

    await _admin(request, room_id)
    app = request.app
    enabled = app.state.config.bridge.backstage_space_enabled
    space = await ensure_backstage_space(app) if enabled else await app.state.repository.get_state(_STATE_KEY)
    return _json({"enabled": enabled, "room_id": space})


@router.post("/widget/api/admin/backstage_space/invite")
async def admin_backstage_space_invite(request: Request) -> JSONResponse:
    """Invite the admin asking into the backstage space. Only on request:
    being in it lets one join every room that trusts it."""
    from bridge.backstage_space import _STATE_KEY, ensure_backstage_space

    body = await _body(request)
    user = await _admin(request, body["room_id"])
    app = request.app
    if app.state.config.bridge.backstage_space_enabled:
        space = await ensure_backstage_space(app)
    else:
        space = await app.state.repository.get_state(_STATE_KEY)
    if not space:
        raise HTTPException(409, "The backstage space doesn't exist yet; turn on bridge.backstage_space_enabled first.")
    try:
        await app.state.homeserver.invite_user(space, user, as_user_id=bot_mxid(app.state.config))
    except HomeserverError as exc:
        raise HTTPException(502, "The homeserver refused that invite; try again.") from exc
    return JSONResponse({"ok": True, "room_id": space})


@router.get("/widget/api/admin/bans")
async def admin_bans(request: Request, room_id: str) -> JSONResponse:
    await _admin(request, room_id)
    guard = getattr(request.app.state, "spam", None)
    bans = await guard.list_bans() if guard is not None else []
    return _json({"bans": [{"kind": b.kind, "key": b.key, "npub": nip19.npub_encode(b.key) if b.kind == "pubkey" else None,
                            "until": b.until, "reason": b.reason, "level": b.level, "created_at": b.created_at}
                           for b in bans]})


_BAN_DURATIONS = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800, "30d": 2592000, "forever": None}


def _ban_target(body: dict[str, Any]) -> tuple[str, str]:
    import ipaddress

    kind = body.get("kind")
    key = str(body.get("key") or "").strip()
    if kind == "ip":
        try:
            ipaddress.ip_address(key)
        except ValueError:
            raise HTTPException(400, "That isn't an IP address.") from None
        return "ip", key
    if kind == "pubkey":
        return "pubkey", _pubkey(key)
    raise HTTPException(400, "kind must be pubkey or ip.")


@router.post("/widget/api/admin/ban")
async def admin_ban(request: Request) -> JSONResponse:
    """Ban an account or IP from the relay, as "[ban" does (without its
    purge, which stays a command)."""
    from datetime import timedelta

    body = await _body(request)
    user = await _admin(request, body["room_id"])
    kind, key = _ban_target(body)
    duration = str(body.get("duration") or "7d")
    if duration not in _BAN_DURATIONS:
        raise HTTPException(400, f"duration must be one of {', '.join(_BAN_DURATIONS)}.")
    seconds = _BAN_DURATIONS[duration]
    reason = str(body.get("reason") or "").strip()[:200] or f"banned by {user}"
    await request.app.state.spam.ban(kind, key, timedelta(seconds=seconds) if seconds else None, reason)
    return JSONResponse({"ok": True})


@router.post("/widget/api/admin/unban")
async def admin_unban(request: Request) -> JSONResponse:
    body = await _body(request)
    await _admin(request, body["room_id"])
    kind, key = _ban_target(body)
    await request.app.state.spam.unban(kind, key)
    return JSONResponse({"ok": True})


def _overrides_off() -> HTTPException:
    return HTTPException(400, "Settings can't be changed here: bridge.allow_db_setting_overrides is off in config.yaml.")


@router.get("/widget/api/admin/settings")
async def admin_settings(request: Request, room_id: str) -> JSONResponse:
    """Every setting, editable or not, so the page documents them all. A
    secret's value is never sent."""
    from bridge.bridge_settings import known_settings, stored_overrides, value_of

    await _admin(request, room_id)
    app = request.app
    enabled = app.state.base_config.bridge.allow_db_setting_overrides
    stored = await stored_overrides(app.state.repository) if enabled else {}
    config = app.state.config
    settings = [
        {
            "key": d.key, "section": d.section, "label": d.label, "description": d.description,
            "type": d.value_type, "enum_choices": list(d.enum_choices) if d.enum_choices else None,
            "editable": d.editable and enabled, "sensitive": d.sensitive,
            "source": "database" if d.key in stored and d.editable else "config",
            "value": None if d.sensitive else value_of(config, d),
        }
        for d in known_settings()
    ]
    return _json({"overrides_enabled": enabled, "settings": settings})


@router.post("/widget/api/admin/settings/{key}")
async def admin_set_setting(request: Request, key: str) -> JSONResponse:
    from bridge.bridge_settings import SettingError, apply_override, definition_of, refresh, value_of

    body = await _body(request)
    user = await _admin(request, body["room_id"])
    app = request.app
    if not app.state.base_config.bridge.allow_db_setting_overrides:
        raise _overrides_off()
    try:
        await apply_override(app.state.repository, key, body.get("value"), base_config=app.state.base_config)
    except SettingError as exc:
        raise HTTPException(400, str(exc)) from exc
    config = await refresh(app)
    definition = definition_of(key)
    assert definition is not None
    logger.info("Setting %s changed by %s", key, user)
    return JSONResponse({"key": key, "value": None if definition.sensitive else value_of(config, definition),
                         "source": "database"})


@router.post("/widget/api/admin/settings/{key}/reset")
async def admin_reset_setting(request: Request, key: str) -> JSONResponse:
    from bridge.bridge_settings import SettingError, clear_override, definition_of, refresh, value_of

    body = await _body(request)
    user = await _admin(request, body["room_id"])
    app = request.app
    if not app.state.base_config.bridge.allow_db_setting_overrides:
        raise _overrides_off()
    try:
        await clear_override(app.state.repository, key)
    except SettingError as exc:
        raise HTTPException(400, str(exc)) from exc
    config = await refresh(app)
    definition = definition_of(key)
    assert definition is not None
    logger.info("Setting %s reset to config.yaml by %s", key, user)
    return JSONResponse({"key": key, "value": None if definition.sensitive else value_of(config, definition),
                         "source": "config"})
