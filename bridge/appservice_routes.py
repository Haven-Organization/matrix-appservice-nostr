"""The Matrix Application Service API: the homeserver -> bridge half.

The homeserver PUTs batches of events here whenever something happens in a
room one of the bridge's users is in, authenticating with the shared
``hs_token``. Implemented per the spec, so any homeserver works:

- ``PUT /_matrix/app/v1/transactions/{txnId}``: events. Homeservers retry
  a transaction until it's acknowledged, so events are deduplicated, by
  event ID rather than transaction ID: Synapse on SQLite starts numbering
  transactions from 1 again after a restart, and dropping a reused ID would
  silently lose new events.
- ``POST /_matrix/app/v1/ping``: the homeserver checking it can reach us.
- ``GET /_matrix/app/v1/users/{userId}`` and ``.../rooms/{alias}``: the
  homeserver asking whether we want to create a user or room it was asked
  about. Ghosts are only ever created by the bridge itself, so no.

Each event goes to a chain of handlers; the first to claim it ends its
processing. The chain grows as features land (see DESIGN.md, "Build
order").
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from bridge.commands import maybe_handle_command
from bridge.confirm import maybe_handle_confirmation
from bridge.dms import maybe_send_dm
from bridge.interactions import maybe_handle_reaction
from bridge.membership import maybe_handle_join, maybe_handle_knock, maybe_handle_leave, maybe_handle_profile_state
from bridge.messages import notice
from bridge.profile_posts import maybe_handle_edit, maybe_handle_redaction, maybe_publish_message
from bridge.polls import maybe_handle_vote, maybe_publish_poll
from bridge.secret_guard import maybe_catch_secret
from bridge.shares import maybe_publish_share, repost_relation
from bridge.concord import maybe_publish as maybe_publish_concord
from bridge.games_room import maybe_handle_card_reaction
from bridge.groups import maybe_publish as maybe_publish_group
from bridge.users import bot_mxid, is_local_user, is_other_bot
from bridge.homeserver_client import HomeserverError

logger = logging.getLogger(__name__)

router = APIRouter()

_EMPTY = {"status_code": 200, "content": "{}", "media_type": "application/json"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_hs_token(request: Request, authorization: str | None, access_token: str | None) -> None:
    """Verify the request is from our homeserver: ``hs_token`` as a Bearer
    header (spec v1.4+) or, from older homeservers, an ``access_token``
    query parameter."""
    expected = request.app.state.config.appservice.hs_token
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[len("bearer ") :]
    elif access_token:
        presented = access_token
    if not presented:
        raise HTTPException(status_code=401, detail={"errcode": "M_UNAUTHORIZED", "error": "Missing hs_token"})
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail={"errcode": "M_FORBIDDEN", "error": "Invalid hs_token"})


async def maybe_accept_invite(request: Request, event: dict) -> bool:
    """An invite to the bot: join, so the homeserver sends us the room's
    events. Returns True if this was one."""
    if event.get("type") != "m.room.member":
        return False
    content = event.get("content") or {}
    config = request.app.state.config
    bot = bot_mxid(config)
    if content.get("membership") != "invite" or event.get("state_key") != bot:
        return False
    room_id = event.get("room_id", "")
    inviter = event.get("sender", "")
    homeserver = request.app.state.homeserver
    try:
        await homeserver.join_room(room_id, as_user_id=bot)
    except HomeserverError:
        logger.warning("Could not join %s on invite from %s", room_id, inviter, exc_info=True)
        return True
    try:
        encrypted = await homeserver.get_room_state(room_id, "m.room.encryption", as_user_id=bot) is not None
    except HomeserverError:
        encrypted = False
    if encrypted:
        # The bridge has no end-to-end encryption support, and clients
        # commonly create direct chats encrypted by default.
        await notice(
            request.app,
            room_id,
            "This room is encrypted, so I can't read anything said in it. "
            "Please invite me to a new room with encryption turned off.",
        )
    elif content.get("is_direct") and is_local_user(config, inviter):
        prefix = config.appservice.command_prefix
        await notice(
            request.app,
            room_id,
            f'Hi! Say "{prefix}create profile" to get a Nostr ID, or "{prefix}help" to see what I can do.',
        )
    return True


_HANDLERS = (
    maybe_accept_invite,
    maybe_handle_knock,
    maybe_handle_join,
    maybe_handle_leave,
    # A 🎮 on a game card, added or taken away: loads the game (before
    # reactions and redactions go to Nostr).
    maybe_handle_card_reaction,
    maybe_handle_redaction,
    maybe_handle_profile_state,
    # Before anything that could act on (or publish) the message.
    maybe_catch_secret,
    # An edit of a published note: republished (after the secret check).
    maybe_handle_edit,
    # Before commands: a bare "confirm" is recognized by what it replies
    # to, not by a prefix or a tag.
    maybe_handle_confirmation,
    maybe_handle_command,
    # A Concord community's channel room: its messages, reactions, edits
    # and redactions go to the community.
    maybe_publish_concord,
    # A NIP-29 group's room: the same, to the group's relay.
    maybe_publish_group,
    maybe_handle_reaction,
    maybe_publish_poll,
    maybe_handle_vote,
    maybe_send_dm,
    # A repost (MSC4501) made in a Profile Room, by the user or through
    # another bridge sharing the room.
    maybe_publish_share,
    # Last: anything else in a Profile Room may be a post, and a reply to a
    # Nostr post anywhere is a reply.
    maybe_publish_message,
)


async def handle_event(request: Request, event: dict[str, Any]) -> None:
    # Other bots and bridges' ghosts: their messages, reactions, votes and
    # redactions are never acted on (they're not people with Nostr IDs, even
    # when they're on this server). Room state they set still counts: another
    # bridge updating the room's name or avatar changes the profile here too.
    # The exceptions: a bridge's card for a repost its user made (published
    # here too, bridge.shares), and redactions (undoing such a repost).
    if (
        "state_key" not in event
        and is_other_bot(request.app.state.config, event.get("sender", ""))
        and event.get("type") != "m.room.redaction"
        and repost_relation(event) is None
    ):
        return
    for handler in _HANDLERS:
        if await handler(request, event):
            return


@router.put("/_matrix/app/v1/transactions/{txn_id}")
async def put_transaction(
    request: Request,
    txn_id: str,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Response:
    _check_hs_token(request, authorization, access_token)
    repository = request.app.state.repository
    try:
        body: dict[str, Any] = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"errcode": "M_NOT_JSON", "error": str(exc)}) from exc
    events = body.get("events") if isinstance(body, dict) else None
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        # A retried transaction carries events already handled. Claimed
        # before handling, so an event that fails isn't retried forever.
        event_id = event.get("event_id")
        if isinstance(event_id, str) and not await repository.claim_event(event_id, _now()):
            continue
        try:
            await handle_event(request, event)
        except Exception:
            logger.exception(
                "Error handling event %s (%s) in %s", event.get("event_id"), event.get("type"), event.get("room_id")
            )
    return Response(**_EMPTY)


@router.post("/_matrix/app/v1/ping")
async def ping(
    request: Request,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Response:
    _check_hs_token(request, authorization, access_token)
    return Response(**_EMPTY)


@router.get("/_matrix/app/v1/users/{user_id}")
async def query_user(
    request: Request,
    user_id: str,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Response:
    _check_hs_token(request, authorization, access_token)
    return JSONResponse({"errcode": "M_NOT_FOUND", "error": "No such user"}, status_code=404)


@router.get("/_matrix/app/v1/rooms/{alias}")
async def query_room(
    request: Request,
    alias: str,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Response:
    _check_hs_token(request, authorization, access_token)
    return JSONResponse({"errcode": "M_NOT_FOUND", "error": "No such room"}, status_code=404)
