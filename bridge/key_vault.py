"""Encrypting Nostr secret keys at rest.

A Nostr key can't be rotated without the identity changing: followers
follow the key. So a copied database must not be a copy of everyone's
identity. Every secret key is stored encrypted with AES-256-GCM under a
key derived from ``nostr.key_encryption_secret``, which lives in the
config file and not in the database.

Each ciphertext is bound to what it belongs to (its ``purpose``, e.g.
``"id:<pubkey>"``) as associated data, so a key can't be moved to another
row and still decrypt.

Stored form: ``v1:<base64 of 12-byte nonce + ciphertext + tag>``.
"""

from __future__ import annotations

import base64
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_VERSION = "v1"
_INFO = b"matrix-appservice-nostr key vault v1"


class KeyVaultError(Exception):
    """A stored key couldn't be decrypted: the secret changed, or the
    stored value was damaged or moved."""


class KeyVault:
    def __init__(self, secret: str) -> None:
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(secret.encode("utf-8"))
        self._aead = AESGCM(key)

    def encrypt(self, secret_key: bytes, purpose: str) -> str:
        nonce = secrets.token_bytes(12)
        sealed = self._aead.encrypt(nonce, secret_key, purpose.encode("utf-8"))
        return f"{_VERSION}:" + base64.b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, stored: str, purpose: str) -> bytes:
        version, _, payload = stored.partition(":")
        if version != _VERSION:
            raise KeyVaultError(f"Unknown key format {version!r}")
        try:
            raw = base64.b64decode(payload, validate=True)
            return self._aead.decrypt(raw[:12], raw[12:], purpose.encode("utf-8"))
        except (ValueError, InvalidTag) as exc:
            raise KeyVaultError(
                "Could not decrypt a stored key. Has nostr.key_encryption_secret changed?"
            ) from exc
