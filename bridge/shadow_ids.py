"""Shadow IDs and third-party access: the same scheme as
matrix-appservice-activitypub's ``bridge.shadow_identity``.

**Shadow and full.** A *shadow* ID is a Nostr ID with no Profile Room: its
profile (kind 0) isn't the user's to edit, it mirrors their Matrix display
name and avatar, kept up to date by ``shadow_profile_loop``. A *full* ID
has a Profile Room, whose name, topic and avatar are its profile. Every
user, local or not, starts with a shadow ID: one is made automatically the
first time they do anything that goes out to Nostr (a reply, a reaction, a
vote, ``[follow``, ``[join``), with a one-time notice saying so.
``[create profile`` with the default name turns that same ID (same key,
same handle, same followers) into a full one, by giving it a Profile Room.

**Third parties.** Users of other homeservers can't use the bridge at all
unless ``bridge.third_party_access`` is on and an admin has granted them
access (``[allow``, stored as ``ThirdPartyAllow`` rows):

- ``mxid`` (one user) or ``homeserver`` (everyone on it), at ``full``
  (everything a local user can do) or ``shadow`` (the default: following,
  posting replies and reacting, all as a shadow ID; no commands that make
  or change a profile, no DMs). ``full`` only allows making a profile; it
  doesn't make one, so until they run ``[create profile`` they act through
  their shadow ID like anyone else.
- ``room``: anyone, from any homeserver, gets a shadow ID just by replying,
  reacting or voting in that one room. Always ``shadow``, only for that
  room, and only for passive content: never commands, not even
  ``[follow`` (that would follow an account as this bridge, into a new
  room the grant was never meant to cover).

The most specific grant wins: an ``mxid`` row (including a ban, which
``[disallow mxid`` stores) over a ``homeserver`` row, over a ``room`` row
for the room the action happens in. Everything is checked again at every
action, never cached on the ID, so a change takes effect at once. Taking
a grant away never deletes anything: a full ID whose grant drops to shadow
keeps its Profile Room, parked: its profile goes back to mirroring Matrix
and the room's posts stop going out, until full access is granted again.

One difference from the ActivityPub bridge: an ID whose key was imported
(or lives in a remote signer) never has its profile overwritten by the
mirror, since that profile is the user's own from before the bridge.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal, NamedTuple

from fastapi import FastAPI

from bridge import ids
from bridge.homeserver_client import HomeserverError
from bridge.repository import NostrId
from bridge.users import is_bridge_user, localpart

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 20 * 60

_UNSAFE_RE = re.compile(r"[^a-z0-9.-]")


def homeserver_of(mxid: str) -> str:
    """``@user:example.org`` -> ``example.org`` (``""`` without a colon)."""
    return mxid.split(":", 1)[1] if ":" in mxid else ""


def is_third_party(config, mxid: str) -> bool:
    """A user of another homeserver."""
    return homeserver_of(mxid) != config.homeserver.server_name


def third_party_name(mxid: str) -> str:
    """A third party's default ID name: localpart and homeserver, so the
    same localpart on two servers can't collide. ``_`` joins them, and a
    ``_`` already in either half is doubled, so two different MXIDs never
    give the same name: ``@a_b:c.org`` -> ``a__b_c.org``, ``@a:b_c.org``
    -> ``a_b__c.org``. Other characters NIP-05 doesn't allow become ``-``."""
    local, _, domain = mxid.lstrip("@").partition(":")

    def part(text: str) -> str:
        return _UNSAFE_RE.sub("-", text.lower().replace("_", "\x00")).replace("\x00", "__")

    return f"{part(local)}_{part(domain)}"


def default_id_name(config, mxid: str) -> str:
    """The name a shadow ID is made with, and the one a bare
    ``[create profile`` uses: the same rule for both, so that creating a
    profile finds the shadow ID and upgrades it rather than making a
    second one."""
    return third_party_name(mxid) if is_third_party(config, mxid) else localpart(mxid)


def _is_generated(nostr_id: NostrId) -> bool:
    return nostr_id.key_origin == ids.KEY_ORIGIN_GENERATED


# -- tiers ----------------------------------------------------------------------------------


async def resolve_third_party_tier(app: FastAPI, mxid: str, room_id: str = "") -> str | None:
    """``full``, ``shadow`` or ``None`` for a third party right now. Pass
    ``room_id`` only where a room grant may count (passive content)."""
    return await app.state.repository.resolve_third_party_tier(
        mxid=mxid, homeserver=homeserver_of(mxid), room_id=room_id
    )


async def effective_tier(app: FastAPI, nostr_id: NostrId) -> Literal["full", "shadow"]:
    """What the ID behaves as right now:

    - no Profile Room: shadow, for anyone (a full grant only allows making
      one);
    - a local user's ID with a Profile Room: full;
    - a third party's with a Profile Room: full only while third-party
      access is on and their own grant (mxid or homeserver; a room grant
      never makes anything full) is full. Otherwise shadow: the room is
      parked, not deleted."""
    if not nostr_id.profile_room_id:
        return "shadow"
    config = app.state.config
    if not is_third_party(config, nostr_id.matrix_user_id):
        return "full"
    if not config.bridge.third_party_access:
        return "shadow"
    return "full" if await resolve_third_party_tier(app, nostr_id.matrix_user_id) == "full" else "shadow"


async def still_allowed(app: FastAPI, nostr_id: NostrId, room_id: str) -> bool:
    """Whether this ID may send anything to Nostr from ``room_id`` right
    now. A local user's always may. A third party's needs third-party
    access on and a grant (theirs, their homeserver's, or this room's) that
    isn't a ban. Getting in through one room's grant counts in that room
    only. ``False`` means: drop the action, as if they had no ID."""
    config = app.state.config
    if not is_third_party(config, nostr_id.matrix_user_id):
        return True
    if not config.bridge.third_party_access:
        return False
    return await resolve_third_party_tier(app, nostr_id.matrix_user_id, room_id) is not None


async def may_post_from_profile_room(app: FastAPI, nostr_id: NostrId) -> bool:
    """Whether the owner's posts in this ID's own Profile Room go out: not
    while a third party's ID is parked at shadow tier (the room stays as
    it is in Matrix; it just stops being posted from)."""
    if not is_third_party(app.state.config, nostr_id.matrix_user_id):
        return True
    return await effective_tier(app, nostr_id) == "full"


async def may_use_bridge(app: FastAPI, mxid: str) -> bool:
    """A local user, or a third party with access on and their own grant
    (not a room's): who the bridge lets into an account's room and so on."""
    config = app.state.config
    if not mxid.startswith("@") or is_bridge_user(config, mxid):
        return False
    if not is_third_party(config, mxid):
        return True
    return config.bridge.third_party_access and await resolve_third_party_tier(app, mxid) is not None


# -- making shadow IDs ------------------------------------------------------------------------


class ShadowResolution(NamedTuple):
    """``created``: this call just made the ID (so the caller can say so)."""

    nostr_id: NostrId | None
    created: bool


def _lock_for(app: FastAPI, mxid: str) -> asyncio.Lock:
    state = app.state
    if not hasattr(state, "shadow_locks"):
        state.shadow_locks = {}
    return state.shadow_locks.setdefault(mxid, asyncio.Lock())


async def may_have_shadow(app: FastAPI, sender: str, room_id: str) -> bool:
    """Whether ``sender`` may be given a new shadow ID for something done
    in ``room_id``: a local user always (unless ``bridge.id_allowed_users``
    leaves them out); a third party with access on and a grant that covers
    this room."""
    config = app.state.config
    if not sender.startswith("@") or is_bridge_user(config, sender):
        return False
    if not is_third_party(config, sender):
        return ids.user_allowed(config, sender)
    if not config.bridge.third_party_access:
        return False
    return await resolve_third_party_tier(app, sender, room_id) is not None


async def ensure_shadow_id(app: FastAPI, sender: str, room_id: str) -> ShadowResolution:
    """Who acts for ``sender`` in ``room_id``, making a shadow ID if they
    have none yet. Every place that sends something to Nostr on a user's
    behalf goes through this rather than refusing someone without an ID.

    1. The ID that would act anyway (``interactions.acting_id``: the
       room's own, or their Active one): found, not created.
    2. Otherwise, if they may have one (``may_have_shadow``), a new ID
       with no Profile Room, named by ``default_id_name`` (or the nearest
       free name), its profile mirrored from Matrix. ``(None, False)`` if
       they may not."""
    from bridge.interactions import acting_id

    existing = await acting_id(app, sender, room_id)
    if existing is not None:
        return ShadowResolution(existing, False)
    if not await may_have_shadow(app, sender, room_id):
        return ShadowResolution(None, False)
    async with _lock_for(app, sender):
        existing = await acting_id(app, sender, room_id)
        if existing is not None:
            return ShadowResolution(existing, False)
        created = await _make_shadow_id(app, sender)
    return ShadowResolution(created, created is not None)


async def _make_shadow_id(app: FastAPI, sender: str) -> NostrId | None:
    from bridge.identity_events import sync_identity

    config = app.state.config
    name = default_id_name(config, sender)
    if await ids.check_new_name(app, name, sender) is not None:
        name = await ids.suggest_available(app, name, sender) or ""
    if not name:
        logger.warning("No free name for a shadow ID for %s", sender)
        return None
    try:
        nostr_id = await ids.create_id(app, sender, name)
    except ids.IdRejected as exc:
        logger.warning("Could not make a shadow ID for %s: %s", sender, exc)
        return None
    repository = app.state.repository
    # No Profile Room on purpose: none is made for it until [create profile.
    await repository.unlink_profile_room(nostr_id.id)
    nostr_id = await repository.get_id(nostr_id.id) or nostr_id
    logger.info("Made shadow ID %s@%s for %s", nostr_id.name, config.bridge.domain, sender)
    try:
        await sync_identity(app, nostr_id)
    except HomeserverError:
        logger.warning("Could not publish the profile of shadow ID %s yet", nostr_id.name, exc_info=True)
    return nostr_id


def shadow_created_notice(app: FastAPI, nostr_id: NostrId) -> str:
    """Said once, wherever a shadow ID was just made."""
    config = app.state.config
    prefix = config.appservice.command_prefix
    return (
        f"I've created a shadow Nostr identity for you: {nostr_id.name}@{config.bridge.domain}. "
        "It mirrors your Matrix display name/avatar automatically, and has no profile of its own yet. "
        f'Run "{prefix}create profile" anytime to turn it into a full, customizable profile.'
    )


async def ensure_shadow_id_and_tell(app: FastAPI, sender: str, room_id: str) -> NostrId | None:
    """``ensure_shadow_id``, sending the notice in ``room_id`` if one was made."""
    from bridge.messages import notice

    resolution = await ensure_shadow_id(app, sender, room_id)
    if resolution.created and resolution.nostr_id is not None:
        await notice(app, room_id, shadow_created_notice(app, resolution.nostr_id))
    return resolution.nostr_id


# -- keeping shadow profiles in step with Matrix -------------------------------------------------


async def mirrors_matrix(app: FastAPI, nostr_id: NostrId) -> bool:
    """Whether the ID's profile is its owner's Matrix profile: shadow tier
    and a key the bridge made. (An imported key's profile is the user's
    own, from before the bridge; it is never overwritten.)"""
    return _is_generated(nostr_id) and await effective_tier(app, nostr_id) == "shadow"


async def sync_shadow_profiles(app: FastAPI) -> None:
    """One pass over every shadow ID: republish its profile if its owner's
    Matrix name or avatar changed (``sync_identity`` signs nothing when
    nothing changed)."""
    from bridge.identity_events import sync_identity

    for nostr_id in await app.state.repository.list_all_ids():
        if not await mirrors_matrix(app, nostr_id):
            continue
        try:
            await sync_identity(app, nostr_id)
        except Exception:
            logger.warning("Could not update the profile of shadow ID %s", nostr_id.name, exc_info=True)


async def shadow_profile_loop(app: FastAPI) -> None:
    """Runs until shutdown. The interval is the rate limit, for everyone alike."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        try:
            await sync_shadow_profiles(app)
        except Exception:
            logger.warning("Shadow profile sync failed", exc_info=True)
