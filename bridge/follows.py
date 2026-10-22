"""Following and blocking Nostr accounts.

``[follow`` takes an account in any form people paste: ``npub1…``,
``nprofile1…`` (whose relay hints are used first), a 64-character hex key,
a NIP-05 handle (``name@domain``, or a bare ``domain`` for ``_@domain``),
or a web link (njump.me, primal.net, ditto.pub, ...).

Following, as an ID:

- the account gets its ghost and Remote User Room (``bridge.remote``),
- the ID's follow list (kind 3) is republished with it in,
- the user is invited to the room, which is filed in their space,
- the account's latest ``nostr.backfill_on_follow`` posts are mirrored,
- the subscriptions are updated so new posts arrive (``bridge.feeds``).

Blocking adds the account to the ID's public mute list (kind 10000), and
the bridge stops showing it interactions from that account.

Both lists are bookkeeping-only events ("inline" sources): their tags are
pubkeys and their content is empty.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import FastAPI

from bridge import remote as remotes
from bridge.event_sources import inline_source
from bridge.homeserver_client import HomeserverError
from bridge.messages import invite_to_room
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19
from bridge.nostr.nip05 import parse_identifier
from bridge.publishing import publish_in_background
from bridge.repository import EventRow, NostrId, RemoteAccount
from bridge.spaces import add_room_to_space
from bridge.tasks import run_in_background

logger = logging.getLogger(__name__)

FOLLOW_LIST_KIND = 3
MUTE_LIST_KIND = 10000
_BECH_RE = re.compile(r"(npub1[02-9ac-hj-np-z]+|nprofile1[02-9ac-hj-np-z]+)")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class ReferenceError(Exception):
    """The account couldn't be found; the message is safe to show."""


@dataclass(frozen=True)
class Reference:
    pubkey: str
    relays: tuple[str, ...] = ()
    nip05: str | None = None


async def resolve_reference(app: FastAPI, text: str) -> Reference:
    text = text.strip().strip("<>")
    if text.startswith("nostr:"):
        text = text[6:]
    lowered = text.lower()
    if _HEX_RE.match(lowered):
        return Reference(lowered)
    match = _BECH_RE.search(text)
    if match:
        try:
            decoded = nip19.decode(match.group(1))
        except nip19.Nip19Error as exc:
            raise ReferenceError(f"That doesn't look like a valid Nostr key ({exc}).") from exc
        if isinstance(decoded, nip19.Profile):
            return Reference(decoded.pubkey, decoded.relays)
        if isinstance(decoded, str):
            return Reference(decoded)
    if lowered.startswith(("https://", "http://")):
        # A profile link with a handle in it (ditto.pub/@q@example.org).
        path = lowered.split("://", 1)[1].split("/", 1)[-1].split("?", 1)[0].strip("/")
        candidate = path.rsplit("/", 1)[-1]
        if "@" in candidate:
            text = lowered = candidate
    identifier = parse_identifier(lowered)
    if identifier is None:
        raise ReferenceError("Give an npub, an nprofile, a hex key, a handle (name@domain), or a profile link.")
    pubkey, relays = await _lookup_nip05(app, identifier.name, identifier.domain)
    if pubkey is None:
        raise ReferenceError(f"{identifier} doesn't lead to a Nostr account.")
    return Reference(pubkey, relays, str(identifier))


async def _lookup_nip05(app: FastAPI, name: str, domain: str) -> tuple[str | None, tuple[str, ...]]:
    if domain == app.state.config.bridge.domain:
        local = await app.state.repository.get_id_by_name(name)
        return (local.pubkey if local else None), ()
    from bridge.safe_fetch import FetchError, UnsafeAddress

    try:
        fetched = await app.state.fetcher.get(
            f"https://{domain}/.well-known/nostr.json?name={name}", max_bytes=256 * 1024, accept="application/json", max_redirects=0
        )
    except (FetchError, UnsafeAddress):
        return None, ()
    if fetched.status != 200:
        return None, ()
    try:
        data = json.loads(fetched.content)
    except ValueError:
        return None, ()
    pubkey = (data.get("names") or {}).get(name) if isinstance(data, dict) else None
    if not isinstance(pubkey, str) or not _HEX_RE.match(pubkey):
        return None, ()
    relays = (data.get("relays") or {}).get(pubkey) or []
    return pubkey, tuple(r for r in relays if isinstance(r, str) and r.startswith(("wss://", "ws://")))[:4]


# -- the ID's lists ------------------------------------------------------------------


async def publish_list(app: FastAPI, nostr_id: NostrId, kind: int) -> None:
    """Sign, index and publish the ID's follow list or mute list as it now
    stands in the database."""
    repository = app.state.repository
    pubkeys = await (repository.list_follows(nostr_id.id) if kind == FOLLOW_LIST_KIND else repository.list_blocks(nostr_id.id))
    tags = [["p", pubkey] for pubkey in pubkeys]
    previous = await repository.get_latest_event(nostr_id.pubkey, kind)
    created_at = int(time.time())
    if previous is not None and created_at <= previous.created_at:
        created_at = previous.created_at + 1
    event = await app.state.signer.sign(nostr_id.pubkey, kind=kind, content="", created_at=created_at, tags=tags)
    if event is None:
        return
    await repository.put_event(
        EventRow(
            id=event.id, pubkey=event.pubkey, kind=kind, created_at=created_at, sig=event.sig,
            source=json.dumps(inline_source(kind, tags)), tags=tuple(("p", p) for p in pubkeys),
        ),
        replace=True,
    )
    publish_in_background(app, event)


# -- following ------------------------------------------------------------------------


async def follow(app: FastAPI, nostr_id: NostrId, reference: Reference, *, backfill: bool = True) -> tuple[RemoteAccount, bool]:
    """Follow as ``nostr_id``. Returns the account and whether this was new.
    Raises ``HomeserverError`` if its room can't be made."""
    repository = app.state.repository
    remote = await remotes.get_or_create_remote(app, reference.pubkey)
    remote = await remotes.refresh(app, remote, force=True, hints=reference.relays)
    remote = await remotes.ensure_room(app, remote)
    assert remote.room_id is not None
    new = await repository.add_follow(nostr_id.id, reference.pubkey, datetime.now(timezone.utc).isoformat())
    if new:
        await publish_list(app, nostr_id, FOLLOW_LIST_KIND)
    await invite_to_room(app, remote.room_id, nostr_id.matrix_user_id)
    await add_room_to_space(app, nostr_id.matrix_user_id, remote.room_id)
    app.state.feeds.changed()
    if new and backfill and app.state.config.nostr.backfill_on_follow > 0:
        run_in_background(app, backfill_posts(app, remote, app.state.config.nostr.backfill_on_follow), name=f"backfill {reference.pubkey[:8]}")
    return remote, new


async def unfollow(app: FastAPI, nostr_id: NostrId, pubkey: str) -> bool:
    if not await app.state.repository.remove_follow(nostr_id.id, pubkey):
        return False
    await publish_list(app, nostr_id, FOLLOW_LIST_KIND)
    app.state.feeds.changed()
    return True


async def block(app: FastAPI, nostr_id: NostrId, pubkey: str, blocked: bool) -> bool:
    if not await app.state.repository.set_block(nostr_id.id, pubkey, blocked):
        return False
    await publish_list(app, nostr_id, MUTE_LIST_KIND)
    return True


async def backfill_posts(app: FastAPI, remote: RemoteAccount, count: int) -> int:
    """Mirror the account's latest ``count`` posts, oldest first."""
    from bridge.inbound import ingest

    relays = remotes.write_relays_for(app, remote)
    found = await app.state.relay_pool.query(
        relays, [{"authors": [remote.pubkey], "kinds": [1, 6, 16, 20, 21, 22, 30023, 34235, 34236], "limit": count}], timeout=12.0
    )
    posts = sorted(
        (e for e in found if e.get("pubkey") == remote.pubkey), key=lambda e: e.get("created_at", 0)
    )[-count:]
    mirrored = 0
    for raw in posts:
        try:
            nostr_event.verify(raw)
        except nostr_event.InvalidEvent:
            continue
        result = await ingest(app, raw, origin="backfill")
        mirrored += int(result.stored)
    logger.info("Backfilled %d posts of %s", mirrored, remote.pubkey)
    return mirrored


async def unfollow_from_room(app: FastAPI, matrix_user_id: str, room_id: str) -> list[str]:
    """A user left a Remote User Room: unfollow it from each of their IDs
    that follows it. Returns the handles that unfollowed."""
    repository = app.state.repository
    remote = await repository.get_remote_account_for_room(room_id)
    if remote is None:
        return []
    done = []
    for nostr_id in await repository.list_ids_for_user(matrix_user_id):
        if await unfollow(app, nostr_id, remote.pubkey):
            done.append(f"{nostr_id.name}@{app.state.config.bridge.domain}")
    return done


async def room_of(app: FastAPI, pubkey: str) -> str | None:
    remote = await app.state.repository.get_remote_account(pubkey)
    return remote.room_id if remote else None


async def safe_follow(app: FastAPI, nostr_id: NostrId, reference: Reference) -> tuple[RemoteAccount | None, bool, str | None]:
    try:
        remote, new = await follow(app, nostr_id, reference)
        return remote, new, None
    except HomeserverError as exc:
        logger.warning("Following %s failed: %s", reference.pubkey, exc)
        return None, False, "Couldn't set up the room for that account just now; try again in a minute."
