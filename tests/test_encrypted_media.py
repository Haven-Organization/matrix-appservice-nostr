"""Files forwarded from end-to-end encrypted rooms: one question, one
decrypted copy, shared with the sibling bridges (bridge.encrypted_media)."""

from __future__ import annotations

import base64
import hashlib
import io
import os

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from bridge.config import EncryptedMediaSection
from bridge.confirm import is_confirm_reaction
from bridge.encrypted_media import ANNOUNCE_TYPE, PROMPT_FIELD
from tests.conftest import make_client
from tests.test_posts import create_id, notes, post, transact

AP = "@fediverse:example.org"
NOSTR = "@nostrbot:example.org"  # the tests' bridge bot


def _b64(data: bytes, *, urlsafe: bool = False) -> str:
    return (base64.urlsafe_b64encode(data) if urlsafe else base64.b64encode(data)).decode().rstrip("=")


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _encrypted(homeserver, plaintext: bytes, name: str = "enc") -> dict:
    """An m.image whose file is encrypted as Matrix clients do it."""
    key, iv = os.urandom(32), os.urandom(8) + b"\0" * 8
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    mxc = f"mxc://example.org/{name}"
    homeserver.media[mxc] = (ciphertext, "application/octet-stream")
    return {"msgtype": "m.image", "body": "image.png", "info": {"mimetype": "image/png", "w": 16, "h": 16},
            "file": {"url": mxc, "key": {"k": _b64(key, urlsafe=True), "alg": "A256CTR", "kty": "oct"},
                     "iv": _b64(iv), "hashes": {"sha256": _b64(hashlib.sha256(ciphertext).digest())}, "v": "v2"}}


def _prompts(homeserver, room):
    return [e for e in homeserver.messages(room) if PROMPT_FIELD in e["content"]]


def _react(client, homeserver, room, target, key, sender="@q:example.org"):
    transact(client, homeserver._store(room, "m.reaction", {
        "m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": key}}, sender))


def _reply(client, homeserver, room, target, body="confirm", sender="@q:example.org"):
    post(client, homeserver, room, {"msgtype": "m.text", "body": body,
                                    "m.relates_to": {"m.in_reply_to": {"event_id": target}}}, sender=sender)


def _with_bridges(config, bridges):
    object.__setattr__(config, "encrypted_media", EncryptedMediaSection(bridges=tuple(bridges), wait_seconds=0))


def test_confirm_reactions() -> None:
    for key in ("👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿", "👍\ufe0f"):
        assert is_confirm_reaction(key), key
    for key in ("✅", "✔️", "☑️", "👎", "❤️", "🔁", "confirm"):
        assert not is_confirm_reaction(key), key


@pytest.mark.parametrize("how", ["reply", "👍", "👍🏽"])
def test_asked_alone_then_confirmed(client, homeserver, relay_pool, how) -> None:
    q = create_id(client)
    room = q.profile_room_id
    picture = _png()
    message = post(client, homeserver, room, _encrypted(homeserver, picture))
    assert notes(relay_pool) == []
    [prompt] = _prompts(homeserver, room)
    assert prompt["content"][PROMPT_FIELD] == {"prompt_for": message["event_id"]}
    assert prompt["content"]["m.relates_to"]["event_id"] == message["event_id"]
    assert "this message is encrypted" in prompt["content"]["body"]
    assert not any("wasn't published" in c.get("body", "") for c in homeserver.sent_to(room))

    # Someone else can't agree for them.
    _reply(client, homeserver, room, prompt["event_id"], sender="@mallory:example.org")
    _react(client, homeserver, room, prompt["event_id"], "👍", sender="@mallory:example.org")
    assert notes(relay_pool) == []

    if how == "reply":
        _reply(client, homeserver, room, prompt["event_id"])
    else:
        _react(client, homeserver, room, prompt["event_id"], how)
    [note] = notes(relay_pool)
    assert note.pubkey == q.pubkey
    copies = [mxc for mxc, (data, _) in homeserver.media.items() if data == picture]
    assert len(copies) == 1  # decrypted once
    assert homeserver.sent_to(room)[-1]["body"] == "✅ Sent."
    # Alone, nothing is announced.
    assert homeserver.messages(room, ANNOUNCE_TYPE) == []

    # The same file forwarded again: no question, the same copy.
    again = dict(_encrypted(homeserver, picture, "unused"))
    again["file"] = message["content"]["file"]
    post(client, homeserver, room, again)
    assert len(notes(relay_pool)) == 2 and len(_prompts(homeserver, room)) == 1
    assert len([1 for data, _ in homeserver.media.values() if data == picture]) == 1


def test_a_reaction_doesnt_confirm_anything_drastic(client, homeserver) -> None:
    q = create_id(client)
    transact(client, homeserver._store("!dm:example.org", "m.room.message", {"msgtype": "m.text", "body": "[delete profile"},
                                       "@q:example.org"))
    warning = homeserver.messages("!dm:example.org")[-1]
    _react(client, homeserver, "!dm:example.org", warning["event_id"], "👍")
    assert client.portal.call(client.app.state.repository.list_ids_for_user, "@q:example.org")[0].id == q.id


def _shared_client(config, repository, homeserver, relay_pool, bridges):
    _with_bridges(config, bridges)
    return make_client(config, repository, homeserver, relay_pool)


def test_waits_for_the_bridge_before_it(config, repository, homeserver, relay_pool) -> None:
    with _shared_client(config, repository, homeserver, relay_pool, [AP, NOSTR]) as client:
        q = create_id(client)
        room = q.profile_room_id
        picture = _png()
        content = _encrypted(homeserver, picture)
        # The ActivityPub bridge (first in line) asks as soon as it sees it.
        message = homeserver._store(room, "m.room.message", content, "@q:example.org")
        homeserver._store(room, "m.room.message", {
            "msgtype": "m.text", "body": "⚠️q, this message is encrypted. ...", PROMPT_FIELD: {"prompt_for": message["event_id"]},
            "m.relates_to": {"rel_type": "m.thread", "event_id": message["event_id"], "is_falling_back": True}}, AP)
        transact(client, message)
        assert _prompts(homeserver, room) and all(p["sender"] == AP for p in _prompts(homeserver, room))
        assert notes(relay_pool) == []

        # Answering the other bridge's question: not published here.
        [question] = _prompts(homeserver, room)
        _reply(client, homeserver, room, question["event_id"])
        _react(client, homeserver, room, question["event_id"], "👍")
        assert notes(relay_pool) == [] and relay_pool.of_kind(7) == []

        # A copy that isn't the file: ignored.
        homeserver.media["mxc://example.org/wrong"] = (b"not the picture", "image/png")
        transact(client, homeserver._store(room, ANNOUNCE_TYPE, {
            "m.relates_to": {"rel_type": ANNOUNCE_TYPE, "event_id": message["event_id"]},
            "url": "mxc://example.org/wrong", "encrypted_url": content["file"]["url"], "info": {"mimetype": "image/png"}}, AP))
        assert notes(relay_pool) == []

        # Confirmed there: its copy, checked, is published here.
        homeserver.media["mxc://example.org/plain"] = (picture, "image/png")
        transact(client, homeserver._store(room, ANNOUNCE_TYPE, {
            "m.relates_to": {"rel_type": ANNOUNCE_TYPE, "event_id": message["event_id"]},
            "url": "mxc://example.org/plain", "encrypted_url": content["file"]["url"], "info": {"mimetype": "image/png"}}, AP))
        [note] = notes(relay_pool)
        assert note.pubkey == q.pubkey
        assert [mxc for mxc, (data, _) in homeserver.media.items() if data == picture] == ["mxc://example.org/plain"]


def test_asks_when_no_bridge_before_it_did(config, repository, homeserver, relay_pool) -> None:
    with _shared_client(config, repository, homeserver, relay_pool, [AP, NOSTR]) as client:
        q = create_id(client)
        room = q.profile_room_id
        message = post(client, homeserver, room, _encrypted(homeserver, _png()))
        [prompt] = _prompts(homeserver, room)
        assert prompt["sender"] == NOSTR
        _reply(client, homeserver, room, prompt["event_id"])
        assert len(notes(relay_pool)) == 1
        # Shared with the others.
        [announced] = homeserver.messages(room, ANNOUNCE_TYPE)
        assert announced["content"]["m.relates_to"] == {"rel_type": ANNOUNCE_TYPE, "event_id": message["event_id"]}
        assert announced["content"]["encrypted_url"] == message["content"]["file"]["url"]
        assert homeserver.media[announced["content"]["url"]][0] == _png()


def test_a_known_copy_is_announced_for_a_new_post(config, repository, homeserver, relay_pool) -> None:
    with _shared_client(config, repository, homeserver, relay_pool, [AP, NOSTR]) as client:
        q = create_id(client)
        room = q.profile_room_id
        first = post(client, homeserver, room, _encrypted(homeserver, _png()))
        _reply(client, homeserver, room, _prompts(homeserver, room)[0]["event_id"])
        forwarded = homeserver._store(room, "m.room.message", {**first["content"]}, "@q:example.org")
        transact(client, forwarded)
        assert len(notes(relay_pool)) == 2
        announced = [e["content"]["m.relates_to"]["event_id"] for e in homeserver.messages(room, ANNOUNCE_TYPE)]
        assert announced == [first["event_id"], forwarded["event_id"]]
