"""NIP-29 relay-based groups.

A group lives on one relay, which holds its rules: who's a member, who's
an admin, what the group is called. Messages carry the group id in an
``h`` tag; the relay signs the group's metadata (kind 39000), admins
(39001) and members (39002) with its own key, and accepts or refuses
what members send. Nothing is encrypted, but a ``private`` group's relay
only lets members read, so the bridge authenticates (NIP-42) as the
member's identity.

Each relay is a Matrix Space, and each group joined from here a room in
it. Chat (kind 9, replies by ``q`` tag), threads (11, 1111), reactions and
deletions go both ways; admins' adds, removals and deletions (9000, 9001,
9005) come in. Only bookkeeping is kept: the group's settings and which
Nostr event is which Matrix event.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from fastapi import FastAPI, Request

from bridge.homeserver_client import HomeserverError
from bridge.messages import notice, room_link_html
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19
from bridge.nostr.relay_pool import normalize_relay_url
from bridge.users import bot_mxid, is_bridge_user

logger = logging.getLogger(__name__)

CHAT, THREAD, COMMENT, REACTION, DELETION = 9, 11, 1111, 7, 5
JOIN_REQUEST, LEAVE_REQUEST = 9021, 9022
PUT_USER, REMOVE_USER, DELETE_EVENT = 9000, 9001, 9005
METADATA, ADMINS, MEMBERS = 39000, 39001, 39002
CONTENT_KINDS = [CHAT, THREAD, COMMENT, REACTION, DELETION]
STATE_KINDS = [PUT_USER, REMOVE_USER, DELETE_EVENT]
GROUP_KINDS = [METADATA, ADMINS, MEMBERS]
_BACKFILL = 50
_MEMBER_GHOSTS = 300  # members brought into a room at most, from the relay's list
_PREVIOUS = 50

_GROUP = "nip29:g:{}"  # relay'id -> the group
_ROOM = "nip29:room:{}"  # Matrix room -> relay'id
_SPACE = "nip29:space:{}"  # relay -> its Space
_MSG = "nip29:m:{}"  # Nostr event -> [room, Matrix event, author]
_MX = "nip29:x:{}"  # Matrix event -> [Nostr event, author, kind]

READABLE = ("This group's relay only lets members read it, but the bridge can't encrypt Matrix rooms, "
            "so its messages are stored readable on this homeserver.")


class GroupProblem(Exception):
    pass


@dataclass
class Group:
    relay: str
    id: str
    name: str = ""
    about: str = ""
    picture: str = ""
    private: bool = False
    closed: bool = False
    restricted: bool = False
    relay_pubkey: str = ""  # who signs the group's metadata (the relay's "self")
    room: str | None = None
    members: list[int] = field(default_factory=list)  # local IDs in it
    admins: dict[str, list[str]] = field(default_factory=dict)
    present: list[str] = field(default_factory=list)  # members by the relay's list and adds
    meta_at: int = 0

    @property
    def key(self) -> str:
        return f"{self.relay}'{self.id}"

    @property
    def host(self) -> str:
        return re.sub(r"^wss?://", "", self.relay).rstrip("/")


# -- references -------------------------------------------------------------------------------

_NADDR = re.compile(r"naddr1[02-9ac-hj-np-z]+")
_HOST_ID = re.compile(r"^(?:(wss?)://)?([A-Za-z0-9.-]+(?::\d+)?(?:/[^\s']*)?)'([A-Za-z0-9_.-]+)$")


def parse_reference(text: str) -> tuple[str, str, str | None] | None:
    """(relay, group id, invite code) of a group reference: an ``naddr1…``
    of its kind 39000 (anywhere in a link, ``?invite=<code>`` after it),
    or ``relay.example'groupid``. ``None`` if it isn't one."""
    text = (text or "").strip().split()[0] if (text or "").strip() else ""
    code = None
    if "?invite=" in text:
        text, _, code = text.partition("?invite=")
        code = code.split("&")[0] or None
    match = _NADDR.search(text)
    if match:
        try:
            address = nip19.decode(match.group(0))
        except ValueError:
            return None
        if not isinstance(address, nip19.Address) or address.kind != METADATA or not address.relays:
            return None
        return normalize_relay_url(address.relays[0]), address.identifier, code
    match = _HOST_ID.match(text)
    if match:
        scheme, host, group_id = match.groups()
        return normalize_relay_url(f"{scheme or 'wss'}://{host}"), group_id, code
    return None


# -- state ------------------------------------------------------------------------------------


async def load(app: FastAPI, relay: str, group_id: str) -> Group | None:
    stored = await app.state.repository.get_state(_GROUP.format(f"{relay}'{group_id}"))
    if not stored:
        return None
    data = json.loads(stored)
    return Group(**{k: v for k, v in data.items() if k in Group.__dataclass_fields__})


async def save(app: FastAPI, group: Group) -> None:
    await app.state.repository.set_state(_GROUP.format(group.key), json.dumps(asdict(group), separators=(",", ":")))


async def all_groups(app: FastAPI) -> list[Group]:
    out = []
    for key, _value in await app.state.repository.list_state(_GROUP.format("")):
        relay, _, group_id = key[len(_GROUP.format("")):].rpartition("'")
        group = await load(app, relay, group_id)
        if group is not None:
            out.append(group)
    return out


def _tag(event: dict[str, Any], name: str) -> str | None:
    return next((t[1] for t in event.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == name), None)


def _flag(event: dict[str, Any], name: str) -> bool:
    return any(isinstance(t, list) and t and t[0] == name for t in event.get("tags") or [])


def _valid(event: dict[str, Any]) -> bool:
    try:
        nostr_event.verify(event)
    except (nostr_event.InvalidEvent, ValueError, KeyError, TypeError):
        return False
    return True


def apply_group_events(group: Group, events: list[dict[str, Any]]) -> set[int]:
    """The relay's metadata, admin and member lists (the newest of each,
    signed by the relay). Returns the kinds that changed."""
    changed: set[int] = set()
    newest: dict[int, dict[str, Any]] = {}
    for event in events:
        kind = event.get("kind")
        if kind not in GROUP_KINDS or _tag(event, "d") != group.id:
            continue
        if group.relay_pubkey and event.get("pubkey") != group.relay_pubkey:
            continue
        if kind not in newest or event.get("created_at", 0) > newest[kind].get("created_at", 0):
            newest[kind] = event
    meta = newest.get(METADATA)
    if meta is not None and meta.get("created_at", 0) >= group.meta_at:
        group.relay_pubkey = group.relay_pubkey or meta["pubkey"]
        before = (group.name, group.about, group.picture, group.private, group.closed, group.restricted)
        group.name = str(_tag(meta, "name") or group.id)[:100]
        group.about = str(_tag(meta, "about") or "")[:2000]
        picture = _tag(meta, "picture") or ""
        group.picture = picture if picture.startswith("https://") else ""
        group.private, group.closed, group.restricted = _flag(meta, "private"), _flag(meta, "closed"), _flag(meta, "restricted")
        group.meta_at = meta.get("created_at", 0)
        if before != (group.name, group.about, group.picture, group.private, group.closed, group.restricted):
            changed.add(METADATA)
    admins = newest.get(ADMINS)
    if admins is not None and admins.get("pubkey") == (group.relay_pubkey or admins.get("pubkey")):
        listed = {t[1]: [r for r in t[2:] if isinstance(r, str)][:8] for t in admins.get("tags") or []
                  if isinstance(t, list) and len(t) >= 2 and t[0] == "p" and len(t[1]) == 64}
        if listed != group.admins:
            group.admins = listed
            changed.add(ADMINS)
    members = newest.get(MEMBERS)
    if members is not None and members.get("pubkey") == (group.relay_pubkey or members.get("pubkey")):
        listed = sorted({t[1] for t in members.get("tags") or []
                         if isinstance(t, list) and len(t) >= 2 and t[0] == "p" and len(t[1]) == 64})
        if listed != group.present:
            group.present = listed[:5000]
            changed.add(MEMBERS)
    return changed


def _membership(events: list[dict[str, Any]], group_id: str, pubkey: str) -> bool | None:
    """Whether the newest add or removal of ``pubkey`` (9000/9001) let it in."""
    latest = None
    for event in events:
        if event.get("kind") in (PUT_USER, REMOVE_USER) and _tag(event, "h") == group_id and any(
                isinstance(t, list) and len(t) >= 2 and t[0] == "p" and t[1] == pubkey for t in event.get("tags") or []):
            if latest is None or event.get("created_at", 0) > latest.get("created_at", 0):
                latest = event
    return None if latest is None else latest["kind"] == PUT_USER


# -- rooms ------------------------------------------------------------------------------------


async def _space(app: FastAPI, group: Group, users: list[str]) -> str:
    repository = app.state.repository
    space = await repository.get_state(_SPACE.format(group.relay))
    if space:
        return space
    name = group.host
    try:
        info = await app.state.relay_pool.relay_info(group.relay)
        name = str(info.get("name") or name)[:100]
    except Exception:
        pass
    space = await app.state.homeserver.create_room(
        as_user_id=bot_mxid(app.state.config), name=name, topic=f"Groups on {group.host}, bridged from Nostr (NIP-29).",
        invite=users, room_type="m.space", preset="private_chat", history_visibility="shared")
    await repository.set_state(_SPACE.format(group.relay), space)
    return space


def _topic(group: Group) -> str:
    about = group.about + "\n\n" if group.about else ""
    return about + f"A Nostr group on {group.host}." + (" " + READABLE if group.private else "")


async def _ensure_room(app: FastAPI, group: Group, users: list[str]) -> None:
    from bridge.concord import _set_avatar
    from bridge.messages import invite_to_room
    from bridge.spaces import add_room_to_space

    homeserver = app.state.homeserver
    config = app.state.config
    bot = bot_mxid(config)
    server = config.homeserver.server_name
    space = await _space(app, group, users)
    if group.room is None:
        group.room = await homeserver.create_room(
            as_user_id=bot, name=group.name or group.id, topic=_topic(group), invite=users, preset="private_chat",
            history_visibility="shared",
            initial_state=[{"type": "m.space.parent", "state_key": space, "content": {"via": [server], "canonical": True}}],
        )
        await homeserver.send_state_event(space, "m.space.child", {"via": [server], "suggested": True},
                                          as_user_id=bot, state_key=group.room)
        await app.state.repository.set_state(_ROOM.format(group.room), group.key)
        await _set_avatar(app, group.room, group.picture)
    for user in users:
        await invite_to_room(app, space, user)
        await add_room_to_space(app, user, space)
        await invite_to_room(app, group.room, user)


async def _refresh_room(app: FastAPI, group: Group, changed: set[int]) -> None:
    """The room's name, topic, avatar and power levels after the relay's lists change."""
    if not group.room:
        return
    from bridge.concord import _set_avatar

    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    try:
        if METADATA in changed:
            await homeserver.send_state_event(group.room, "m.room.name", {"name": group.name or group.id}, as_user_id=bot)
            await homeserver.send_state_event(group.room, "m.room.topic", {"topic": _topic(group)}, as_user_id=bot)
            await _set_avatar(app, group.room, group.picture)
        if ADMINS in changed:
            await _power_levels(app, group)
    except HomeserverError:
        logger.info("Could not update the room of %s", group.key, exc_info=True)


async def _power_levels(app: FastAPI, group: Group) -> None:
    """The relay's admins at 50, for display: nothing is enforced from Matrix."""
    from bridge.remote import ghost_user_id

    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    wanted = {}
    for pubkey in group.admins:
        local = await app.state.repository.get_id_by_pubkey(pubkey)
        wanted[local.matrix_user_id if local else ghost_user_id(app, pubkey)] = 50
    levels = await homeserver.get_room_state(group.room, "m.room.power_levels", as_user_id=bot) or {}
    users = {u: level for u, level in (levels.get("users") or {}).items() if level >= 100 or u == bot or u in wanted}
    users.update(wanted)
    if users != (levels.get("users") or {}):
        await homeserver.send_state_event(group.room, "m.room.power_levels", {**levels, "users": users}, as_user_id=bot)


async def _bring_members(app: FastAPI, group: Group, pubkeys: list[str]) -> None:
    from bridge import remote as remotes

    for pubkey in pubkeys[:_MEMBER_GHOSTS]:
        if await app.state.repository.get_id_by_pubkey(pubkey) is not None:
            continue
        try:
            ghost = await remotes.get_or_create_remote(app, pubkey)
            await remotes.ensure_ghost_in_room(app, ghost.ghost_user_id, group.room)
        except HomeserverError:
            logger.info("Could not bring %s into %s", pubkey, group.room, exc_info=True)


async def _leave(app: FastAPI, group: Group, pubkey: str) -> None:
    from bridge.remote import ghost_user_id

    if group.room and await app.state.repository.get_id_by_pubkey(pubkey) is None:
        try:
            await app.state.homeserver.leave_room(group.room, as_user_id=ghost_user_id(app, pubkey))
        except HomeserverError:
            pass


# -- joining ----------------------------------------------------------------------------------


async def handle_join(app: FastAPI, room_id: str, sender: str, reference: tuple[str, str, str | None]) -> None:
    from bridge.tasks import run_in_background

    config = app.state.config
    p = config.appservice.command_prefix
    relay, group_id, code = reference
    from bridge.shadow_ids import ensure_shadow_id_and_tell

    # Like [follow: makes a shadow ID for someone who has none.
    member = await ensure_shadow_id_and_tell(app, sender, room_id)
    if member is None:
        await notice(app, room_id, f'You need a Nostr identity first: "{p}create profile".')
        return
    handle = f"{member.name}@{config.bridge.domain}"
    pool = app.state.relay_pool
    found = [e for e in await pool.query(
        [relay], [{"kinds": GROUP_KINDS, "#d": [group_id]},
                  {"kinds": [PUT_USER, REMOVE_USER], "#h": [group_id], "#p": [member.pubkey]}],
        timeout=10.0, auth_pubkey=member.pubkey) if _valid(e)]
    group = await load(app, relay, group_id) or Group(relay=relay, id=group_id)
    if not group.relay_pubkey:
        try:
            info = await pool.relay_info(relay)
        except Exception:
            info = {}
        # NIP-11 "self" is the relay's own key. ("pubkey" is its operator's,
        # which may differ.) Without it, the first signer of the metadata is
        # trusted from then on.
        self_key = info.get("self")
        group.relay_pubkey = self_key if isinstance(self_key, str) and len(self_key) == 64 else ""
    apply_group_events(group, found)
    if not group.meta_at and group.room is None:
        await notice(app, room_id, f"I couldn't find the group {group_id} on {group.host}.")
        return
    name = group.name or group.id
    status = ""
    if not _membership(found, group_id, member.pubkey) and member.pubkey not in group.present:
        tags = [["h", group_id]] + ([["code", code]] if code else [])
        request = await app.state.signer.sign(member.pubkey, kind=JOIN_REQUEST, content="", created_at=int(time.time()), tags=tags)
        if request is None:
            await notice(app, room_id, f"I couldn't sign the join request as {handle}.")
            return
        [result] = await pool.publish(request, [relay]) or [None]
        if result is not None and not result.accepted and not result.message.startswith("duplicate:"):
            reason = result.message.split(":", 1)[-1].strip() or "no reason given"
            if group.closed and not code:
                await notice(app, room_id, f"{name} is closed: it needs an invite link. The relay said: {reason}")
                return
            status = f" The relay hasn't let you in yet ({reason}); I'll say when it does."
    if member.id not in group.members:
        group.members.append(member.id)
    users = sorted({m.matrix_user_id for i in group.members if (m := await app.state.repository.get_id(i))})
    try:
        await _ensure_room(app, group, users)
        await _power_levels(app, group)
    except HomeserverError:
        logger.warning("Could not make the room of %s", group.key, exc_info=True)
        await notice(app, room_id, "I couldn't make the group's room just now; try again in a minute.")
        return
    await save(app, group)
    brought = await backfill(app, group, member.pubkey)
    run_in_background(app, _bring_members(app, group, list(group.present)), name=f"members of {group.key}")
    app.state.feeds.changed()
    warning = " " + READABLE if group.private else ""
    await notice(app, room_id, f"Joined {name} on {group.host} as {handle}: {group.room}.{status}{warning}",
                 html_message=f"Joined <strong>{_esc(name)}</strong> on {_esc(group.host)} as {_esc(handle)}: "
                 f"{room_link_html(group.room or '', name)}.{_esc(status)}{_esc(warning)}")
    logger.info("Joined %s as %s; %d messages brought in", group.key, member.pubkey, brought)


async def _join_quietly(app: FastAPI, group: Group, sender: str, room_id: str):
    """Someone talking in a group's room whose ID isn't a member yet: if
    they may send from this room (``bridge.shadow_ids``: a local user, or a
    third party with a grant, a room grant included), the ID that acts for
    them here (a shadow one made if they have none) asks the relay to join.
    ``None`` if they may not, or the relay refuses (a closed group needs an
    invite, which only "[join" can bring)."""
    from bridge.shadow_ids import ensure_shadow_id_and_tell, still_allowed

    member = await ensure_shadow_id_and_tell(app, sender, room_id)
    if member is None or not await still_allowed(app, member, room_id):
        return None
    if member.pubkey not in group.present:
        request = await app.state.signer.sign(member.pubkey, kind=JOIN_REQUEST, content="", created_at=int(time.time()),
                                              tags=[["h", group.id]])
        if request is None:
            return None
        [result] = await app.state.relay_pool.publish(request, [group.relay]) or [None]
        if result is not None and not result.accepted and not result.message.startswith("duplicate:"):
            logger.info("%s couldn't join %s from its room: %s", sender, group.key, result.message)
            return None
    if member.id not in group.members:
        group.members.append(member.id)
        await save(app, group)
        logger.info("%s joined %s as %s from its room", sender, group.key, member.name)
    return member


def _esc(text: str) -> str:
    import html

    return html.escape(text)


async def backfill(app: FastAPI, group: Group, auth: str | None = None) -> int:
    events = await app.state.relay_pool.query(
        [group.relay], [{"kinds": [CHAT, THREAD, COMMENT], "#h": [group.id], "limit": _BACKFILL}], timeout=12.0,
        auth_pubkey=auth)
    brought = 0
    for event in sorted(events, key=lambda e: e.get("created_at", 0)):
        if _valid(event) and await _receive_content(app, group, event):
            brought += 1
    return brought


# -- incoming ---------------------------------------------------------------------------------


def _recent(app: FastAPI, group: Group) -> list[str]:
    return app.state.__dict__.setdefault("nip29_recent", {}).setdefault(group.key, [])


async def receive(app: FastAPI, relay: str, raw: dict[str, Any]) -> None:
    """An event from a group's relay."""
    if not app.state.seen.add("nip29:" + str(raw.get("id"))) or not _valid(raw):
        return
    relay = normalize_relay_url(relay)
    kind = raw.get("kind")
    if kind in GROUP_KINDS:
        group = await load(app, relay, _tag(raw, "d") or "")
        if group is None:
            return
        changed = apply_group_events(group, [raw])
        if changed:
            await save(app, group)
            await _refresh_room(app, group, changed)
        return
    group = await load(app, relay, _tag(raw, "h") or "")
    if group is None or group.room is None:
        return
    if kind in STATE_KINDS:
        await _receive_moderation(app, group, raw)
    elif kind in CONTENT_KINDS:
        recent = _recent(app, group)
        recent.append(raw["id"])
        del recent[:-_PREVIOUS]
        await _receive_content(app, group, raw)


def _is_staff(group: Group, pubkey: str) -> bool:
    return pubkey in group.admins or (bool(group.relay_pubkey) and pubkey == group.relay_pubkey)


async def _receive_moderation(app: FastAPI, group: Group, raw: dict[str, Any]) -> None:
    """The relay accepted these only from admins (or made them itself)."""
    if not _is_staff(group, raw["pubkey"]):
        return
    kind = raw["kind"]
    targets = [t[1] for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] in ("p", "e")]
    if kind == DELETE_EVENT:
        for target in targets:
            stored = await app.state.repository.get_state(_MSG.format(target))
            if stored:
                room, event_id, _author = json.loads(stored)
                if event_id:
                    try:
                        await app.state.homeserver.redact_event(room, event_id, as_user_id=bot_mxid(app.state.config),
                                                                reason="Deleted by the group's admins")
                    except HomeserverError:
                        pass
        return
    changed = False
    for pubkey in [t for t in targets if len(t) == 64]:
        local = await app.state.repository.get_id_by_pubkey(pubkey)
        if kind == PUT_USER and pubkey not in group.present:
            group.present.append(pubkey)
            changed = True
            if local is not None and local.id in group.members:
                await notice(app, group.room, f"{local.name}@{app.state.config.bridge.domain} is now a member of "
                                              f"{group.name or group.id}.")
            elif local is None:
                await _bring_members(app, group, [pubkey])
        elif kind == REMOVE_USER:
            if pubkey in group.present:
                group.present.remove(pubkey)
                changed = True
            if local is not None and local.id in group.members:
                group.members.remove(local.id)
                changed = True
                await notice(app, group.room, f"{local.name}@{app.state.config.bridge.domain} was removed from "
                                              f"{group.name or group.id}.")
                others = [m for i in group.members if (m := await app.state.repository.get_id(i))
                          and m.matrix_user_id == local.matrix_user_id]
                if not others:  # none of their other identities is still in it
                    try:
                        await app.state.homeserver.kick_user(group.room, local.matrix_user_id,
                                                             as_user_id=bot_mxid(app.state.config),
                                                             reason="Removed from the group")
                    except HomeserverError:
                        pass
            else:
                await _leave(app, group, pubkey)
    if changed:
        await save(app, group)
        app.state.feeds.changed()


async def _lookup(app: FastAPI, event_id: str | None) -> tuple[str, str, str] | None:
    """(room, Matrix event, author) of a group message, by its Nostr id."""
    if not event_id:
        return None
    stored = await app.state.repository.get_state(_MSG.format(event_id))
    if not stored:
        return None
    room, matrix_event, author = json.loads(stored)
    return (room, matrix_event, author) if matrix_event else None


async def _receive_content(app: FastAPI, group: Group, raw: dict[str, Any]) -> bool:
    from bridge import remote as remotes
    from bridge.inbound import _per_message_profile

    repository = app.state.repository
    if _tag(raw, "h") != group.id or await repository.get_state(_MSG.format(raw["id"])):
        return False
    room, author, kind = group.room, raw["pubkey"], raw["kind"]
    if not room:
        return False
    local = await repository.get_id_by_pubkey(author)
    if local is not None:
        sender, extra = bot_mxid(app.state.config), {"com.beeper.per_message_profile": await _per_message_profile(app, local)}
    else:
        ghost = await remotes.get_or_create_remote(app, author)
        await remotes.ensure_ghost_in_room(app, ghost.ghost_user_id, room)
        sender, extra = ghost.ghost_user_id, {}
    homeserver = app.state.homeserver
    ts = int(raw.get("created_at", 0)) * 1000
    try:
        if kind in (CHAT, THREAD, COMMENT):
            content = await _message(app, raw, sender)
            content.update(extra)
            if kind == COMMENT:
                root = await _lookup(app, _tag(raw, "E"))
                parent = await _lookup(app, _tag(raw, "e"))
                if root or parent:
                    content["m.relates_to"] = {"rel_type": "m.thread", "event_id": (root or parent)[1],
                                               "is_falling_back": False, "m.in_reply_to": {"event_id": (parent or root)[1]}}
            else:
                quoted = await _lookup(app, _tag(raw, "q") or _reply_e(raw))
                if quoted:
                    content["m.relates_to"] = {"m.in_reply_to": {"event_id": quoted[1]}}
            event_id = await homeserver.send_message_event(room, content, as_user_id=sender, ts=ts)
        elif kind == REACTION:
            from bridge.inbound import _emoji_mxc, reaction_key

            target = await _lookup(app, _tag(raw, "e"))
            found = reaction_key(raw)
            if target is None or found is None:
                return False
            reaction, fields = found
            emoji_url = fields.pop("_emoji_url", None)
            mxc = await _emoji_mxc(app, emoji_url, sender) if emoji_url and app.state.config.bridge.msc4027_custom_reactions else None
            if mxc is None:
                fields = {}
            else:
                reaction = mxc
            event_id = await homeserver.send_message_event(
                room, {"m.relates_to": {"rel_type": "m.annotation", "event_id": target[1], "key": reaction}, **fields, **extra},
                as_user_id=sender, ts=ts, event_type="m.reaction")
        elif kind == DELETION:
            for tag in raw.get("tags") or []:
                if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "e":
                    target = await _lookup(app, tag[1])
                    if target is not None and target[2] == author:
                        await homeserver.redact_event(target[0], target[1], as_user_id=sender, reason="Deleted on Nostr")
            await repository.set_state(_MSG.format(raw["id"]), json.dumps([room, "", author]))
            return True
        else:
            return False
    except HomeserverError:
        logger.info("Could not show %s from %s", raw.get("id"), group.key, exc_info=True)
        return False
    await repository.set_state(_MSG.format(raw["id"]), json.dumps([room, event_id, author]))
    await repository.set_state(_MX.format(event_id), json.dumps([raw["id"], author, kind]))
    return True


def _reply_e(raw: dict[str, Any]) -> str | None:
    """An ``e`` tag marked "reply" (older group clients answer this way)."""
    return next((t[1] for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 4 and t[0] == "e" and t[3] == "reply"), None)


async def _message(app: FastAPI, raw: dict[str, Any], sender: str) -> dict[str, Any]:
    from bridge.render import message_content

    text = raw.get("content") or ""
    title = _tag(raw, "title") if raw.get("kind") == THREAD else None
    note = {"id": raw["id"], "pubkey": raw["pubkey"], "kind": 1, "created_at": raw.get("created_at", 0),
            "content": (f"{title}\n\n{text}" if title else text),
            "tags": [t for t in raw.get("tags") or [] if isinstance(t, list) and t and t[0] in ("imeta", "emoji", "p", "t")]}
    content = await message_content(app, note, as_user_id=sender)
    content.pop("external_url", None)
    return content


# -- outgoing ---------------------------------------------------------------------------------


async def maybe_publish(request: Request, event: dict[str, Any]) -> bool:
    """A message, reaction or redaction in a group's room, from a member here."""
    app = request.app
    config = app.state.config
    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    if event.get("type") not in ("m.room.message", "m.reaction", "m.room.redaction"):
        return False
    mapped = await app.state.repository.get_state(_ROOM.format(room_id))
    if not mapped:
        return False
    if is_bridge_user(config, sender) or not sender.startswith("@"):
        return True
    from bridge.commands import message_for_a_bot

    content = event.get("content") or {}
    if event.get("type") == "m.room.message" and message_for_a_bot(content, config):
        return False
    relay, _, group_id = mapped.rpartition("'")
    group = await load(app, relay, group_id)
    if group is None:
        return True
    member = None
    for id_ref in group.members:
        candidate = await app.state.repository.get_id(id_ref)
        if candidate is not None and candidate.matrix_user_id == sender:
            member = candidate
            break
    from bridge.shadow_ids import still_allowed

    if member is not None and not await still_allowed(app, member, room_id):
        return True  # a third party whose access was taken away: stays in Matrix
    if member is None:
        member = await _join_quietly(app, group, sender, room_id)
    if member is None:
        from bridge.shadow_ids import may_use_bridge

        if await may_use_bridge(app, sender):  # anyone else isn't told about commands they can't run
            await notice(app, room_id, f'Join the group first: "{config.appservice.command_prefix}join {group.host}\'{group.id}".')
        return True
    built = await _outgoing(app, group, event, content)
    if built is None:
        return True
    kind, text, tags = built
    own = {m.pubkey for i in group.members if (m := await app.state.repository.get_id(i))}
    previous = [e[:8] for e in reversed(_recent(app, group)) if e not in own][:3]
    tags = [["h", group.id], *tags, *([["previous", *previous]] if previous else [])]
    signed = await app.state.signer.sign(member.pubkey, kind=kind, content=text, created_at=int(time.time()), tags=tags)
    if signed is None:
        await notice(app, room_id, "I couldn't sign that as you, so it wasn't sent.")
        return True
    # Recorded before it goes out: the relay echoes it straight back.
    app.state.seen.add("nip29:" + signed.id)
    repository = app.state.repository
    matrix_event = event.get("event_id", "")
    await repository.set_state(_MSG.format(signed.id), json.dumps([room_id, matrix_event if kind != DELETION else "", member.pubkey]))
    if kind != DELETION:
        await repository.set_state(_MX.format(matrix_event), json.dumps([signed.id, member.pubkey, kind]))
    # In the background, so a slow relay doesn't hold up the homeserver's
    # next batch of events (see concord.maybe_publish).
    from bridge.tasks import run_in_background

    run_in_background(app, _send(app, group, signed, room_id, matrix_event), name=f"nip29 send {signed.id[:8]}")
    return True


async def _send(app: FastAPI, group: Group, signed, room_id: str, matrix_event: str) -> None:
    results = await app.state.relay_pool.publish(signed, [group.relay])
    refused = next((r for r in results if not r.accepted), None)
    if refused is not None:
        logger.info("%s didn't take a message for %s: %s", group.relay, group.key, refused.message)
        reason = refused.message.split(":", 1)[-1].strip() or "no reason given"
        await notice(app, room_id, f"{group.host} didn't accept that: {reason}",
                     extra={"m.relates_to": {"m.in_reply_to": {"event_id": matrix_event}}} if matrix_event else None)


async def _outgoing(app: FastAPI, group: Group, event: dict[str, Any], content: dict[str, Any]):
    """(kind, content, tags) of the group event a Matrix event becomes, or None."""
    repository = app.state.repository

    async def nostr_of(matrix_event: str | None):
        stored = await repository.get_state(_MX.format(matrix_event)) if matrix_event else None
        return json.loads(stored) if stored else None

    kind_type = event.get("type")
    if kind_type == "m.reaction":
        relates = content.get("m.relates_to") or {}
        target = await nostr_of(relates.get("event_id"))
        key = relates.get("key")
        if target is None or not isinstance(key, str):
            return None
        refs = [["e", target[0], group.relay, target[1]], ["p", target[1]], ["k", str(target[2])]]
        if key.startswith("mxc://"):
            from bridge.media import PROFILE_IMAGE_MAX_BYTES, publish_matrix_image

            shortcode = content.get("shortcode") or content.get("com.beeper.reaction.shortcode")
            name = re.sub(r"[^a-zA-Z0-9_-]", "", str(shortcode or "").strip(":")) or "emoji"
            published = await publish_matrix_image(app, key, max_bytes=PROFILE_IMAGE_MAX_BYTES)
            if published is None:
                return None
            return REACTION, f":{name}:", [*refs, ["emoji", name, published.url]]
        return REACTION, ("+" if key == "👍" else key), refs
    if kind_type == "m.room.redaction":
        target = await nostr_of(event.get("redacts") or content.get("redacts"))
        if target is None:
            return None
        return DELETION, str(content.get("reason") or ""), [["e", target[0]], ["k", str(target[2])]]
    from bridge.media import EncryptedMedia, MediaProblem, publish_matrix_media
    from bridge.notes import MEDIA_MSGTYPES, message_body

    relates = content.get("m.relates_to") or {}
    if relates.get("rel_type") == "m.replace":
        return None  # group messages can't be edited
    text = message_body(content)
    tags: list[list[str]] = []
    if content.get("msgtype") in MEDIA_MSGTYPES:
        try:
            media = await publish_matrix_media(app, content)
        except EncryptedMedia:
            from bridge.encrypted_media import hold

            hold(app, event.get("room_id", ""), event)
            return None
        except MediaProblem as exc:
            await notice(app, event.get("room_id", ""), f"That wasn't sent: {exc}.")
            return None
        caption = text if content.get("filename") and content.get("filename") != content.get("body") else ""
        text = f"{caption}\n{media.url}".strip()
        imeta = ["imeta", f"url {media.url}", f"m {media.mime_type}", f"x {media.sha256}", f"size {media.size}"]
        if media.width and media.height:
            imeta.append(f"dim {media.width}x{media.height}")
        tags.append(imeta)
    if not text:
        return None
    thread_root = relates.get("event_id") if relates.get("rel_type") == "m.thread" else None
    in_reply_to = (relates.get("m.in_reply_to") or {}).get("event_id")
    root = await nostr_of(thread_root)
    if root is not None:
        parent = (await nostr_of(in_reply_to) if not relates.get("is_falling_back") else None) or root
        return COMMENT, text, [*tags, ["K", str(root[2])], ["E", root[0], group.relay, root[1]], ["P", root[1]],
                               ["k", str(parent[2])], ["e", parent[0], group.relay, parent[1]], ["p", parent[1]]]
    parent = await nostr_of(in_reply_to)
    if parent is not None:
        return CHAT, text, [*tags, ["q", parent[0], group.relay, parent[1]], ["p", parent[1]]]
    return CHAT, text, tags


# -- subscriptions ----------------------------------------------------------------------------


async def desired(app: FastAPI) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """For bridge.feeds: each relay's groups, read as one of their members
    here (who the relay may ask to authenticate as)."""
    by_reader: dict[tuple[str, str], list[str]] = {}
    for group in await all_groups(app):
        reader = None
        for id_ref in group.members:
            member = await app.state.repository.get_id(id_ref)
            if member is not None:
                reader = member.pubkey
                break
        if reader is not None and group.room:
            by_reader.setdefault((group.relay, reader), []).append(group.id)
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (relay, reader), group_ids in by_reader.items():
        out[(relay, f"nip29:{reader[:16]}")] = [
            {"kinds": [*CONTENT_KINDS, *STATE_KINDS], "#h": sorted(group_ids), "_auth": reader},
            {"kinds": GROUP_KINDS, "#d": sorted(group_ids)},
        ]
    return out
