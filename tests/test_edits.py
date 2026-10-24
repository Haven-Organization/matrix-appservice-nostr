"""Editing a published post in Matrix (m.replace): a NIP-09 deletion of
the old note and a new note with the edited text."""

from __future__ import annotations

from bridge.event_sources import rebuild
from tests.test_federation import Stranger, local_post, post, relay_send, run


def edit(client, homeserver, room, target, text, sender="@q:example.org"):
    return post(client, homeserver, room, {
        "msgtype": "m.text", "body": f"* {text}",
        "m.new_content": {"msgtype": "m.text", "body": text},
        "m.relates_to": {"rel_type": "m.replace", "event_id": target}}, sender=sender)


def test_editing_a_post_replaces_its_note(client, homeserver, relay_pool) -> None:
    q, message, note = local_post(client, homeserver, relay_pool, "helo nostr")
    repo = client.app.state.repository
    edit(client, homeserver, q.profile_room_id, message["event_id"], "hello nostr")
    [deletion] = relay_pool.of_kind(5)
    assert ("e", note.id) in deletion.tags and deletion.pubkey == q.pubkey
    new = relay_pool.of_kind(1)[-1]
    assert new.id != note.id and new.content == "hello nostr" and new.pubkey == q.pubkey
    assert len(relay_pool.of_kind(1)) == 2  # the edit's "* " fallback wasn't posted
    assert run(client, repo.get_event, note.id) is None
    row = run(client, repo.get_event_by_matrix_event, message["event_id"])
    assert row.id == new.id
    rebuilt = run(client, rebuild, client.app, row)
    assert rebuilt.status == "ok" and rebuilt.event["content"] == "hello nostr"
    # Edits always point at the original: a second one replaces the new note.
    edit(client, homeserver, q.profile_room_id, message["event_id"], "hello, nostr")
    assert ("e", new.id) in relay_pool.of_kind(5)[-1].tags
    assert relay_pool.of_kind(1)[-1].content == "hello, nostr"
    latest = run(client, repo.get_event_by_matrix_event, message["event_id"])
    # And redacting the post deletes what it is now.
    from tests.test_federation import transact

    transact(client, {"type": "m.room.redaction", "event_id": "$red1", "room_id": q.profile_room_id,
                      "sender": "@q:example.org", "redacts": message["event_id"], "content": {}})
    assert ("e", latest.id) in relay_pool.of_kind(5)[-1].tags


def test_an_edited_reply_still_answers_its_post(client, homeserver, relay_pool) -> None:
    q, message, note = local_post(client, homeserver, relay_pool)
    alice = Stranger()
    reply = alice.event(1, "nice", [["e", note.id, "", "root"], ["p", q.pubkey]])
    relay_send(client, reply)
    shown = homeserver.messages(q.profile_room_id)[-1]
    mine = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "thnaks",
                                                        "m.relates_to": {"m.in_reply_to": {"event_id": shown["event_id"]}}})
    first = relay_pool.of_kind(1)[-1]
    assert ("p", alice.pubkey) in [tuple(t[:2]) for t in first.tags]
    edit(client, homeserver, q.profile_room_id, mine["event_id"], "thanks")
    new = relay_pool.of_kind(1)[-1]
    assert new.content == "thanks" and [t for t in new.tags if t[0] == "e"] == [t for t in first.tags if t[0] == "e"]


def test_edits_that_change_nothing_or_arent_the_authors_are_left(client, homeserver, relay_pool) -> None:
    q, message, note = local_post(client, homeserver, relay_pool, "same")
    edit(client, homeserver, q.profile_room_id, message["event_id"], "same")
    homeserver.profiles["@other:example.org"] = {}
    edit(client, homeserver, q.profile_room_id, message["event_id"], "hijacked", sender="@other:example.org")
    assert relay_pool.of_kind(5) == [] and [e.content for e in relay_pool.of_kind(1)] == ["same"]
