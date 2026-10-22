"""NIP-62 "Request to Vanish" (kind 62): a key asking relays to delete
everything it has published, up to the request's ``created_at``.

The bridge's relay honours requests naming it (or ``ALL_RELAYS``):

- everything the key signed comes out of the relay's index, and for a remote
  account its posts are redacted in Matrix too (Matrix is where the relay
  keeps them);
- DMs addressed to the key (NIP-59 gift wraps) come out of the index;
- nothing it signed up to then is taken again (``is_vanished``).

For a local ID (one deleted with ``[delete profile``, or a request made with
its key from another app) the Matrix side is the user's own history, which
stays: only the index is cleared.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.nostr.relay_pool import normalize_relay_url
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

VANISH_KIND = 62
ALL_RELAYS = "ALL_RELAYS"
_STATE_KEY = "vanished:{}"
_CACHE_LIMIT = 50_000


def names_this_relay(app: FastAPI, raw: dict[str, Any]) -> bool:
    ours = normalize_relay_url(app.state.config.nostr.relay_url)
    for tag in raw.get("tags") or []:
        if len(tag) >= 2 and tag[0] == "relay":
            if tag[1] == ALL_RELAYS:
                return True
            try:
                if normalize_relay_url(tag[1]) == ours:
                    return True
            except ValueError:
                continue
    return False


def _cache(app: FastAPI) -> dict[str, int]:
    cache = app.state.__dict__.setdefault("vanished_cache", {})
    if len(cache) > _CACHE_LIMIT:
        cache.clear()
    return cache


async def vanished_until(app: FastAPI, pubkey: str) -> int:
    """The newest request to vanish this relay honoured for ``pubkey``
    (its created_at), or 0."""
    cache = _cache(app)
    if pubkey not in cache:
        value = await app.state.repository.get_state(_STATE_KEY.format(pubkey))
        cache[pubkey] = int(value) if value and value.isdigit() else 0
    return cache[pubkey]


async def is_vanished(app: FastAPI, pubkey: str, created_at: int) -> bool:
    return created_at <= await vanished_until(app, pubkey)


async def apply_vanish(app: FastAPI, pubkey: str, until: int, *, redact: bool, keep: tuple[str, ...] = ()) -> int:
    """Clear ``pubkey``'s events up to ``until`` from the relay (and, with
    ``redact``, their Matrix events). Returns how many events went."""
    repository = app.state.repository
    if until > await vanished_until(app, pubkey):
        await repository.set_state(_STATE_KEY.format(pubkey), str(until))
        _cache(app)[pubkey] = until
    rows = await repository.delete_events_by(pubkey, until=until, keep=keep)
    hub = getattr(app.state, "relay_hub", None)
    if hub is not None:
        for row in rows:
            hub.cache.drop(row.id)
    await repository.delete_gift_wraps_to(pubkey)
    if redact:
        from bridge.remote import ghost_user_id

        ghost = ghost_user_id(app, pubkey)
        bot = bot_mxid(app.state.config)
        for row in rows:
            if not row.room_id or not row.matrix_event_id:
                continue
            for sender in (ghost, bot):
                try:
                    await app.state.homeserver.redact_event(
                        row.room_id, row.matrix_event_id, as_user_id=sender, reason="Its author deleted it from Nostr"
                    )
                    break
                except HomeserverError:
                    continue
    logger.info("Vanish: %d event(s) of %s up to %d removed", len(rows), pubkey, until)
    return len(rows)


async def receive_vanish(app: FastAPI, raw: dict[str, Any], *, local: bool):
    """A request to vanish sent to the relay (or found by a subscription)."""
    from bridge.inbound import IngestResult

    if not names_this_relay(app, raw):
        return IngestResult(False, "restricted: this request to vanish names other relays")
    await apply_vanish(app, raw["pubkey"], int(raw["created_at"]), redact=not local)
    return IngestResult(True, "")
