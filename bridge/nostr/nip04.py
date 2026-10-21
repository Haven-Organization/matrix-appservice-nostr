"""NIP-04: the legacy encrypted direct message (kind 4).

Deprecated in favour of NIP-17 (it leaks who talks to whom and when, and
its AES-CBC has no authentication), but still sent by older clients. The
bridge receives and shows these; it never sends them (``encrypt`` exists
for tests and interop checks).

Content is ``base64(ciphertext) + "?iv=" + base64(iv)``: AES-256-CBC with
PKCS#7 padding, keyed directly with the 32-byte shared x coordinate of
ECDH (not hashed).
"""

from __future__ import annotations

import base64
import binascii
import secrets

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bridge.nostr import secp256k1


class Nip04Error(ValueError):
    """Not a valid NIP-04 payload, or it doesn't decrypt."""


def _shared_key(secret_key: bytes, pubkey_hex: str) -> bytes:
    try:
        return secp256k1.ecdh_x(secret_key, bytes.fromhex(pubkey_hex))
    except (ValueError, secp256k1.InvalidKey) as exc:
        raise Nip04Error(f"invalid key: {exc}") from exc


def encrypt(plaintext: str, secret_key: bytes, pubkey_hex: str, iv: bytes | None = None) -> str:
    key = _shared_key(secret_key, pubkey_hex)
    iv = iv or secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii") + "?iv=" + base64.b64encode(iv).decode("ascii")


def decrypt(content: str, secret_key: bytes, pubkey_hex: str) -> str:
    """The plaintext of a kind 4 ``content``, from the point of view of the
    holder of ``secret_key`` talking to ``pubkey_hex``. Raises ``Nip04Error``."""
    body, separator, iv_text = content.partition("?iv=")
    if not separator:
        raise Nip04Error("missing iv")
    try:
        ciphertext = base64.b64decode(body, validate=True)
        iv = base64.b64decode(iv_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Nip04Error("invalid base64") from exc
    if len(iv) != 16 or not ciphertext or len(ciphertext) % 16:
        raise Nip04Error("invalid ciphertext or iv length")
    key = _shared_key(secret_key, pubkey_hex)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        data = unpadder.update(padded) + unpadder.finalize()
        return data.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise Nip04Error("does not decrypt") from exc
