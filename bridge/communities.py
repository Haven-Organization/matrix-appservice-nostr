"""A user's Concord communities and NIP-29 groups together: what the
widget's Communities section lists, and ``[leave``.

``[leave`` with no argument leaves the community or group whose room it's
run in; otherwise it takes a community's or group's name, or a group
reference (``relay.example'groupid`` or its ``naddr1…``). Leaving is done
by ``bridge.concord.leave`` and ``bridge.groups.leave``, with every one of
the user's IDs that's a member.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from bridge import concord, groups
from bridge.messages import notice


def _role(community: concord.Community, pubkey: str) -> str:
    if pubkey == community.owner:
        return "owner"
    if community.can(pubkey, concord.MANAGE_CHANNELS):
        return "manager"
    return "member"


async def _mine(app: FastAPI, member_ids: list[int], user: str) -> list[Any]:
    repository = app.state.repository
    return [m for i in member_ids if (m := await repository.get_id(i)) is not None and m.matrix_user_id == user]


async def list_for(app: FastAPI, user: str) -> dict[str, list[dict[str, Any]]]:
    """``user``'s communities and groups, for the widget."""
    from bridge.concord_owner import _live, can_write

    domain = app.state.config.bridge.domain
    out: dict[str, list[dict[str, Any]]] = {"concord": [], "groups": []}
    for community in await concord.all_communities(app):
        if community.removed:
            continue
        mine = await _mine(app, community.members, user)
        if not mine:
            continue
        roles = [_role(community, m.pubkey) for m in mine]
        role = "owner" if "owner" in roles else "manager" if "manager" in roles else "member"
        owner_here = role == "owner" and bool(community.control_root)
        channels = [{"id": c, "name": community.channels[c].get("name") or "channel",
                     "room_id": community.channels[c].get("room"), "private": bool(community.channels[c].get("private"))}
                    for c in community.readable()]
        out["concord"].append({
            "id": community.id, "name": community.name, "description": community.description,
            "space_id": community.space, "channels": channels, "role": role,
            "as": [f"{m.name}@{domain}" for m in mine],
            "members": len(community.present),
            # What the widget offers: the owner of one made here manages
            # its links; managers add channels; any member invites.
            "can_manage_links": owner_here,
            "can_add_channels": role in ("owner", "manager") and can_write(community),
            "can_invite": bool(community.owner_salt),
            "can_leave": not owner_here,
            "links": [{"number": n, "label": link.get("label") or "", "url": link.get("url"),
                       "created_at": link.get("created_at")}
                      for n, link in enumerate(_live(community), 1)] if owner_here else [],
        })
    for group in await groups.all_groups(app):
        mine = await _mine(app, group.members, user)
        if not mine:
            continue
        admin = any(m.pubkey in group.admins for m in mine)
        out["groups"].append({
            "id": group.id, "relay": group.relay, "host": group.host, "key": group.key,
            "name": group.name or group.id, "about": group.about, "room_id": group.room,
            "private": group.private, "closed": group.closed, "role": "admin" if admin else "member",
            "as": [f"{m.name}@{domain}" for m in mine], "members": len(group.present),
        })
    return out


async def find(app: FastAPI, user: str, reference: str, room_id: str = "") -> tuple[str, Any] | None:
    """("concord", community) or ("group", group) that ``user`` is in,
    named by ``reference`` (an id, name or group reference), or by the
    room ``room_id`` when ``reference`` is empty."""
    wanted = reference.strip()
    folded = wanted.casefold()
    parsed = groups.parse_reference(wanted) if wanted else None
    for community in await concord.all_communities(app):
        if community.removed or not await _mine(app, community.members, user):
            continue
        rooms = {community.space, *(info.get("room") for info in community.channels.values())}
        if (wanted and (community.id == wanted or community.name.casefold() == folded)) or (
                not wanted and room_id and room_id in rooms):
            return "concord", community
    for group in await groups.all_groups(app):
        if not await _mine(app, group.members, user):
            continue
        if parsed is not None and (group.relay, group.id) == parsed[:2]:
            return "group", group
        if (wanted and (group.key == wanted or (group.name or group.id).casefold() == folded)) or (
                not wanted and room_id and group.room == room_id):
            return "group", group
    return None


async def leave(app: FastAPI, user: str, kind: str, target: Any) -> str:
    """Leave ``target`` (from ``find``) and say what happened. Raises
    ``concord.ConcordProblem`` when it can't be left."""
    if kind == "concord":
        left = await concord.leave(app, target, user)
        name = target.name
    else:
        left = await groups.leave(app, target, user)
        name = target.name or target.id
    if not left:
        return f"None of your identities is in {name}."
    return f"Left {name} as {', '.join(left)}."


async def handle_leave(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    found = await find(app, sender, argument, room_id)
    if found is None:
        listed = await list_for(app, sender)
        names = [c["name"] for c in listed["concord"]] + [g["name"] for g in listed["groups"]]
        if not names:
            await notice(app, room_id, "You're not in any Concord community or NIP-29 group.")
        else:
            await notice(app, room_id, f'Which one ({", ".join(names)})? Say "{p}leave <name>", or run it in one of '
                                       "its rooms.")
        return
    kind, target = found
    try:
        message = await leave(app, sender, kind, target)
    except concord.ConcordProblem as exc:
        await notice(app, room_id, f"Couldn't leave {target.name}: {exc}.")
        return
    await notice(app, room_id, message)
