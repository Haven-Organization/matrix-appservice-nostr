"""[unlink, [link and [create room: moving an ID's Profile Room."""

from __future__ import annotations

import json

from bridge.msc4501 import SOCIAL_PROFILE_USER_ID_STATE_TYPE
from tests.test_ids import ids_of, last_body, message, send

BOT = "@nostrbot:example.org"


def ask_and_confirm(client, homeserver, body, room, sender="@q:example.org"):
    send(client, message(body, room=room, sender=sender))
    ask = homeserver.sent_to(room)[-1]
    ask_id = next(e for e in homeserver.rooms[room]["events"].values() if e["content"] is ask)["event_id"]
    send(client, message("confirm", room=room, sender=sender, reply_to=ask_id))
    return ask["body"]


def established_room(homeserver, *, q_level=100, bot_level=100, encrypted=False, name="Q's Hangout"):
    room = "!hangout:example.org"
    homeserver._store(room, "m.room.create", {"room_version": "10"}, "@q:example.org", "")
    homeserver._store(room, "m.room.power_levels", {"users": {"@q:example.org": q_level, BOT: bot_level},
                                                    "state_default": 50}, "@q:example.org", "")
    homeserver._store(room, "m.room.name", {"name": name}, "@q:example.org", "")
    homeserver._store(room, "m.room.topic", {"topic": "where Q hangs out"}, "@q:example.org", "")
    if encrypted:
        homeserver._store(room, "m.room.encryption", {"algorithm": "m.megolm.v1.aes-sha2"}, "@q:example.org", "")
    homeserver._room(room)["members"].update({"@q:example.org", BOT})
    return room


def latest_profile(client, pubkey):
    row = client.portal.call(client.app.state.repository.get_latest_event, pubkey, 0)
    from bridge.event_sources import rebuild

    return json.loads(client.portal.call(rebuild, client.app, row).event["content"])


def test_unlink_then_link_existing_room(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    old_room = q.profile_room_id
    send(client, homeserver._store(old_room, "m.room.topic", {"topic": "my nostr bio"}, "@q:example.org", ""))

    body = ask_and_confirm(client, homeserver, "[unlink 1", "!dm:example.org")
    assert "Unlink q@example.org" in body
    [q] = ids_of(client)
    assert q.profile_room_id is None and q.room_unlinked
    assert homeserver.state_of(old_room, SOCIAL_PROFILE_USER_ID_STATE_TYPE) == {}
    assert "no longer the Profile Room" in homeserver.sent_to(old_room)[-1]["body"]
    # Messages there aren't posts any more.
    before = len(relay_pool.of_kind(1))
    send(client, message("just chatting", room=old_room))
    assert len(relay_pool.of_kind(1)) == before
    # Creating another ID doesn't bring the room back.
    send(client, message("[create profile qbot"))
    assert ids_of(client)[0].profile_room_id is None

    room = established_room(homeserver)
    body = ask_and_confirm(client, homeserver, "[link q", room)
    assert "published publicly on Nostr" in body and "replaced with the current profile" in body
    [q, _] = ids_of(client)
    assert q.profile_room_id == room and not q.room_unlinked
    # The room took on the ID's profile, so the Nostr profile is unchanged.
    assert homeserver.state_of(room, "m.room.name") == {"name": "Q Person"}
    assert homeserver.state_of(room, "m.room.topic") == {"topic": "my nostr bio"}
    assert homeserver.state_of(room, SOCIAL_PROFILE_USER_ID_STATE_TYPE) == {"user_id": "@q:example.org"}
    assert latest_profile(client, q.pubkey)["about"] == "my nostr bio"
    send(client, message("first post from the hangout", room=room))
    assert relay_pool.of_kind(1)[-1].content == "first post from the hangout"


def test_link_keep_moves_directly(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    old_room = q.profile_room_id
    room = established_room(homeserver)
    body = ask_and_confirm(client, homeserver, "[link 1 keep", room)
    assert "become the Nostr profile" in body and old_room in body
    [q] = ids_of(client)
    assert q.profile_room_id == room
    assert homeserver.state_of(room, "m.room.name") == {"name": "Q's Hangout"}
    profile = latest_profile(client, q.pubkey)
    assert profile["name"] == "Q's Hangout" and profile["about"] == "where Q hangs out"
    assert "no longer the Profile Room" in homeserver.sent_to(old_room)[-1]["body"]
    assert homeserver.state_of(old_room, SOCIAL_PROFILE_USER_ID_STATE_TYPE) == {}


def test_link_refusals(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    room = established_room(homeserver, q_level=50)
    send(client, message("[link 1", room=room))
    assert "need to be an admin" in last_body(homeserver, room)

    room = established_room(homeserver, bot_level=0)
    send(client, message("[link 1 keep", room=room))
    warning = homeserver.sent_to(room)[-1]
    assert "I need power level 50 (Moderator) here, and I have 0" in warning["body"]
    assert "- change the room's name" in warning["body"] and "- mark the room as bridged to Nostr" in warning["body"]
    assert warning["body"].endswith("then send this again:\n[link q keep")
    assert "<pre><code>[link q keep</code></pre>" in warning["formatted_body"]
    assert ids_of(client)[0].profile_room_id == q.profile_room_id

    # Fixed: the same command now asks to confirm. Power taken away again
    # before confirming: the warning comes back and nothing is linked.
    levels = homeserver.state_of(room, "m.room.power_levels")
    homeserver._store(room, "m.room.power_levels", {**levels, "users": {**levels["users"], BOT: 50}}, "@q:example.org", "")
    send(client, message("[link 1 keep", room=room))
    ask = homeserver.sent_to(room)[-1]
    assert "Make this room the Profile Room" in ask["body"]
    homeserver._store(room, "m.room.power_levels", levels, "@q:example.org", "")
    ask_id = next(e for e in homeserver.rooms[room]["events"].values() if e["content"] is ask)["event_id"]
    send(client, message("confirm", room=room, reply_to=ask_id))
    assert "I need power level 50" in last_body(homeserver, room)
    assert ids_of(client)[0].profile_room_id == q.profile_room_id

    room = established_room(homeserver, encrypted=True)
    send(client, message("[link 1", room=room))
    assert "encrypted" in last_body(homeserver, room)

    send(client, message("[link 1", room=q.profile_room_id))
    assert "already the Profile Room" in last_body(homeserver, q.profile_room_id)

    send(client, message("[link 7", room="!dm:example.org"))
    assert "Usage" in last_body(homeserver)

    # Someone else can't confirm, and nothing changed.
    room = established_room(homeserver)
    send(client, message("[link 1", room=room))
    assert ids_of(client)[0].profile_room_id == q.profile_room_id


def test_create_room_for_unlinked_id(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    send(client, homeserver._store(q.profile_room_id, "m.room.topic", {"topic": "bio kept"}, "@q:example.org", ""))
    send(client, message("[create room 1"))
    assert "already has a Profile Room" in last_body(homeserver)

    ask_and_confirm(client, homeserver, "[unlink q", "!dm:example.org")
    send(client, message("[create room q"))
    [q] = ids_of(client)
    assert q.profile_room_id and not q.room_unlinked
    assert "Created a new Profile Room" in last_body(homeserver)
    new = homeserver.created[-1]
    assert new["room_id"] == q.profile_room_id
    assert homeserver.state_of(q.profile_room_id, "m.room.topic") == {"topic": "bio kept"}
    assert homeserver.state_of(q.profile_room_id, "m.room.name") == {"name": "Q Person"}
    assert (q.profile_room_id, "@q:example.org") in homeserver.invites
