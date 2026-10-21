"""Nostr-signed HTTP authorization: ``Authorization: Nostr <base64 event>``.

Used by Blossom uploads (BUD-02, kind 24242) and the NIP-86 relay
management API (NIP-98, kind 27235). The event is checked fully (shape,
id, signature) and for freshness; what it must say beyond that is up to
the caller.
"""

from __future__ import annotations

import base64
import json
import time

from bridge.nostr import event as nostr_event


class AuthError(Exception):
    """Not authorized; the message is safe to return."""


def parse(header: str | None, *, kind: int, max_age: int = 600) -> nostr_event.Event:
    if not header or not header.startswith("Nostr "):
        raise AuthError("Authorization: Nostr <base64 event> required")
    try:
        raw = json.loads(base64.b64decode(header[6:].strip(), validate=False))
        event = nostr_event.verify(raw)
    except (ValueError, nostr_event.InvalidEvent) as exc:
        raise AuthError(f"invalid authorization event: {exc}") from exc
    if event.kind != kind:
        raise AuthError(f"authorization event must be kind {kind}")
    now = time.time()
    if event.created_at > now + 60 or (max_age and event.created_at < now - max_age):
        raise AuthError("authorization event is too old or in the future")
    return event
