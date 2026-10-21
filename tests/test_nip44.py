"""NIP-44 v2 against the official vectors, plus NIP-04, NIP-49 (and its
XChaCha20-Poly1305) and NIP-59 gift wraps."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from bridge.nostr import nip04, nip44, nip49, nip59, secp256k1, xchacha
from bridge.nostr import event as nostr_event

VECTORS = json.loads((Path(__file__).parent / "vectors" / "nip44.vectors.json").read_text())["v2"]
VALID = VECTORS["valid"]
INVALID = VECTORS["invalid"]


# -- NIP-44 ----------------------------------------------------------------------


@pytest.mark.parametrize("case", VALID["get_conversation_key"], ids=lambda c: c["conversation_key"][:12])
def test_conversation_key(case) -> None:
    key = nip44.get_conversation_key(bytes.fromhex(case["sec1"]), case["pub2"])
    assert key.hex() == case["conversation_key"]


@pytest.mark.parametrize("case", VALID["get_message_keys"]["keys"], ids=lambda c: c["nonce"][:12])
def test_message_keys(case) -> None:
    conversation_key = bytes.fromhex(VALID["get_message_keys"]["conversation_key"])
    chacha_key, chacha_nonce, hmac_key = nip44.get_message_keys(conversation_key, bytes.fromhex(case["nonce"]))
    assert (chacha_key.hex(), chacha_nonce.hex(), hmac_key.hex()) == (
        case["chacha_key"],
        case["chacha_nonce"],
        case["hmac_key"],
    )


@pytest.mark.parametrize(("length", "padded"), VALID["calc_padded_len"])
def test_padded_len(length, padded) -> None:
    assert nip44.calc_padded_len(length) == padded


@pytest.mark.parametrize("case", VALID["encrypt_decrypt"], ids=lambda c: c["plaintext"][:12])
def test_encrypt_decrypt(case) -> None:
    sec1, sec2 = bytes.fromhex(case["sec1"]), bytes.fromhex(case["sec2"])
    key = nip44.get_conversation_key(sec1, secp256k1.public_key(sec2).hex())
    assert key.hex() == case["conversation_key"]
    # Symmetric: the other side derives the same key.
    assert nip44.get_conversation_key(sec2, secp256k1.public_key(sec1).hex()) == key
    payload = nip44.encrypt(case["plaintext"], key, bytes.fromhex(case["nonce"]))
    assert payload == case["payload"]
    assert nip44.decrypt(payload, key) == case["plaintext"]


@pytest.mark.parametrize("case", VALID["encrypt_decrypt_long_msg"], ids=lambda c: f"{c['pattern']}x{c['repeat']}")
def test_long_messages(case) -> None:
    plaintext = case["pattern"] * case["repeat"]
    assert hashlib.sha256(plaintext.encode()).hexdigest() == case["plaintext_sha256"]
    key = bytes.fromhex(case["conversation_key"])
    payload = nip44.encrypt(plaintext, key, bytes.fromhex(case["nonce"]))
    assert hashlib.sha256(payload.encode()).hexdigest() == case["payload_sha256"]
    assert nip44.decrypt(payload, key) == plaintext


@pytest.mark.parametrize("length", INVALID["encrypt_msg_lengths"])
def test_invalid_message_lengths(length) -> None:
    with pytest.raises(nip44.Nip44Error):
        nip44.encrypt("a" * length, b"\x01" * 32)


@pytest.mark.parametrize("case", INVALID["get_conversation_key"], ids=lambda c: c["note"])
def test_invalid_conversation_keys(case) -> None:
    with pytest.raises(nip44.Nip44Error):
        nip44.get_conversation_key(bytes.fromhex(case["sec1"]), case["pub2"])


@pytest.mark.parametrize("case", INVALID["decrypt"], ids=lambda c: c["note"])
def test_invalid_decrypt(case) -> None:
    with pytest.raises(nip44.Nip44Error):
        nip44.decrypt(case["payload"], bytes.fromhex(case["conversation_key"]))


def test_random_nonce_roundtrip() -> None:
    a, b = secp256k1.generate_secret_key(), secp256k1.generate_secret_key()
    key = nip44.get_conversation_key(a, secp256k1.public_key(b).hex())
    first, second = nip44.encrypt("hello 🌍", key), nip44.encrypt("hello 🌍", key)
    assert first != second
    assert nip44.decrypt(first, key) == nip44.decrypt(second, key) == "hello 🌍"


# -- NIP-04 ----------------------------------------------------------------------


def test_nip04_between_two_keys() -> None:
    alice, bob = secp256k1.generate_secret_key(), secp256k1.generate_secret_key()
    alice_pub, bob_pub = secp256k1.public_key(alice).hex(), secp256k1.public_key(bob).hex()
    content = nip04.encrypt("legacy hello ✉️", alice, bob_pub)
    assert "?iv=" in content
    assert nip04.decrypt(content, bob, alice_pub) == "legacy hello ✉️"
    assert nip04.decrypt(content, alice, bob_pub) == "legacy hello ✉️"


@pytest.mark.parametrize("content", ["no-iv", "!!!?iv=AAAAAAAAAAAAAAAAAAAAAA==", "AAAA?iv=AAAA"])
def test_nip04_rejects_garbage(content) -> None:
    alice, bob = secp256k1.generate_secret_key(), secp256k1.generate_secret_key()
    with pytest.raises(nip04.Nip04Error):
        nip04.decrypt(content, alice, secp256k1.public_key(bob).hex())


def test_nip04_wrong_key_fails() -> None:
    alice, bob, eve = (secp256k1.generate_secret_key() for _ in range(3))
    content = nip04.encrypt("secret", alice, secp256k1.public_key(bob).hex())
    try:
        result = nip04.decrypt(content, eve, secp256k1.public_key(alice).hex())
    except nip04.Nip04Error:
        return
    assert result != "secret"  # CBC without a MAC may "decrypt" to garbage


# -- XChaCha20-Poly1305 (draft-irtf-cfrg-xchacha-03) -----------------------------


def test_hchacha20_vector() -> None:
    key = bytes(range(32))
    nonce = bytes.fromhex("000000090000004a0000000031415927")
    assert xchacha.hchacha20(key, nonce).hex() == (
        "82413b4227b27bfed30e42508a877d73a0f9e4d58a74a853c12ec41326d3ecdc"
    )


def test_xchacha20_poly1305_vector() -> None:
    plaintext = bytes.fromhex(
        "4c616469657320616e642047656e746c656d656e206f662074686520636c6173"
        "73206f66202739393a204966204920636f756c64206f6666657220796f75206f"
        "6e6c79206f6e652074697020666f7220746865206675747572652c2073756e73"
        "637265656e20776f756c642062652069742e"
    )
    aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
    key = bytes.fromhex("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
    iv = bytes.fromhex("404142434445464748494a4b4c4d4e4f5051525354555657")
    expected = bytes.fromhex(
        "bd6d179d3e83d43b9576579493c0e939572a1700252bfaccbed2902c21396cbb"
        "731c7f1b0b4aa6440bf3a82f4eda7e39ae64c6708c54c216cb96b72e1213b452"
        "2f8c9ba40db5d945b11b69b982c1bb9e3f3fac2bc369488f76b2383565d3fff9"
        "21f9664c97637da9768812f615c68b13b52e"
        "c0875924c1c7987947deafd8780acf49"
    )
    assert xchacha.encrypt(key, iv, plaintext, aad) == expected
    assert xchacha.decrypt(key, iv, expected, aad) == plaintext
    with pytest.raises(xchacha.XChaChaError):
        xchacha.decrypt(key, iv, expected[:-1] + b"\x00", aad)


# -- NIP-49 ----------------------------------------------------------------------

SPEC_NCRYPTSEC = (
    "ncryptsec1qgg9947rlpvqu76pj5ecreduf9jxhselq2nae2kghhvd5g7dgjtcxfqtd67p9m0w57lspw8gsq6yphnm8623nsl8xn9j4"
    "jdzz84zm3frztj3z7s35vpzmqf6ksu8r89qk5z2zxfmu5gv8th8wclt0h4p"
)


def test_nip49_spec_vector() -> None:
    assert nip49.decrypt(SPEC_NCRYPTSEC, "nostr").hex() == (
        "3501454135014541350145413501453fefb02227e449e57cf4d3a3ce05378683"
    )


def test_nip49_wrong_password_and_garbage() -> None:
    with pytest.raises(nip49.Nip49Error):
        nip49.decrypt(SPEC_NCRYPTSEC, "not nostr")
    with pytest.raises(nip49.Nip49Error):
        nip49.decrypt("ncryptsec1qqqqqq", "nostr")
    with pytest.raises(nip49.Nip49Error):
        nip49.decrypt("npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg", "nostr")


def test_nip49_roundtrip_with_normalized_password() -> None:
    secret = secp256k1.generate_secret_key()
    # The spec's normalization example: these two spellings are the same password.
    ncryptsec = nip49.encrypt(secret, "ÅΩẛ̣")
    assert ncryptsec.startswith("ncryptsec1")
    assert nip49.decrypt(ncryptsec, "ÅΩṩ") == secret


# -- NIP-59 ----------------------------------------------------------------------


def _keys():
    secret = secp256k1.generate_secret_key()
    return secret, secp256k1.public_key(secret).hex()


def test_gift_wrap_roundtrip() -> None:
    alice, alice_pub = _keys()
    bob, bob_pub = _keys()
    rumor = nip59.make_rumor(alice_pub, nip59.CHAT_MESSAGE_KIND, "hi bob 👋", [["p", bob_pub]], 1_761_000_000)
    wrap = nip59.wrap_message(rumor, alice, bob_pub)
    assert wrap.kind == nip59.GIFT_WRAP_KIND and wrap.pubkey not in (alice_pub, bob_pub)
    assert [list(t) for t in wrap.tags] == [["p", bob_pub]]
    assert wrap.created_at <= int(time.time())

    opened = nip59.unwrap(wrap.to_dict(), bob)
    assert opened.sender == alice_pub
    assert opened.rumor == rumor and "sig" not in opened.rumor
    assert opened.seal["kind"] == nip59.SEAL_KIND and opened.seal["tags"] == []

    # The sender's own copy opens with the sender's key.
    own_copy = nip59.wrap_message(rumor, alice, alice_pub)
    assert nip59.unwrap(own_copy.to_dict(), alice).rumor == rumor


def test_gift_wrap_wrong_recipient() -> None:
    alice, alice_pub = _keys()
    _bob, bob_pub = _keys()
    eve, _ = _keys()
    wrap = nip59.wrap_message(nip59.make_rumor(alice_pub, 14, "for bob", [], 1), alice, bob_pub)
    with pytest.raises(nip59.UnwrapError):
        nip59.unwrap(wrap.to_dict(), eve)


def test_gift_wrap_tampering() -> None:
    alice, alice_pub = _keys()
    bob, bob_pub = _keys()
    raw = nip59.wrap_message(nip59.make_rumor(alice_pub, 14, "x", [], 1), alice, bob_pub).to_dict()
    raw["content"] = raw["content"][:-4] + "AAAA"
    with pytest.raises(nip59.UnwrapError):
        nip59.unwrap(raw, bob)


def test_rumor_pubkey_spoof_rejected() -> None:
    """Eve seals a rumor claiming to be from Alice: the seal's signer gives
    her away."""
    _alice, alice_pub = _keys()
    eve, _eve_pub = _keys()
    bob, bob_pub = _keys()
    forged = nip59.make_rumor(alice_pub, 14, "it's me, alice", [], 1)
    # seal() refuses outright; build the forgery by hand.
    with pytest.raises(ValueError):
        nip59.seal(forged, eve, bob_pub)
    content = nip44.encrypt(json.dumps(forged), nip44.get_conversation_key(eve, bob_pub))
    bad_seal = nostr_event.build(eve, kind=nip59.SEAL_KIND, content=content, created_at=1, tags=[])
    wrap = nip59.gift_wrap(bad_seal, bob_pub)
    with pytest.raises(nip59.UnwrapError, match="seal's signer"):
        nip59.unwrap(wrap.to_dict(), bob)


def test_rumor_id_checked() -> None:
    alice, alice_pub = _keys()
    bob, bob_pub = _keys()
    rumor = nip59.make_rumor(alice_pub, 14, "original", [], 1)
    rumor["content"] = "changed after the id was computed"
    content = nip44.encrypt(json.dumps(rumor), nip44.get_conversation_key(alice, bob_pub))
    bad_seal = nostr_event.build(alice, kind=nip59.SEAL_KIND, content=content, created_at=1, tags=[])
    with pytest.raises(nip59.UnwrapError, match="id"):
        nip59.unwrap(nip59.gift_wrap(bad_seal, bob_pub).to_dict(), bob)
