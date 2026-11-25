"""Steps 4-7: the relay, incoming events, following, interactions, DMs."""

from __future__ import annotations

import itertools
import json
import time

import pytest

from bridge.app_handler import client_tag
from bridge.event_sources import EMBED_FIELD
from bridge.inbound import ingest
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19, nip59, secp256k1
from tests.conftest import HS_TOKEN, settle

AUTH = {"Authorization": f"Bearer {HS_TOKEN}"}
_txn = itertools.count(1)


def transact(client, *events):
    response = client.put(f"/_matrix/app/v1/transactions/f{next(_txn)}", json={"events": list(events)}, headers=AUTH)
    assert response.status_code == 200
    settle(client)


def run(client, fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)

    return client.portal.call(call)


def command(client, body, sender="@q:example.org", room="!dm:example.org"):
    transact(client, {"type": "m.room.message", "event_id": f"$c{next(_txn)}", "room_id": room, "sender": sender,
                      "content": {"msgtype": "m.text", "body": body}})


def create_id(client, user="@q:example.org", name=None):
    command(client, "[create profile" + (f" {name}" if name else ""), sender=user)
    return run(client, client.app.state.repository.list_ids_for_user, user)[-1]


def post(client, homeserver, room, content, sender="@q:example.org"):
    event = homeserver._store(room, "m.room.message", content, sender)
    transact(client, event)
    return event


def local_post(client, homeserver, relay_pool, text="hello nostr"):
    q = create_id(client)
    message = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": text})
    note = [e for e, _ in relay_pool.published if e.kind == 1][-1]
    return q, message, note


class Stranger:
    def __init__(self):
        self.secret = secp256k1.generate_secret_key()
        self.pubkey = secp256k1.public_key(self.secret).hex()

    def event(self, kind, content="", tags=(), created_at=None):
        return nostr_event.build(self.secret, kind=kind, content=content, tags=tags,
                                 created_at=created_at or int(time.time())).to_dict()


def relay_send(client, raw):
    with client.websocket_connect("/") as ws:
        assert ws.receive_json()[0] == "AUTH"
        ws.send_json(["EVENT", raw])
        reply = ws.receive_json()
    settle(client)
    return reply


def relay_req(client, *filters, auth_secret=None):
    with client.websocket_connect("/") as ws:
        _, challenge = ws.receive_json()
        if auth_secret is not None:
            auth = nostr_event.build(auth_secret, kind=22242, content="", created_at=int(time.time()),
                                     tags=[["relay", "wss://nostr.example.org"], ["challenge", challenge]]).to_dict()
            ws.send_json(["AUTH", auth])
            assert ws.receive_json()[2] is True
        ws.send_json(["REQ", "s", *filters])
        events = []
        while True:
            message = ws.receive_json()
            if message[0] == "EVENT":
                events.append(message[2])
            else:
                return events, message


def notifications(client, homeserver, user="@q:example.org"):
    room = run(client, client.app.state.repository.get_user_rooms, user).notifications_room_id
    return [c["body"] for c in homeserver.sent_to(room)]


# -- the relay ------------------------------------------------------------------


def test_nip11(client) -> None:
    info = client.get("/", headers={"Accept": "application/nostr+json"}).json()
    assert info["limitation"]["restricted_writes"] is True and 42 in info["supported_nips"]
    assert info["pubkey"] == client.app.state.bot_identity.pubkey


def test_relay_serves_local_posts(client, homeserver, relay_pool) -> None:
    q, _, note = local_post(client, homeserver, relay_pool)
    events, end = relay_req(client, {"authors": [q.pubkey], "kinds": [1]})
    assert end == ["EOSE", "s"] and events == [note.to_dict()]
    events, _ = relay_req(client, {"ids": [note.id]})
    assert events == [note.to_dict()]
    profiles, _ = relay_req(client, {"authors": [q.pubkey], "kinds": [0]})
    assert len(profiles) == 1 and json.loads(profiles[0]["content"])["nip05"] == "q@example.org"
    with client.websocket_connect("/") as ws:
        ws.receive_json()
        ws.send_json(["COUNT", "c", {"authors": [q.pubkey]}])
        assert ws.receive_json() == ["COUNT", "c", {"count": 5}]  # kinds 0, 1, 10002, 10050, 10063


def test_hashtag_search_refused(client, homeserver, relay_pool) -> None:
    q, _, _ = local_post(client, homeserver, relay_pool)
    events, end = relay_req(client, {"#t": ["nostr"]})
    assert end[0] == "CLOSED" and end[2].startswith("error:")
    events, end = relay_req(client, {"#t": ["nostr"], "authors": [q.pubkey]})
    assert end[0] == "EOSE"


def test_unrelated_and_invalid_events_refused(client) -> None:
    stranger = Stranger()
    reply = relay_send(client, stranger.event(1, "just posting"))
    assert reply[2] is False and reply[3].startswith("restricted:")
    bad = stranger.event(1, "x")
    bad["content"] = "tampered"
    assert relay_send(client, bad)[3].startswith("invalid:")


# -- incoming interactions ----------------------------------------------------------


@pytest.mark.parametrize("style", ["nip10", "nip22"])
def test_reply_threads_under_local_post(client, homeserver, relay_pool, style) -> None:
    q, message, note = local_post(client, homeserver, relay_pool)
    stranger = Stranger()
    if style == "nip10":
        raw = stranger.event(1, "Sup dude", [["e", note.id, "", "root", q.pubkey], ["p", q.pubkey]])
    else:  # Ditto's comment on a note
        raw = stranger.event(1111, "Sup dude", [["E", note.id, "", q.pubkey], ["K", "1"], ["P", q.pubkey],
                                                ["e", note.id, "", q.pubkey], ["k", "1"], ["p", q.pubkey]])
    assert relay_send(client, raw)[2] is True
    replies = [e for e in homeserver.messages(q.profile_room_id) if e["content"].get(EMBED_FIELD)]
    [reply] = replies
    assert reply["sender"] == f"@nostr_{stranger.pubkey}:example.org"
    assert reply["content"]["body"] == "Sup dude"
    assert reply["content"]["m.relates_to"]["rel_type"] == "m.thread"
    assert reply["content"]["m.relates_to"]["event_id"] == message["event_id"]
    # The siblings' notification format: type emoji, pill, phrase with
    # "your post" linked, then a quote of what they said.
    room = run(client, client.app.state.repository.get_user_rooms, "@q:example.org").notifications_room_id
    [notice] = [c for c in homeserver.sent_to(room) if "replied to your post" in c["body"]]
    assert notice["body"].startswith("💬 ") and "replied to your post\n> Sup dude\nhttps://matrix.to/#/" in notice["body"]
    assert notice["formatted_body"].startswith(f'<p>💬 <a href="https://matrix.to/#/@nostr_{stranger.pubkey}:example.org" title="')
    assert f'<a href="https://matrix.to/#/{q.profile_room_id}/{message["event_id"]}?via=example.org">your post</a>' in notice["formatted_body"]
    assert notice["formatted_body"].endswith("<blockquote>Sup dude</blockquote>")
    assert "View" not in notice["formatted_body"]
    # Served back from the relay, rebuilt from the Matrix event.
    events, _ = relay_req(client, {"#e": [note.id]})
    assert raw in events


def test_reaction_and_repost(client, homeserver, relay_pool) -> None:
    q, message, note = local_post(client, homeserver, relay_pool)
    stranger = Stranger()
    tags = [["e", note.id], ["p", q.pubkey]]
    assert relay_send(client, stranger.event(7, "+", tags))[2] is True
    assert relay_send(client, stranger.event(6, "", tags))[2] is True
    reactions = homeserver.messages(q.profile_room_id, "m.reaction")
    keys = sorted(r["content"]["m.relates_to"]["key"] for r in reactions)
    assert keys == ["👍", "🔁"]
    assert all(r["content"]["m.relates_to"]["event_id"] == message["event_id"] for r in reactions)
    bodies = notifications(client, homeserver)
    assert any(b.startswith("👍 ") and "liked your post\n> hello nostr" in b for b in bodies)
    assert any(b.startswith("🔁 ") and "reposted your post\n> hello nostr" in b for b in bodies)
    assert relay_send(client, stranger.event(7, "🔥", tags))[2] is True
    assert any(b.startswith("🔥 ") and "reacted to your post with 🔥" in b for b in notifications(client, homeserver))
    assert relay_send(client, stranger.event(7, "-", tags))[2] is False  # dislikes aren't shown


def test_long_follow_list_is_not_a_hellthread(client, homeserver, relay_pool) -> None:
    """A follow list tags everyone followed: dozens of p tags is normal
    there, and must not count against its author."""
    q = create_id(client)
    stranger = Stranger()
    tags = [["p", f"{n:064x}"] for n in range(150)] + [["p", q.pubkey]]
    relay_send(client, stranger.event(3, "", tags))
    assert any("followed you" in b for b in notifications(client, homeserver))


def test_follow_notification_once(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    stranger = Stranger()
    first = relay_send(client, stranger.event(3, "", [["p", q.pubkey]], created_at=int(time.time()) - 10))
    assert first[2] is False and "not stored" in first[3]
    relay_send(client, stranger.event(3, "", [["p", q.pubkey], ["p", "ab" * 32]]))
    assert sum("followed you" in b for b in notifications(client, homeserver)) == 1
    assert [b for b in notifications(client, homeserver) if "followed you" in b][0].startswith("👤 ")
    assert run(client, client.app.state.repository.count_followers, q.id) == 1


def test_mention_and_deletion(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    stranger = Stranger()
    mention = stranger.event(1, f"hey nostr:{nip19.npub_encode(q.pubkey)}", [["p", q.pubkey]])
    assert relay_send(client, mention)[2] is True
    assert any("mentioned you in a post" in b for b in notifications(client, homeserver))
    # Not in Matrix but in the notification itself: "post" links to the web.
    room = run(client, client.app.state.repository.get_user_rooms, "@q:example.org").notifications_room_id
    [shown] = [c for c in homeserver.sent_to(room) if "mentioned you" in c["body"]]
    viewer = client.app.state.config.nostr.web_viewer_url
    assert f'mentioned you in a <a href="{viewer}nevent1' in shown["formatted_body"]
    events, _ = relay_req(client, {"#p": [q.pubkey], "kinds": [1]})
    assert mention in events
    # Its author deletes it: the notification carrying it is redacted.
    assert relay_send(client, stranger.event(5, "", [["e", mention["id"]]]))[2] is True
    events, _ = relay_req(client, {"#p": [q.pubkey], "kinds": [1]})
    assert mention not in events
    assert homeserver.redactions
    # Someone else can't delete it.
    other = Stranger()
    reply = relay_send(client, other.event(5, "", [["e", mention["id"]]]))
    assert reply[2] is True and "not stored" in reply[3]


def test_hellthread_refused(client) -> None:
    q = create_id(client)
    tags = [["p", q.pubkey]] + [["p", f"{n:064x}"] for n in range(25)]
    reply = relay_send(client, Stranger().event(1, "everyone look", tags))
    assert reply[2] is False


# -- DMs ----------------------------------------------------------------------------


def test_incoming_dm(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    stranger = Stranger()
    rumor = nip59.make_rumor(stranger.pubkey, 14, "psst, hi", [["p", q.pubkey]], int(time.time()))
    wrap = nip59.wrap_message(rumor, stranger.secret, q.pubkey).to_dict()
    assert relay_send(client, wrap)[2] is True
    dm_room = run(client, client.app.state.repository.get_dm_room, q.id, stranger.pubkey)
    assert dm_room and (dm_room, "@q:example.org") in homeserver.invites
    [message] = [e for e in homeserver.messages(dm_room) if e["content"].get(EMBED_FIELD)]
    assert message["content"]["body"] == "psst, hi" and message["sender"].startswith("@nostr_")
    # Only its recipient may read the wrap back.
    assert relay_req(client, {"kinds": [1059], "#p": [q.pubkey]})[0] == []
    secret = run(client, client.app.state.signer.secret_key, q.pubkey)
    assert relay_req(client, {"kinds": [1059], "#p": [q.pubkey]}, auth_secret=secret)[0] == [wrap]


def test_shadow_ids_take_no_dms_unless_allowed(client, homeserver, relay_pool) -> None:
    from dataclasses import replace

    from bridge.shadow_ids import ensure_shadow_id

    run(client, ensure_shadow_id, client.app, "@bob:example.org", "")
    [bob] = run(client, client.app.state.repository.list_ids_for_user, "@bob:example.org")
    assert bob.profile_room_id is None
    stranger = Stranger()

    def dm(text):
        rumor = nip59.make_rumor(stranger.pubkey, 14, text, [["p", bob.pubkey]], int(time.time()))
        return relay_send(client, nip59.wrap_message(rumor, stranger.secret, bob.pubkey).to_dict())

    reply = dm("buy my coin")
    assert reply[2] is False and "doesn't take DMs" in reply[3]
    assert run(client, client.app.state.repository.get_dm_room, bob.id, stranger.pubkey) is None
    config = client.app.state.config
    client.app.state.config = replace(config, bridge=replace(config.bridge, shadow_id_dms=True))
    try:
        assert dm("hi bob")[2] is True
        assert run(client, client.app.state.repository.get_dm_room, bob.id, stranger.pubkey)
    finally:
        client.app.state.config = config


def test_outgoing_dm(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    stranger = Stranger()
    command(client, f"[dm {nip19.npub_encode(stranger.pubkey)}")
    dm_room = run(client, client.app.state.repository.get_dm_room, q.id, stranger.pubkey)
    post(client, homeserver, dm_room, {"msgtype": "m.text", "body": "hello there"})
    wraps = [e for e, _ in relay_pool.published if e.kind == 1059]
    to_them = next(w for w in wraps if ["p", stranger.pubkey] in [list(t) for t in w.tags])
    opened = nip59.unwrap(to_them.to_dict(), stranger.secret)
    assert opened.sender == q.pubkey and opened.rumor["content"] == "hello there"
    # The sender's copy is in Matrix (hidden) and on the relay for them.
    secret = run(client, client.app.state.signer.secret_key, q.pubkey)
    mine = relay_req(client, {"kinds": [1059], "#p": [q.pubkey]}, auth_secret=secret)[0]
    assert len(mine) == 1 and nip59.unwrap(mine[0], secret).rumor["content"] == "hello there"


# -- following ------------------------------------------------------------------------


def test_follow_backfill_and_live_posts(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    alice = Stranger()
    relay_pool.network += [
        alice.event(0, json.dumps({"name": "Alice", "about": "hi"}), created_at=1000),
        alice.event(10002, "", [["r", "wss://alice.relay"]], created_at=1000),
        alice.event(1, "an older post", created_at=int(time.time()) - 500),
        alice.event(1, "a newer post", created_at=int(time.time()) - 100),
    ]
    command(client, f"[follow {nip19.npub_encode(alice.pubkey)}")
    remote = run(client, client.app.state.repository.get_remote_account, alice.pubkey)
    assert remote.room_id and remote.write_relays == ("wss://alice.relay",)
    assert homeserver.profiles[remote.ghost_user_id]["displayname"] == "Alice"
    assert homeserver.state_of(remote.room_id, "m.room.name") == {"name": "Alice"}
    assert (remote.room_id, "@q:example.org") in homeserver.invites
    kind3 = [e for e, _ in relay_pool.published if e.kind == 3][-1]
    assert [list(t) for t in kind3.tags] == [["p", alice.pubkey]]
    bodies = [e["content"]["body"] for e in homeserver.messages(remote.room_id)]
    assert bodies == ["an older post", "a newer post"]

    live = alice.event(1, "brand new")
    assert run(client, ingest, client.app, live, origin="subscription").stored
    assert homeserver.messages(remote.room_id)[-1]["content"]["body"] == "brand new"

    # Leaving the room unfollows.
    transact(client, {"type": "m.room.member", "state_key": "@q:example.org", "sender": "@q:example.org",
                      "room_id": remote.room_id, "event_id": "$leave", "content": {"membership": "leave"}})
    assert run(client, client.app.state.repository.list_follows, q.id) == []
    assert [list(t) for t in [e for e, _ in relay_pool.published if e.kind == 3][-1].tags] == []


def _followed_post(client, homeserver, relay_pool, *, kind=1):
    q = create_id(client)
    alice = Stranger()
    relay_pool.network.append(alice.event(0, json.dumps({"name": "Alice"}), created_at=1000))
    command(client, f"[follow {alice.pubkey}")
    raw = alice.event(kind, "alice says hi", [["k", "1"]] if kind == 1111 else [])
    run(client, ingest, client.app, raw, origin="subscription")
    remote = run(client, client.app.state.repository.get_remote_account, alice.pubkey)
    mirrored = homeserver.messages(remote.room_id)[-1]
    return q, alice, raw, remote, mirrored


@pytest.mark.parametrize("ours", [True, False])
def test_repost_of_our_own_fediverse_post(client, homeserver, relay_pool, ours) -> None:
    """Mostr republishes our ActivityPub bridge's notes on Nostr under its
    own keys. A repost of one links to the Matrix post it came from (the
    ActivityPub bridge says which, at <note>/matrix), not a ghost's copy."""
    from bridge.safe_fetch import Fetched

    _q, alice, _raw, remote, _mirrored = _followed_post(client, homeserver, relay_pool)
    host = "example.org" if ours else "elsewhere.example"
    note_url = f"https://{host}/actor/q/notes/bdbd800d"
    asked = []

    class Fetcher:
        async def get(self, url, **kwargs):
            asked.append(url)
            body = {"room_id": "!approfile:example.org", "event_id": "$original", "sender": "@q:example.org"}
            return Fetched(url, 200, json.dumps(body).encode(), "application/json")

        async def aclose(self):
            pass

    client.app.state.fetcher = Fetcher()
    homeserver.profiles["@q:example.org"] = {"displayname": "Q"}
    mostr = Stranger()
    relay_pool.network.append(mostr.event(0, json.dumps({"name": "q (via Mostr)"}), created_at=1000))
    original = mostr.event(1, "from the fediverse", [["proxy", note_url, "activitypub"]])
    repost = alice.event(6, json.dumps(original), [["e", original["id"]], ["p", mostr.pubkey]])
    assert run(client, ingest, client.app, repost, origin="subscription").stored

    card = homeserver.messages(remote.room_id)[-1]
    relation = card["content"]["org.matrix.msc4501.social.relates_to"]
    ghost = run(client, client.app.state.repository.get_remote_account, mostr.pubkey)
    if ours:
        assert asked == [note_url + "/matrix"]
        assert card["content"]["formatted_body"].startswith("🔁 reposted Q's ")
        assert card["content"]["body"].startswith("https://matrix.to/#/!approfile:example.org/$original")
        assert (relation["room_id"], relation["event_id"], relation["sender"]) == ("!approfile:example.org", "$original", "@q:example.org")
        assert ghost is None or ghost.room_id is None
    else:
        assert asked == [] and ghost is not None and ghost.room_id and relation["room_id"] == ghost.room_id


def test_repost_card_reuses_the_originals_media(client, homeserver, relay_pool) -> None:
    """A repost card shows the original's video/image from its Matrix copy,
    not a second download (which a big video can fail)."""
    from bridge.safe_fetch import Fetched

    _q, alice, _raw, remote, _mirrored = _followed_post(client, homeserver, relay_pool)
    fetched = []

    class Fetcher:
        async def get(self, url, **kwargs):
            fetched.append(url)
            return Fetched(url, 200, b"\x00" * 64, "video/mp4")

        async def aclose(self):
            pass

    client.app.state.fetcher = Fetcher()
    video = "https://cdn.example/clip.mp4"
    carol = Stranger()
    original = carol.event(1, video, [["imeta", f"url {video}", "m video/mp4"]])
    repost = alice.event(6, json.dumps(original), [["e", original["id"]], ["p", carol.pubkey]])
    assert run(client, ingest, client.app, repost, origin="subscription").stored

    carols_room = run(client, client.app.state.repository.get_remote_account, carol.pubkey).room_id
    copy = homeserver.messages(carols_room)[-1]["content"]
    card = homeserver.messages(remote.room_id)[-1]["content"]
    assert copy["msgtype"] == card["msgtype"] == "m.video" and card["url"] == copy["url"]
    assert card["formatted_body"].startswith("🔁 reposted ") and card["body"].startswith("https://matrix.to/#/")
    assert fetched.count(video) == 1


def test_media_types_clients_write_loosely() -> None:
    from bridge.render import attachments_of

    raw = {"kind": 1, "content": "", "tags": [
        ["imeta", "url https://blossom.example/abc.jpg", "m jpeg"],
        ["imeta", "url https://blossom.example/def", "m png"],
        ["imeta", "url https://blossom.example/ghi.mp4"],
        ["imeta", "url https://blossom.example/jkl", "m nonsense"],
    ]}
    assert [a.mime for a in attachments_of(raw)] == ["image/jpeg", "image/png", "video/mp4", None]


def test_outgoing_reaction_and_undo(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    reaction = homeserver._store(remote.room_id, "m.reaction",
                                 {"m.relates_to": {"rel_type": "m.annotation", "event_id": mirrored["event_id"], "key": "👍🏽"}},
                                 "@q:example.org")
    transact(client, reaction)
    [like] = [e for e, _ in relay_pool.published if e.kind == 7]
    assert like.pubkey == q.pubkey and like.content == "+"
    assert [list(t) for t in like.tags] == [["e", raw["id"], "wss://nostr.example.org", alice.pubkey], ["p", alice.pubkey], ["k", "1"], client_tag(client.app)]
    redaction = homeserver._store(remote.room_id, "m.room.redaction", {"redacts": reaction["event_id"]}, "@q:example.org")
    redaction["redacts"] = reaction["event_id"]
    transact(client, redaction)
    [undo] = [e for e, _ in relay_pool.published if e.kind == 5]
    assert [list(t) for t in undo.tags] == [["e", like.id], ["k", "7"]]


def test_reply_to_a_reaction_goes_in_the_reacted_posts_thread(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    transact(client, homeserver._store(remote.room_id, "m.reaction", {
        "m.relates_to": {"rel_type": "m.annotation", "event_id": mirrored["event_id"], "key": "👍🏽"}}, "@q:example.org"))
    [like] = [e for e, _ in relay_pool.published if e.kind == 7]
    stranger = Stranger()
    comment = stranger.event(1111, "so true fren", [
        ["E", like.id, "", q.pubkey], ["K", "7"], ["P", q.pubkey], ["e", like.id, "", q.pubkey], ["k", "7"], ["p", q.pubkey]])
    assert relay_send(client, comment)[2] is True
    reply = homeserver.messages(remote.room_id)[-1]
    assert reply["sender"] != "@q:example.org" and "so true fren" in reply["content"]["body"]
    assert reply["content"]["body"].startswith('⤵️ Reply to q@example.org\'s reaction "👍🏽":')
    relation = reply["content"]["m.relates_to"]
    assert relation["rel_type"] == "m.thread" and relation["event_id"] == mirrored["event_id"]
    assert relation["is_falling_back"] is True  # in the thread, not an answer to the post
    assert '<a href="https://matrix.to/#/@q:example.org">q@example.org</a>\'s reaction' in reply["content"]["formatted_body"]
    told = [b for b in notifications(client, homeserver) if "so true fren" in b]
    assert len(told) == 1 and "replied to your reaction on Alice's post" in told[0]  # and not a mention too
    [rich] = [c["formatted_body"] for _, c, _ in homeserver.sent if "replied to your reaction" in c.get("body", "")]
    assert f"Alice&#x27;s <a href=\"https://matrix.to/#/{remote.room_id}/{mirrored['event_id']}" in rich
    assert rich.split("</p>")[0].endswith(">post</a>")


def test_followed_reply_copy_carries_the_msc4501_reply(client, homeserver, relay_pool) -> None:
    """A followed account's reply to a post in another room is copied to
    their own room with the relation, the replied-to post's content inside
    it, and their own words alone as social.body."""
    _q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    bob = Stranger()
    relay_pool.network.append(bob.event(0, json.dumps({"name": "Bob"}), created_at=1000))
    command(client, f"[follow {bob.pubkey}")
    reply = bob.event(1, "didn't one of them die?", [["e", raw["id"], "", "root"], ["p", alice.pubkey]])
    run(client, ingest, client.app, reply, origin="subscription")
    bobs = run(client, client.app.state.repository.get_remote_account, bob.pubkey)
    copy = homeserver.messages(bobs.room_id)[-1]["content"]
    assert copy["body"].startswith("⤵️ Reply to Alice's post:")
    relation = copy["org.matrix.msc4501.social.relates_to"]
    assert relation["rel_type"] == "org.matrix.msc4501.social.reply"
    assert (relation["event_id"], relation["room_id"], relation["displayname"]) == (mirrored["event_id"], remote.room_id, "Alice")
    assert relation["content"]["body"] == "alice says hi" and "content_inline" not in relation
    assert copy["org.matrix.msc4501.social.body"] == "didn't one of them die?"
    assert "Reply to" not in copy["org.matrix.msc4501.social.formatted_body"]


def test_reply_to_a_custom_emoji_reaction_shows_it(client, homeserver, relay_pool) -> None:
    from bridge.inbound import _Reacted, _reaction_html

    shown = _reaction_html(_Reacted("$post", ":blobcat:", "mxc://example.org/blobcat"))
    assert shown.startswith("<img data-mx-emoticon") and 'src="mxc://example.org/blobcat"' in shown
    assert 'alt=":blobcat:"' in shown
    assert _reaction_html(_Reacted("$post", "🔥", None)) == "&quot;🔥&quot;"


@pytest.mark.parametrize("kind", [1, 1111])
def test_outgoing_reply(client, homeserver, relay_pool, kind) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool, kind=kind)
    post(client, homeserver, remote.room_id, {"msgtype": "m.text", "body": "> <@alice> hi\n\nhi alice",
                                              "m.relates_to": {"m.in_reply_to": {"event_id": mirrored["event_id"]}}})
    reply = [e for e, _ in relay_pool.published if e.kind in (1, 1111) and e.pubkey == q.pubkey][-1]
    assert reply.content == "hi alice"
    tags = [list(t) for t in reply.tags]
    if kind == 1:
        assert reply.kind == 1 and ["e", raw["id"], "wss://nostr.example.org", "root", alice.pubkey] in tags
    else:
        assert reply.kind == 1111 and ["e", raw["id"], "wss://nostr.example.org", alice.pubkey] in tags and ["k", "1111"] in tags
    assert ["p", alice.pubkey] in tags
    # Chatting in the account's room without replying publishes nothing.
    before = len(relay_pool.published)
    post(client, homeserver, remote.room_id, {"msgtype": "m.text", "body": "nice"})
    assert len(relay_pool.published) == before


def test_quote_post(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    link = "https://njump.me/" + nip19.nevent_encode(nip19.EventPointer(raw["id"], (), alice.pubkey))
    post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": f"look at this {link}"})
    quote = [e for e, _ in relay_pool.published if e.kind == 1 and e.pubkey == q.pubkey][-1]
    assert quote.content.startswith("look at this nostr:nevent1")
    assert ["q", raw["id"], "wss://nostr.example.org", alice.pubkey] in [list(t) for t in quote.tags]
    row = run(client, client.app.state.repository.get_event, quote.id)
    from bridge.event_sources import rebuild

    assert run(client, rebuild, client.app, row).event == quote.to_dict()


def test_remote_posts_carry_external_url(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    url = mirrored["content"]["external_url"]
    assert url.startswith("https://ditto.pub/nevent1")
    assert nip19.decode(url.rsplit("/", 1)[1]).id == raw["id"]


def test_every_ghost_gets_profile_fields(repository, homeserver, relay_pool) -> None:
    from bridge.config import parse_config
    from tests.conftest import make_client, raw_config

    config = parse_config(raw_config(bridge={"set_msc4501_profile_room": True, "msc4503_external_handle": "profile"}))
    with make_client(config, repository, homeserver, relay_pool) as client:
        q, message, note = local_post(client, homeserver, relay_pool)
        stranger = Stranger()
        relay_send(client, stranger.event(1, "hi", [["e", note.id, "", "root", q.pubkey], ["p", q.pubkey]]))
        remote = run(client, client.app.state.repository.get_remote_account, stranger.pubkey)
        # A stranger who only replied still has a room (nobody invited) and the fields.
        assert remote.room_id and not [u for r, u in homeserver.invites if r == remote.room_id and u.startswith("@q")]
        profile = homeserver.profiles[remote.ghost_user_id]
        assert profile["org.matrix.msc4501.social.profile_room"] == {"room_id": remote.room_id, "via": ["example.org"]}
        assert profile["org.matrix.msc4503.external_handle"]["protocol"]["id"] == "nostr"
        assert profile["org.matrix.msc4503.external_handle"]["url"].startswith("https://ditto.pub/npub1")


def test_posts_from_another_app_are_mirrored_with_external_url(client, homeserver, relay_pool) -> None:
    """A post made with the ID's key in another Nostr app shows in its Profile
    Room, from the bot as the user (MSC4144), linked to the post on the web."""
    q = create_id(client)
    homeserver._store(q.profile_room_id, "m.room.avatar", {"url": "mxc://example.org/qface"}, "@q:example.org", "")
    secret = run(client, client.app.state.signer.secret_key, q.pubkey)
    raw = nostr_event.build(secret, kind=1, content="posted from my phone", created_at=int(time.time())).to_dict()
    assert run(client, ingest, client.app, raw, origin="client").stored
    mirrored = homeserver.messages(q.profile_room_id)[-1]
    content = mirrored["content"]
    assert mirrored["sender"] == "@nostrbot:example.org" and content["body"] == "posted from my phone"
    assert content["external_url"].startswith("https://ditto.pub/nevent1")
    assert nip19.decode(content["external_url"].rsplit("/", 1)[1]).id == raw["id"]
    assert content["com.beeper.per_message_profile"] == {
        "id": "@q:example.org", "displayname": "Q Person", "avatar_url": "mxc://example.org/qface"}


def test_account_commands_default_to_the_rooms_account(client, homeserver, relay_pool) -> None:
    """Run in a Nostr account's room (or a DM room), [follow, [unfollow,
    [block, [unblock and [dm need no account named."""
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    repo = client.app.state.repository
    command(client, "[unfollow", room=remote.room_id)
    assert run(client, repo.list_follows, q.id) == []
    command(client, "[follow", room=remote.room_id)
    assert run(client, repo.list_follows, q.id) == [alice.pubkey]
    command(client, "[block", room=remote.room_id)
    assert alice.pubkey in run(client, repo.list_blocks, q.id)
    command(client, "[unblock", room=remote.room_id)
    assert alice.pubkey not in run(client, repo.list_blocks, q.id)
    command(client, "[dm", room=remote.room_id)
    dm_room = run(client, repo.get_dm_room, q.id, alice.pubkey)
    assert dm_room
    command(client, "[block", room=dm_room)
    assert alice.pubkey in run(client, repo.list_blocks, q.id)
    # Anywhere else, it still asks.
    command(client, "[follow")
    assert "Which account?" in homeserver.sent_to("!dm:example.org")[-1]["body"]


def test_followed_account_reposting_a_local_post(client, homeserver, relay_pool) -> None:
    """The repost shows in the reposter's room as well as a 🔁 on the post,
    and undoing it removes both."""
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    message = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "my own post"})
    note = [e for e, _ in relay_pool.published if e.kind == 1 and e.pubkey == q.pubkey][-1]
    repost = alice.event(6, "", [["e", note.id, "wss://nostr.example.org"], ["p", q.pubkey]])
    assert run(client, ingest, client.app, repost, origin="subscription").stored

    [reaction] = homeserver.messages(q.profile_room_id, "m.reaction")
    assert reaction["content"]["m.relates_to"] == {"rel_type": "m.annotation", "event_id": message["event_id"], "key": "🔁"}
    assert any("reposted your post" in b for b in notifications(client, homeserver))
    card = homeserver.messages(remote.room_id)[-1]
    assert card["content"]["formatted_body"].startswith("🔁 reposted ") and "my own post" in card["content"]["formatted_body"]
    # A client that understands the relation shows the post's words alone.
    assert card["content"]["body"] == f"https://matrix.to/#/{message['room_id']}/{message['event_id']}"
    assert card["content"]["org.matrix.msc4501.social.body"] == "my own post"
    assert card["content"]["org.matrix.msc4501.social.relates_to"]["event_id"] == message["event_id"]

    undo = alice.event(5, "", [["e", repost["id"]], ["k", "6"]])
    run(client, ingest, client.app, undo, origin="subscription")
    redacted = {event_id for _, event_id, _ in homeserver.redactions}
    assert {reaction["event_id"], card["event_id"]} <= redacted


def test_repair_reposts_that_only_got_a_reaction(client, homeserver, relay_pool) -> None:
    from bridge.inbound import repair_repost_cards

    q, message, note = local_post(client, homeserver, relay_pool, text="worth reposting")
    bob = Stranger()
    relay_pool.network.append(bob.event(0, json.dumps({"name": "Bob"}), created_at=1000))
    repost = bob.event(6, json.dumps(note.to_dict()), [["e", note.id, "wss://nostr.example.org"], ["p", q.pubkey]])
    assert relay_send(client, repost)[2] is True  # not followed yet: only the 🔁
    command(client, f"[follow {bob.pubkey}")
    remote = run(client, client.app.state.repository.get_remote_account, bob.pubkey)
    before = [m["content"].get("body", "") for m in homeserver.messages(remote.room_id)]
    assert not any(b.startswith("🔁") for b in before)

    assert run(client, repair_repost_cards, client.app) == 1
    card = homeserver.messages(remote.room_id)[-1]
    assert card["content"]["formatted_body"].startswith("🔁 reposted ") and "worth reposting" in card["content"]["formatted_body"]
    relation = card["content"]["org.matrix.msc4501.social.relates_to"]
    assert relation["rel_type"] == "org.matrix.msc4501.social.repost" and relation["event_id"] == message["event_id"]
    assert card["content"]["external_url"].startswith("https://ditto.pub/nevent1")
    # Done once only.
    assert run(client, repair_repost_cards, client.app) == 0
    row = run(client, client.app.state.repository.get_event, repost["id"])
    assert row.matrix_event_id == card["event_id"]


@pytest.mark.parametrize("msc4027", [False, True])
def test_custom_emoji_reactions(repository, homeserver, relay_pool, msc4027) -> None:
    """NIP-30 custom emoji: an MSC4027 image reaction only when
    bridge.msc4027_custom_reactions is on, as in matrix-appservice-activitypub."""
    import io

    from PIL import Image

    from bridge.config import parse_config
    from bridge.safe_fetch import Fetched
    from tests.conftest import make_client, raw_config

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (250, 200, 0)).save(buffer, format="PNG")

    class Fetcher:
        async def get(self, url, **kwargs):
            return Fetched(url, 200, buffer.getvalue(), "image/png")

        async def aclose(self):
            pass

    config = parse_config(raw_config(bridge={"msc4027_custom_reactions": msc4027}))
    with make_client(config, repository, homeserver, relay_pool) as client:
        client.app.state.fetcher = Fetcher()
        q, message, note = local_post(client, homeserver, relay_pool)
        stranger = Stranger()
        tags = [["e", note.id], ["p", q.pubkey], ["emoji", "blobcat", "https://img.example/blobcat.png"]]
        assert relay_send(client, stranger.event(7, ":blobcat:", tags))[2] is True
        [reaction] = homeserver.messages(q.profile_room_id, "m.reaction")
        key = reaction["content"]["m.relates_to"]["key"]
        if msc4027:
            assert key.startswith("mxc://") and reaction["content"]["shortcode"] == ":blobcat:"
        else:
            assert key == ":blobcat:" and "shortcode" not in reaction["content"]


def test_backfill_a_thread(client, homeserver, relay_pool) -> None:
    """[backfill inside a thread brings in that conversation's replies, as
    matrix-appservice-activitypub's ;backfill does."""
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    bob, carol = Stranger(), Stranger()
    first = bob.event(1, "first reply", [["e", raw["id"], "", "root"], ["p", alice.pubkey]], created_at=raw["created_at"] + 10)
    nested = carol.event(1, "reply to the reply", [["e", raw["id"], "", "root"], ["e", first["id"], "", "reply"]],
                         created_at=raw["created_at"] + 20)
    comment = carol.event(1111, "a comment", [["E", raw["id"]], ["K", "1"], ["e", raw["id"]], ["k", "1"]],
                          created_at=raw["created_at"] + 30)
    unrelated = bob.event(1, "elsewhere")
    relay_pool.network += [first, nested, comment, unrelated]
    transact(client, {"type": "m.room.message", "event_id": "$bf1", "room_id": remote.room_id, "sender": "@q:example.org",
                      "content": {"msgtype": "m.text", "body": "[backfill",
                                  "m.relates_to": {"rel_type": "m.thread", "event_id": mirrored["event_id"],
                                                   "is_falling_back": True, "m.in_reply_to": {"event_id": mirrored["event_id"]}}}})
    repo = client.app.state.repository
    rows = {e["id"]: run(client, repo.get_event, e["id"]) for e in (first, nested, comment, unrelated)}
    assert rows[unrelated["id"]] is None
    for event in (first, nested, comment):
        assert rows[event["id"]] is not None and rows[event["id"]].room_id == remote.room_id
    nested_msg = next(m for m in homeserver.messages(remote.room_id) if m["event_id"] == rows[nested["id"]].matrix_event_id)
    relation = nested_msg["content"]["m.relates_to"]
    assert relation["event_id"] == mirrored["event_id"]
    assert relation["m.in_reply_to"]["event_id"] == rows[first["id"]].matrix_event_id
    assert "Done: 3 replies brought in." in homeserver.sent_to(remote.room_id)[-1]["body"]
    # The bot answered in the thread it was asked in, before and after.
    for notice in [c for c in homeserver.sent_to(remote.room_id) if c.get("msgtype") == "m.notice"][-2:]:
        assert notice["m.relates_to"] == {"rel_type": "m.thread", "event_id": mirrored["event_id"], "is_falling_back": True,
                                          "m.in_reply_to": {"event_id": "$bf1"}}
    # Again: nothing new.
    transact(client, {"type": "m.room.message", "event_id": "$bf2", "room_id": remote.room_id, "sender": "@q:example.org",
                      "content": {"msgtype": "m.text", "body": "[backfill",
                                  "m.relates_to": {"rel_type": "m.thread", "event_id": mirrored["event_id"]}}})
    assert homeserver.sent_to(remote.room_id)[-1]["body"] == "No more replies found."


def _repost_by_reaction(client, homeserver, relay_pool):
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    reaction = homeserver._store(remote.room_id, "m.reaction",
                                 {"m.relates_to": {"rel_type": "m.annotation", "event_id": mirrored["event_id"], "key": "🔁"}},
                                 "@q:example.org")
    transact(client, reaction)
    [repost] = relay_pool.of_kind(6)
    card = homeserver.messages(q.profile_room_id)[-1]
    return q, alice, raw, remote, mirrored, reaction, repost, card


def test_repost_card_matches_the_sibling_bridges(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored, reaction, repost, card = _repost_by_reaction(client, homeserver, relay_pool)
    content = card["content"]
    link = f"https://matrix.to/#/{remote.room_id}/{mirrored['event_id']}?via=example.org"
    assert card["sender"] == "@nostrbot:example.org"
    assert content["msgtype"] == "m.text" and content["body"] == link
    assert content["formatted_body"] == (
        f'<p>🔁 <a href="https://matrix.to/#/@q:example.org">Q Person</a> reposted '
        f'<a href="https://matrix.to/#/{remote.ghost_user_id}">Alice</a>\'s <a href="{link}">post</a></p>'
        "<blockquote>alice says hi</blockquote>")
    assert content["external_url"].startswith("https://ditto.pub/nevent1")
    assert content["com.beeper.per_message_profile"] == {"id": "@q:example.org", "displayname": "Q Person"}
    assert content["org.matrix.msc4501.social.relates_to"] == {
        "rel_type": "org.matrix.msc4501.social.repost", "event_id": mirrored["event_id"], "room_id": remote.room_id,
        "sender": remote.ghost_user_id, "via": ["example.org"], "displayname": "Alice", "content_inline": True}
    assert content["org.matrix.msc4501.social.body"] == "alice says hi"


def test_redacting_the_card_undoes_the_repost(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored, reaction, repost, card = _repost_by_reaction(client, homeserver, relay_pool)
    redaction = homeserver._store(q.profile_room_id, "m.room.redaction", {"redacts": card["event_id"]}, "@q:example.org")
    redaction["redacts"] = card["event_id"]
    transact(client, redaction)
    [undo] = relay_pool.of_kind(5)
    assert ["e", repost.id] in [list(t) for t in undo.tags]
    assert reaction["event_id"] in {event_id for _, event_id, _ in homeserver.redactions}


def test_redacting_the_reaction_removes_the_card(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored, reaction, repost, card = _repost_by_reaction(client, homeserver, relay_pool)
    redaction = homeserver._store(remote.room_id, "m.room.redaction", {"redacts": reaction["event_id"]}, "@q:example.org")
    redaction["redacts"] = reaction["event_id"]
    transact(client, redaction)
    assert len(relay_pool.of_kind(5)) == 1
    assert card["event_id"] in {event_id for _, event_id, _ in homeserver.redactions}


def test_repost_card_without_msc4501(repository, homeserver, relay_pool) -> None:
    from bridge.config import parse_config
    from tests.conftest import make_client, raw_config

    config = parse_config(raw_config(bridge={"set_msc4501_relates_to": False, "msc4144_per_message_profiles": False}))
    with make_client(config, repository, homeserver, relay_pool) as client:
        q, alice, raw, remote, mirrored, reaction, repost, card = _repost_by_reaction(client, homeserver, relay_pool)
    content = card["content"]
    assert "org.matrix.msc4501.social.relates_to" not in content and "com.beeper.per_message_profile" not in content
    assert content["body"].startswith("🔁 reposted Alice's post:\n> alice says hi\nhttps://matrix.to/#/")


def test_repair_gives_old_own_reposts_their_card(client, homeserver, relay_pool) -> None:
    from bridge.inbound import repair_repost_cards

    q, alice, raw, remote, mirrored, reaction, repost, card = _repost_by_reaction(client, homeserver, relay_pool)
    repo = client.app.state.repository
    # As before cards existed: no card recorded.
    client.portal.call(lambda: repo._run(lambda conn: conn.execute("DELETE FROM mirrored_events WHERE nostr_id = ?", (repost.id,))))
    before = len(homeserver.messages(q.profile_room_id))
    assert run(client, repair_repost_cards, client.app) == 1
    assert len(homeserver.messages(q.profile_room_id)) == before + 1
    assert run(client, repair_repost_cards, client.app) == 0


def test_reacting_to_a_repost_card_acts_on_the_original(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    alice, bob = Stranger(), Stranger()
    relay_pool.network.append(alice.event(0, json.dumps({"name": "Alice"}), created_at=1000))
    command(client, f"[follow {alice.pubkey}")
    original = bob.event(1, "bob's original post")
    repost = alice.event(6, json.dumps(original), [["e", original["id"], "wss://relay.example"], ["p", bob.pubkey]])
    assert run(client, ingest, client.app, repost, origin="subscription").stored
    remote = run(client, client.app.state.repository.get_remote_account, alice.pubkey)
    card = homeserver.messages(remote.room_id)[-1]
    assert card["content"]["formatted_body"].startswith("🔁 reposted ")
    for key in ("🔁", "👍"):
        transact(client, homeserver._store(remote.room_id, "m.reaction", {"m.relates_to": {
            "rel_type": "m.annotation", "event_id": card["event_id"], "key": key}}, "@q:example.org"))
    [mine] = [e for e in relay_pool.of_kind(6) if e.pubkey == q.pubkey]
    assert ["e", original["id"]] == [list(t) for t in mine.tags if t[0] == "e"][0][:2]
    assert relay_pool.of_kind(16) == []
    [like] = [e for e in relay_pool.of_kind(7) if e.pubkey == q.pubkey]
    assert [list(t) for t in like.tags if t[0] == "e"][0][1] == original["id"]
    # The card describes the original, not the repost.
    my_card = homeserver.messages(q.profile_room_id)[-1]
    original_row = run(client, client.app.state.repository.get_event, original["id"])
    relation = my_card["content"]["org.matrix.msc4501.social.relates_to"]
    assert relation["event_id"] == original_row.matrix_event_id
    assert my_card["content"]["org.matrix.msc4501.social.body"] == "bob's original post"


def test_an_at_sign_before_a_mention_belongs_to_the_pill(client) -> None:
    from bridge.render import render

    q = create_id(client)
    npub = nip19.npub_encode(q.pubkey)
    note = nip19.note_encode("ab" * 32)
    raw = Stranger().event(1, f"hi @nostr:{npub} and @{npub}!", [["p", q.pubkey]])
    shown = run(client, render, client.app, raw)
    assert "@<a" not in shown.html and "@@" not in shown.body
    assert shown.html.count("matrix.to/#/@q:example.org") == 2
    kept = run(client, render, client.app, Stranger().event(1, f"see @nostr:{note}"))
    assert kept.body.startswith("see @")


def test_only_strangers_reaching_someone_here_count_toward_a_wave(client, homeserver, relay_pool) -> None:
    from tests.test_ids import ids_of, message, send

    send(client, message("[create profile"))
    [q] = ids_of(client)
    guard = client.app.state.spam
    passing = [Stranger().event(1, f"just passing {i}") for i in range(3)]
    for raw in passing:
        run(client, ingest, client.app, raw, origin="relay")
    assert len(guard._wave) == 0
    mention = Stranger().event(1, "hi q", tags=[["p", q.pubkey]])
    run(client, ingest, client.app, mention, origin="relay")
    assert len(guard._wave) == 1


# -- an ID's key used in another Nostr app (bridge.elsewhere) ----------------------------


def _signed_elsewhere(client, q, kind, content="", tags=(), created_at=None):
    secret = run(client, client.app.state.signer.secret_key, q.pubkey)
    return nostr_event.build(secret, kind=kind, content=content, tags=list(tags),
                             created_at=created_at or int(time.time())).to_dict()


def test_replies_reactions_and_reposts_made_elsewhere_show_as_theirs(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    bot = "@nostrbot:example.org"
    # A reply in another app: in the post's thread, in the room it's shown in.
    reply = _signed_elsewhere(client, q, 1, "replying from my phone", [["e", raw["id"], "", "root"], ["p", alice.pubkey]])
    assert run(client, ingest, client.app, reply, origin="subscription").stored
    shown = homeserver.messages(remote.room_id)[-1]
    assert shown["sender"] == bot and shown["content"]["body"].startswith("replying from my phone")
    assert shown["content"]["m.relates_to"]["event_id"] == mirrored["event_id"]
    assert shown["content"]["com.beeper.per_message_profile"]["id"] == "@q:example.org"
    # A like and a repost: on the post, as theirs; the repost's card in their room.
    like = _signed_elsewhere(client, q, 7, "+", [["e", raw["id"]], ["p", alice.pubkey], ["k", "1"]])
    assert run(client, ingest, client.app, like, origin="subscription").stored
    repost = _signed_elsewhere(client, q, 6, "", [["e", raw["id"], "wss://nostr.example.org"], ["p", alice.pubkey]])
    assert run(client, ingest, client.app, repost, origin="subscription").stored
    reactions = homeserver.messages(remote.room_id, "m.reaction")
    assert [r["content"]["m.relates_to"]["key"] for r in reactions] == ["👍", "🔁"]
    assert all(r["sender"] == bot and r["content"]["com.beeper.per_message_profile"]["id"] == "@q:example.org"
               for r in reactions)
    card = homeserver.messages(q.profile_room_id)[-1]
    assert "reposted" in card["content"]["formatted_body"]
    # Deleting the repost there takes back the 🔁 and the card.
    undo = _signed_elsewhere(client, q, 5, "", [["e", repost["id"]], ["k", "6"]])
    run(client, ingest, client.app, undo, origin="subscription")
    redacted = {event_id for _, event_id, _ in homeserver.redactions}
    assert {reactions[1]["event_id"], card["event_id"]} <= redacted
    # Nothing the bridge made itself comes back in twice.
    transact(client, homeserver._store(remote.room_id, "m.reaction", {
        "m.relates_to": {"rel_type": "m.annotation", "event_id": mirrored["event_id"], "key": "😂"}}, "@q:example.org"))
    [ours] = [e for e, _ in relay_pool.published if e.kind == 7]
    before = len(homeserver.messages(remote.room_id, "m.reaction"))
    run(client, ingest, client.app, ours.to_dict(), origin="subscription")
    assert len(homeserver.messages(remote.room_id, "m.reaction")) == before


def test_follow_and_mute_lists_made_elsewhere(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    repo = client.app.state.repository
    alice, bob, carol = Stranger(), Stranger(), Stranger()
    follows = _signed_elsewhere(client, q, 3, "", [["p", alice.pubkey], ["p", bob.pubkey], ["t", "nostr"]])
    assert run(client, ingest, client.app, follows, origin="subscription").stored
    assert sorted(run(client, repo.list_follows, q.id)) == sorted([alice.pubkey, bob.pubkey])
    assert not [e for e, _ in relay_pool.published if e.kind == 3 and e.id != follows["id"]]  # not published over
    # Unfollowed there: unfollowed here. An older list changes nothing.
    fewer = _signed_elsewhere(client, q, 3, "", [["p", alice.pubkey], ["t", "nostr"]], created_at=follows["created_at"] + 1)
    run(client, ingest, client.app, fewer, origin="subscription")
    assert run(client, repo.list_follows, q.id) == [alice.pubkey]
    stale = _signed_elsewhere(client, q, 3, "", [], created_at=follows["created_at"] - 50)
    run(client, ingest, client.app, stale, origin="subscription")
    assert run(client, repo.list_follows, q.id) == [alice.pubkey]
    # Following from Matrix next keeps what the bridge can't model.
    command(client, f"[follow {carol.pubkey}")
    published = [e for e, _ in relay_pool.published if e.kind == 3][-1]
    assert ["t", "nostr"] in [list(t) for t in published.tags]
    assert {t[1] for t in published.tags if t[0] == "p"} == {alice.pubkey, carol.pubkey}
    # A mute list: blocks.
    mutes = _signed_elsewhere(client, q, 10000, "", [["p", bob.pubkey]])
    run(client, ingest, client.app, mutes, origin="subscription")
    assert run(client, repo.list_blocks, q.id) == [bob.pubkey]


def test_a_profile_edited_elsewhere(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    profile = _signed_elsewhere(client, q, 0, json.dumps({"name": "Q Prime", "about": "now on my phone",
                                                          "website": "https://q.example"}), created_at=int(time.time()) + 5)
    run(client, ingest, client.app, profile, origin="subscription")
    assert homeserver.state_of(q.profile_room_id, "m.room.name")["name"] == "Q Prime"
    assert homeserver.state_of(q.profile_room_id, "m.room.topic")["topic"] == "now on my phone"
    assert homeserver.state_of(q.profile_room_id, "software.haven.nostr.profile_extra") == {"website": "https://q.example"}


def test_the_relay_keeps_whatever_a_local_id_signs_elsewhere(client, homeserver, relay_pool) -> None:
    """An app's settings, relay list or the like from another Nostr app is
    taken and served, never answered "restricted": poster.place reads that
    as the relay refusing it and stops using it for hours."""
    q = create_id(client)
    now = int(time.time())
    settings = _signed_elsewhere(client, q, 30078, "encrypted settings", tags=[["d", "poster.place/settings"]], created_at=now)
    reply = relay_send(client, settings)
    assert reply[2] is True and "restricted" not in reply[3]
    assert [e["id"] for e in relay_req(client, {"authors": [q.pubkey], "kinds": [30078]})[0]] == [settings["id"]]
    # An older copy isn't kept over it, and still isn't refused.
    older = _signed_elsewhere(client, q, 30078, "stale", tags=[["d", "poster.place/settings"]], created_at=now - 60)
    reply = relay_send(client, older)
    assert reply[2] is True and reply[3].startswith("duplicate:")
    assert [e["id"] for e in relay_req(client, {"authors": [q.pubkey], "kinds": [30078]})[0]] == [settings["id"]]
    # A newer one replaces it.
    newer = _signed_elsewhere(client, q, 30078, "fresh", tags=[["d", "poster.place/settings"]], created_at=now + 5)
    assert relay_send(client, newer)[2] is True
    assert [e["content"] for e in relay_req(client, {"authors": [q.pubkey], "kinds": [30078]})[0]] == ["fresh"]
    # A reaction to something not in Matrix is kept too.
    like = _signed_elsewhere(client, q, 7, "+", tags=[["e", "ab" * 32], ["p", "cd" * 32]], created_at=now)
    assert relay_send(client, like)[2] is True
    assert relay_req(client, {"ids": [like["id"]]})[0][0]["content"] == "+"


def test_a_local_ids_app_is_never_told_restricted(client, homeserver, relay_pool) -> None:
    """poster.place stops using a relay of its user's for hours on any answer
    saying "restricted", "blocked" or "rate-limited". In a session that's a
    local ID's own, refusing someone else's event says neither."""
    q = create_id(client)
    stranger = Stranger().event(1, "not for anyone here")
    assert relay_send(client, stranger)[3].startswith("restricted:")  # an ordinary connection
    mine = _signed_elsewhere(client, q, 30078, "settings", tags=[["d", "x"]])
    other = Stranger().event(1, "passed on by q's app")
    with client.websocket_connect("/") as ws:
        ws.receive_json()
        ws.send_json(["EVENT", mine])
        assert ws.receive_json()[2] is True
        ws.send_json(["EVENT", other])
        reply = ws.receive_json()
    assert reply[2] is False and reply[3] == "error: not kept by this relay"


def test_a_dm_sent_from_another_app(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    alice = Stranger()
    secret = run(client, client.app.state.signer.secret_key, q.pubkey)
    rumor = nip59.make_rumor(q.pubkey, 14, "sent from my phone", [["p", alice.pubkey]], int(time.time()))
    own_copy = nip59.wrap_message(rumor, secret, q.pubkey).to_dict()
    assert relay_send(client, own_copy)[2] is True
    dm_room = run(client, client.app.state.repository.get_dm_room, q.id, alice.pubkey)
    [message] = [e for e in homeserver.messages(dm_room) if e["content"].get(EMBED_FIELD)]
    assert message["sender"] == "@nostrbot:example.org" and message["content"]["body"] == "sent from my phone"
    assert message["content"]["com.beeper.per_message_profile"]["id"] == "@q:example.org"
    # Another copy of the same message isn't shown twice.
    again = nip59.wrap_message(rumor, secret, q.pubkey).to_dict()
    relay_send(client, again)
    assert len([e for e in homeserver.messages(dm_room) if e["content"].get(EMBED_FIELD)]) == 1


def test_feeds_ask_for_what_local_ids_sign_elsewhere(client) -> None:
    q = create_id(client)
    desired = run(client, client.app.state.feeds._desired)
    inbox = next(filters for (relay, name), filters in desired.items() if name == "inbox")
    assert any(f.get("authors") == [q.pubkey] and 1 in f["kinds"] and 3 in f["kinds"] for f in inbox)
