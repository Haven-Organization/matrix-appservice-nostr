"""Step 3: posting from Profile Rooms, deleting, and publishing to relays."""

from __future__ import annotations

import io
import itertools
import json
from pathlib import Path

import pytest
from PIL import Image

from bridge import publishing
from bridge.event_sources import rebuild
from bridge.nostr import nip19
from bridge.nostr import event as nostr_event
from bridge.nostr.event import compute_id
from bridge.notes import deletion_v1, note_v1
from tests.conftest import HS_TOKEN, settle

AUTH = {"Authorization": f"Bearer {HS_TOKEN}"}
GOLDEN = json.loads((Path(__file__).parent / "vectors" / "note-v1-golden.json").read_text())
_txn = itertools.count(1)

RELAY_ONE = "wss://relay.one.example"
RELAY_TWO = "wss://relay.two.example"


# -- golden vectors -------------------------------------------------------------
# These pin the v1 converters byte for byte. Never edit a case once v1 has
# shipped: events made under v1 must rebuild to the same id forever. A
# different conversion is a new version with new cases.


@pytest.mark.parametrize("case", GOLDEN["notes"], ids=lambda c: c["name"])
def test_note_v1_golden(case) -> None:
    content, tags = note_v1(case["matrix_event"], case["recipe"])
    expected = case["expected"]
    assert (content, tags) == (expected["content"], expected["tags"])
    assert compute_id(GOLDEN["pubkey"], expected["created_at"], 1, tags, content) == expected["id"]


@pytest.mark.parametrize("case", GOLDEN["deletions"], ids=lambda c: c["name"])
def test_deletion_v1_golden(case) -> None:
    content, tags = deletion_v1(case["matrix_event"], case["recipe"])
    expected = case["expected"]
    assert (content, tags) == (expected["content"], expected["tags"])
    assert compute_id(GOLDEN["pubkey"], expected["created_at"], 5, tags, content) == expected["id"]


# -- helpers ------------------------------------------------------------------


def transact(client, *events: dict) -> None:
    response = client.put(f"/_matrix/app/v1/transactions/p{next(_txn)}", json={"events": list(events)}, headers=AUTH)
    assert response.status_code == 200
    settle(client)


def post(client, homeserver, room: str, content: dict, sender: str = "@q:example.org") -> dict:
    """A message as the homeserver would store it and push it."""
    event = homeserver._store(room, "m.room.message", content, sender)
    transact(client, event)
    return event


def redact(client, homeserver, room: str, target: dict, sender: str = "@q:example.org", reason: str | None = None) -> dict:
    content = {"redacts": target["event_id"], **({"reason": reason} if reason else {})}
    redaction = homeserver._store(room, "m.room.redaction", content, sender)
    redaction["redacts"] = target["event_id"]
    target["content"] = {}
    target.setdefault("unsigned", {})["redacted_because"] = redaction
    transact(client, redaction)
    return redaction


def run(client, fn, *args):
    async def call():
        return await fn(*args)

    return client.portal.call(call)


def create_id(client, user: str = "@q:example.org", name: str | None = None):
    command = "[create profile" + (f" {name}" if name else "")
    transact(client, {"type": "m.room.message", "event_id": f"$c{next(_txn)}", "room_id": "!dm:example.org",
                      "sender": user, "content": {"msgtype": "m.text", "body": command}})
    owned = run(client, client.app.state.repository.list_ids_for_user, user)
    return owned[-1]


def index_row(client, matrix_event_id: str):
    return run(client, client.app.state.repository.get_event_by_matrix_event, matrix_event_id)


def rebuilt(client, row):
    return run(client, rebuild, client.app, row)


def notes(relay_pool):
    return relay_pool.of_kind(1)


# -- posting ------------------------------------------------------------------


def test_text_post(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    message = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "gm nostr\nfrom matrix"})
    [note] = notes(relay_pool)
    assert note.content == "gm nostr\nfrom matrix" and note.pubkey == q.pubkey
    assert note.created_at == message["origin_server_ts"] // 1000
    assert nostr_event.verify(note.to_dict()) == note
    assert relay_pool.published[-1][1] == [RELAY_ONE, RELAY_TWO]

    row = index_row(client, message["event_id"])
    assert row.id == note.id and row.room_id == q.profile_room_id
    assert "gm nostr" not in row.source  # no content in the index
    assert rebuilt(client, row).event == note.to_dict()


def test_what_is_not_published(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    room = q.profile_room_id
    original = post(client, homeserver, room, {"msgtype": "m.text", "body": "first"})
    # An edit of something that was never published (here, a notice) isn't posted.
    notice_event = post(client, homeserver, room, {"msgtype": "m.notice", "body": "n"})
    post(client, homeserver, room, {"msgtype": "m.text", "body": "* edited", "m.new_content": {"msgtype": "m.text", "body": "edited"},
                                    "m.relates_to": {"rel_type": "m.replace", "event_id": notice_event["event_id"]}})
    post(client, homeserver, room, {"msgtype": "m.notice", "body": "a notice"})
    post(client, homeserver, room, {"msgtype": "m.text", "body": "[status"})
    post(client, homeserver, room, {"msgtype": "m.text", "body": "   "})
    post(client, homeserver, room, {"msgtype": "m.text", "body": "a visitor chatting"}, sender="@admin:example.org")
    post(client, homeserver, room, {"msgtype": "m.text", "body": "a remote visitor"}, sender="@x:other.org")
    assert [n.content for n in notes(relay_pool)] == ["first"]


def test_mentions_become_nostr_references(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    admin = create_id(client, "@admin:example.org", "boss")
    post(client, homeserver, q.profile_room_id, {
        "msgtype": "m.text", "body": "Admin: and @nobody",
        "format": "org.matrix.custom.html",
        "formatted_body": '<a href="https://matrix.to/#/@admin:example.org">Admin</a>: and '
                          '<a href="https://matrix.to/#/@nobody:example.org">@nobody</a>',
    })
    [note] = notes(relay_pool)
    assert note.content == f"nostr:{nip19.npub_encode(admin.pubkey)}: and @nobody"
    assert [list(t) for t in note.tags] == [["p", admin.pubkey]]


def test_replies_thread_on_nostr(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    room = q.profile_room_id
    root = post(client, homeserver, room, {"msgtype": "m.text", "body": "a thread"})
    second = post(client, homeserver, room, {"msgtype": "m.text", "body": "two",
                                             "m.relates_to": {"rel_type": "m.thread", "event_id": root["event_id"],
                                                              "m.in_reply_to": {"event_id": root["event_id"]}, "is_falling_back": True}})
    third = post(client, homeserver, room, {"msgtype": "m.text", "body": "> <@q:example.org> two\n\nthree",
                                            "m.relates_to": {"m.in_reply_to": {"event_id": second["event_id"]}}})
    root_note, second_note, third_note = notes(relay_pool)
    assert [list(t) for t in second_note.tags] == [["e", root_note.id, "wss://nostr.example.org", "root", q.pubkey]]
    assert third_note.content == "three"
    assert [list(t) for t in third_note.tags] == [
        ["e", root_note.id, "wss://nostr.example.org", "root", q.pubkey],
        ["e", second_note.id, "wss://nostr.example.org", "reply", q.pubkey],
    ]
    # Indexed for #e queries later.
    assert ("e", root_note.id) in index_row(client, third["event_id"]).tags


def test_visitor_reply_is_their_own(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    visitor = create_id(client, "@admin:example.org", "boss")
    root = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "hello"})
    post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "hi back",
                                                 "m.relates_to": {"m.in_reply_to": {"event_id": root["event_id"]}}},
         sender="@admin:example.org")
    root_note, reply = notes(relay_pool)
    assert reply.pubkey == visitor.pubkey
    assert ["p", q.pubkey] in [list(t) for t in reply.tags]


def _photo_with_gps() -> bytes:
    exif = Image.Exif()
    exif[0x8825] = {2: (1.0, 2.0, 3.0)}
    buffer = io.BytesIO()
    Image.new("RGB", (120, 90), (0, 128, 0)).save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def test_image_post(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    homeserver.media["mxc://example.org/photo"] = (_photo_with_gps(), "image/jpeg")
    message = post(client, homeserver, q.profile_room_id, {
        "msgtype": "m.image", "body": "my garden", "filename": "IMG_1.jpg", "url": "mxc://example.org/photo",
        "info": {"mimetype": "image/jpeg", "w": 120, "h": 90},
    })
    [note] = notes(relay_pool)
    caption, url = note.content.split("\n")
    assert caption == "my garden" and url.startswith("https://nostr.example.org/") and url.endswith(".jpg")
    imeta = next(list(t) for t in note.tags if t[0] == "imeta")
    assert imeta[1] == f"url {url}" and "m image/jpeg" in imeta and "dim 120x90" in imeta
    served = client.get("/" + url.rsplit("/", 1)[1])
    assert served.status_code == 200 and not Image.open(io.BytesIO(served.content)).getexif()
    assert f"x {imeta[3].split()[1]}" == imeta[3]
    assert rebuilt(client, index_row(client, message["event_id"])).status == "ok"


def test_file_post_without_caption(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    homeserver.media["mxc://example.org/doc"] = (b"%PDF-1.4 tiny", "application/pdf")
    post(client, homeserver, q.profile_room_id, {"msgtype": "m.file", "body": "notes.pdf", "url": "mxc://example.org/doc",
                                                 "info": {"mimetype": "application/pdf", "size": 13}})
    [note] = notes(relay_pool)
    assert note.content.endswith(".pdf") and "\n" not in note.content
    assert client.get("/" + note.content.rsplit("/", 1)[1]).content == b"%PDF-1.4 tiny"


def test_too_large_and_encrypted_files(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    room = q.profile_room_id
    post(client, homeserver, room, {"msgtype": "m.video", "body": "huge.mp4", "url": "mxc://example.org/huge",
                                    "info": {"size": 500 * 1024 * 1024}})
    assert "more than the 50.0 MB" in homeserver.sent_to(room)[-1]["body"]
    post(client, homeserver, room, {"msgtype": "m.image", "body": "secret.jpg", "file": {"url": "mxc://example.org/enc"}})
    assert "end-to-end encrypted" in homeserver.sent_to(room)[-1]["body"]
    assert notes(relay_pool) == []


# -- deleting -----------------------------------------------------------------


def test_redaction_deletes(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    message = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "oops"})
    [note] = notes(relay_pool)
    redaction = redact(client, homeserver, q.profile_room_id, message, reason="typo")
    [deletion] = relay_pool.of_kind(5)
    assert deletion.pubkey == q.pubkey and deletion.content == "typo"
    assert [list(t) for t in deletion.tags] == [["e", note.id], ["k", "1"]]
    assert index_row(client, message["event_id"]) is None  # gone from the index
    row = index_row(client, redaction["event_id"])
    assert rebuilt(client, row).event == deletion.to_dict()
    # Redacting a message that was never published does nothing.
    other = homeserver._store(q.profile_room_id, "m.room.message", {"msgtype": "m.notice", "body": "x"}, "@q:example.org")
    redact(client, homeserver, q.profile_room_id, other)
    assert len(relay_pool.of_kind(5)) == 1


def test_redacted_post_is_gone_on_rebuild(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    message = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "here"})
    row = index_row(client, message["event_id"])
    message["content"] = {}
    message["unsigned"] = {"redacted_because": {}}
    assert rebuilt(client, row).status == "gone"


# -- publishing trouble -------------------------------------------------------


def test_no_relay_takes_it(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    relay_pool.answers = {RELAY_ONE: (False, "error: timed out"), RELAY_TWO: (False, "rate-limited: slow down")}
    message = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "anyone?"})
    warning = homeserver.sent_to(q.profile_room_id)[-1]
    assert "Couldn't get this post onto any public Nostr relay" in warning["body"]
    assert "I'll keep retrying" in warning["body"]
    assert warning["m.relates_to"] == {"m.in_reply_to": {"event_id": message["event_id"]}}

    repository = client.app.state.repository
    [pending] = run(client, repository.list_due_publishes, "9999")
    assert set(pending.relays) == {RELAY_ONE, RELAY_TWO} and pending.attempts == 1

    # Next round the relays are back: the event is rebuilt from Matrix and sent.
    relay_pool.answers = {}
    before = len(relay_pool.published)
    pending_due = pending.__class__(pending.event_id, pending.relays, pending.attempts, "2000-01-01T00:00:00+00:00")
    run(client, repository.put_pending_publish, pending_due)
    assert run(client, publishing.retry_due, client.app) == 1
    resent, relays = relay_pool.published[before]
    assert resent.id == pending.event_id and set(relays) == {RELAY_ONE, RELAY_TWO}
    assert run(client, repository.list_due_publishes, "9999") == []


def test_policy_refusal_told_once_and_skipped(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    relay_pool.answers = {RELAY_TWO: (False, "blocked: not in our web of trust")}
    room = q.profile_room_id
    sent_before = len(homeserver.sent_to(room))
    post(client, homeserver, room, {"msgtype": "m.text", "body": "one"})
    told = homeserver.sent_to(room)[sent_before:]
    assert len(told) == 1 and "wss://relay.two.example: blocked: not in our web of trust" in told[0]["body"]
    assert "24 hours" in told[0]["body"]

    post(client, homeserver, room, {"msgtype": "m.text", "body": "two"})
    assert relay_pool.published[-1][1] == [RELAY_ONE]  # skipped
    assert len(homeserver.sent_to(room)) == sent_before + 1  # not told again
    assert run(client, client.app.state.repository.list_due_publishes, "9999") == []  # not retried either


def test_markdown_links_go_out_plain(client, homeserver, relay_pool) -> None:
    """Element's plain text is the Markdown it was typed as; Nostr notes are
    plain text, so links become "text (url)" (or the url alone)."""
    q = create_id(client)
    body = ("Enhancing the widget for [matrix-appservice-activitypub](https://github.com/Haven-Organization/"
            "matrix-appservice-activitypub), see [ditto.pub](https://ditto.pub). Not a link: [x](y)")
    html_body = ('<p>Enhancing the widget for <a href="https://github.com/Haven-Organization/matrix-appservice-activitypub">'
                 'matrix-appservice-activitypub</a>, see <a href="https://ditto.pub">ditto.pub</a>. Not a link: [x](y)</p>')
    post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html",
                                                 "formatted_body": html_body})
    note = notes(relay_pool)[-1]
    assert note.content == ("Enhancing the widget for matrix-appservice-activitypub (https://github.com/Haven-Organization/"
                            "matrix-appservice-activitypub), see https://ditto.pub. Not a link: [x](y)")
    row = client.portal.call(client.app.state.repository.get_event, note.id)
    assert rebuilt(client, row).event == note.to_dict()


def test_a_cross_posted_reply_says_what_it_answers(client, homeserver, relay_pool) -> None:
    """A reply another bridge cross-posts (MSC4501) to a post that isn't on
    Nostr gets the siblings' "⤵️ Reply to X's post" header, with the link."""
    q = create_id(client)
    relation = {"rel_type": "org.matrix.msc4501.social.reply", "event_id": "$fedi", "room_id": "!fedi:example.org",
                "displayname": "GaryBussy", "sender": "@fedi_garybussy:example.org",
                "content": {"body": "Doin it Trump style", "external_url": "https://shitpost.example/objects/2cac",
                            "msgtype": "m.text"}}
    post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "Tired of winning!", "format": "plain",
                                                 "org.matrix.msc4501.social.relates_to": relation})
    note = notes(relay_pool)[-1]
    assert note.content == "⤵️ Reply to GaryBussy's post: https://shitpost.example/objects/2cac\n\nTired of winning!"
    row = client.portal.call(client.app.state.repository.get_event, note.id)
    assert rebuilt(client, row).event == note.to_dict()
    # Without a name or link given inline, they come from the post itself.
    homeserver.profiles["@fedi_garybussy:example.org"] = {"displayname": "Gary"}
    original = homeserver._store("!fedi:example.org", "m.room.message", {"msgtype": "m.text", "body": "hi",
                                 "external_url": "https://shitpost.example/objects/x"}, "@fedi_garybussy:example.org")
    post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "Second",
                                                 "org.matrix.msc4501.social.relates_to": {
                                                     "rel_type": "org.matrix.msc4501.social.reply",
                                                     "event_id": original["event_id"], "room_id": "!fedi:example.org"}})
    assert notes(relay_pool)[-1].content == "⤵️ Reply to Gary's post: https://shitpost.example/objects/x\n\nSecond"


def test_a_cross_posted_reply_to_a_nostr_post_is_a_nostr_reply(client, homeserver, relay_pool) -> None:
    from tests.test_federation import _followed_post

    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "agreed", "org.matrix.msc4501.social.relates_to": {
        "rel_type": "org.matrix.msc4501.social.reply", "event_id": mirrored["event_id"], "room_id": remote.room_id,
        "displayname": "Alice"}})
    reply = notes(relay_pool)[-1]
    assert reply.content == "agreed" and ("e", raw["id"]) in [tuple(t[:2]) for t in reply.tags]
