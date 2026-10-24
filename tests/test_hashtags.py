"""Hashtags as NIP-24 "t" tags (note v3, comment v2, share v3)."""

from __future__ import annotations

import pytest

from bridge.event_sources import rebuild
from bridge.notes import NOTE_VERSION, comment_v1, comment_v2, hashtag_tags, note_v1, note_v2, note_v3
from tests.test_federation import _followed_post, post, run
from tests.test_ids import ids_of, message, send


@pytest.mark.parametrize(
    "text, tags",
    [
        ("Election integrity #election #voting #Democracy", ["election", "voting", "democracy"]),
        ("#first word", ["first"]),
        ("line one\n#second", ["second"]),
        ("(#inparens) and [#brackets]", ["inparens", "brackets"]),
        ("a url https://example.com/page#section and x#y", []),
        ("#2024 is a number but #2024election isn't", ["2024election"]),
        ("#nostr #Nostr #NOSTR", ["nostr"]),
        ("#日本語 #café", ["日本語", "café"]),
        ("trailing #dash- and #snake_", ["dash", "snake"]),
        ("not #", []),
    ],
)
def test_hashtag_rules(text, tags) -> None:
    assert hashtag_tags(text) == [["t", tag] for tag in tags]


def test_at_most_twenty() -> None:
    assert len(hashtag_tags(" ".join(f"#tag{n}" for n in range(30)))) == 20


def test_old_versions_are_unchanged() -> None:
    """v1/v2 stay frozen; v3 only adds t tags after everything else."""
    event = {"content": {"msgtype": "m.text", "body": "hello #world"}}
    recipe = {"mentions": [], "p": [], "reply": None, "relay_hint": "", "media": None}
    assert note_v1(event, recipe) == ("hello #world", [])
    assert note_v2(event, recipe) == ("hello #world", [])
    assert note_v3(event, recipe) == ("hello #world", [["t", "world"]])
    comment = {**recipe, "comment": {"root": "a" * 64, "root_pubkey": "b" * 64, "root_kind": 1,
                                     "parent": "a" * 64, "parent_pubkey": "b" * 64, "parent_kind": 1}}
    v1_text, v1_tags = comment_v1(event, comment)
    assert comment_v2(event, comment) == (v1_text, [*v1_tags, ["t", "world"]])


def test_posts_carry_hashtags_and_rebuild(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "Free software matters #FOSS #freedom"})
    note = relay_pool.of_kind(1)[-1]
    assert [list(t) for t in note.tags if t[0] == "t"] == [["t", "foss"], ["t", "freedom"]]
    row = run(client, client.app.state.repository.get_event, note.id)
    assert f'"v":{NOTE_VERSION}' in row.source
    assert run(client, rebuild, client.app, row).event == note.to_dict()


def test_comment_replies_carry_hashtags(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool, kind=1111)
    post(client, homeserver, remote.room_id, {"msgtype": "m.text", "body": "agreed #nostr",
                                              "m.relates_to": {"m.in_reply_to": {"event_id": mirrored["event_id"]}}})
    reply = [e for e in relay_pool.of_kind(1111) if e.pubkey == q.pubkey][-1]
    assert ["t", "nostr"] in [list(t) for t in reply.tags]
    row = run(client, client.app.state.repository.get_event, reply.id)
    assert run(client, rebuild, client.app, row).event == reply.to_dict()
