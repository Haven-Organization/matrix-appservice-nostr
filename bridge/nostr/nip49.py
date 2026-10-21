"""NIP-49: a secret key encrypted with a password (``ncryptsec1...``).

How someone can bring their existing Nostr key to the bridge without
handing over the bare ``nsec``, and how the bridge exports a key without
showing one. https://github.com/nostr-protocol/nips/blob/master/49.md

Layout before bech32: version (0x02) || log_n (1) || salt (16) || nonce (24)
|| key security byte (1) || XChaCha20-Poly1305 ciphertext of the 32-byte
key, with the key security byte as associated data (48). The symmetric key
is scrypt(NFKC(password), salt, N=2^log_n, r=8, p=1, 32 bytes).
"""

from __future__ import annotations

import hashlib
import secrets
import unicodedata

from bridge.nostr import xchacha
from bridge.nostr.nip19 import Nip19Error, bech32_decode, bech32_encode

VERSION = 0x02
HRP = "ncryptsec"
# Key security byte values (the spec's, for whoever reads the export).
KEY_KNOWN_INSECURE = 0x00  # the key has been handled insecurely
KEY_NOT_KNOWN_INSECURE = 0x01
KEY_SECURITY_UNKNOWN = 0x02
# scrypt cost bounds: below 16 is weak; above 22 needs gigabytes of memory.
MIN_LOG_N = 16
MAX_LOG_N = 22
_RAW_LENGTH = 1 + 1 + 16 + 24 + 1 + 48


class Nip49Error(ValueError):
    """Not a valid ncryptsec, or the password is wrong. The message is safe
    to show the user."""


def _symmetric_key(password: str, salt: bytes, log_n: int) -> bytes:
    n = 1 << log_n
    normalized = unicodedata.normalize("NFKC", password).encode("utf-8")
    # scrypt needs 128 * r * N bytes, plus headroom.
    return hashlib.scrypt(normalized, salt=salt, n=n, r=8, p=1, dklen=32, maxmem=128 * 8 * n + 64 * 1024 * 1024)


def encrypt(
    secret_key: bytes,
    password: str,
    log_n: int = MIN_LOG_N,
    key_security_byte: int = KEY_SECURITY_UNKNOWN,
) -> str:
    """``secret_key`` (32 bytes) as an ``ncryptsec1...`` string."""
    if len(secret_key) != 32:
        raise Nip49Error("A secret key is 32 bytes")
    if not MIN_LOG_N <= log_n <= MAX_LOG_N:
        raise Nip49Error(f"log_n must be between {MIN_LOG_N} and {MAX_LOG_N}")
    if key_security_byte not in (KEY_KNOWN_INSECURE, KEY_NOT_KNOWN_INSECURE, KEY_SECURITY_UNKNOWN):
        raise Nip49Error("Invalid key security byte")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(24)
    ad = bytes([key_security_byte])
    ciphertext = xchacha.encrypt(_symmetric_key(password, salt, log_n), nonce, secret_key, ad)
    return bech32_encode(HRP, bytes([VERSION, log_n]) + salt + nonce + ad + ciphertext)


def decrypt(ncryptsec: str, password: str) -> bytes:
    """The 32-byte secret key inside an ``ncryptsec1...``. Raises
    ``Nip49Error`` for a malformed string or a wrong password."""
    try:
        hrp, raw = bech32_decode(ncryptsec.strip())
    except Nip19Error as exc:
        raise Nip49Error(f"Not a valid ncryptsec: {exc}") from exc
    if hrp != HRP or len(raw) != _RAW_LENGTH:
        raise Nip49Error("Not a valid ncryptsec")
    if raw[0] != VERSION:
        raise Nip49Error(f"Unsupported ncryptsec version {raw[0]}")
    log_n = raw[1]
    if not 1 <= log_n <= MAX_LOG_N:
        raise Nip49Error("ncryptsec asks for too much memory to decrypt")
    salt, nonce, ad, ciphertext = raw[2:18], raw[18:42], raw[42:43], raw[43:]
    try:
        secret_key = xchacha.decrypt(_symmetric_key(password, salt, log_n), nonce, ciphertext, ad)
    except xchacha.XChaChaError as exc:
        raise Nip49Error("Wrong password, or the ncryptsec is damaged") from exc
    if len(secret_key) != 32:
        raise Nip49Error("Not a valid ncryptsec")
    return secret_key
