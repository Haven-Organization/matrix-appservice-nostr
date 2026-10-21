"""Step 9: polls, pins, long-form, quotes, Blossom uploads, NIP-86, shared block lists."""

from __future__ import annotations

import base64
import hashlib
import json
import time

from bridge.inbound import ingest
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19
from tests.test_federation import Stranger, _followed_post, command, create_id, post, run, transact

RELAY_HTTP = "https://nostr.example.org"


def nostr_auth(secret: bytes, kind: int, tags: list[list[str]], created_at: int | None = None) -> str:
    event = nostr_event.build(secret, kind=kind, content="", tags=tags, created_at=created_at or int(time.time()))
    return "Nostr " + base64.b64encode(json.dumps(event.to_dict()).encode()).decode()


def id_secret(client, nostr_id) -> bytes:
    return run(client, client.app.state.signer.secret_key, nostr_id.pubkey)


# -- polls --------------------------------------------------------------------------


def test_outgoing_poll_and_remote_vote(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    poll = homeserver._store(q.profile_room_id, "org.matrix.msc3381.poll.start", {
        "org.matrix.msc3381.poll.start": {
            "question": {"org.matrix.msc1767.text": "Tea or coffee?"},
            "kind": "org.matrix.msc3381.poll.disclosed",
            "max_selections": 1,
            "answers": [{"id": "a", "org.matrix.msc1767.text": "Tea"}, {"id": "b", "org.matrix.msc1767.text": "Coffee"}],
        },
    }, "@q:example.org")
    transact(client, poll)
    [published] = relay_pool.of_kind(1068)
    assert published.content == "Tea or coffee?"
    options = [list(t) for t in published.tags if t[0] == "option"]
    assert [o[2] for o in options] == ["Tea", "Coffee"]
    assert ["relay", "wss://nostr.example.org"] in [list(t) for t in published.tags]

    # A Nostr vote on it shows up as a Matrix vote from the voter's ghost.
    voter = Stranger()
    vote = voter.event(1018, "", [["e", published.id], ["response", options[1][1]]])
    assert run(client, ingest, client.app, vote, origin="client").stored
    [response] = homeserver.messages(q.profile_room_id, "org.matrix.msc3381.poll.response")
    assert response["content"]["org.matrix.msc3381.poll.response"]["answers"] == [options[1][1]]
    assert response["content"]["m.relates_to"]["event_id"] == poll["event_id"]


def test_incoming_poll_and_local_vote(client, homeserver, relay_pool) -> None:
    q, alice, _, remote, _ = _followed_post(client, homeserver, relay_pool)
    raw = alice.event(1068, "Best editor?", [["option", "x1", "vim"], ["option", "x2", "emacs"],
                                             ["relay", "wss://alice.polls"]])
    assert run(client, ingest, client.app, raw, origin="subscription").stored
    [shown] = homeserver.messages(remote.room_id, "org.matrix.msc3381.poll.start")
    start = shown["content"]["org.matrix.msc3381.poll.start"]
    assert [a["id"] for a in start["answers"]] == ["x1", "x2"] and start["max_selections"] == 1
    assert shown["content"]["external_url"].startswith("https://ditto.pub/")

    vote = homeserver._store(remote.room_id, "org.matrix.msc3381.poll.response", {
        "m.relates_to": {"rel_type": "m.reference", "event_id": shown["event_id"]},
        "org.matrix.msc3381.poll.response": {"answers": ["x2"]},
    }, "@q:example.org")
    transact(client, vote)
    [(sent, relays)] = [(e, r) for e, r in relay_pool.published if e.kind == 1018]
    assert sent.pubkey == q.pubkey
    tags = [list(t) for t in sent.tags]
    assert ["e", raw["id"]] == tags[0][:2] and ["response", "x2"] in tags
    assert "wss://alice.polls" in relays


# -- pins ---------------------------------------------------------------------------


def test_pins_publish_a_pin_list(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    assert relay_pool.of_kind(10001) == []  # nothing pinned: nothing published
    message = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "pin me"})
    note = relay_pool.of_kind(1)[-1]
    pins = homeserver._store(q.profile_room_id, "m.room.pinned_events", {"pinned": [message["event_id"], "$unknown"]},
                             "@q:example.org", state_key="")
    transact(client, pins)
    pin_list = relay_pool.of_kind(10001)[-1]
    assert pin_list.pubkey == q.pubkey
    assert [list(t)[:2] for t in pin_list.tags] == [["e", note.id]]


# -- long-form and quotes -------------------------------------------------------------


def test_long_form_shows_summary_and_link(client, homeserver, relay_pool) -> None:
    q, alice, _, remote, _ = _followed_post(client, homeserver, relay_pool)
    raw = alice.event(30023, "# Big\n\n" + "words " * 2000,
                      [["d", "my-article"], ["title", "My Article"], ["summary", "It is about things."]])
    assert run(client, ingest, client.app, raw, origin="subscription").stored
    body = homeserver.messages(remote.room_id)[-1]["content"]["body"]
    assert "My Article" in body and "It is about things." in body
    assert "https://ditto.pub/naddr1" in body
    assert len(body) < 1000


def test_incoming_quote_gets_footer(client, homeserver, relay_pool) -> None:
    q, alice, first, remote, _ = _followed_post(client, homeserver, relay_pool)
    nevent = nip19.nevent_encode(nip19.EventPointer(first["id"], (), alice.pubkey))
    raw = alice.event(1, f"quoting myself nostr:{nevent}", [["q", first["id"], "", alice.pubkey]])
    assert run(client, ingest, client.app, raw, origin="subscription").stored
    content = homeserver.messages(remote.room_id)[-1]["content"]
    assert "alice says hi" in content["body"]


# -- Blossom uploads (BUD-02) -----------------------------------------------------------


def test_upload_by_local_id(client, homeserver) -> None:
    q = create_id(client)
    data = b"\x89PNG fake image bytes"
    sha = hashlib.sha256(data).hexdigest()
    auth = nostr_auth(id_secret(client, q), 24242, [["t", "upload"], ["x", sha], ["expiration", str(int(time.time()) + 300)]])
    response = client.put("/upload", content=data, headers={"Authorization": auth, "Content-Type": "image/png"})
    assert response.status_code == 200, response.text
    descriptor = response.json()
    assert descriptor["sha256"] == sha and descriptor["size"] == len(data) and descriptor["type"] == "image/png"
    assert descriptor["url"] == f"{RELAY_HTTP}/{sha}.png"
    fetched = client.get(f"/{sha}.png")
    assert fetched.status_code == 200 and fetched.content == data

    # Uploading the same file again doesn't store it twice.
    stored = len(homeserver.media)
    assert client.put("/upload", content=data, headers={"Authorization": auth, "Content-Type": "image/png"}).status_code == 200
    assert len(homeserver.media) == stored


def test_upload_refusals(client) -> None:
    q = create_id(client)
    secret = id_secret(client, q)
    future = str(int(time.time()) + 300)
    data = b"some bytes"

    def put(auth):
        return client.put("/upload", content=data, headers={"Authorization": auth} if auth else {})

    assert put(None).status_code == 401
    assert put(nostr_auth(secret, 24242, [["t", "delete"], ["expiration", future]])).status_code == 401
    assert put(nostr_auth(secret, 24242, [["t", "upload"], ["expiration", str(int(time.time()) - 5)]])).status_code == 401
    assert put(nostr_auth(secret, 24242, [["t", "upload"], ["expiration", future], ["x", "0" * 64]])).status_code == 403
    assert put(nostr_auth(Stranger().secret, 24242, [["t", "upload"], ["expiration", future]])).status_code == 403
    assert put(nostr_auth(secret, 27235, [["t", "upload"], ["expiration", future]])).status_code == 401


# -- NIP-86 relay management -----------------------------------------------------------------


def rpc(client, secret, method, params=(), *, url=RELAY_HTTP, tamper=False):
    body = json.dumps({"method": method, "params": list(params)}).encode()
    payload = hashlib.sha256(b"other" if tamper else body).hexdigest()
    auth = nostr_auth(secret, 27235, [["u", url], ["method", "POST"], ["payload", payload]])
    return client.post("/", content=body, headers={"Authorization": auth, "Content-Type": "application/nostr+json+rpc"})


def test_nip86_admin_can_ban(client, homeserver, relay_pool) -> None:
    admin = create_id(client, user="@admin:example.org", name="boss")
    secret = id_secret(client, admin)
    assert rpc(client, secret, "supportedmethods").json()["result"][0] == "supportedmethods"

    spammer = Stranger()
    assert rpc(client, secret, "banpubkey", [spammer.pubkey, "spam"]).json()["result"] is True
    listed = rpc(client, secret, "listbannedpubkeys").json()["result"]
    assert {"pubkey": spammer.pubkey, "reason": "spam"} in listed
    assert not run(client, ingest, client.app, spammer.event(1, "buy now"), origin="client").ok
    assert rpc(client, secret, "unbanpubkey", [spammer.pubkey]).json()["result"] is True
    assert spammer.pubkey not in [b["pubkey"] for b in rpc(client, secret, "listbannedpubkeys").json()["result"]]

    assert rpc(client, secret, "blockip", ["203.0.113.9"]).json()["result"] is True
    assert rpc(client, secret, "listblockedips").json()["result"][0]["ip"] == "203.0.113.9"
    assert "error" in rpc(client, secret, "nosuchmethod").json()


def test_nip86_banevent_redacts(client, homeserver, relay_pool) -> None:
    admin = create_id(client, user="@admin:example.org", name="boss")
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    assert rpc(client, id_secret(client, admin), "banevent", [raw["id"]]).json()["result"] is True
    assert run(client, client.app.state.repository.get_event, raw["id"]) is None
    assert any(event_id == mirrored["event_id"] for _, event_id, _ in homeserver.redactions)


def test_nip86_refusals(client) -> None:
    admin = create_id(client, user="@admin:example.org", name="boss")
    q = create_id(client)
    secret = id_secret(client, admin)
    assert rpc(client, id_secret(client, q), "supportedmethods").status_code == 403
    assert rpc(client, Stranger().secret, "supportedmethods").status_code == 403
    assert rpc(client, secret, "supportedmethods", url="https://elsewhere.example").status_code == 401
    assert rpc(client, secret, "supportedmethods", tamper=True).status_code == 401
    body = json.dumps({"method": "supportedmethods", "params": []})
    assert client.post("/", content=body, headers={"Content-Type": "application/json"}).status_code == 415


# -- shared block lists ------------------------------------------------------------------


def test_shared_block_lists(repository, homeserver, relay_pool) -> None:
    from bridge.config import parse_config
    from tests.conftest import make_client, raw_config

    curator, muted, reported, friend = Stranger(), Stranger(), Stranger(), Stranger()
    config = parse_config(raw_config(nostr={"shared_block_lists": [nip19.npub_encode(curator.pubkey)]}))
    relay_pool.network += [
        curator.event(10000, "", [["p", muted.pubkey], ["p", friend.pubkey]]),
        curator.event(1984, "", [["p", reported.pubkey, "spam"]]),
        friend.event(0, json.dumps({"name": "Friend"}), created_at=1000),
    ]
    with make_client(config, repository, homeserver, relay_pool) as client:
        run(client, client.app.state.feeds.refresh_shared_blocks)
        assert client.app.state.shared_blocks == {muted.pubkey, reported.pubkey, friend.pubkey}
        q = create_id(client)
        message = post(client, homeserver, q.profile_room_id, {"msgtype": "m.text", "body": "hello"})
        note = relay_pool.of_kind(1)[-1]
        reply = lambda who: who.event(1, "reply", [["e", note.id, "", "root"], ["p", q.pubkey]])  # noqa: E731
        assert not run(client, ingest, client.app, reply(muted), origin="client").ok
        assert not run(client, ingest, client.app, reply(reported), origin="client").ok
        # Someone here follows them: their posts still come through.
        command(client, f"[follow {friend.pubkey}")
        assert run(client, ingest, client.app, reply(friend), origin="client").ok
        assert message


# -- zaps (NIP-57) -------------------------------------------------------------------


class _LnurlFetcher:
    def __init__(self, routes):
        self.routes = routes

    async def get(self, url, **kwargs):
        from bridge.safe_fetch import Fetched

        body = self.routes.get(url)
        return Fetched(url, 200 if body is not None else 404, json.dumps(body or {}).encode(), "application/json")

    async def aclose(self):
        pass


def test_zap_receipts_must_come_from_the_ids_provider(client, homeserver, relay_pool) -> None:
    q = create_id(client)
    provider, zapper, forger = Stranger(), Stranger(), Stranger()
    homeserver._store(q.profile_room_id, "software.haven.nostr.profile_extra", {"lud16": "q@wallet.example"},
                      "@q:example.org", state_key="")
    client.app.state.fetcher = _LnurlFetcher({
        "https://wallet.example/.well-known/lnurlp/q": {"allowsNostr": True, "nostrPubkey": provider.pubkey},
    })
    request = zapper.event(9734, "great post", [["p", q.pubkey], ["amount", "21000"]])

    def receipt(signer, description=json.dumps(request)):
        return signer.event(9735, "", [["p", q.pubkey], ["bolt11", "lnbc..."], ["description", description]])

    assert not run(client, ingest, client.app, receipt(forger), origin="client").ok
    assert not run(client, ingest, client.app, receipt(provider, json.dumps({**request, "content": "tampered"})), origin="client").ok
    assert run(client, ingest, client.app, receipt(provider), origin="client").stored
    room = run(client, client.app.state.repository.get_user_rooms, "@q:example.org").notifications_room_id
    assert "21" in homeserver.sent_to(room)[-1]["body"]


def test_banned_event_stays_out(client, homeserver, relay_pool) -> None:
    admin = create_id(client, user="@admin:example.org", name="boss")
    q, alice, raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    secret = id_secret(client, admin)
    assert rpc(client, secret, "banevent", [raw["id"], "nope"]).json()["result"] is True
    assert rpc(client, secret, "listbannedevents").json()["result"] == [{"id": raw["id"], "reason": "nope"}]
    client.app.state.seen = type(client.app.state.seen)()  # forget it was ever seen
    assert not run(client, ingest, client.app, raw, origin="client").ok
    assert "error" in rpc(client, secret, "banpubkey", ["npub-not-hex"]).json()
    assert "error" in rpc(client, secret, "blockip", ["not an ip"]).json()
