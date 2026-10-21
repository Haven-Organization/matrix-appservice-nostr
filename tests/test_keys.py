"""Step 8: the widget, bringing your own key, remote signers, and the
pasted-secret guard."""

from __future__ import annotations

import asyncio
import itertools
import json
import time

import pytest

from bridge.nostr import event as nostr_event
from bridge.nostr import nip19, nip44, nip49, nip59, secp256k1
from tests.conftest import HS_TOKEN, settle

AUTH = {"Authorization": f"Bearer {HS_TOKEN}"}
_n = itertools.count(1)


def transact(client, *events):
    assert client.put(f"/_matrix/app/v1/transactions/k{next(_n)}", json={"events": list(events)}, headers=AUTH).status_code == 200
    settle(client)


def run(client, fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)

    return client.portal.call(call)


def command(client, body, sender="@q:example.org", room="!dm:example.org"):
    transact(client, {"type": "m.room.message", "event_id": f"$k{next(_n)}", "room_id": room, "sender": sender,
                      "content": {"msgtype": "m.text", "body": body}})


def widget(client, homeserver, user="@q:example.org", room="!dm:example.org"):
    """A signed-in widget session for ``user`` in ``room``."""
    homeserver.openid_tokens["tok"] = user
    homeserver._room(room)["members"].add(user)
    homeserver._store(room, "m.room.member", {"membership": "join"}, user, user)
    session = client.post("/widget/api/session", json={"access_token": "tok", "matrix_server_name": "example.org"}).json()
    headers = {"Authorization": f"Bearer {session['session_id']}"}

    def call(path, body=None):
        if body is None:
            return client.get(path, params={"room_id": room}, headers=headers)
        response = client.post(path, json={"room_id": room, **body}, headers=headers)
        settle(client)
        return response

    return call


def ids_of(client, user="@q:example.org"):
    return run(client, client.app.state.repository.list_ids_for_user, user)


# -- the widget -----------------------------------------------------------------------


def test_widget_needs_a_verified_session(client, homeserver) -> None:
    assert client.post("/widget/api/session", json={"access_token": "bad", "matrix_server_name": "example.org"}).status_code == 403
    assert client.get("/widget/api/context", params={"room_id": "!x"}).status_code == 401
    call = widget(client, homeserver)
    assert call("/widget/api/context").json()["ids"] == []
    # Not a member of that room: refused.
    response = client.get("/widget/api/context", params={"room_id": "!elsewhere:example.org"},
                          headers={"Authorization": call.__closure__ and client.post("/widget/api/session",
                                   json={"access_token": "tok", "matrix_server_name": "example.org"}).json() and "Bearer x"})
    assert response.status_code == 401
    assert "Nostr Bridge" in client.get("/widget").text


def test_widget_create_and_switch(client, homeserver) -> None:
    call = widget(client, homeserver)
    call("/widget/api/create", {"name": ""})
    call("/widget/api/create", {"name": "qbot"})
    context = call("/widget/api/context").json()
    assert [i["handle"] for i in context["ids"]] == ["q@example.org", "qbot@example.org"]
    assert context["ids"][0]["active"] and context["ids"][0]["key"] == "held by the bridge"
    call("/widget/api/switch", {"id": "2"})
    assert call("/widget/api/context").json()["ids"][1]["active"]


# -- importing and exporting keys ----------------------------------------------------


def test_import_nsec_as_new_id_keeps_profile(client, homeserver, relay_pool) -> None:
    secret = secp256k1.generate_secret_key()
    pubkey = secp256k1.public_key(secret).hex()
    relay_pool.network.append(nostr_event.build(secret, kind=0, created_at=1000, content=json.dumps(
        {"name": "Old Me", "about": "been here a while", "lud16": "me@getalby.com", "website": "https://me.example"})).to_dict())
    call = widget(client, homeserver)
    response = call("/widget/api/key/import", {"mode": "new", "name": "oldme", "key": nip19.nsec_encode(secret),
                                               "import_follows": False, "import_posts": 0})
    assert response.status_code == 200, response.text
    [created] = ids_of(client)
    assert created.pubkey == pubkey and created.key_origin == "imported"
    assert homeserver.state_of(created.profile_room_id, "m.room.name") == {"name": "Old Me"}
    profile = json.loads([e for e, _ in relay_pool.published if e.kind == 0 and e.pubkey == pubkey][-1].content)
    assert profile == {"name": "Old Me", "display_name": "Old Me", "about": "been here a while",
                       "website": "https://me.example", "lud16": "me@getalby.com", "nip05": "oldme@example.org"}
    # The same key can't make a second ID.
    again = call("/widget/api/key/import", {"mode": "new", "name": "other", "key": nip19.nsec_encode(secret)})
    assert again.status_code == 400 and "already belongs" in again.json()["detail"]


def test_import_ncryptsec_and_replace_key(client, homeserver, relay_pool) -> None:
    command(client, "[create profile")
    [q] = ids_of(client)
    secret = secp256k1.generate_secret_key()
    protected = nip49.encrypt(secret, "hunter2", log_n=16)
    call = widget(client, homeserver)
    unconfirmed = call("/widget/api/key/import", {"mode": "replace", "id": "1", "key": protected, "password": "hunter2"})
    assert unconfirmed.status_code == 400 and 'Type "q"' in unconfirmed.json()["detail"]
    wrong = call("/widget/api/key/import", {"mode": "replace", "id": "1", "key": protected, "password": "nope", "confirm": "q"})
    assert wrong.status_code == 400
    done = call("/widget/api/key/import", {"mode": "replace", "id": "1", "key": protected, "password": "hunter2", "confirm": "q"})
    assert done.status_code == 200
    [replaced] = ids_of(client)
    new_pubkey = secp256k1.public_key(secret).hex()
    assert replaced.id == q.id and replaced.pubkey == new_pubkey
    assert client.get("/.well-known/nostr.json", params={"name": "q"}).json()["names"] == {"q": new_pubkey}
    assert any(e.kind == 0 and e.pubkey == new_pubkey for e, _ in relay_pool.published)


def test_costly_ncryptsec_refused(client, homeserver) -> None:
    secret = secp256k1.generate_secret_key()
    hrp, data = nip19.bech32_decode(nip49.encrypt(secret, "x", log_n=16))
    tampered = nip19.bech32_encode(hrp, data[:1] + bytes([22]) + data[2:])
    call = widget(client, homeserver)
    response = call("/widget/api/key/import", {"mode": "new", "name": "x", "key": tampered, "password": "x"})
    assert response.status_code == 400 and "stronger than this server allows" in response.json()["detail"]


def test_export(client, homeserver) -> None:
    command(client, "[create profile")
    call = widget(client, homeserver)
    plain = call("/widget/api/key/export", {"id": "1"}).json()
    secret = nip19.nsec_decode(plain["key"])
    [q] = ids_of(client)
    assert secp256k1.public_key(secret).hex() == q.pubkey
    protected = call("/widget/api/key/export", {"id": "1", "password": "pw"}).json()
    assert protected["encrypted"] and nip49.decrypt(protected["key"], "pw") == secret


def test_pasted_nsec_is_redacted_and_never_published(client, homeserver, relay_pool) -> None:
    command(client, "[create profile")
    [q] = ids_of(client)
    leaked = nip19.nsec_encode(secp256k1.generate_secret_key())
    message = homeserver._store(q.profile_room_id, "m.room.message", {"msgtype": "m.text", "body": f"my key is {leaked}"}, "@q:example.org")
    before = len(relay_pool.published)
    transact(client, message)
    assert (q.profile_room_id, message["event_id"], "@nostrbot:example.org") in homeserver.redactions
    assert "secret key" in homeserver.sent_to(q.profile_room_id)[-1]["body"]
    assert len(relay_pool.published) == before
    command(client, "[import key nsec1abc")
    assert "widget" in homeserver.sent_to("!dm:example.org")[-1]["body"]


# -- remote signers (NIP-46) -----------------------------------------------------------


class FakeSignerApp:
    """A NIP-46 signer app on a fake relay: holds the user's key, answers
    requests from the bridge's client key."""

    RELAY = "wss://signer.relay"

    def __init__(self, pool):
        self.pool = pool
        self.signer_secret = secp256k1.generate_secret_key()
        self.signer_pubkey = secp256k1.public_key(self.signer_secret).hex()
        self.user_secret = secp256k1.generate_secret_key()
        self.user_pubkey = secp256k1.public_key(self.user_secret).hex()
        self.requests: list[str] = []
        pool.publish_hooks.append(self._on_publish)

    @property
    def uri(self):
        return f"bunker://{self.signer_pubkey}?relay={self.RELAY}&secret=s3cret"

    def _on_publish(self, event, relays):
        if event.kind != 24133 or self.RELAY not in relays:
            return
        key = nip44.get_conversation_key(self.signer_secret, event.pubkey)
        request = json.loads(nip44.decrypt(event.content, key))
        self.requests.append(request["method"])
        params = request["params"]
        if request["method"] == "connect":
            result = "ack" if params[1:2] == ["s3cret"] else None
        elif request["method"] == "get_public_key":
            result = self.user_pubkey
        elif request["method"] == "sign_event":
            unsigned = json.loads(params[0])
            result = json.dumps(nostr_event.build(self.user_secret, kind=unsigned["kind"], content=unsigned["content"],
                                                  created_at=unsigned["created_at"], tags=unsigned["tags"]).to_dict())
        elif request["method"] == "nip44_encrypt":
            result = nip44.encrypt(params[1], nip44.get_conversation_key(self.user_secret, params[0]))
        elif request["method"] == "nip44_decrypt":
            result = nip44.decrypt(params[1], nip44.get_conversation_key(self.user_secret, params[0]))
        else:
            result = None
        response = {"id": request["id"], "result": result} if result is not None else {"id": request["id"], "error": "no"}
        reply = nostr_event.build(self.signer_secret, kind=24133, created_at=int(time.time()), tags=[["p", event.pubkey]],
                                  content=nip44.encrypt(json.dumps(response), key)).to_dict()
        asyncio.get_running_loop().call_soon(self.pool.deliver, self.RELAY, reply)


def test_remote_signer_id_posts_and_dms(client, homeserver, relay_pool) -> None:
    signer = FakeSignerApp(relay_pool)
    call = widget(client, homeserver)
    response = call("/widget/api/key/import", {"mode": "new", "name": "remote", "uri": signer.uri})
    assert response.status_code == 200, response.text
    [remote_id] = ids_of(client)
    assert remote_id.pubkey == signer.user_pubkey and remote_id.key_origin == "bunker" and remote_id.secret_key is None
    assert "sign_event" in signer.requests  # its identity events were signed remotely
    assert call("/widget/api/key/export", {"id": "1"}).status_code == 400

    # Posting signs through the signer.
    message = homeserver._store(remote_id.profile_room_id, "m.room.message", {"msgtype": "m.text", "body": "signed elsewhere"}, "@q:example.org")
    transact(client, message)
    note = [e for e, _ in relay_pool.published if e.kind == 1][-1]
    assert note.pubkey == signer.user_pubkey and nostr_event.verify(note.to_dict())

    # An incoming DM is opened by the signer.
    stranger = secp256k1.generate_secret_key()
    rumor = nip59.make_rumor(secp256k1.public_key(stranger).hex(), 14, "hi remote", [["p", signer.user_pubkey]], int(time.time()))
    wrap = nip59.wrap_message(rumor, stranger, signer.user_pubkey).to_dict()
    from bridge.inbound import ingest

    assert run(client, ingest, client.app, wrap, origin="subscription").stored
    assert "nip44_decrypt" in signer.requests


def test_signer_offline_tells_the_user(client, homeserver, relay_pool) -> None:
    signer = FakeSignerApp(relay_pool)
    call = widget(client, homeserver)
    call("/widget/api/key/import", {"mode": "new", "name": "remote", "uri": signer.uri})
    [remote_id] = ids_of(client)
    relay_pool.publish_hooks.clear()  # the signer goes offline
    from bridge.nostr import nip46

    signer_client = run(client, client.app.state.signer.remote_signer, remote_id)
    original = nip46.RemoteSigner.request

    async def quick(self, method, params, *, timeout=30.0):
        return await original(self, method, params, timeout=0.2)

    nip46.RemoteSigner.request = quick
    try:
        message = homeserver._store(remote_id.profile_room_id, "m.room.message", {"msgtype": "m.text", "body": "anyone?"}, "@q:example.org")
        transact(client, message)
    finally:
        nip46.RemoteSigner.request = original
    assert "remote signer didn't answer" in homeserver.sent_to(remote_id.profile_room_id)[-1]["body"]
    assert signer_client is not None
