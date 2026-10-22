"""[import <post>: one Nostr post into Matrix, as ;import <url> does."""

from __future__ import annotations

import json

from bridge.nostr import nip19
from tests.test_federation import Stranger, command, create_id, run


def last(homeserver):
    return homeserver.sent_to("!dm:example.org")[-1]


def test_import_a_post_by_link(client, homeserver, relay_pool) -> None:
    create_id(client)
    alice = Stranger()
    post = alice.event(1, "worth a look")
    relay_pool.network += [alice.event(0, json.dumps({"name": "Alice"}), created_at=1000), post]
    link = "https://njump.me/" + nip19.nevent_encode(nip19.EventPointer(post["id"], (), alice.pubkey))
    command(client, f"[import {link}")
    row = run(client, client.app.state.repository.get_event, post["id"])
    assert row is not None and row.room_id and row.matrix_event_id
    assert homeserver.messages(row.room_id)[-1]["content"]["body"] == "worth a look"
    assert (row.room_id, "@q:example.org") in homeserver.invites
    assert last(homeserver)["body"] == f"Imported: https://matrix.to/#/{row.room_id}/{row.matrix_event_id}"
    # Not followed, and not brought in twice.
    assert run(client, client.app.state.repository.list_followers_of_remote, alice.pubkey) == []
    count = len(homeserver.messages(row.room_id))
    command(client, f"[import nostr:{nip19.note_encode(post['id'])}")
    assert last(homeserver)["body"].startswith("Already here: ")
    assert len(homeserver.messages(row.room_id)) == count


def test_import_a_reply_threads_its_conversation(client, homeserver, relay_pool) -> None:
    create_id(client)
    alice, bob = Stranger(), Stranger()
    root = alice.event(1, "the root")
    reply = bob.event(1, "a reply", [["e", root["id"], "", "root"], ["p", alice.pubkey]])
    relay_pool.network += [root, reply]
    command(client, f"[import {nip19.nevent_encode(nip19.EventPointer(reply['id']))}")
    repo = client.app.state.repository
    root_row = run(client, repo.get_event, root["id"])
    reply_row = run(client, repo.get_event, reply["id"])
    assert root_row.room_id == reply_row.room_id  # in the root's author's room, threaded
    mirrored = next(m for m in homeserver.messages(reply_row.room_id) if m["event_id"] == reply_row.matrix_event_id)
    relation = mirrored["content"]["m.relates_to"]
    assert relation["rel_type"] == "m.thread" and relation["event_id"] == root_row.matrix_event_id


def test_import_an_article_by_naddr(client, homeserver, relay_pool) -> None:
    create_id(client)
    alice = Stranger()
    old = alice.event(30023, "old text", [["d", "essay"], ["title", "Essay"]], created_at=1000)
    new = alice.event(30023, "new text", [["d", "essay"], ["title", "Essay v2"], ["summary", "Better"]], created_at=2000)
    relay_pool.network += [old, new]
    naddr = nip19.naddr_encode(nip19.Address("essay", alice.pubkey, 30023))
    command(client, f"[import https://ditto.pub/{naddr}")
    row = run(client, client.app.state.repository.get_event, new["id"])
    assert row is not None
    assert "Essay v2" in homeserver.messages(row.room_id)[-1]["content"]["body"]


def test_import_refusals(client, homeserver, relay_pool) -> None:
    create_id(client)
    command(client, "[import")
    assert last(homeserver)["body"].startswith('Usage: "[import')
    command(client, "[import https://example.com/not-a-post")
    assert "Give a link to a Nostr post" in last(homeserver)["body"]
    command(client, f"[import {nip19.note_encode('ab' * 32)}")
    assert "couldn't find that post" in last(homeserver)["body"]
    reaction = Stranger().event(7, "+")
    relay_pool.network.append(reaction)
    command(client, f"[import {nip19.note_encode(reaction['id'])}")
    assert "isn't a post" in last(homeserver)["body"]
    command(client, "[import key")
    assert "Keys never go through chat" in last(homeserver)["body"]


def test_import_an_addressable_short_video(client, homeserver, relay_pool) -> None:
    """NIP-71 kind 34236, as in a ditto.pub naddr link."""
    create_id(client)
    alice = Stranger()
    video = alice.event(34236, "dance", [["d", "clip1"], ["title", "A clip"],
                                         ["imeta", "url https://media.example/clip.mp4", "m video/mp4"]])
    relay_pool.network.append(video)
    command(client, f"[import https://ditto.pub/{nip19.naddr_encode(nip19.Address('clip1', alice.pubkey, 34236))}")
    row = run(client, client.app.state.repository.get_event, video["id"])
    assert row is not None and row.d_tag == "clip1"
    assert last(homeserver)["body"].startswith("Imported: ")


def test_import_finds_posts_on_their_authors_relays(client, homeserver, relay_pool) -> None:
    """A link with no relay hints, to a post only on its author's relays."""
    create_id(client)
    alice = Stranger()
    relay_pool.network.append(alice.event(10002, "", [["r", "wss://alice.only"]], created_at=1000))
    video = alice.event(34236, "clip", [["d", "c"], ["imeta", "url https://media.example/c.mp4", "m video/mp4"]])
    asked: list[list[str]] = []
    original = relay_pool.query

    async def query(urls, filters, **kwargs):
        asked.append(list(urls))
        found = await original(urls, filters, **kwargs)
        return found + ([video] if "wss://alice.only" in urls else [])

    relay_pool.query = query
    command(client, f"[import {nip19.naddr_encode(nip19.Address('c', alice.pubkey, 34236))}")
    assert run(client, client.app.state.repository.get_event, video["id"]) is not None
    assert any("wss://alice.only" in urls for urls in asked)
