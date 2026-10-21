"""Nostr events (NIP-01): their id, signature and shape.

An event's ``id`` is the SHA-256 of one exact serialization of its fields,
and its ``sig`` signs that id. Byte-exactness matters twice over in this
bridge: a relay must hand back exactly what it accepted, and events made
from Matrix messages are rebuilt on request and must hash to the same id
every time (see DESIGN.md, "The storage rule"). So serialization is done
here by hand, to NIP-01's rules, rather than trusting ``json.dumps`` to
agree with them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from bridge.nostr import secp256k1

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")

# NIP-01: these are escaped; every other character is written verbatim,
# including other control characters and non-ASCII text.
_ESCAPES = {
    "\n": "\\n",
    '"': '\\"',
    "\\": "\\\\",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}
_ESCAPE_RE = re.compile('[\n"\\\\\r\t\b\f]')

MAX_KIND = 65535


class InvalidEvent(ValueError):
    """An event that isn't well-formed or whose id/signature don't check out.
    The message is suitable for an ``invalid:`` relay reply."""


def _string(value: str) -> str:
    return '"' + _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(0)], value) + '"'


def serialize(pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str) -> bytes:
    """The NIP-01 serialization ``[0,pubkey,created_at,kind,tags,content]``,
    as UTF-8 bytes, with no whitespace."""
    tags_json = "[" + ",".join("[" + ",".join(_string(item) for item in tag) + "]" for tag in tags) + "]"
    text = f"[0,{_string(pubkey)},{created_at},{kind},{tags_json},{_string(content)}]"
    return text.encode("utf-8")


def compute_id(pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str) -> str:
    return hashlib.sha256(serialize(pubkey, created_at, kind, tags, content)).hexdigest()


@dataclass(frozen=True)
class Event:
    id: str
    pubkey: str
    created_at: int
    kind: int
    tags: tuple[tuple[str, ...], ...]
    content: str
    sig: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "kind": self.kind,
            "tags": [list(tag) for tag in self.tags],
            "content": self.content,
            "sig": self.sig,
        }

    def tag_values(self, name: str) -> list[str]:
        """The first value of every tag called ``name``."""
        return [tag[1] for tag in self.tags if len(tag) >= 2 and tag[0] == name]

    def first_tag_value(self, name: str) -> str | None:
        values = self.tag_values(name)
        return values[0] if values else None

    @property
    def d_tag(self) -> str:
        """An addressable event's identifier (empty if it has none)."""
        return self.first_tag_value("d") or ""


def _require_int(value: Any, field: str) -> int:
    # bool is an int subclass in Python, but not in JSON.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEvent(f"{field} must be an integer")
    return value


def parse(raw: Any) -> Event:
    """Check an event's shape (not its id or signature) and return it as an
    ``Event``. Raises ``InvalidEvent``."""
    if not isinstance(raw, dict):
        raise InvalidEvent("event must be a JSON object")
    event_id = raw.get("id")
    pubkey = raw.get("pubkey")
    sig = raw.get("sig")
    content = raw.get("content")
    tags = raw.get("tags")
    if not isinstance(event_id, str) or not _HEX64.match(event_id):
        raise InvalidEvent("id must be 64 lowercase hex characters")
    if not isinstance(pubkey, str) or not _HEX64.match(pubkey):
        raise InvalidEvent("pubkey must be 64 lowercase hex characters")
    if not isinstance(sig, str) or not _HEX128.match(sig):
        raise InvalidEvent("sig must be 128 lowercase hex characters")
    created_at = _require_int(raw.get("created_at"), "created_at")
    if created_at < 0:
        raise InvalidEvent("created_at must not be negative")
    kind = _require_int(raw.get("kind"), "kind")
    if not 0 <= kind <= MAX_KIND:
        raise InvalidEvent(f"kind must be between 0 and {MAX_KIND}")
    if not isinstance(content, str):
        raise InvalidEvent("content must be a string")
    if not isinstance(tags, list) or not all(
        isinstance(tag, list) and all(isinstance(item, str) for item in tag) for tag in tags
    ):
        raise InvalidEvent("tags must be a list of lists of strings")
    return Event(
        id=event_id,
        pubkey=pubkey,
        created_at=created_at,
        kind=kind,
        tags=tuple(tuple(tag) for tag in tags),
        content=content,
        sig=sig,
    )


def check_id(event: Event) -> bool:
    return compute_id(event.pubkey, event.created_at, event.kind, [list(t) for t in event.tags], event.content) == event.id


def check_signature(event: Event) -> bool:
    return secp256k1.verify(bytes.fromhex(event.id), bytes.fromhex(event.pubkey), bytes.fromhex(event.sig))


def verify(raw: Any) -> Event:
    """Parse and fully check an event: shape, id and signature. Raises
    ``InvalidEvent``. Costs a signature verification (a few milliseconds
    in pure Python), so do cheap rejections first."""
    event = parse(raw)
    if not check_id(event):
        raise InvalidEvent("id does not match the event's contents")
    if not check_signature(event):
        raise InvalidEvent("signature is not valid")
    return event


def build(
    secret_key: bytes,
    *,
    kind: int,
    content: str,
    created_at: int,
    tags: Iterable[Iterable[str]] = (),
    aux_rand: bytes | None = None,
) -> Event:
    """A new event signed with ``secret_key``."""
    tag_list = [list(tag) for tag in tags]
    pubkey = secp256k1.public_key(secret_key).hex()
    event_id = compute_id(pubkey, created_at, kind, tag_list, content)
    sig = secp256k1.sign(bytes.fromhex(event_id), secret_key, aux_rand).hex()
    return Event(
        id=event_id,
        pubkey=pubkey,
        created_at=created_at,
        kind=kind,
        tags=tuple(tuple(tag) for tag in tag_list),
        content=content,
        sig=sig,
    )


# -- kind ranges (NIP-01) ------------------------------------------------


def is_replaceable(kind: int) -> bool:
    return kind in (0, 3) or 10000 <= kind < 20000


def is_ephemeral(kind: int) -> bool:
    return 20000 <= kind < 30000


def is_addressable(kind: int) -> bool:
    return 30000 <= kind < 40000


def is_regular(kind: int) -> bool:
    return not (is_replaceable(kind) or is_ephemeral(kind) or is_addressable(kind))
