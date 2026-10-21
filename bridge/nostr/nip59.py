"""NIP-59 gift wraps, as NIP-17 private DMs use them.

A DM is three layers, so relays learn neither the sender nor when it was
written:

- the **rumor**: the actual message (kind 14 for chat), an event with an
  id but *no signature*, so it can't be shown to third parties as proof,
- the **seal** (kind 13): the rumor NIP-44-encrypted to the recipient,
  signed by the real sender, with no tags,
- the **gift wrap** (kind 1059): the seal NIP-44-encrypted again, signed
  by a throwaway key, tagged only with the recipient.

The seal's and wrap's ``created_at`` are randomized up to two days into
the past. A DM is wrapped twice: once for the recipient and once for the
sender's own copy.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Iterable

from bridge.nostr import event as nostr_event
from bridge.nostr import nip44, secp256k1
from bridge.nostr.event import Event, InvalidEvent

SEAL_KIND = 13
CHAT_MESSAGE_KIND = 14
GIFT_WRAP_KIND = 1059
# How far into the past seal and wrap timestamps are pushed (NIP-59).
_TWO_DAYS = 2 * 24 * 3600


class UnwrapError(ValueError):
    """A gift wrap that isn't addressed to us, doesn't decrypt, or whose
    layers don't agree."""


def _random_past(now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    return now - secrets.randbelow(_TWO_DAYS)


def make_rumor(pubkey: str, kind: int, content: str, tags: Iterable[Iterable[str]], created_at: int) -> dict[str, Any]:
    """An unsigned event: everything but ``sig``."""
    tag_list = [list(tag) for tag in tags]
    return {
        "id": nostr_event.compute_id(pubkey, created_at, kind, tag_list, content),
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tag_list,
        "content": content,
    }


def _rumor_json(rumor: dict[str, Any]) -> str:
    return json.dumps(rumor, ensure_ascii=False, separators=(",", ":"))


def seal(rumor: dict[str, Any], sender_secret: bytes, recipient_pubkey: str) -> Event:
    """The kind 13 seal of ``rumor``, signed by the sender."""
    sender_pubkey = secp256k1.public_key(sender_secret).hex()
    if rumor.get("pubkey") != sender_pubkey:
        raise ValueError("The rumor's pubkey must be the sender's")
    key = nip44.get_conversation_key(sender_secret, recipient_pubkey)
    content = nip44.encrypt(_rumor_json(rumor), key)
    return nostr_event.build(sender_secret, kind=SEAL_KIND, content=content, created_at=_random_past(), tags=[])


def gift_wrap(seal_event: Event, recipient_pubkey: str, *, extra_tags: Iterable[Iterable[str]] = ()) -> Event:
    """The kind 1059 wrap of a seal, signed by a fresh throwaway key."""
    throwaway = secp256k1.generate_secret_key()
    key = nip44.get_conversation_key(throwaway, recipient_pubkey)
    content = nip44.encrypt(json.dumps(seal_event.to_dict(), ensure_ascii=False, separators=(",", ":")), key)
    tags = [["p", recipient_pubkey], *[list(tag) for tag in extra_tags]]
    return nostr_event.build(throwaway, kind=GIFT_WRAP_KIND, content=content, created_at=_random_past(), tags=tags)


def wrap_message(rumor: dict[str, Any], sender_secret: bytes, recipient_pubkey: str) -> Event:
    """``rumor`` sealed by the sender and wrapped for ``recipient_pubkey``."""
    return gift_wrap(seal(rumor, sender_secret, recipient_pubkey), recipient_pubkey)


@dataclass(frozen=True)
class Unwrapped:
    rumor: dict[str, Any]
    # The real sender: the seal's (and so the rumor's) pubkey.
    sender: str
    seal: dict[str, Any]


def unwrap(wrap: dict[str, Any], recipient_secret: bytes) -> Unwrapped:
    """Open a gift wrap addressed to the holder of ``recipient_secret``,
    checking every layer. Raises ``UnwrapError``."""
    try:
        wrap_event = nostr_event.verify(wrap)
    except InvalidEvent as exc:
        raise UnwrapError(f"invalid gift wrap: {exc}") from exc
    if wrap_event.kind != GIFT_WRAP_KIND:
        raise UnwrapError("not a gift wrap")
    try:
        seal_json = nip44.decrypt(wrap_event.content, nip44.get_conversation_key(recipient_secret, wrap_event.pubkey))
        seal_raw = json.loads(seal_json)
        seal_event = nostr_event.verify(seal_raw)
    except (nip44.Nip44Error, ValueError, InvalidEvent) as exc:
        raise UnwrapError(f"gift wrap doesn't open: {exc}") from exc
    if seal_event.kind != SEAL_KIND:
        raise UnwrapError("wrapped event is not a seal")
    try:
        rumor_json = nip44.decrypt(seal_event.content, nip44.get_conversation_key(recipient_secret, seal_event.pubkey))
        rumor = json.loads(rumor_json)
    except (nip44.Nip44Error, ValueError) as exc:
        raise UnwrapError(f"seal doesn't open: {exc}") from exc
    if not isinstance(rumor, dict):
        raise UnwrapError("rumor is not an event")
    # A rumor claiming someone other than the seal's signer is a forgery.
    if rumor.get("pubkey") != seal_event.pubkey:
        raise UnwrapError("rumor author doesn't match the seal's signer")
    try:
        unsigned = nostr_event.parse({**rumor, "sig": "0" * 128})
    except InvalidEvent as exc:
        raise UnwrapError(f"invalid rumor: {exc}") from exc
    if not nostr_event.check_id(unsigned):
        raise UnwrapError("rumor id doesn't match its contents")
    return Unwrapped(rumor=rumor, sender=seal_event.pubkey, seal=seal_raw)
