"""Moving an ID's Profile Room: ``[unlink``, ``[link`` and ``[create room``.

- ``[unlink <id>`` (confirmed): the room stops being the ID's Profile Room.
  Its history stays where it is; nothing new said there is published. The
  ID keeps its key, handle and followers, and is left without a room (and
  none is made for it automatically) until it gets one again.
- ``[link <id> [keep]`` (confirmed), sent in an existing room: that room
  becomes the ID's Profile Room, moving it from its old one if it had one.
  The bot must be in the room (invite it first) with enough power to set
  the room's profile state, and the sender must be an admin there. By
  default the room takes on the ID's current profile, so the ID's Nostr
  profile doesn't change; with ``keep`` the room's own name, topic and
  avatar become the ID's profile instead.
- ``[create room <id>``: a fresh Profile Room for an ID that has none.

Posts already published stay as they are: they were signed by the ID's key
and their Matrix events stay in whichever room they were posted in.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI

from bridge import confirm, ids
from bridge.bridge_info import _EVENT_TYPES as BRIDGE_INFO_TYPES
from bridge.bridge_info import PROTOCOL_ID
from bridge.homeserver_client import HomeserverError
from bridge.identity_events import PROFILE_EXTRA_STATE_TYPE, sync_identity
from bridge.messages import notice, room_link_html
from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE, SOCIAL_PROFILE_USER_ID_STATE_TYPE
from bridge.repository import NostrId
from bridge.spaces import add_room_to_space, profile_room_space_order, remove_room_from_space
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

LINK_ACTION = "link_room"
UNLINK_ACTION = "unlink_room"

# The room state a Profile Room's profile is made of.
_PROFILE_TYPES = ("m.room.name", "m.room.topic", "m.room.avatar", PROFILE_BANNER_STATE_TYPE, PROFILE_EXTRA_STATE_TYPE)
# What the bot must be able to set in a linked room.
_BOT_STATE_TYPES = (*_PROFILE_TYPES, SOCIAL_PROFILE_USER_ID_STATE_TYPE, *BRIDGE_INFO_TYPES)
_ADMIN_LEVEL = 100
_CREATOR = 1 << 62  # room v12+: creators outrank every power level


def _handle(app: FastAPI, nostr_id: NostrId) -> str:
    return f"{nostr_id.name}@{app.state.config.bridge.domain}"


# -- room state helpers --------------------------------------------------------


async def _state(app: FastAPI, room_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    events = await app.state.homeserver.get_full_state(room_id, as_user_id=bot_mxid(app.state.config))
    return {(e.get("type", ""), e.get("state_key", "")): e for e in events}


def _power_of(state: dict[tuple[str, str], dict[str, Any]], user: str) -> int:
    create = state.get(("m.room.create", "")) or {}
    content = create.get("content") or {}
    version = str(content.get("room_version") or "1")
    if version.isdigit() and int(version) >= 12:
        creators = {create.get("sender"), *(content.get("additional_creators") or [])}
        if user in creators:
            return _CREATOR
    levels = (state.get(("m.room.power_levels", "")) or {}).get("content")
    if levels is None:
        # No power levels yet: the creator has 100, everyone else 0.
        return 100 if user == create.get("sender") else 0
    value = (levels.get("users") or {}).get(user, levels.get("users_default", 0))
    return int(value) if isinstance(value, int) else 0


def _level_for_state(state: dict[tuple[str, str], dict[str, Any]], event_type: str) -> int:
    levels = (state.get(("m.room.power_levels", "")) or {}).get("content")
    if levels is None:
        return 0
    value = (levels.get("events") or {}).get(event_type, levels.get("state_default", 50))
    return int(value) if isinstance(value, int) else 50


def _other_bridges(state: dict[tuple[str, str], dict[str, Any]]) -> list[str]:
    """Other bridges marking this room as theirs (``m.bridge`` state not
    ours), by their protocol's name: a room shared with sibling bridges."""
    names = []
    for (event_type, state_key), event in state.items():
        content = event.get("content") or {}
        if event_type != "m.bridge" or not content or state_key.startswith(f"{PROTOCOL_ID}://"):
            continue
        protocol = content.get("protocol") or {}
        name = protocol.get("displayname") or protocol.get("id") or "another bridge"
        if name not in names:
            names.append(str(name))
    return names


def _owner_marked(state: dict[tuple[str, str], dict[str, Any]], owner: str) -> bool:
    """Whether MSC4501 already says the room is ``owner``'s profile (set by
    this bridge or a sibling)."""
    content = (state.get((SOCIAL_PROFILE_USER_ID_STATE_TYPE, "")) or {}).get("content") or {}
    return content.get("user_id") == owner


# What each piece of room state the bot sets is for, in plain words.
_STATE_PURPOSES = {
    "m.room.name": "change the room's name",
    "m.room.topic": "change the room's topic",
    "m.room.avatar": "change the room's avatar",
    PROFILE_BANNER_STATE_TYPE: "change the room's banner",
    PROFILE_EXTRA_STATE_TYPE: "set the profile's website and Lightning address",
    SOCIAL_PROFILE_USER_ID_STATE_TYPE: "mark whose profile the room is (MSC4501)",
    "m.bridge": "mark the room as bridged to Nostr",
}


def _level_name(level: int) -> str:
    return "Admin" if level >= 100 else "Moderator" if level >= 50 else "a custom role"


def _power_warning(app: FastAPI, bot: str, have: int, needed: int, missing: list[str], retry: str) -> tuple[str, str]:
    purposes = list(dict.fromkeys(_STATE_PURPOSES.get(t, "") for t in missing if _STATE_PURPOSES.get(t)))
    head = (f"⚠️ I can't link this room yet: I need power level {needed} ({_level_name(needed)}) here, "
            f"and I have {have}.")
    why = "That lets me keep the room and the Nostr profile in sync. Right now I can't:"
    fix = f"Give {bot} power level {needed} in the room's settings (Roles & Permissions), then send this again:"
    plain = "\n".join([head, why, *(f"- {p}" for p in purposes), fix, retry])
    rich = (f"<p>⚠️ {html.escape(head)}</p><p>{html.escape(why)}</p><ul>"
            + "".join(f"<li>{html.escape(p)}</li>" for p in purposes)
            + f"</ul><p>{html.escape(fix)}</p><pre><code>{html.escape(retry)}</code></pre>")
    return plain, rich


async def _room_problem(
    app: FastAPI, room_id: str, sender: str, nostr_id: NostrId, retry: str
) -> tuple[str, str | None] | None:
    """Why ``room_id`` can't become ``nostr_id``'s Profile Room, if it can't,
    as (plain, html). ``retry`` is the command to send once it's fixed."""
    problem = await _room_problem_text(app, room_id, sender, nostr_id, retry)
    return (problem, None) if isinstance(problem, str) else problem


async def _room_problem_text(
    app: FastAPI, room_id: str, sender: str, nostr_id: NostrId, retry: str
) -> str | tuple[str, str] | None:
    repository = app.state.repository
    prefix = app.state.config.appservice.command_prefix
    linked = await repository.get_id_for_profile_room(room_id)
    if linked is not None:
        if linked.id == nostr_id.id:
            return f"This is already the Profile Room of {_handle(app, nostr_id)}."
        return f"This room is already the Profile Room of {_handle(app, linked)}. Unlink that first: {prefix}unlink {linked.name}"
    if await repository.get_remote_account_for_room(room_id) is not None:
        return "This room shows a Nostr account's posts; it can't be a Profile Room."
    rooms = await repository.get_user_rooms(sender)
    if room_id in (rooms.notifications_room_id, rooms.space_room_id):
        return "Your notifications room and your space can't be Profile Rooms."
    try:
        state = await _state(app, room_id)
    except HomeserverError:
        return "I can't read this room's state. Make sure I'm in the room."
    if ("m.room.encryption", "") in state:
        return "This room is encrypted, so I can't read it. A Profile Room has to be unencrypted."
    if ((state.get(("m.room.create", "")) or {}).get("content") or {}).get("type") == "m.space":
        return "A space can't be a Profile Room."
    mine = _power_of(state, sender)
    profile_editor = all(mine >= _level_for_state(state, t) for t in ("m.room.name", "m.room.topic", "m.room.avatar"))
    if mine < _ADMIN_LEVEL and not (_owner_marked(state, sender) and profile_editor):
        # Rooms the bridges create give their owner 99 and mark them as the
        # owner's (MSC4501): that's enough. Anywhere else, only an admin.
        return (
            f"You (not me) need to be an admin of this room to make it your Profile Room: power level 100, "
            f"and you have {mine}. If it's already your profile room on another bridge, being able to change "
            "its name, topic and avatar is enough."
        )
    bot = bot_mxid(app.state.config)
    # State another bridge already set correctly needn't be set again: rooms
    # the bridges create keep that one at 100, which the owner can't grant.
    types = [t for t in _BOT_STATE_TYPES if not (t == SOCIAL_PROFILE_USER_ID_STATE_TYPE and _owner_marked(state, sender))]
    have = _power_of(state, bot)
    missing = [t for t in types if _level_for_state(state, t) > have]
    if missing:
        needed = max(_level_for_state(state, t) for t in missing)
        return _power_warning(app, bot, have, needed, missing, retry)
    return None


# -- the ID's current profile, applied to a room ---------------------------------


async def _current_profile(app: FastAPI, nostr_id: NostrId) -> dict[str, dict[str, Any]]:
    """The ID's profile as room state contents, by state type: copied from
    its current Profile Room if it has one, else from its last published
    profile."""
    bot = bot_mxid(app.state.config)
    if nostr_id.profile_room_id:
        try:
            state = await _state(app, nostr_id.profile_room_id)
        except HomeserverError:
            state = {}
        found = {t: dict((state.get((t, "")) or {}).get("content") or {}) for t in _PROFILE_TYPES}
        found = {t: c for t, c in found.items() if c}
        if found:
            return found
    from bridge.event_sources import rebuild
    from bridge.remote import _fetch_image, parse_profile

    row = await app.state.repository.get_latest_event(nostr_id.pubkey, 0)
    if row is None:
        return {}
    rebuilt = await rebuild(app, row)
    if rebuilt.status != "ok" or rebuilt.event is None:
        return {}
    fields = parse_profile(rebuilt.event)
    out: dict[str, dict[str, Any]] = {}
    if fields.get("name"):
        out["m.room.name"] = {"name": fields["name"]}
    if fields.get("about"):
        out["m.room.topic"] = {"topic": fields["about"]}
    for field, state_type in (("picture", "m.room.avatar"), ("banner", PROFILE_BANNER_STATE_TYPE)):
        url = fields.get(field)
        if not url:
            continue
        mxc = await _own_blob_mxc(app, url)
        if mxc is None:
            image = await _fetch_image(app, url)
            if image is not None:
                try:
                    mxc = await app.state.homeserver.upload_media(image[0], image[1], field, as_user_id=bot)
                except HomeserverError:
                    mxc = None
        if mxc:
            out[state_type] = {"url": mxc}
    extra = {k: fields[k] for k in ("website", "lud16") if fields.get(k)}
    if extra:
        out[PROFILE_EXTRA_STATE_TYPE] = extra
    return out


async def _own_blob_mxc(app: FastAPI, url: str) -> str | None:
    """The Matrix media behind one of the bridge's own media URLs."""
    from bridge.media_routes import BLOB_PATH_RE

    base = app.state.config.nostr.relay_http_base
    if not url.startswith(base + "/"):
        return None
    match = BLOB_PATH_RE.match(url[len(base):])
    blob = await app.state.repository.get_blob(match.group(1)) if match else None
    return blob.mxc if blob else None


async def _apply_profile(app: FastAPI, room_id: str, profile: dict[str, dict[str, Any]]) -> None:
    bot = bot_mxid(app.state.config)
    for state_type in _PROFILE_TYPES:
        content = profile.get(state_type)
        if content is None:
            continue
        try:
            await app.state.homeserver.send_state_event(room_id, state_type, content, as_user_id=bot)
        except HomeserverError:
            logger.info("Could not set %s in %s", state_type, room_id, exc_info=True)


# -- attaching and detaching -------------------------------------------------------


async def _detach(app: FastAPI, nostr_id: NostrId, room_id: str, *, moved_to: str | None) -> None:
    """Make ``room_id`` stop looking like ``nostr_id``'s Profile Room."""
    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    clears = [(t, f"{PROTOCOL_ID}://{nostr_id.pubkey}") for t in BRIDGE_INFO_TYPES]
    try:
        shared = bool(_other_bridges(await _state(app, room_id)))
    except HomeserverError:
        shared = True  # when unsure, leave the shared marker alone
    if not shared:
        # Whose profile the room is: only cleared when no other bridge uses it.
        clears.insert(0, (SOCIAL_PROFILE_USER_ID_STATE_TYPE, ""))
    for state_type, state_key in clears:
        try:
            await homeserver.send_state_event(room_id, state_type, {}, as_user_id=bot, state_key=state_key)
        except HomeserverError:
            logger.info("Could not clear %s in %s", state_type, room_id, exc_info=True)
    await remove_room_from_space(app, nostr_id.matrix_user_id, room_id)
    where = f" Its Profile Room is now {moved_to}." if moved_to else ""
    try:
        await notice(
            app, room_id,
            f"This is no longer the Profile Room of {_handle(app, nostr_id)}: nothing said here is published "
            f"to Nostr any more. Posts already published stay as they are.{where}",
        )
    except HomeserverError:
        pass


async def _attach(app: FastAPI, nostr_id: NostrId, room_id: str) -> NostrId:
    await app.state.repository.set_profile_room(nostr_id.id, room_id)
    try:
        if not _owner_marked(await _state(app, room_id), nostr_id.matrix_user_id):
            await app.state.homeserver.send_state_event(
                room_id, SOCIAL_PROFILE_USER_ID_STATE_TYPE, {"user_id": nostr_id.matrix_user_id},
                as_user_id=bot_mxid(app.state.config),
            )
    except HomeserverError:
        logger.info("Could not mark %s as %s's profile room", room_id, nostr_id.matrix_user_id, exc_info=True)
    from bridge.room_widget import add_widget_once

    await add_widget_once(app, room_id)
    await add_room_to_space(app, nostr_id.matrix_user_id, room_id, order=profile_room_space_order(nostr_id.id))
    updated = await app.state.repository.get_id(nostr_id.id)
    assert updated is not None
    try:
        await sync_identity(app, updated)
    except HomeserverError:
        logger.warning("Could not publish the profile of %s", updated.name, exc_info=True)
    return updated


# -- [unlink ------------------------------------------------------------------------


async def handle_unlink(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    prefix = app.state.config.appservice.command_prefix
    reference = argument.split()[0] if argument.split() else ""
    if not reference:
        linked = await app.state.repository.get_id_for_profile_room(room_id)
        reference = linked.name if linked is not None and linked.matrix_user_id == sender else ""
    target = await ids.find_own_id(app, sender, reference) if reference else None
    if target is None:
        await notice(app, room_id, f'Which ID? "{prefix}unlink <number or ID>"; "{prefix}profiles" lists yours.')
        return
    if not target.profile_room_id:
        await notice(app, room_id, f"{_handle(app, target)} has no Profile Room.")
        return
    handle = _handle(app, target)
    details = (
        "The room and its history stay, but nothing said there is published to Nostr any more. "
        f"{handle} keeps its key, handle, followers and profile, and has no Profile Room until you give it one: "
        f'"{prefix}link {target.name}" in another room, or "{prefix}create room {target.name}" for a new one.'
    )
    room = target.profile_room_id
    await confirm.ask(
        app, room_id, action=UNLINK_ACTION, user=sender, args={"id": target.id, "room": room},
        plain=f"Unlink {handle} from its Profile Room {room}?\n{details}",
        rich=f"<p>Unlink <strong>{html.escape(handle)}</strong> from its Profile Room {room_link_html(room)}?</p>"
        f"<p>{html.escape(details)}</p>",
    )


async def _execute_unlink(app: FastAPI, room_id: str, sender: str, args: dict[str, Any]) -> None:
    target = await app.state.repository.get_id(args.get("id")) if isinstance(args.get("id"), int) else None
    if target is None or target.matrix_user_id != sender or target.profile_room_id != args.get("room"):
        await notice(app, room_id, "That has changed since; nothing was unlinked.")
        return
    old_room = target.profile_room_id
    assert old_room is not None
    await app.state.repository.unlink_profile_room(target.id)
    await _detach(app, target, old_room, moved_to=None)
    logger.info("Unlinked %s from %s", target.name, old_room)
    if room_id != old_room:
        await notice(app, room_id, f"Unlinked {_handle(app, target)} from {old_room}.")


# -- [link ----------------------------------------------------------------------------


async def handle_link(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    prefix = app.state.config.appservice.command_prefix
    words = argument.split()
    keep = len(words) > 1 and words[1].lower() == "keep"
    target = await ids.find_own_id(app, sender, words[0]) if words else None
    if target is None or (len(words) > 1 and not keep) or len(words) > 2:
        await notice(app, room_id, f'Usage: "{prefix}link <number or ID> [keep]", sent in the room to link. '
                                   f'"{prefix}profiles" lists your IDs.')
        return
    retry = f"{prefix}link {target.name}" + (" keep" if keep else "")
    problem = await _room_problem(app, room_id, sender, target, retry)
    if problem is not None:
        await notice(app, room_id, problem[0], html_message=problem[1])
        return
    handle = _handle(app, target)
    lines = [
        f"Everything you post in this room from now on is published publicly on Nostr as {handle}, "
        "and so are replies to your posts here from other people on this server who have Nostr IDs. "
        "Nothing already in the room is published.",
    ]
    try:
        others = _other_bridges(await _state(app, room_id))
    except HomeserverError:
        others = []
    if keep:
        lines.append(f"This room's name, topic and avatar become the Nostr profile of {handle}.")
    else:
        lines.append(f"This room's name, topic and avatar are replaced with the current profile of {handle} "
                     f'(add "keep" to the command to use the room\'s own instead).')
    if others:
        bridged = ", ".join(others)
        lines.append(f"This room is also bridged to {bridged}: posts here go there too, and "
                     + ("so does any change to its name, topic or avatar." if keep else
                        f"replacing its profile changes it on {bridged} as well. \"keep\" avoids that."))
    if target.profile_room_id:
        lines.append(f"{target.profile_room_id} stops being its Profile Room; its history stays there.")
    await confirm.ask(
        app, room_id, action=LINK_ACTION, user=sender,
        args={"id": target.id, "room": room_id, "keep": keep, "from": target.profile_room_id},
        plain=f"Make this room the Profile Room of {handle}?\n" + "\n".join(lines),
        rich=f"<p>Make this room the Profile Room of <strong>{html.escape(handle)}</strong>?</p>"
        + "".join(f"<p>{html.escape(line)}</p>" for line in lines),
    )


async def _execute_link(app: FastAPI, room_id: str, sender: str, args: dict[str, Any]) -> None:
    target = await app.state.repository.get_id(args.get("id")) if isinstance(args.get("id"), int) else None
    if target is None or target.matrix_user_id != sender or args.get("room") != room_id \
            or target.profile_room_id != args.get("from"):
        await notice(app, room_id, "That has changed since; nothing was linked.")
        return
    prefix = app.state.config.appservice.command_prefix
    retry = f"{prefix}link {target.name}" + (" keep" if args.get("keep") else "")
    problem = await _room_problem(app, room_id, sender, target, retry)
    if problem is not None:
        await notice(app, room_id, problem[0], html_message=problem[1])
        return
    profile = {} if args.get("keep") else await _current_profile(app, target)
    old_room = target.profile_room_id
    if profile:
        await _apply_profile(app, room_id, profile)
    linked = await _attach(app, target, room_id)
    if old_room:
        await _detach(app, target, old_room, moved_to=room_id)
    logger.info("Linked %s to %s (was %s)", target.name, room_id, old_room)
    await notice(app, room_id, f"This is now the Profile Room of {_handle(app, linked)}. "
                               "What you post here is published on Nostr.")


# -- [create room ------------------------------------------------------------------------


async def handle_create_room(app: FastAPI, room_id: str, sender: str, reference: str) -> None:
    from bridge.profile_rooms import create_profile_room, ensure_user_rooms

    prefix = app.state.config.appservice.command_prefix
    target = await ids.find_own_id(app, sender, reference) if reference else None
    if target is None:
        await notice(app, room_id, f'Which ID? "{prefix}create room <number or ID>"; "{prefix}profiles" lists yours.')
        return
    if target.profile_room_id:
        await notice(app, room_id, f"{_handle(app, target)} already has a Profile Room: {target.profile_room_id}. "
                                   f'Run "{prefix}unlink {target.name}" first to give it a new one.',
                     html_message=f"<strong>{html.escape(_handle(app, target))}</strong> already has a Profile Room: "
                     f"{room_link_html(target.profile_room_id)}. Run <code>{html.escape(prefix)}unlink "
                     f"{html.escape(target.name)}</code> first to give it a new one.")
        return
    profile = await _current_profile(app, target)
    try:
        created = await create_profile_room(app, target, first=False, profile=profile)
    except HomeserverError:
        logger.warning("Could not create a Profile Room for %s", target.name, exc_info=True)
        await notice(app, room_id, "I couldn't create the room. Try again in a minute.")
        return
    assert created.profile_room_id is not None
    await ensure_user_rooms(app, sender)
    try:
        await sync_identity(app, created)
    except HomeserverError:
        logger.warning("Could not publish the profile of %s", created.name, exc_info=True)
    await notice(
        app, room_id,
        f"Created a new Profile Room for {_handle(app, created)} and invited you: {created.profile_room_id}.",
        html_message=f"Created a new Profile Room for <strong>{html.escape(_handle(app, created))}</strong> "
        f"and invited you: {room_link_html(created.profile_room_id)}.",
    )


confirm.register(LINK_ACTION, _execute_link)
confirm.register(UNLINK_ACTION, _execute_unlink)
