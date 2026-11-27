"""The bridge's single, global "backstage" Matrix space (``m.space``) that
every ghost is a member of once ``bridge.backstage_space_enabled`` is on.

Ported from the sibling ActivityPub bridge's identical feature. Every room
a ghost is ever added to via ``bridge.remote.ensure_ghost_in_room``, the
one real choke point for that in this bridge (Remote User Room replies,
NIP-29 group rooms, Concord channels, Git repo rooms, polls, webxdc, ...),
gets its ``m.room.join_rules`` pointed at this space (``restricted``/
``knock_restricted``, MSC3083/MSC3787), so a ghost who's already a member
of the space can self-join with one ``join`` event instead of costing an
``invite`` + ``join`` pair every time.

Unlike the ActivityPub port, no room-creation call sites need touching at
all: because every ghost-room-access ever goes through the one choke point
above, the lazy on-demand upgrade in ``ensure_ghost_access`` below gives
full coverage on its own, for both brand new and long-existing rooms alike.
DM rooms, the per-user Notifications room, and the Games room are never
reached via that choke point at all (they're either a ghost's own
ghost-created room or have no ghosts in them), so they're excluded from
this feature for free, the same way the ActivityPub bridge's DM/Chat/
Notification rooms are.

A target room may already be ``restricted`` to something else (e.g. a
Concord channel restricted to its own community Space, or a Git repo room
restricted to its own Git Space), this always APPENDS our space's
``allow`` condition alongside the existing one rather than replacing it,
so a community/group member and a ghost can both still self-join.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverClient, HomeserverError
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

BACKSTAGE_SPACE_NAME = "Bridge Ghosts"
_STATE_KEY = "backstage_space"
_RESTRICTABLE_JOIN_RULES = ("restricted", "knock_restricted")


async def ensure_backstage_space(app: FastAPI) -> str | None:
    """Get-or-create the bridge's one backstage space. Returns its room ID,
    or None if creation failed, best-effort, same as this bridge's own
    per-relay/per-community spaces (``bridge.groups._space``,
    ``bridge.concord``'s community space). Callers must still check
    ``config.bridge.backstage_space_enabled`` themselves; this function
    doesn't gate on it so it stays usable from a one-off backfill script
    even before the flag is flipped on."""
    repository = app.state.repository
    existing = await repository.get_state(_STATE_KEY)
    if existing:
        return existing

    config = app.state.config
    homeserver: HomeserverClient = app.state.homeserver
    try:
        space_room_id = await homeserver.create_room(
            as_user_id=bot_mxid(config), name=BACKSTAGE_SPACE_NAME, room_type="m.space",
            preset="private_chat", history_visibility="shared",
            avatar_mxc=config.bridge.space_avatar_mxc or config.appservice.bot_avatar_mxc,
        )
    except HomeserverError:
        logger.warning("Could not create backstage space", exc_info=True)
        return None

    await repository.set_state(_STATE_KEY, space_room_id)
    return space_room_id


async def open_backstage_space_publicly(app: FastAPI, space_room_id: str) -> str:
    """Temporarily set the backstage space's own ``join_rule`` to
    ``public``, returning whatever it was before (to restore via
    ``close_backstage_space``). Used ONLY by the one-off backfill script's
    ``--public-join`` option, so every ghost can self-join the space with a
    single ``join`` event in one pass instead of costing an invite+join (or
    needing the Admin API) one ghost at a time."""
    homeserver: HomeserverClient = app.state.homeserver
    bot = bot_mxid(app.state.config)
    current = await homeserver.get_room_state(space_room_id, "m.room.join_rules", as_user_id=bot)
    previous_join_rule = (current or {}).get("join_rule") or "invite"
    await homeserver.send_state_event(space_room_id, "m.room.join_rules", {"join_rule": "public"}, as_user_id=bot)
    return previous_join_rule


async def close_backstage_space(app: FastAPI, space_room_id: str, join_rule: str) -> None:
    """Restore the backstage space's ``join_rule`` after a public backfill
    window (see ``open_backstage_space_publicly``)."""
    homeserver: HomeserverClient = app.state.homeserver
    await homeserver.send_state_event(
        space_room_id, "m.room.join_rules", {"join_rule": join_rule}, as_user_id=bot_mxid(app.state.config)
    )


async def join_ghost_to_backstage_space(app: FastAPI, ghost_mxid: str) -> None:
    """Make ``ghost_mxid`` a member of the backstage space. No-op if the
    feature is off. Uses the Admin API force-join (no invite round-trip at
    all) when ``bridge.use_synapse_admin_api`` is on; otherwise falls back
    to an ordinary bot-invite + ghost-join, same 2-event cost as today's
    per-room invite, just paid once per ghost instead of once per room.
    Idempotent, joining an already-joined room is a harmless no-op, so
    this is safe to call on every ghost provisioning, not just the first."""
    config = app.state.config
    if not config.bridge.backstage_space_enabled:
        return
    space_room_id = await ensure_backstage_space(app)
    if space_room_id is None:
        return

    homeserver: HomeserverClient = app.state.homeserver
    bot = bot_mxid(config)
    try:
        admin_token = config.homeserver.synapse_admin_token
        if config.bridge.use_synapse_admin_api and admin_token:
            await homeserver.admin_join_room(space_room_id, ghost_mxid, admin_token=admin_token)
        else:
            await homeserver.invite_user(space_room_id, ghost_mxid, as_user_id=bot)
            await homeserver.join_room(space_room_id, as_user_id=ghost_mxid)
    except HomeserverError:
        logger.warning("Could not join ghost %s to backstage space", ghost_mxid, exc_info=True)


async def maybe_fast_join_ghost(app: FastAPI, *, room_id: str, ghost_mxid: str) -> bool:
    """Try to self-join ``ghost_mxid`` into ``room_id`` directly, skipping
    the invite. Returns True on success (caller should NOT also invite).
    Returns False if the feature is off, or the join was rejected
    (``M_FORBIDDEN``, the room was never upgraded, or the ghost isn't yet
    a backstage-space member), the caller's existing invite flow is the
    correct fallback either way. Any other error re-raises, same as every
    other "swallow one specific expected errcode" call in this codebase
    (e.g. ``bridge.remote``'s ``M_USER_IN_USE`` handling)."""
    if not app.state.config.bridge.backstage_space_enabled:
        return False
    homeserver: HomeserverClient = app.state.homeserver
    try:
        await homeserver.join_room(room_id, as_user_id=ghost_mxid)
    except HomeserverError as exc:
        if exc.errcode != "M_FORBIDDEN":
            raise
        return False
    return True


async def maybe_upgrade_join_rule(app: FastAPI, room_id: str) -> bool:
    """Best-effort: add the backstage-space ``allow`` condition to
    ``room_id``'s CURRENT ``join_rules``, read live, not assumed,
    (``invite``->``restricted``, ``knock``->``knock_restricted``, or append
    to an existing ``restricted``/``knock_restricted`` condition list
    without disturbing whatever else is already there, e.g. a Concord
    channel's or Git repo room's own community-Space restriction). Never
    touches an already-``public``/``public_chat`` room. Silently does
    nothing on a permission error, the room just keeps working exactly as
    it does today.

    Returns True if ``room_id`` ends this call restricted to the backstage
    space (already was, or was just upgraded); False otherwise (feature
    off, space missing, state unreadable, an already-public/unrecognized
    room left alone on purpose, or a permission denial), used by the
    one-off backfill script to report successes vs. skips."""
    config = app.state.config
    if not config.bridge.backstage_space_enabled:
        return False
    space_room_id = await ensure_backstage_space(app)
    if space_room_id is None:
        return False

    homeserver: HomeserverClient = app.state.homeserver
    bot = bot_mxid(config)
    try:
        current = await homeserver.get_room_state(room_id, "m.room.join_rules", as_user_id=bot)
    except HomeserverError:
        logger.debug("Could not read join_rules for %s", room_id, exc_info=True)
        return False

    condition: dict[str, Any] = {"type": "m.room_membership", "room_id": space_room_id}
    current_rule = (current or {}).get("join_rule")
    if current_rule in _RESTRICTABLE_JOIN_RULES:
        allow = list((current or {}).get("allow") or [])
        if condition in allow:
            return True
        allow.append(condition)
        new_rule = current_rule
    elif current_rule == "invite":
        new_rule, allow = "restricted", [condition]
    elif current_rule == "knock":
        new_rule, allow = "knock_restricted", [condition]
    else:
        return False  # public/public_chat/unrecognized, never tighten or guess

    try:
        await homeserver.send_state_event(
            room_id, "m.room.join_rules", {"join_rule": new_rule, "allow": allow}, as_user_id=bot,
        )
    except HomeserverError as exc:
        if exc.errcode != "M_FORBIDDEN":
            raise
        logger.info("No permission to add the backstage-space restriction to %s, leaving join_rules as-is", room_id)
        return False
    return True


async def ensure_ghost_access(app: FastAPI, *, room_id: str, ghost_mxid: str) -> bool:
    """The one thing ``bridge.remote.ensure_ghost_in_room`` actually needs:
    try the fast self-join first; if that fails because this particular
    room was never upgraded (or the feature was flipped on without a
    backfill ever running), opportunistically do right now, lazily, exactly
    what a backfill would have done for this one room/ghost pair, add
    the room to the backstage space's allow-list and make sure the ghost is
    a space member, then retry the self-join once. Returns True if the
    ghost ended up joined (caller must NOT also invite); returns False if
    even that retry was rejected, in which case the caller's existing
    invite fallback is still exactly correct and still runs. This is how
    the backstage space fills in gradually over time, room by room and
    ghost by ghost, for a deployment that enables the feature but never
    runs the one-off backfill script at all."""
    if not app.state.config.bridge.backstage_space_enabled:
        return False
    if await maybe_fast_join_ghost(app, room_id=room_id, ghost_mxid=ghost_mxid):
        return True
    await join_ghost_to_backstage_space(app, ghost_mxid)
    await maybe_upgrade_join_rule(app, room_id)
    return await maybe_fast_join_ghost(app, room_id=room_id, ghost_mxid=ghost_mxid)
