"""Naming an account in a command by a pill, or by replying to its message."""

from __future__ import annotations

from bridge import remote as remotes
from tests.test_federation import Stranger, create_id, run, transact


def _ghost(client):
    stranger = Stranger()
    remote = run(client, remotes.get_or_create_remote, client.app, stranger.pubkey)
    return stranger, remote


def _blocks(client, q):
    return run(client, client.app.state.repository.list_blocks, q.id)


def _send(client, homeserver, room, content, sender="@q:example.org"):
    event = homeserver._store(room, "m.room.message", content, sender)
    transact(client, event)
    return event


def test_block_by_pill_in_a_thread(client, homeserver) -> None:
    q = create_id(client)
    stranger, remote = _ghost(client)
    room = q.profile_room_id
    root = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "spam"}, remote.ghost_user_id)
    _send(client, homeserver, room, {
        "msgtype": "m.text", "body": "[block moneydesk ", "format": "org.matrix.custom.html",
        "formatted_body": f'<p>[block <a href="https://matrix.to/#/{remote.ghost_user_id}">moneydesk</a></p>\n',
        "m.mentions": {"user_ids": [remote.ghost_user_id]},
        "m.relates_to": {"rel_type": "m.thread", "event_id": root["event_id"], "is_falling_back": True,
                         "m.in_reply_to": {"event_id": root["event_id"]}}})
    assert stranger.pubkey in _blocks(client, q)
    assert homeserver.sent_to(room)[-1]["body"].startswith("Blocked ")


def test_block_by_replying_to_their_message(client, homeserver) -> None:
    q = create_id(client)
    stranger, remote = _ghost(client)
    room = q.profile_room_id
    root = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "a post"}, remote.ghost_user_id)
    message = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "spam", "m.relates_to": {
        "rel_type": "m.thread", "event_id": root["event_id"]}}, remote.ghost_user_id)
    # A real reply, inside a thread.
    _send(client, homeserver, room, {"msgtype": "m.text", "body": "[block", "m.relates_to": {
        "rel_type": "m.thread", "event_id": root["event_id"], "is_falling_back": False,
        "m.in_reply_to": {"event_id": message["event_id"]}}})
    assert stranger.pubkey in _blocks(client, q)
    _send(client, homeserver, room, {"msgtype": "m.text", "body": "[unblock", "m.relates_to": {
        "m.in_reply_to": {"event_id": message["event_id"]}}})
    assert _blocks(client, q) == []
    assert homeserver.sent_to(room)[-1]["body"].startswith("Unblocked ")


def test_a_threads_fallback_link_isnt_a_reply(client, homeserver) -> None:
    q = create_id(client)
    _stranger, remote = _ghost(client)
    room = q.profile_room_id
    root = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "a post"}, remote.ghost_user_id)
    _send(client, homeserver, room, {"msgtype": "m.text", "body": "[block", "m.relates_to": {
        "rel_type": "m.thread", "event_id": root["event_id"], "is_falling_back": True,
        "m.in_reply_to": {"event_id": root["event_id"]}}})
    assert _blocks(client, q) == []
    assert homeserver.sent_to(room)[-1]["body"].startswith("Which account?")


def test_block_by_bare_ghost_mxid(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    stranger, remote = _ghost(client)
    _send(client, homeserver, "!dm:example.org", {"msgtype": "m.text", "body": f"[block {remote.ghost_user_id}"})
    assert stranger.pubkey in _blocks(client, q)
