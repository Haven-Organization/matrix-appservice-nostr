"""XChaCha20-Poly1305 (draft-irtf-cfrg-xchacha), which NIP-49 encrypts keys
with.

``cryptography`` offers ChaCha20-Poly1305 with a 12-byte nonce but not the
24-byte-nonce XChaCha variant. XChaCha is ChaCha20-Poly1305 under a subkey
derived with HChaCha20 from the key and the first 16 nonce bytes, with the
remaining 8 nonce bytes (after 4 zero bytes) as the inner nonce. Only
HChaCha20, a single 20-round ChaCha block with no final addition, is done
here in Python; the bulk encryption stays in ``cryptography``.
"""

from __future__ import annotations

import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

_CONSTANTS = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
_MASK = 0xFFFFFFFF


class XChaChaError(ValueError):
    """Authentication failed: wrong key, or the data was altered."""


def _rotl(value: int, shift: int) -> int:
    return ((value << shift) & _MASK) | (value >> (32 - shift))


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & _MASK
    state[d] = _rotl(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & _MASK
    state[b] = _rotl(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & _MASK
    state[d] = _rotl(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & _MASK
    state[b] = _rotl(state[b] ^ state[c], 7)


def hchacha20(key: bytes, nonce16: bytes) -> bytes:
    """The 32-byte HChaCha20 subkey of ``key`` (32 bytes) and ``nonce16``."""
    if len(key) != 32 or len(nonce16) != 16:
        raise ValueError("HChaCha20 takes a 32-byte key and a 16-byte nonce")
    state = list(_CONSTANTS) + list(struct.unpack("<8I", key)) + list(struct.unpack("<4I", nonce16))
    for _ in range(10):
        _quarter_round(state, 0, 4, 8, 12)
        _quarter_round(state, 1, 5, 9, 13)
        _quarter_round(state, 2, 6, 10, 14)
        _quarter_round(state, 3, 7, 11, 15)
        _quarter_round(state, 0, 5, 10, 15)
        _quarter_round(state, 1, 6, 11, 12)
        _quarter_round(state, 2, 7, 8, 13)
        _quarter_round(state, 3, 4, 9, 14)
    return struct.pack("<8I", *(state[0:4] + state[12:16]))


def _inner(key: bytes, nonce24: bytes) -> tuple[ChaCha20Poly1305, bytes]:
    if len(nonce24) != 24:
        raise ValueError("XChaCha20 takes a 24-byte nonce")
    return ChaCha20Poly1305(hchacha20(key, nonce24[:16])), b"\x00" * 4 + nonce24[16:]


def encrypt(key: bytes, nonce24: bytes, plaintext: bytes, associated_data: bytes | None = None) -> bytes:
    """Ciphertext followed by the 16-byte Poly1305 tag."""
    aead, nonce = _inner(key, nonce24)
    return aead.encrypt(nonce, plaintext, associated_data)


def decrypt(key: bytes, nonce24: bytes, ciphertext: bytes, associated_data: bytes | None = None) -> bytes:
    aead, nonce = _inner(key, nonce24)
    try:
        return aead.decrypt(nonce, ciphertext, associated_data)
    except InvalidTag as exc:
        raise XChaChaError("authentication failed") from exc
