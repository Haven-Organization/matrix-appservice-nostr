"""Room membership and state events the bridge reacts to.

- A Profile Room's owner knocking is let straight back in (anyone else's
  knock is left for the people inside to answer).
- The owner joining their Profile Room files it in their space and gets a
  welcome explaining the room; joining the notifications room files that.
- A change to a Profile Room's name, topic, avatar or banner republishes
  the ID's Nostr profile.
"""

from __future__ import annotations

import html
import logging

from fastapi import Request

from bridge.homeserver_client import HomeserverError
from bridge.identity_events import sync_identity
from bridge.messages import notice
from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE
from bridge.nostr import nip19
from bridge.spaces import NOTIFICATIONS_ROOM_SPACE_ORDER, add_room_to_space, profile_room_space_order
from bridge.tasks import run_in_background
from bridge.shadow_ids import may_use_bridge
from bridge.users import bot_mxid, is_bridge_user

logger = logging.getLogger(__name__)

PROFILE_STATE_TYPES = frozenset(
    {
        "m.room.name", "m.room.topic", "m.room.avatar", PROFILE_BANNER_STATE_TYPE,
        "software.haven.nostr.profile_extra", "m.room.pinned_events",
    }
)


def _membership(event: dict) -> str | None:
    if event.get("type") != "m.room.member":
        return None
    membership = (event.get("content") or {}).get("membership")
    return membership if isinstance(membership, str) else None


async def maybe_handle_knock(request: Request, event: dict) -> bool:
    """Returns True if this was a knock on a Profile Room."""
    if _membership(event) != "knock":
        return False
    room_id = event.get("room_id", "")
    knocker = event.get("state_key", "")
    app = request.app
    remote = await app.state.repository.get_remote_account_for_room(room_id)
    if remote is not None:
        # Anyone from this server knocking on an account's room is let in.
        config = app.state.config
        if await may_use_bridge(app, knocker) or config.bridge.accept_federated_knocks:
            try:
                await app.state.homeserver.invite_user(room_id, knocker, as_user_id=remote.ghost_user_id)
            except HomeserverError:
                logger.warning("Could not let %s into %s", knocker, room_id, exc_info=True)
        return True
    nostr_id = await app.state.repository.get_id_for_profile_room(room_id)
    if nostr_id is None:
        return False
    if knocker == nostr_id.matrix_user_id:
        try:
            await request.app.state.homeserver.invite_user(
                room_id, knocker, as_user_id=bot_mxid(request.app.state.config)
            )
        except HomeserverError:
            logger.warning("Could not let %s back into %s", knocker, room_id, exc_info=True)
    return True


async def maybe_handle_join(request: Request, event: dict) -> bool:
    """Returns True if a local user joined their own Profile Room or their
    notifications room."""
    if _membership(event) != "join":
        return False
    if ((event.get("unsigned") or {}).get("prev_content") or {}).get("membership") == "join":
        return False  # a profile change, not a join
    app = request.app
    config = app.state.config
    room_id = event.get("room_id", "")
    user = event.get("state_key", "")
    if not room_id or not await may_use_bridge(app, user):
        return False
    repository = app.state.repository
    nostr_id = await repository.get_id_for_profile_room(room_id)
    if nostr_id is not None and nostr_id.matrix_user_id == user:
        await add_room_to_space(app, user, room_id, order=profile_room_space_order(nostr_id.id))
        handle = f"{nostr_id.name}@{config.bridge.domain}"
        npub = nip19.npub_encode(nostr_id.pubkey)
        explanation = (
            "The room's name, topic and avatar are this ID's Nostr profile: change them here and Nostr sees the "
            "change. Anything published from this room is public on Nostr, even though the room itself is private."
        )
        await notice(
            app,
            room_id,
            f"This is the Profile Room of {handle} ({npub}).\n{explanation}",
            html_message=f"<p>This is the Profile Room of <strong>{html.escape(handle)}</strong> "
            f"(<code>{html.escape(npub)}</code>).</p><p>{html.escape(explanation)}</p>",
        )
        return True
    if (await repository.get_user_rooms(user)).notifications_room_id == room_id:
        await add_room_to_space(app, user, room_id, order=NOTIFICATIONS_ROOM_SPACE_ORDER)
        return True
    if await repository.get_remote_account_for_room(room_id) is not None:
        await add_room_to_space(app, user, room_id)
        return True
    return False


async def maybe_handle_leave(request: Request, event: dict) -> bool:
    """Returns True if a local user left (or was removed from) a Remote User
    Room: that unfollows the account from each of their IDs following it,
    since the room is where its posts reach them."""
    if _membership(event) not in ("leave", "ban"):
        return False
    app = request.app
    user = event.get("state_key", "")
    room_id = event.get("room_id", "")
    if not user.startswith("@") or is_bridge_user(app.state.config, user):
        return False
    if await app.state.repository.get_remote_account_for_room(room_id) is None:
        return False
    from bridge.follows import unfollow_from_room
    from bridge.spaces import remove_room_from_space

    done = await unfollow_from_room(app, user, room_id)
    await remove_room_from_space(app, user, room_id)
    if done:
        logger.info("%s left %s: unfollowed as %s", user, room_id, ", ".join(done))
    return True


async def maybe_handle_profile_state(request: Request, event: dict) -> bool:
    """Returns True if a Profile Room's name, topic, avatar or banner
    changed (the ID's profile is then republished in the background)."""
    if event.get("type") not in PROFILE_STATE_TYPES or event.get("state_key") != "":
        return False
    app = request.app
    nostr_id = await app.state.repository.get_id_for_profile_room(event.get("room_id", ""))
    if nostr_id is None:
        return False
    run_in_background(app, _sync(app, nostr_id.id), name=f"profile sync {nostr_id.id}")
    return True


async def _sync(app, id_number: int) -> None:
    nostr_id = await app.state.repository.get_id(id_number)
    if nostr_id is None:
        return
    try:
        await sync_identity(app, nostr_id)
    except HomeserverError:
        logger.warning("Could not update the profile of %s", nostr_id.name, exc_info=True)
