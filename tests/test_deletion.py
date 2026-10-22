"""[delete profile, and NIP-62 requests to vanish."""

from __future__ import annotations

import json
import time

from bridge.inbound import ingest
from bridge.nostr import event as nostr_event
from tests.test_federation import Stranger, _followed_post, relay_send, run
from tests.test_ids import ids_of, last_body, message, send


def asked(homeserver, room="!dm:example.org"):
    ask = homeserver.sent_to(room)[-1]
    ask_id = next(e for e in homeserver.rooms[room]["events"].values() if e["content"] is ask)["event_id"]
    return ask, ask_id


def test_delete_profile(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    send(client, message("[create profile qbot"))
    q, qbot = ids_of(client)
    room = q.profile_room_id
    send(client, message("old post", room=room))
    old_post = relay_pool.of_kind(1)[-1]
    secret = run(client, client.app.state.signer.secret_key, q.pubkey)

    # With two identities, it never guesses.
    send(client, message("[delete profile"))
    assert last_body(homeserver).startswith("Which identity?")
    send(client, message("[delete profile q"))
    ask, ask_id = asked(homeserver)
    assert ask["body"].startswith("⚠️ Delete q@example.org for good? This can't be undone.")
    assert "Its key is destroyed" in ask["body"] and "within 10 minutes" in ask["body"]
    assert ids_of(client)[0].id == q.id  # nothing yet

    # Only its owner can confirm.
    send(client, message("confirm", sender="@admin:example.org", reply_to=ask_id))
    assert len(ids_of(client)) == 2
    send(client, message("confirm", reply_to=ask_id))
    assert [i.name for i in ids_of(client)] == ["qbot"]
    assert "Deleted q@example.org" in last_body(homeserver) and "Active identity is now qbot" in last_body(homeserver)

    [vanish] = relay_pool.of_kind(62)
    assert vanish.pubkey == q.pubkey and [list(t) for t in vanish.tags] == [["relay", "ALL_RELAYS"]]
    profile = relay_pool.of_kind(0)[-1]
    assert profile.pubkey == q.pubkey and json.loads(profile.content)["deleted"] is True
    assert profile.created_at > vanish.created_at
    assert [list(t) for t in relay_pool.of_kind(3)[-1].tags] == [] and relay_pool.of_kind(3)[-1].pubkey == q.pubkey
    sent_to = next(relays for e, relays in relay_pool.published if e.id == vanish.id)
    assert "wss://relay.one.example" in sent_to and len(sent_to) > 2

    # The relay keeps only the tombstones, and won't take the old post back.
    repo = client.app.state.repository
    assert run(client, repo.get_event, old_post.id) is None
    assert {run(client, repo.get_event, e.id) is not None for e in (vanish, profile)} == {True}
    assert not run(client, ingest, client.app, old_post.to_dict(), origin="relay").ok

    # The handle stops answering, and nobody else can take it.
    assert client.get("/.well-known/nostr.json?name=q").json().get("names", {}).get("q") is None
    send(client, message("[create profile q", sender="@admin:example.org"))
    assert ids_of(client, "@admin:example.org") == []

    # The room stays, with its history; nothing posted there is published now.
    assert "q@example.org was deleted" in homeserver.sent_to(room)[-1]["body"]
    before = len(relay_pool.published)
    send(client, message("still here", room=room))
    assert len(relay_pool.published) == before
    # Its key is gone from the bridge.
    assert run(client, client.app.state.signer.secret_key, q.pubkey) is None
    assert secret  # (the key existed before)


def test_delete_confirmation_expires(client, homeserver, relay_pool, monkeypatch) -> None:
    send(client, message("[create profile"))
    send(client, message("[delete profile"))
    ask, ask_id = asked(homeserver)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 601)
    send(client, message("confirm", reply_to=ask_id))
    assert "expired; nothing was deleted" in last_body(homeserver)
    assert len(ids_of(client)) == 1 and relay_pool.of_kind(62) == []


def test_delete_needs_profile_word(client, homeserver) -> None:
    send(client, message("[create profile"))
    send(client, message("[delete q"))
    assert last_body(homeserver) == 'Did you mean "[delete profile [<number|id>]"?'
    send(client, message("[help danger"))
    assert "[delete profile [<number|id>]" in last_body(homeserver)


def test_remote_request_to_vanish(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    elsewhere = alice.event(62, "", [["relay", "wss://some.other.relay"]])
    assert relay_send(client, elsewhere)[2] is False
    assert run(client, client.app.state.repository.get_event, raw["id"]) is not None

    request = alice.event(62, "bye", [["relay", "wss://nostr.example.org/"]])
    assert relay_send(client, request)[2] is True
    assert run(client, client.app.state.repository.get_event, raw["id"]) is None
    assert any(event_id == mirrored["event_id"] for _, event_id, _ in homeserver.redactions)
    # What it asked to delete can't come back; what it posts later can.
    client.app.state.seen = type(client.app.state.seen)()
    assert not run(client, ingest, client.app, raw, origin="subscription").ok
    later = alice.event(1, "new start", created_at=request["created_at"] + 5)
    assert run(client, ingest, client.app, later, origin="subscription").ok


def test_vanish_names_check(client) -> None:
    from bridge.vanish import names_this_relay

    stranger = Stranger()
    for tags, expected in (([["relay", "ALL_RELAYS"]], True), ([["relay", "wss://NOSTR.example.org"]], True),
                           ([["relay", "wss://x.example"]], False), ([], False), ([["relay", "ws://[bad"]], False)):
        raw = nostr_event.build(stranger.secret, kind=62, content="", tags=tags, created_at=1).to_dict()
        assert names_this_relay(client.app, raw) is expected
