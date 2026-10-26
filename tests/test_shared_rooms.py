"""Rooms shared with other bridges: other bots' commands are never posts,
replies to things that aren't on Nostr stay in Matrix, and linking and
unlinking leave other bridges' state alone."""

from __future__ import annotations

import json

import pytest

from bridge.commands import message_addresses_other_bot
from bridge.config import ConfigError, parse_config
from bridge.msc4501 import SOCIAL_PROFILE_USER_ID_STATE_TYPE
from tests.conftest import make_client, raw_config
from tests.test_ids import ids_of, last_body, message, send
from tests.test_room_links import ask_and_confirm, established_room

ATPROTO_BOT = "@atprotobot:example.org"
OTHERS = {"other_command_prefixes": [";", "]"], "other_bot_users": [ATPROTO_BOT],
          "other_bridge_users": [r"@atproto_.*:example\.org"]}


@pytest.fixture
def shared(repository, homeserver, relay_pool):
    config = parse_config(raw_config(appservice=OTHERS))
    with make_client(config, repository, homeserver, relay_pool) as client:
        yield client


@pytest.mark.parametrize(
    "body, other",
    [
        ("]follow alice.bsky.social", True),
        (";help", True),
        ("]help\nmore lines", True),
        ("[Update] the new site is up", False),
        ("; not a command", False),
        (";)", False),
        ("hello ]follow", False),
        ("hey @atprotobot:example.org help", True),
    ],
)
def test_other_bot_detection(body, other) -> None:
    config = parse_config(raw_config(appservice=OTHERS))
    assert message_addresses_other_bot({"msgtype": "m.text", "body": body}, config) is other


def test_other_bot_mention_by_pill() -> None:
    config = parse_config(raw_config(appservice=OTHERS))
    content = {"msgtype": "m.text", "body": "Bluesky Bridge: status", "m.mentions": {"user_ids": [ATPROTO_BOT]}}
    assert message_addresses_other_bot(content, config)


@pytest.mark.parametrize(
    "appservice",
    [{"other_command_prefixes": [""]}, {"other_command_prefixes": ["["]}, {"other_bot_users": ["atprotobot"]},
     {"other_bridge_users": ["@atproto_(:example.org"]}, {"other_bridge_users": [""]}],
)
def test_bad_other_bot_config(appservice) -> None:
    with pytest.raises(ConfigError):
        parse_config(raw_config(appservice=appservice))


def test_other_bots_commands_are_not_posts(shared, homeserver, relay_pool) -> None:
    send(shared, message("[create profile"))
    [q] = ids_of(shared)
    room = q.profile_room_id
    answered = len(homeserver.sent_to(room))
    for body in ("]follow alice.bsky.social", ";follow @bob@mastodon.social", "@atprotobot:example.org status"):
        send(shared, message(body, room=room))
    assert relay_pool.of_kind(1) == []
    assert len(homeserver.sent_to(room)) == answered  # and not answered either
    send(shared, message("[Update] a real post", room=room))
    assert relay_pool.of_kind(1)[-1].content == "[Update] a real post"


def test_replies_to_things_not_on_nostr_stay_in_matrix(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    room = q.profile_room_id
    # Another bridge's bot asks something, and its ghost mirrors a reply.
    question = homeserver._store(room, "m.room.message", {"msgtype": "m.notice", "body": 'Reply "confirm"'}, ATPROTO_BOT)
    ghost = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "nice post"}, "@atproto_alice:example.org")
    for target in (question, ghost):
        send(client, message("confirm", room=room, reply_to=target["event_id"]))
    assert relay_pool.of_kind(1) == []
    # Nor is replying to your own message that never went to Nostr.
    own = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "older"}, "@q:example.org")
    send(client, message("following up", room=room, reply_to=own["event_id"]))
    assert relay_pool.of_kind(1) == []


def test_chatting_with_a_guest_in_a_profile_room(shared, homeserver, relay_pool) -> None:
    send(shared, message("[create profile"))
    [q] = ids_of(shared)
    room = q.profile_room_id

    def threaded(body, root, reply, falling_back):
        event = message(body, room=room)
        event["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": root, "is_falling_back": falling_back,
                                            "m.in_reply_to": {"event_id": reply}}
        return event

    guest = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "hey there"}, "@guest:example.org")
    send(shared, message("hi back", room=room, reply_to=guest["event_id"]))
    send(shared, threaded("in his thread", guest["event_id"], guest["event_id"], True))
    assert relay_pool.of_kind(1) == []
    # A thread under a post: replying to the post is a Nostr reply, but an
    # explicit reply to the guest in it stays in Matrix.
    posted = message("a real post", room=room)
    send(shared, posted)
    [post] = relay_pool.of_kind(1)
    said = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "cool"}, "@guest:example.org")
    send(shared, threaded("thanks", posted["event_id"], said["event_id"], False))
    assert relay_pool.of_kind(1) == [post] and not relay_pool.of_kind(1111)
    send(shared, threaded("more on this", posted["event_id"], said["event_id"], True))
    assert relay_pool.of_kind(1)[-1].content == "more on this"
    # Asides: a prefix and a space (or a prefix alone), ours or another bot's.
    published = len(relay_pool.of_kind(1))
    answered = len(homeserver.sent_to(room))
    for body in ("[ just between us", "[ help", "[", "] and this", "[\nsecond line"):
        send(shared, message(body, room=room))
    assert len(relay_pool.of_kind(1)) == published
    assert len(homeserver.sent_to(room)) == answered


def _mark_shared(homeserver, room, *, msc4501_level=None):
    homeserver._store(room, "m.bridge", {"protocol": {"id": "atproto", "displayname": "Bluesky"}}, ATPROTO_BOT,
                      "atproto://did:plc:abc")
    homeserver._store(room, SOCIAL_PROFILE_USER_ID_STATE_TYPE, {"user_id": "@q:example.org"}, ATPROTO_BOT, "")
    if msc4501_level is not None:
        levels = homeserver.state_of(room, "m.room.power_levels")
        levels["events"] = {SOCIAL_PROFILE_USER_ID_STATE_TYPE: msc4501_level}
        homeserver._store(room, "m.room.power_levels", levels, "@q:example.org", "")


def test_link_and_unlink_in_a_shared_room(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    # Another bridge's room: the MSC4501 marker needs 100, more than our bot has.
    room = established_room(homeserver, bot_level=50)
    _mark_shared(homeserver, room, msc4501_level=100)
    send(client, message("[link 1", room=room))
    ask = last_body(homeserver, room)
    assert "also bridged to Bluesky" in ask and '"keep" avoids that' in ask

    ask_and_confirm(client, homeserver, "[link 1 keep", room)
    assert ids_of(client)[0].profile_room_id == room

    ask_and_confirm(client, homeserver, "[unlink 1", room)
    assert ids_of(client)[0].profile_room_id is None
    # Bluesky still uses the room: the shared marker stays; only ours goes.
    assert homeserver.state_of(room, SOCIAL_PROFILE_USER_ID_STATE_TYPE) == {"user_id": "@q:example.org"}
    pubkey = ids_of(client)[0].pubkey
    assert homeserver._room(room)["state"][("m.bridge", f"nostr://{pubkey}")]["content"] == {}
    assert homeserver._room(room)["state"][("m.bridge", "atproto://did:plc:abc")]["content"]


def test_other_bridges_ghosts_and_bots_are_never_acted_on(shared, homeserver, relay_pool) -> None:
    send(shared, message("[create profile"))
    [q] = ids_of(shared)
    room = q.profile_room_id
    note_count = len(relay_pool.of_kind(1))
    answered = len(homeserver.sent_to(room))
    ghost = "@atproto_alice:example.org"
    # A Bluesky user's "[create profile", mirrored into the room by their ghost.
    send(shared, message("[create profile", sender=ghost, room=room))
    send(shared, message("[help", sender=ATPROTO_BOT, room=room))
    assert ids_of(shared, ghost) == []
    assert len(homeserver.sent_to(room)) == answered
    # Not this bridge's ghost-pattern: an ordinary local user still can.
    send(shared, message("[create profile", sender="@atprotofan:example.org"))
    assert ids_of(shared, "@atprotofan:example.org")
    # Room state another bridge sets still updates the Nostr profile.
    send(shared, homeserver._store(room, "m.room.topic", {"topic": "synced from Bluesky"}, ATPROTO_BOT, ""))
    profiles = relay_pool.of_kind(0)
    assert json.loads(profiles[-1].content)["about"] == "synced from Bluesky"
    assert len(relay_pool.of_kind(1)) == note_count


def test_link_another_bridges_profile_room_as_its_owner(client, homeserver, relay_pool) -> None:
    """The Bluesky bridge's own Profile Room (room v12, its bot the creator):
    the owner has 99, the room is marked as theirs, and that's enough."""
    send(client, message("[create profile"))
    room = "!v12room"
    homeserver._store(room, "m.room.create", {"room_version": "12", "type": "org.matrix.msc4501.social.profile"},
                      ATPROTO_BOT, "")
    homeserver._store(room, "m.room.power_levels", {
        "users": {ATPROTO_BOT: 99, "@nostrbot:example.org": 99, "@q:example.org": 99}, "state_default": 50,
        "events": {SOCIAL_PROFILE_USER_ID_STATE_TYPE: 100}}, ATPROTO_BOT, "")
    homeserver._store(room, SOCIAL_PROFILE_USER_ID_STATE_TYPE, {"user_id": "@q:example.org"}, ATPROTO_BOT, "")
    homeserver._room(room)["members"].update({"@q:example.org", "@nostrbot:example.org"})
    # Someone else's marked room still needs an admin.
    send(client, message("[create profile boss", sender="@admin:example.org"))
    homeserver._room(room)["members"].add("@admin:example.org")
    send(client, message("[link 1", sender="@admin:example.org", room=room))
    assert "You (not me) need to be an admin" in last_body(homeserver, room)

    ask_and_confirm(client, homeserver, "[link 1 keep", room)
    assert ids_of(client)[0].profile_room_id == room
