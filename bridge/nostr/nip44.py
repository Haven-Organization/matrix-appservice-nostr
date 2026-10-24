"""NIP-44 version 2: the encryption under private DMs (NIP-17 seals),
remote signing (NIP-46) and anything else end-to-end between two keys.

https://github.com/nostr-protocol/nips/blob/master/44.md

- conversation key: HKDF-extract(salt=b"nip44-v2", IKM = shared x of ECDH),
  the same in both directions,
- per message: a random 32-byte nonce; HKDF-expand(conversation key,
  info=nonce, 76 bytes) gives the ChaCha20 key (32), ChaCha20 nonce (12)
  and HMAC key (32),
- the plaintext is length-prefixed and padded to a size bucket, so the
  ciphertext reveals only roughly how long a message is,
- payload = base64(0x02 || nonce || ciphertext || HMAC-SHA256(nonce ||
  ciphertext)).

Checked against the official test vectors (``tests/vectors/nip44.vectors.json``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from bridge.nostr import secp256k1

VERSION = 2
_SALT = b"nip44-v2"
MIN_PLAINTEXT_SIZE = 1
MAX_PLAINTEXT_SIZE = 65535
# Base64 payload and decoded data bounds from the spec.
_MIN_PAYLOAD_LENGTH = 132
_MAX_PAYLOAD_LENGTH = 87472
_MIN_DATA_LENGTH = 99
_MAX_DATA_LENGTH = 65603


class Nip44Error(ValueError):
    """Not a valid NIP-44 v2 payload or key, or it doesn't decrypt."""


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    output = b""
    block = b""
    counter = 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def get_conversation_key(secret_key: bytes, pubkey_hex: str) -> bytes:
    """The key two parties share: from one's secret key and the other's
    x-only public key (hex). Raises ``Nip44Error`` for invalid keys."""
    try:
        pubkey = bytes.fromhex(pubkey_hex)
        shared_x = secp256k1.ecdh_x(secret_key, pubkey)
    except (ValueError, secp256k1.InvalidKey) as exc:
        raise Nip44Error(f"invalid key: {exc}") from exc
    return _hkdf_extract(_SALT, shared_x)


def get_message_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    """(ChaCha20 key, ChaCha20 nonce, HMAC key) for one message."""
    if len(conversation_key) != 32:
        raise Nip44Error("invalid conversation key length")
    if len(nonce) != 32:
        raise Nip44Error("invalid nonce length")
    keys = _hkdf_expand(conversation_key, nonce, 76)
    return keys[:32], keys[32:44], keys[44:76]


def calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << ((unpadded_len - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((unpadded_len - 1) // chunk + 1)


def _pad(plaintext: str | bytes) -> bytes:
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    length = len(data)
    if not MIN_PLAINTEXT_SIZE <= length <= MAX_PLAINTEXT_SIZE:
        raise Nip44Error("invalid plaintext length: must be between 1 and 65535 bytes")
    return length.to_bytes(2, "big") + data + bytes(calc_padded_len(length) - length)


def _unpad(padded: bytes) -> str:
    data = _unpad_bytes(padded)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Nip44Error("plaintext is not UTF-8") from exc


def _unpad_bytes(padded: bytes) -> bytes:
    if len(padded) < 2:
        raise Nip44Error("invalid padding")
    length = int.from_bytes(padded[:2], "big")
    data = padded[2 : 2 + length]
    if (
        length < MIN_PLAINTEXT_SIZE
        or length > MAX_PLAINTEXT_SIZE
        or len(data) != length
        or len(padded) != 2 + calc_padded_len(length)
    ):
        raise Nip44Error("invalid padding")
    return data


def _chacha20(key: bytes, nonce: bytes, data: bytes) -> bytes:
    # cryptography's ChaCha20 takes a 16-byte nonce: a 4-byte little-endian
    # block counter (0 here) followed by the 12-byte IETF nonce.
    cipher = Cipher(algorithms.ChaCha20(key, b"\x00" * 4 + nonce), mode=None)
    return cipher.encryptor().update(data)


def _mac(hmac_key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()


def encrypt(plaintext: str | bytes, conversation_key: bytes, nonce: bytes | None = None) -> str:
    """The base64 payload for ``plaintext``. ``nonce`` defaults to fresh
    randomness; pass one only for test vectors."""
    if nonce is None:
        nonce = secrets.token_bytes(32)
    chacha_key, chacha_nonce, hmac_key = get_message_keys(conversation_key, nonce)
    ciphertext = _chacha20(chacha_key, chacha_nonce, _pad(plaintext))
    payload = bytes([VERSION]) + nonce + ciphertext + _mac(hmac_key, nonce, ciphertext)
    return base64.b64encode(payload).decode("ascii")


def decrypt(payload: str, conversation_key: bytes) -> str:
    """The plaintext of a payload. Raises ``Nip44Error``."""
    return _unpad(_open(payload, conversation_key))


def decrypt_bytes(payload: str, conversation_key: bytes) -> bytes:
    """The plaintext as bytes, for binary plaintexts (Concord's key blobs)."""
    return _unpad_bytes(_open(payload, conversation_key))


def _open(payload: str, conversation_key: bytes) -> bytes:
    if not payload or payload.startswith("#"):
        raise Nip44Error("unknown encryption version")
    if not _MIN_PAYLOAD_LENGTH <= len(payload) <= _MAX_PAYLOAD_LENGTH:
        raise Nip44Error(f"invalid payload length: {len(payload)}")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Nip44Error("invalid base64") from exc
    if not _MIN_DATA_LENGTH <= len(data) <= _MAX_DATA_LENGTH:
        raise Nip44Error(f"invalid data length: {len(data)}")
    if data[0] != VERSION:
        raise Nip44Error(f"unknown encryption version {data[0]}")
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = get_message_keys(conversation_key, nonce)
    if not hmac.compare_digest(mac, _mac(hmac_key, nonce, ciphertext)):
        raise Nip44Error("invalid MAC")
    return _chacha20(chacha_key, chacha_nonce, ciphertext)
