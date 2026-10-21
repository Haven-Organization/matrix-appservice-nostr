"""BIP340 signatures, NIP-01 events and NIP-19 encodings, against the
specs' own test vectors and real events signed elsewhere."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from bridge.key_vault import KeyVault, KeyVaultError
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19, secp256k1

VECTORS = Path(__file__).parent / "vectors"


def _bip340_rows() -> list[dict[str, str]]:
    with (VECTORS / "bip340-test-vectors.csv").open() as fh:
        return list(csv.DictReader(fh))


@pytest.mark.parametrize("row", _bip340_rows(), ids=lambda row: f"vector-{row['index']}")
def test_bip340_vector(row: dict[str, str]) -> None:
    pubkey = bytes.fromhex(row["public key"])
    message = bytes.fromhex(row["message"])
    signature = bytes.fromhex(row["signature"])
    if row["secret key"]:
        secret = bytes.fromhex(row["secret key"])
        assert secp256k1.public_key(secret) == pubkey
        assert secp256k1.sign(message, secret, bytes.fromhex(row["aux_rand"])) == signature
    assert secp256k1.verify(message, pubkey, signature) is (row["verification result"] == "TRUE")


def test_ecdh_is_symmetric() -> None:
    a, b = secp256k1.generate_secret_key(), secp256k1.generate_secret_key()
    assert secp256k1.ecdh_x(a, secp256k1.public_key(b)) == secp256k1.ecdh_x(b, secp256k1.public_key(a))


def test_invalid_keys_rejected() -> None:
    with pytest.raises(secp256k1.InvalidKey):
        secp256k1.public_key(b"\0" * 32)
    with pytest.raises(secp256k1.InvalidKey):
        secp256k1.public_key(secp256k1.N.to_bytes(32, "big"))
    assert not secp256k1.is_valid_public_key(b"\xff" * 32)
    assert not secp256k1.verify(b"x", b"short", b"\0" * 64)


# -- NIP-01 --------------------------------------------------------------


def _real_events() -> list[dict]:
    return json.loads((VECTORS / "real-events.json").read_text())


@pytest.mark.parametrize("raw", _real_events(), ids=lambda raw: f"kind{raw['kind']}-{raw['id'][:8]}")
def test_real_events_verify(raw: dict) -> None:
    """Events signed by other implementations hash and verify here: our
    serialization agrees with theirs, escapes and non-ASCII included."""
    event = nostr_event.verify(raw)
    assert event.to_dict() == raw


def test_serialization_escapes_only_what_nip01_says() -> None:
    content = 'line\nquote" back\\ cr\r tab\t bs\b ff\f ctl\x01 del\x7f é 🎉  '
    serialized = nostr_event.serialize("ab" * 32, 1, 1, [["t", 'a"b']], content).decode()
    assert '\\n' in serialized and '\\"' in serialized and "\\\\" in serialized
    assert "\\r" in serialized and "\\t" in serialized and "\\b" in serialized and "\\f" in serialized
    # Everything else verbatim: other control characters, DEL, non-ASCII.
    assert "\x01" in serialized and "\x7f" in serialized and "é 🎉  " in serialized
    assert serialized.startswith('[0,"' + "ab" * 32 + '",1,1,[["t","a\\"b"]],')


def test_build_and_verify_roundtrip() -> None:
    secret = secp256k1.generate_secret_key()
    built = nostr_event.build(secret, kind=1, content="hi\nthere", created_at=1700000000, tags=[["p", "00" * 32]])
    assert nostr_event.verify(built.to_dict()) == built


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e.update(content="tampered"),
        lambda e: e.update(created_at=e["created_at"] + 1),
        lambda e: e.update(sig="00" * 64),
        lambda e: e.update(id="00" * 32),
    ],
)
def test_tampering_detected(mutation) -> None:
    raw = nostr_event.build(secp256k1.generate_secret_key(), kind=1, content="x", created_at=1).to_dict()
    mutation(raw)
    with pytest.raises(nostr_event.InvalidEvent):
        nostr_event.verify(raw)


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": True},
        {"kind": 70000},
        {"created_at": "1"},
        {"created_at": -1},
        {"tags": [["ok"], [1]]},
        {"content": None},
        {"pubkey": "AB" * 32},
    ],
)
def test_malformed_events_rejected(bad: dict) -> None:
    raw = nostr_event.build(secp256k1.generate_secret_key(), kind=1, content="x", created_at=1).to_dict()
    raw.update(bad)
    with pytest.raises(nostr_event.InvalidEvent):
        nostr_event.parse(raw)


def test_kind_ranges() -> None:
    assert nostr_event.is_replaceable(0) and nostr_event.is_replaceable(3) and nostr_event.is_replaceable(10002)
    assert nostr_event.is_ephemeral(22242)
    assert nostr_event.is_addressable(30023)
    assert nostr_event.is_regular(1) and nostr_event.is_regular(7)


# -- NIP-19 (vectors from the spec) ---------------------------------------


def test_nip19_npub_nsec_vectors() -> None:
    npub = "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg"
    pubkey = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
    assert nip19.npub_decode(npub) == pubkey
    assert nip19.npub_encode(pubkey) == npub
    nsec = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
    secret = bytes.fromhex("67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa")
    assert nip19.nsec_decode(nsec) == secret
    assert nip19.nsec_encode(secret) == nsec


def test_nip19_nprofile_vector() -> None:
    text = (
        "nprofile1qqsrhuxx8l9ex335q7he0f09aej04zpazpl0ne2cgukyawd24mayt8gpp4mhxue69uhhytnc9e3k7mgpz4mhxue69uhkg6nzv9ejuumpv34kytnrdaksjlyr9p"
    )
    profile = nip19.decode(text)
    assert profile == nip19.Profile(
        "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d", ("wss://r.x.com", "wss://djbas.sadkb.com")
    )
    assert nip19.nprofile_encode(profile) == text


def test_nip19_nevent_and_naddr_roundtrip() -> None:
    pointer = nip19.EventPointer("aa" * 32, ("wss://relay.example",), "bb" * 32, 1)
    assert nip19.decode(nip19.nevent_encode(pointer)) == pointer
    address = nip19.Address("my-article", "cc" * 32, 30023, ("wss://relay.example",))
    assert nip19.decode(nip19.naddr_encode(address)) == address


@pytest.mark.parametrize(
    "text",
    [
        "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptx",  # bad checksum
        "Npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg",  # mixed case
        "nope",
        "nsec1" + "q" * 6000,
    ],
)
def test_nip19_rejects_garbage(text: str) -> None:
    with pytest.raises(nip19.Nip19Error):
        nip19.decode(text)


# -- key vault -------------------------------------------------------------


def test_key_vault_roundtrip_and_binding() -> None:
    vault = KeyVault("x" * 40)
    secret = secp256k1.generate_secret_key()
    stored = vault.encrypt(secret, "id:abc")
    assert stored.startswith("v1:") and secret.hex() not in stored
    assert vault.decrypt(stored, "id:abc") == secret
    with pytest.raises(KeyVaultError):
        vault.decrypt(stored, "id:other")  # moved to another row
    with pytest.raises(KeyVaultError):
        KeyVault("y" * 40).decrypt(stored, "id:abc")  # secret changed
