"""Reposts made in Matrix (MSC4501) of posts from other networks, on Nostr."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bridge.config import parse_config
from bridge.event_sources import rebuild
from tests.conftest import make_client, raw_config
from tests.test_federation import _followed_post
from tests.test_ids import ids_of, message, send

FEDI_BOT = "@fediverse:example.org"
FEDI_ROOM = "!joebravo:example.org"
FEDI_GHOST = "@fedi_joebravo77_poa.st:example.org"
POST_URL = "https://poa.st/objects/e5307b57"
RELATION = "org.matrix.msc4501.social.relates_to"
REPOST = "org.matrix.msc4501.social.repost"


@pytest.fixture
def shared(repository, homeserver, relay_pool):
    config = parse_config(raw_config(appservice={
        "other_command_prefixes": [";"], "other_bot_users": [FEDI_BOT],
        "other_bridge_users": [r"@fedi_.*:example\.org"],
    }))
    with make_client(config, repository, homeserver, relay_pool) as client:
        yield client


def fedi_post(homeserver, *, image=True, text="look at this"):
    if image:
        buffer = io.BytesIO()
        Image.new("RGB", (76, 96), (10, 120, 200)).save(buffer, format="PNG")
        mxc = "mxc://example.org/fediimage"
        homeserver.media[mxc] = (buffer.getvalue(), "image/png")
        content = {"msgtype": "m.image", "body": "d2bc.png", "filename": "d2bc.png", "url": mxc,
                   "info": {"mimetype": "image/png", "w": 76, "h": 96}, "external_url": POST_URL}
    else:
        content = {"msgtype": "m.text", "body": text, "external_url": POST_URL}
    return homeserver._store(FEDI_ROOM, "m.room.message", content, FEDI_GHOST)


def relation_to(original, room=FEDI_ROOM, name="JoeBravo77"):
    return {"rel_type": REPOST, "event_id": original["event_id"], "room_id": room, "displayname": name,
            "sender": original["sender"], "content_inline": True}


def bot_card(homeserver, profile_room, original, *, shown_as="@q:example.org"):
    permalink = f"https://matrix.to/#/{FEDI_ROOM}/{original['event_id']}?via=example.org"
    content = {"msgtype": "m.image", "body": permalink, "filename": "d2bc.png", "url": original["content"].get("url"),
               RELATION: relation_to(original),
               "com.beeper.per_message_profile": {"id": shown_as, "displayname": "q"}}
    return homeserver._store(profile_room, "m.room.message", content, FEDI_BOT)


def test_fediverse_bridge_repost_card(shared, homeserver, relay_pool) -> None:
    send(shared, message("[create profile"))
    [q] = ids_of(shared)
    original = fedi_post(homeserver)
    card = bot_card(homeserver, q.profile_room_id, original)
    send(shared, card)
    [note] = relay_pool.of_kind(1)
    assert note.pubkey == q.pubkey
    lines = note.content.split("\n\n")
    assert lines[0] == f"🔁 reposted JoeBravo77's post: {POST_URL}"
    assert lines[1].startswith("https://nostr.example.org/") and lines[1].endswith(".png")
    assert len(lines) == 2
    tags = [list(t) for t in note.tags]
    assert ["r", POST_URL] in tags and any(t[0] == "imeta" for t in tags)
    row = shared.portal.call(shared.app.state.repository.get_event, note.id)
    assert shared.portal.call(rebuild, shared.app, row).event == note.to_dict()

    # Undoing the repost on the fediverse side (the bridge redacts its card)
    # deletes it on Nostr.
    redaction = homeserver._store(q.profile_room_id, "m.room.redaction", {"redacts": card["event_id"]}, FEDI_BOT)
    redaction["redacts"] = card["event_id"]
    send(shared, redaction)
    [deletion] = relay_pool.of_kind(5)
    assert ["e", note.id] in [list(t) for t in deletion.tags]


def test_cards_for_someone_else_and_bot_redactions_of_posts_do_nothing(shared, homeserver, relay_pool) -> None:
    send(shared, message("[create profile"))
    [q] = ids_of(shared)
    send(shared, bot_card(homeserver, q.profile_room_id, fedi_post(homeserver), shown_as="@other:example.org"))
    assert relay_pool.of_kind(1) == []
    post = homeserver._store(q.profile_room_id, "m.room.message", {"msgtype": "m.text", "body": "mine"}, "@q:example.org")
    send(shared, post)
    redaction = homeserver._store(q.profile_room_id, "m.room.redaction", {"redacts": post["event_id"]}, FEDI_BOT)
    redaction["redacts"] = post["event_id"]
    send(shared, redaction)
    assert relay_pool.of_kind(5) == []


@pytest.mark.parametrize("caption", ["", "this is so true"])
def test_haven_repost_record_of_a_fediverse_post(shared, homeserver, relay_pool, caption) -> None:
    send(shared, message("[create profile"))
    [q] = ids_of(shared)
    original = fedi_post(homeserver, image=False, text="the original words")
    permalink = f"https://matrix.to/#/{FEDI_ROOM}/{original['event_id']}"
    body = f"{caption}\n\n{permalink}" if caption else permalink
    send(shared, homeserver._store(q.profile_room_id, "m.room.message",
                                   {"msgtype": "m.text", "body": body,
                                    RELATION: {**relation_to(original), "content_inline": not caption}}, "@q:example.org"))
    [note] = relay_pool.of_kind(1)
    expected = [f"🔁 reposted JoeBravo77's post: {POST_URL}", "the original words"]
    assert note.content.split("\n\n") == ([caption] if caption else []) + expected
    assert "matrix.to" not in note.content


@pytest.mark.parametrize("caption", ["", "quoting this"])
def test_haven_repost_record_of_a_nostr_post(client, homeserver, relay_pool, caption) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    permalink = f"https://matrix.to/#/{remote.room_id}/{mirrored['event_id']}"
    relation = {"rel_type": REPOST, "event_id": mirrored["event_id"], "room_id": remote.room_id}
    before = len(relay_pool.of_kind(1))
    body = f"{caption} {permalink}" if caption else permalink
    send(client, homeserver._store(q.profile_room_id, "m.room.message",
                                   {"msgtype": "m.text", "body": body, RELATION: relation}, "@q:example.org"))
    mine = [e for e in relay_pool.of_kind(1) if e.pubkey == q.pubkey]
    if not caption:
        # The 🔁 reaction that comes with it does the reposting.
        assert len(relay_pool.of_kind(1)) == before
        return
    [quote] = mine
    assert quote.content.startswith("quoting this nostr:nevent1")
    assert ["q", raw["id"], "wss://nostr.example.org", alice.pubkey] in [list(t) for t in quote.tags]


def test_bluesky_quote_card_uses_only_the_commentary(shared, homeserver, relay_pool) -> None:
    """A repost with commentary made through the Bluesky bridge: its card's
    body also has "🔁 reposted X's post:", a quote and the link."""
    send(shared, message("[create profile"))
    [q] = ids_of(shared)
    original = homeserver._store("!bsky:example.org", "m.room.message",
                                 {"msgtype": "m.text", "body": "skeet text", "external_url": "https://bsky.app/profile/a/post/1"},
                                 "@bsky_alice:example.org")
    link = f"https://matrix.to/#/!bsky:example.org/{original['event_id']}"
    card = {
        "msgtype": "m.text",
        "body": f"so good\n\n🔁 reposted Alice's post:\n> skeet text\n{link}",
        RELATION: {**relation_to(original, room="!bsky:example.org", name="Alice"), "content_inline": False},
        "org.matrix.msc4501.social.body": "so good",
        "com.beeper.per_message_profile": {"id": "@q:example.org"},
    }
    send(shared, homeserver._store(q.profile_room_id, "m.room.message", card, FEDI_BOT))
    [note] = relay_pool.of_kind(1)
    assert note.content.split("\n\n") == [
        "so good", "🔁 reposted Alice's post: https://bsky.app/profile/a/post/1", "skeet text"]


def test_share_v1_still_rebuilds() -> None:
    """Reposts published before v2 keep rebuilding to the same event."""
    from bridge.notes import share_v1

    record = {"content": {"msgtype": "m.text", "body": "https://matrix.to/#/!r/$e",
                          RELATION: {"rel_type": REPOST, "content_inline": True}}}
    original = {"content": {"msgtype": "m.text", "body": "words", "external_url": POST_URL}}
    text, tags = share_v1(record, original, {"author": "JoeBravo77", "media": None})
    assert text == f"🔁 JoeBravo77:\n\nwords\n\n{POST_URL}" and tags == [["r", POST_URL]]
