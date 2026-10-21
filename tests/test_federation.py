"""Steps 4-7: the relay, incoming events, following, interactions, DMs."""

from __future__ import annotations

import itertools
import json
import time

import pytest

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
    assert any("mentioned you" in b for b in notifications(client, homeserver))
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


def test_outgoing_reaction_and_undo(client, homeserver, relay_pool) -> None:
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    reaction = homeserver._store(remote.room_id, "m.reaction",
                                 {"m.relates_to": {"rel_type": "m.annotation", "event_id": mirrored["event_id"], "key": "👍🏽"}},
                                 "@q:example.org")
    transact(client, reaction)
    [like] = [e for e, _ in relay_pool.published if e.kind == 7]
    assert like.pubkey == q.pubkey and like.content == "+"
    assert [list(t) for t in like.tags] == [["e", raw["id"], "wss://nostr.example.org", alice.pubkey], ["p", alice.pubkey], ["k", "1"]]
    redaction = homeserver._store(remote.room_id, "m.room.redaction", {"redacts": reaction["event_id"]}, "@q:example.org")
    redaction["redacts"] = reaction["event_id"]
    transact(client, redaction)
    [undo] = [e for e, _ in relay_pool.published if e.kind == 5]
    assert [list(t) for t in undo.tags] == [["e", like.id], ["k", "7"]]


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
