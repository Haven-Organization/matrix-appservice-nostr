"""Polls (NIP-88) as Matrix polls (MSC3381), both ways.

- A poll posted in a Profile Room by its owner becomes a kind 1068 poll:
  the question as its content, each answer an ``option``, single or multiple
  choice. Its ``relay`` tag names the bridge's relay, which is where
  NIP-88 clients send their votes, so they come back as votes here.
- Voting on a bridged poll (anyone's) from Matrix sends a kind 1018 vote as
  the voter's acting ID.
- A followed account's poll appears in its room as a Matrix poll from its
  ghost; votes on any poll that's in Matrix appear as Matrix votes from the
  voters' ghosts.

The poll's results are Matrix's own tally of those votes. Matrix polls use
the unstable MSC3381 event types here: that's what clients render today.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request

from bridge import remote as remotes
from bridge.event_sources import EMBED_FIELD
from bridge.homeserver_client import HomeserverError
from bridge.inbound import REJECTED, IngestResult, _store, event_url, index_tags
from bridge import shadow_ids
from bridge.notes import POLL_RESPONSE_TYPES, POLL_START_TYPES, poll_v1, vote_v1
from bridge.publishing import NotifyTarget, publish_in_background
from bridge.repository import EventRow
from bridge.users import is_bridge_user

logger = logging.getLogger(__name__)

POLL_KIND = 1068
VOTE_KIND = 1018
_POLL_START = "org.matrix.msc3381.poll.start"
_POLL_RESPONSE = "org.matrix.msc3381.poll.response"


async def _sign_store_send(app: FastAPI, author, *, kind: int, content: str, tags: list[list[str]], created_at: int,
                           recipe: dict[str, Any], room_id: str, matrix_event_id: str, notify: NotifyTarget | None,
                           extra_relays: list[str] | None = None) -> EventRow | None:
    event = await app.state.signer.sign(author.pubkey, kind=kind, content=content, created_at=created_at, tags=tags)
    if event is None:
        return None
    row = EventRow(
        id=event.id, pubkey=event.pubkey, kind=kind, created_at=created_at, sig=event.sig,
        source=json.dumps(recipe, ensure_ascii=False, separators=(",", ":")), room_id=room_id,
        matrix_event_id=matrix_event_id, tags=index_tags(tags),
    )
    await app.state.repository.put_event(row)
    publish_in_background(app, event, notify=notify, extra_relays=extra_relays)
    return row


async def maybe_publish_poll(request: Request, event: dict[str, Any]) -> bool:
    if event.get("type") not in POLL_START_TYPES:
        return False
    app = request.app
    room_id = event.get("room_id", "")
    profile = await app.state.repository.get_id_for_profile_room(room_id)
    if profile is None or event.get("sender") != profile.matrix_user_id:
        return False
    if not await shadow_ids.may_post_from_profile_room(app, profile) or not await shadow_ids.still_allowed(
        app, profile, room_id
    ):
        return True  # a third party's parked room: stays in Matrix
    recipe = {"kind": "poll", "v": 1, "room_id": room_id, "event_id": event.get("event_id", ""),
              "relay_hint": app.state.config.nostr.relay_url}
    content, tags = poll_v1(event, recipe)
    if not content or not any(t[0] == "option" for t in tags):
        return True
    await _sign_store_send(
        app, profile, kind=POLL_KIND, content=content, tags=tags, created_at=int(event.get("origin_server_ts", 0)) // 1000,
        recipe=recipe, room_id=room_id, matrix_event_id=event.get("event_id", ""),
        notify=NotifyTarget(room_id, event.get("event_id", ""), "this poll"),
    )
    return True


async def maybe_handle_vote(request: Request, event: dict[str, Any]) -> bool:
    if event.get("type") not in POLL_RESPONSE_TYPES:
        return False
    app = request.app
    config = app.state.config
    sender = event.get("sender", "")
    if is_bridge_user(config, sender) or not sender.startswith("@"):
        return False
    poll_event = ((event.get("content") or {}).get("m.relates_to") or {}).get("event_id")
    poll = await app.state.repository.get_event_by_matrix_event(poll_event) if isinstance(poll_event, str) else None
    if poll is None or poll.kind != POLL_KIND:
        return False
    room_id = event.get("room_id", "")
    voter = await shadow_ids.ensure_shadow_id_and_tell(app, sender, room_id)
    if voter is None or not await shadow_ids.still_allowed(app, voter, room_id):
        return True
    recipe = {"kind": "vote", "v": 1, "room_id": room_id, "event_id": event.get("event_id", ""),
              "poll": poll.id, "poll_pubkey": poll.pubkey}
    content, tags = vote_v1(event, recipe)
    relays = await _poll_relays(app, poll)
    await _sign_store_send(
        app, voter, kind=VOTE_KIND, content=content, tags=tags, created_at=int(event.get("origin_server_ts", 0)) // 1000,
        recipe=recipe, room_id=room_id, matrix_event_id=event.get("event_id", ""), notify=None, extra_relays=relays,
    )
    return True


async def _poll_relays(app: FastAPI, poll: EventRow) -> list[str]:
    """Where a poll asks for its votes (its ``relay`` tags)."""
    from bridge.event_sources import rebuild

    rebuilt = await rebuild(app, poll)
    if rebuilt.status != "ok" or rebuilt.event is None:
        return []
    return [t[1] for t in rebuilt.event.get("tags") or [] if len(t) >= 2 and t[0] == "relay" and t[1].startswith("wss://")][:4]


# -- incoming ---------------------------------------------------------------------------


def matrix_poll(raw: dict[str, Any]) -> dict[str, Any]:
    options = [t for t in raw.get("tags") or [] if len(t) >= 3 and t[0] == "option"]
    multiple = any(len(t) >= 2 and t[0] == "polltype" and t[1] == "multiplechoice" for t in raw.get("tags") or [])
    question = (raw.get("content") or "").strip() or "Poll"
    fallback = question + "\n" + "\n".join(f"{n}. {t[2]}" for n, t in enumerate(options, start=1))
    return {
        _POLL_START: {
            "question": {"org.matrix.msc1767.text": question},
            "kind": "org.matrix.msc3381.poll.disclosed",
            "max_selections": len(options) if multiple else 1,
            "answers": [{"id": t[1], "org.matrix.msc1767.text": t[2]} for t in options],
        },
        "org.matrix.msc1767.text": fallback,
        "body": fallback,
    }


async def receive_poll(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    """A followed account's poll: a Matrix poll in its room."""
    repository = app.state.repository
    if not await repository.list_followers_of_remote(raw["pubkey"]):
        return REJECTED
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    remote = await remotes.ensure_room(app, remote)
    assert remote.room_id is not None
    content = {**matrix_poll(raw), EMBED_FIELD: raw, "external_url": event_url(app, raw)}
    try:
        event_id = await app.state.homeserver.send_message_event(
            remote.room_id, content, as_user_id=remote.ghost_user_id, ts=int(raw["created_at"]) * 1000, event_type=_POLL_START
        )
    except HomeserverError:
        return IngestResult(False, "error: could not show this poll")
    await _store(app, raw, room_id=remote.room_id, matrix_event_id=event_id, scope="followed")
    return IngestResult(True, "", stored=True)


async def receive_vote(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    """A vote on a poll that's in Matrix: a Matrix vote from the voter's ghost."""
    repository = app.state.repository
    poll_id = next((t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "e"), None)
    poll = await repository.get_event(poll_id) if poll_id else None
    if poll is None or poll.kind != POLL_KIND or not poll.room_id or not poll.matrix_event_id:
        return REJECTED
    answers = [t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "response"]
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    await remotes.ensure_ghost_in_room(app, remote.ghost_user_id, poll.room_id)
    content = {
        "m.relates_to": {"rel_type": "m.reference", "event_id": poll.matrix_event_id},
        _POLL_RESPONSE: {"answers": answers},
        EMBED_FIELD: raw,
    }
    try:
        event_id = await app.state.homeserver.send_message_event(
            poll.room_id, content, as_user_id=remote.ghost_user_id, event_type=_POLL_RESPONSE
        )
    except HomeserverError:
        return IngestResult(False, "error: could not record this vote")
    local = await repository.get_id_by_pubkey(poll.pubkey)
    await _store(app, raw, room_id=poll.room_id, matrix_event_id=event_id, scope="public" if local else "followed")
    return IngestResult(True, "", stored=True)
