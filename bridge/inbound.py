"""Everything that comes in from Nostr goes through ``ingest``: events
written to the bridge's own relay, and events the bridge's subscriptions
pick up on other relays (followed accounts' posts, anything tagging a
local ID).

The rule (DESIGN.md, "The invariant"): an event is stored if and only if
it has a home in Matrix. Its raw signed JSON is carried inside the Matrix
event that shows it (``EMBED_FIELD``), and the index row points there, so
the relay can serve it and redacting the Matrix event deletes it.

What gets a home:

- a **reply** (kind 1 per NIP-10, or a kind 1111 comment per NIP-22) to a
  post that is in Matrix: threaded under that post, from the replier's
  ghost. Its author is told, if the post is a local ID's.
- a **reaction** (kind 7) to a post in Matrix: an ``m.reaction`` from the
  reacting account's ghost.
- a **repost** (kind 6/16) of a local post: a 🔁 reaction on it; of anything,
  by a followed account: a repost card in that account's room.
- a **post** by a followed account: a message in its Remote User Room.
- a **mention** of a local ID that has no other home: the notification.
- a **zap receipt** for a local ID: the notification.
- a followed account's **profile** (kind 0): its room's state.

Processed but not stored: follow lists (kind 3) that add or drop a local
ID (a "followed you" notification), deletions (kind 5: the Matrix events
are redacted), and relay lists (kinds 10002/10050: cached).

Anything else is refused (``restricted:``) when written to the relay, and
ignored when it arrives from a subscription.
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI

from bridge import remote as remotes
from bridge.event_sources import EMBED_FIELD, embedded_source
from bridge.homeserver_client import HomeserverError
from bridge.msc4501 import EXTERNAL_HANDLE_FIELD, PER_MESSAGE_PROFILE_FIELD, external_handle_value
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19
from bridge import notify_format
from bridge.notifications import notify
from bridge.render import message_content, render
from bridge.repository import EventRow, NostrId, RemoteAccount
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

INDEXED_TAG_LETTERS = frozenset("epaqkdiEPAK")
# 34235/34236: NIP-71's addressable videos, shown like kinds 21 and 22.
# 1063: a NIP-94 file (how Ditto posts webxdc games, among other files).
POST_KINDS = frozenset({1, 20, 21, 22, 1063, 1111, 30023, 34235, 34236})
# Reactions (NIP-25, and the generic kind 17): a reply to one goes in the
# thread of what was reacted to.
REACTION_KINDS = frozenset({7, 17})
REACTION_KIND = 7
REPOST_KINDS = frozenset({6, 16})
ZAP_RECEIPT_KIND = 9735
GIFT_WRAP_KIND = 1059
LEGACY_DM_KIND = 4
_SEEN_MAX = 20000
SOCIAL_RELATES_TO_FIELD = "org.matrix.msc4501.social.relates_to"


@dataclass(frozen=True)
class IngestResult:
    ok: bool
    message: str = ""
    stored: bool = False

    @property
    def reply(self) -> str:
        return self.message


REJECTED = IngestResult(False, "restricted: this relay only takes events for its own users")


def index_tags(tags: Any) -> tuple[tuple[str, str], ...]:
    out = []
    for tag in tags or []:
        if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] in INDEXED_TAG_LETTERS and isinstance(tag[1], str):
            out.append((tag[0], tag[1]))
    return tuple(dict.fromkeys(out))


class Seen:
    """Recently processed event ids, so the same event from several relays
    (or a resubscription) is only acted on once."""

    def __init__(self, size: int = _SEEN_MAX) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._size = size

    def add(self, event_id: str) -> bool:
        if event_id in self._ids:
            self._ids.move_to_end(event_id)
            return False
        self._ids[event_id] = None
        while len(self._ids) > self._size:
            self._ids.popitem(last=False)
        return True


# -- helpers ----------------------------------------------------------------------


def _tags(raw: dict[str, Any], name: str) -> list[list[str]]:
    return [t for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == name]


def reply_targets(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """(parent, root) event ids a post answers, per NIP-10 (kind 1, marked
    or positional e tags) or NIP-22 (kind 1111: e = parent, E = root)."""
    if raw.get("kind") == 1111:
        parent = next((t[1] for t in _tags(raw, "e")), None)
        root = next((t[1] for t in _tags(raw, "E")), None)
        return parent, root or parent
    e_tags = [t for t in _tags(raw, "e") if not (len(t) >= 4 and t[3] == "mention")]
    if not e_tags:
        return None, None
    marked_reply = next((t[1] for t in e_tags if len(t) >= 4 and t[3] == "reply"), None)
    marked_root = next((t[1] for t in e_tags if len(t) >= 4 and t[3] == "root"), None)
    if marked_reply or marked_root:
        return marked_reply or marked_root, marked_root or marked_reply
    return e_tags[-1][1], e_tags[0][1]


def reaction_target(raw: dict[str, Any]) -> str | None:
    e_tags = _tags(raw, "e")
    return e_tags[-1][1] if e_tags else None


def reaction_key(raw: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The Matrix reaction key for a kind 7, plus extra content fields
    (MSC4027 custom emoji). ``None`` for a dislike, which isn't shown."""
    content = (raw.get("content") or "+").strip()
    if content in ("", "+"):
        return "👍", {}
    if content == "-":
        return None
    if content.startswith(":") and content.endswith(":") and len(content) > 2:
        shortcode = content[1:-1]
        url = next((t[2] for t in _tags(raw, "emoji") if len(t) >= 3 and t[1] == shortcode), None)
        if url:
            return content, {"shortcode": content, "_emoji_url": url}
    return content[:64], {}


async def _local_ids_for(app: FastAPI, pubkeys: list[str]) -> list[NostrId]:
    found = []
    for pubkey in dict.fromkeys(pubkeys):
        local = await app.state.repository.get_id_by_pubkey(pubkey)
        if local is not None:
            found.append(local)
    return found


async def _ghost_label(app: FastAPI, remote: RemoteAccount) -> str:
    try:
        name = (await app.state.homeserver.get_profile(remote.ghost_user_id)).get("displayname")
    except HomeserverError:
        name = None
    return name or remote.nip05 or remotes.short_npub(remote.pubkey)


def _pill(mxid: str, label: str) -> str:
    return f'<a href="https://matrix.to/#/{html.escape(mxid)}">{html.escape(label)}</a>'


def _event_link(room_id: str, event_id: str) -> str:
    return f"https://matrix.to/#/{room_id}/{event_id}"


def _viewer_link(app: FastAPI, event_id: str) -> str:
    return app.state.config.nostr.web_viewer_url + nip19.note_encode(event_id)


async def _send(app: FastAPI, room_id: str, content: dict[str, Any], *, sender: str, ts: int | None, event_type: str = "m.room.message") -> str:
    return await app.state.homeserver.send_message_event(room_id, content, as_user_id=sender, ts=ts, event_type=event_type)


async def _store(
    app: FastAPI, raw: dict[str, Any], *, room_id: str, matrix_event_id: str, scope: str
) -> None:
    await app.state.repository.put_event(
        EventRow(
            id=raw["id"],
            pubkey=raw["pubkey"],
            kind=int(raw["kind"]),
            created_at=int(raw["created_at"]),
            sig=raw["sig"],
            source=json.dumps(embedded_source()),
            d_tag=next((t[1] for t in _tags(raw, "d")), "") if 30000 <= int(raw["kind"]) < 40000 else "",
            room_id=room_id,
            matrix_event_id=matrix_event_id,
            expires_at=_expiration(raw),
            scope=scope,
            tags=index_tags(raw.get("tags")),
        )
    )
    from bridge.webxdc import register_games

    await register_games(app, raw)
    hub = getattr(app.state, "relay_hub", None)
    if hub is not None:
        hub.broadcast(raw, scope)


def _expiration(raw: dict[str, Any]) -> int | None:
    value = next((t[1] for t in _tags(raw, "expiration")), None)
    return int(value) if value and value.isdigit() else None


def event_url(app: FastAPI, raw: dict[str, Any]) -> str:
    """Where the event can be seen on the web (``nostr.web_viewer_url``)."""
    viewer = app.state.config.nostr.web_viewer_url
    kind = int(raw.get("kind", 1))
    if 30000 <= kind < 40000:
        d = next((t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "d"), "")
        return viewer + nip19.naddr_encode(nip19.Address(d, raw["pubkey"], kind))
    return viewer + nip19.nevent_encode(nip19.EventPointer(raw["id"], (), raw["pubkey"], kind))


def _with_extras(
    app: FastAPI, content: dict[str, Any], raw: dict[str, Any], remote: RemoteAccount, *, link: str | None = None
) -> dict[str, Any]:
    """The raw event (the relay's copy), and for a post shown in Matrix its
    ``external_url`` (the original on the web, as the sibling bridges set
    it) and MSC4503 handle per ``bridge.msc4503_external_handle``."""
    content = {**content, EMBED_FIELD: raw}
    if link is not None:
        content["external_url"] = link
    if app.state.config.bridge.msc4503_external_handle in ("events", "both"):
        content[EXTERNAL_HANDLE_FIELD] = external_handle_value(app.state.config, pubkey=remote.pubkey, nip05=remote.nip05)
    return content


async def _thread_root_of(app: FastAPI, room_id: str, event_id: str) -> str:
    """The Matrix thread a message belongs to (its root), or the message
    itself if it isn't in a thread."""
    try:
        event = await app.state.homeserver.get_event(room_id, event_id, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return event_id
    relates = ((event or {}).get("content") or {}).get("m.relates_to") or {}
    if relates.get("rel_type") == "m.thread" and isinstance(relates.get("event_id"), str):
        return relates["event_id"]
    return event_id


def _post_event_type(app: FastAPI) -> str:
    return "org.matrix.msc4501.social.post" if app.state.config.bridge.use_msc4501_post_event_type else "m.room.message"


# -- the pipeline -------------------------------------------------------------------


async def ingest(
    app: FastAPI, raw: Any, *, origin: str, ip: str | None = None, authed: frozenset[str] = frozenset()
) -> IngestResult:
    """Take one event from a relay client (``origin="relay"``) or a
    subscription (``"subscription"``). Never raises."""
    try:
        event = nostr_event.parse(raw)
    except nostr_event.InvalidEvent as exc:
        return IngestResult(False, f"invalid: {exc}")
    repository = app.state.repository
    if await repository.get_event(event.id) is not None:
        return IngestResult(True, "duplicate: already have it", stored=True)
    inflight: dict[str, asyncio.Future] = app.state.ingest_inflight
    if event.id in inflight:
        return await asyncio.shield(inflight[event.id])
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    inflight[event.id] = future
    try:
        result = await _ingest(app, raw, event, origin=origin, ip=ip, authed=authed)
    except Exception:
        logger.exception("Ingesting %s failed", event.id)
        result = IngestResult(False, "error: could not process this event")
    finally:
        inflight.pop(event.id, None)
    future.set_result(result)
    return result


async def _tier(app: FastAPI, pubkey: str):
    from bridge.spam import Tier

    repository = app.state.repository
    if await repository.get_id_by_pubkey(pubkey) is not None:
        return Tier.LOCAL
    if await repository.list_followers_of_remote(pubkey):
        return Tier.TRUSTED
    if pubkey in app.state.web_of_trust:
        return Tier.WEB_OF_TRUST
    return Tier.STRANGER


async def _ingest(app: FastAPI, raw: dict[str, Any], event: nostr_event.Event, *, origin: str, ip: str | None, authed: frozenset[str]) -> IngestResult:
    repository = app.state.repository
    raw = event.to_dict()
    local_author = await repository.get_id_by_pubkey(event.pubkey)
    targets = [t[1] for t in raw["tags"] if len(t) >= 2 and t[0] in ("p", "P")]
    if await app.state.spam.active_ban("event", event.id):
        return IngestResult(False, "blocked: removed by a relay admin")
    if not app.state.seen.add(event.id):
        return IngestResult(True, "duplicate: already seen")
    from bridge.vanish import VANISH_KIND, is_vanished

    if event.kind != VANISH_KIND and await is_vanished(app, event.pubkey, event.created_at):
        # NIP-62: what a key asked this relay to delete can't come back.
        return IngestResult(False, "blocked: its author asked this relay to delete it")
    if (
        local_author is None
        and event.pubkey in getattr(app.state, "shared_blocks", ())
        and not await repository.list_followers_of_remote(event.pubkey)
    ):
        return IngestResult(False, "blocked: on a block list this relay follows")

    from bridge import webxdc

    if event.kind == webxdc.REALTIME_KIND:
        # Ephemeral game traffic, 20-30 a second, from keys made per session:
        # only for a game someone here is playing, and never stored.
        try:
            await asyncio.to_thread(nostr_event.verify, raw)
        except nostr_event.InvalidEvent as exc:
            return IngestResult(False, f"invalid: {exc}")
        return await webxdc.receive_realtime(app, raw)
    guard = getattr(app.state, "spam", None)
    if guard is not None and origin in ("relay", "subscription"):
        verdict = await guard.check_event(
            event, ip=ip, tier=await _tier(app, event.pubkey), targets=targets,
            # An old interaction written to the relay now is suspicious; one
            # the bridge fetched itself (catching up) is not.
            interaction=origin == "relay" and event.kind in (1, 6, 7, 16, 1111, ZAP_RECEIPT_KIND),
            fetched=origin == "subscription",
        )
        if not verdict.ok:
            return IngestResult(False, verdict.reply)

    if any(t[0] == "-" for t in raw["tags"] if t) and event.pubkey not in authed:
        # NIP-70: only its author may publish a protected event.
        return IngestResult(False, "auth-required: this event may only be published by its author")

    try:
        await asyncio.to_thread(nostr_event.verify, raw)
    except nostr_event.InvalidEvent as exc:
        return IngestResult(False, f"invalid: {exc}")

    if event.kind == VANISH_KIND:
        from bridge.vanish import receive_vanish

        return await receive_vanish(app, raw, local=local_author is not None)
    if event.kind == webxdc.UPDATE_KIND:
        return await webxdc.receive_update(app, raw)
    if local_author is not None:
        return await _from_local_author(app, raw, local_author, origin=origin)

    kind = event.kind
    if kind == 5:
        return await _deletion(app, raw)
    if kind == 0:
        return await _profile(app, raw)
    if kind in (remotes.RELAY_LIST_KIND, remotes.DM_RELAY_LIST_KIND):
        return await _relay_list(app, raw)
    if kind == 3:
        return await _contact_list(app, raw)
    if kind in (GIFT_WRAP_KIND, LEGACY_DM_KIND):
        from bridge.dms import receive_dm

        return await receive_dm(app, raw, origin=origin, ip=ip)
    if kind == ZAP_RECEIPT_KIND:
        return await _zap_receipt(app, raw)
    if kind == REACTION_KIND:
        return await _reaction(app, raw)
    if kind in REPOST_KINDS:
        return await _repost(app, raw)
    if kind in POST_KINDS:
        return await _post(app, raw)
    if kind == 1068:
        from bridge.polls import receive_poll

        return await receive_poll(app, raw)
    if kind == 1018:
        from bridge.polls import receive_vote

        return await receive_vote(app, raw)
    return REJECTED


# -- kinds --------------------------------------------------------------------------


async def _deletion(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    """NIP-09: redact the Matrix events of the author's own events."""
    repository = app.state.repository
    redacted = 0
    for target in [t[1] for t in _tags(raw, "e")]:
        row = await repository.get_event(target)
        if row is None or row.pubkey != raw["pubkey"]:
            continue  # only an event's author may delete it
        remote = await repository.get_remote_account(raw["pubkey"])
        sender = remote.ghost_user_id if remote else bot_mxid(app.state.config)
        places = [(row.room_id, row.matrix_event_id)] if row.room_id and row.matrix_event_id else []
        places += await repository.list_mirrored_copies(row.id)
        for room_id, event_id in places:
            try:
                await app.state.homeserver.redact_event(room_id, event_id, as_user_id=sender, reason="Deleted on Nostr")
            except HomeserverError:
                try:
                    await app.state.homeserver.redact_event(room_id, event_id, as_user_id=bot_mxid(app.state.config))
                except HomeserverError:
                    logger.info("Could not redact %s in %s", event_id, room_id, exc_info=True)
        await repository.delete_event(row.id)
        redacted += 1
    return IngestResult(True, "" if redacted else "restricted: not stored")


async def _profile(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    remote = await app.state.repository.get_remote_account(raw["pubkey"])
    if remote is None:
        return REJECTED
    await remotes.apply_profile(app, remote, raw)
    return IngestResult(True, "", stored=bool(remote.room_id))


async def _relay_list(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    remote = await app.state.repository.get_remote_account(raw["pubkey"])
    if remote is None:
        return REJECTED
    await app.state.repository.save_remote_account(remotes.apply_relay_list(remote, raw))
    app.state.feeds.changed()
    return IngestResult(False, "restricted: not stored")


async def _contact_list(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    """A follow list. Announce a local ID gaining a follower (once), and
    note one dropping it. Followed accounts' lists also feed the web of
    trust."""
    repository = app.state.repository
    follows = {t[1] for t in _tags(raw, "p")}
    if await repository.list_followers_of_remote(raw["pubkey"]):
        app.state.web_of_trust.update(p for p in follows if len(p) == 64)
    touched = False
    for local in await repository.list_all_ids():
        following = local.pubkey in follows
        if await repository.set_follower(local.id, raw["pubkey"], int(raw["created_at"]), following):
            touched = True
            if following and not await repository.is_blocked_by_any(raw["pubkey"], [local.id]):
                await notify_format.send(app, local, "follow", raw["pubkey"])
    return IngestResult(False, "restricted: not stored") if not touched else IngestResult(False, "restricted: noted, not stored")


_ZAP_PROVIDER_TTL = 3600.0


async def zap_provider_pubkey(app: FastAPI, local: Any) -> str | None:
    """The key the ID's Lightning address (``lud16``) signs zap receipts
    with: its LNURL-pay ``nostrPubkey``. None without a usable address."""
    import time as _time

    from bridge.safe_fetch import FetchError, UnsafeAddress

    if not local.profile_room_id:
        return None
    try:
        extra = await app.state.homeserver.get_room_state(
            local.profile_room_id, "software.haven.nostr.profile_extra", as_user_id=bot_mxid(app.state.config)
        )
    except HomeserverError:
        return None
    address = str((extra or {}).get("lud16") or "").strip().lower()
    name, _, domain = address.partition("@")
    if not name or not domain or "/" in address:
        return None
    cache = app.state.__dict__.setdefault("zap_providers", {})
    hit = cache.get(address)
    if hit and hit[1] > _time.monotonic():
        return hit[0]
    pubkey = None
    try:
        fetched = await app.state.fetcher.get(f"https://{domain}/.well-known/lnurlp/{name}", max_bytes=64 * 1024,
                                              accept="application/json")
        info = json.loads(fetched.content) if fetched.status == 200 else {}
        candidate = info.get("nostrPubkey") if isinstance(info, dict) and info.get("allowsNostr") else None
        if isinstance(candidate, str) and len(candidate) == 64:
            bytes.fromhex(candidate)
            pubkey = candidate.lower()
    except (FetchError, UnsafeAddress, ValueError):
        pubkey = None
    cache[address] = (pubkey, _time.monotonic() + _ZAP_PROVIDER_TTL)
    return pubkey


async def _zap_receipt(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    """A zap receipt (NIP-57) for a local ID: a notification, which is also
    the receipt's home."""
    repository = app.state.repository
    locals_ = await _local_ids_for(app, [t[1] for t in _tags(raw, "p")])
    if not locals_:
        return REJECTED
    local = locals_[0]
    # Only the ID's own Lightning provider can say it was paid (NIP-57).
    if raw["pubkey"] != await zap_provider_pubkey(app, local):
        return IngestResult(False, "restricted: not from this account's Lightning provider")
    description = next((t[1] for t in _tags(raw, "description")), "")
    try:
        request = json.loads(description) if description else {}
        if request:
            nostr_event.verify(request)
    except (ValueError, nostr_event.InvalidEvent):
        return IngestResult(False, "invalid: the zap request isn't a valid signed event")
    if not isinstance(request, dict) or request.get("kind") != 9734:
        return IngestResult(False, "invalid: the zap request isn't a valid signed event")
    sender = request.get("pubkey")
    amount_msats = next((t[1] for t in (request.get("tags") or []) if isinstance(t, list) and len(t) >= 2 and t[0] == "amount"), None)
    sats = int(amount_msats) // 1000 if amount_msats and str(amount_msats).isdigit() else None
    note = str(request.get("content") or "").strip()
    zapped = next((t[1] for t in _tags(raw, "e")), None)
    row = await repository.get_event(zapped) if zapped else None
    own = (row.room_id, row.matrix_event_id) if row and row.room_id and row.matrix_event_id else None
    extra = {EMBED_FIELD: raw}
    if isinstance(sender, str) and len(sender) == 64:
        event_id = await notify_format.send(app, local, "zap", sender, own=own, sats=sats, their_text=note, extra=extra)
    else:
        amount = f" {sats:,} sats" if sats else ""
        event_id = await notify(app, local, f"⚡ Someone zapped you{amount}", f"<p>⚡ Someone zapped you{html.escape(amount)}</p>", extra=extra)
    if event_id:
        room = (await repository.get_user_rooms(local.matrix_user_id)).notifications_room_id
        await _store(app, raw, room_id=room or "", matrix_event_id=event_id, scope="public")
    return IngestResult(True, "", stored=bool(event_id))


async def _reaction(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    repository = app.state.repository
    target_id = reaction_target(raw)
    target = await repository.get_event(target_id) if target_id else None
    if target is None or not target.room_id or not target.matrix_event_id:
        return REJECTED
    key = reaction_key(raw)
    if key is None:
        return IngestResult(False, "restricted: dislikes aren't shown")
    local = await repository.get_id_by_pubkey(target.pubkey)
    if local is None and not await repository.list_followers_of_remote(raw["pubkey"]):
        return REJECTED
    if local is not None and await repository.is_blocked_by_any(raw["pubkey"], [local.id]):
        return IngestResult(False, "blocked: not accepted")
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    reaction, extra = key
    emoji_url = extra.pop("_emoji_url", None)
    if emoji_url and app.state.config.bridge.msc4027_custom_reactions:
        mxc = await _emoji_mxc(app, emoji_url, remote.ghost_user_id)
        if mxc is None:
            extra = {}  # the image couldn't be had: the ":name:" text
        else:
            reaction = mxc
    else:
        extra = {}
    await remotes.ensure_ghost_in_room(app, remote.ghost_user_id, target.room_id)
    content = _with_extras(
        app, {"m.relates_to": {"rel_type": "m.annotation", "event_id": target.matrix_event_id, "key": reaction}, **extra}, raw, remote
    )
    try:
        reaction_id = await _send(app, target.room_id, content, sender=remote.ghost_user_id, ts=None, event_type="m.reaction")
    except HomeserverError:
        logger.info("Could not mirror reaction %s", raw["id"], exc_info=True)
        return IngestResult(False, "error: could not store this reaction")
    await _store(app, raw, room_id=target.room_id, matrix_event_id=reaction_id, scope="public" if local else "followed")
    if local is not None:
        own = (target.room_id, target.matrix_event_id)
        if reaction == "👍":
            await notify_format.send(app, local, "like", raw["pubkey"], own=own)
        elif reaction.startswith("mxc://"):
            await notify_format.send(app, local, "like", raw["pubkey"], own=own, reaction=raw.get("content", ""), reaction_mxc=reaction)
        else:
            await notify_format.send(app, local, "like", raw["pubkey"], own=own, reaction=reaction)
    return IngestResult(True, "", stored=True)


async def _emoji_mxc(app: FastAPI, url: str, ghost: str) -> str | None:
    cache: dict[str, str] = app.state.emoji_cache
    if url in cache:
        return cache[url]
    from bridge.render import Attachment, copy_attachment

    copied = await copy_attachment(app, Attachment(url, None), as_user_id=ghost)
    if copied is None:
        return None
    cache[url] = copied["url"]
    return copied["url"]


async def _repost(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    repository = app.state.repository
    target_id = reaction_target(raw)
    if not target_id:
        return REJECTED
    target = await repository.get_event(target_id)
    local = await repository.get_id_by_pubkey(target.pubkey) if target else None
    followed = bool(await repository.list_followers_of_remote(raw["pubkey"]))
    remote = await remotes.get_or_create_remote(app, raw["pubkey"]) if (local or followed) else None
    if remote is None:
        return REJECTED
    reaction: tuple[str, str] | None = None
    if local is not None and target is not None and target.room_id and target.matrix_event_id:
        # One of ours: a 🔁 on the post and a notification for its author.
        if await repository.is_blocked_by_any(raw["pubkey"], [local.id]):
            return IngestResult(False, "blocked: not accepted")
        await remotes.ensure_ghost_in_room(app, remote.ghost_user_id, target.room_id)
        content = _with_extras(
            app, {"m.relates_to": {"rel_type": "m.annotation", "event_id": target.matrix_event_id, "key": "🔁"}}, raw, remote
        )
        reaction_id = await _send(app, target.room_id, content, sender=remote.ghost_user_id, ts=None, event_type="m.reaction")
        await notify_format.send(app, local, "repost", raw["pubkey"], own=(target.room_id, target.matrix_event_id))
        if not followed:
            await _store(app, raw, room_id=target.room_id, matrix_event_id=reaction_id, scope="public")
            return IngestResult(True, "", stored=True)
        # Followed too: the repost also belongs in the reposter's own room,
        # like any of its reposts. The 🔁 is a copy, undone along with it.
        reaction = (target.room_id, reaction_id)
    if not followed:
        return REJECTED
    return await _repost_card(app, raw, remote, target_id, reaction)


async def _repost_card(
    app: FastAPI, raw: dict[str, Any], remote: RemoteAccount, target_id: str, reaction: tuple[str, str] | None
) -> IngestResult:
    """A followed account's repost, shown in its room as "🔁 reposted X's
    post:" with the original, and the MSC4501 repost relation to it.
    ``reaction``: the 🔁 already on the original (a local post), recorded
    as a copy so that undoing the repost removes it too."""
    repository = app.state.repository
    original = await _original_of_repost(app, raw, target_id)
    if original is None:
        return IngestResult(False, "restricted: the reposted post couldn't be found")
    # Our own fediverse post, republished on Nostr (by Mostr): point at the
    # Matrix post it came from rather than copying it to a ghost's room.
    source = await matrix_source(app, original)
    original_row = None if source else await _ensure_post_in_matrix(app, original)
    remote = await remotes.ensure_room(app, remote)
    assert remote.room_id is not None
    target: tuple[str, str, str] | None = None
    if source is not None:
        target = source
        author_label = await _matrix_name(app, source[2])
    else:
        author_label = await _author_label(app, original["pubkey"])
        if original_row is not None and original_row.room_id and original_row.matrix_event_id:
            target = (original_row.room_id, original_row.matrix_event_id, await _author_mxid(app, original["pubkey"]))
    link = _event_link(target[0], target[1]) if target else _viewer_link(app, original["id"])
    content = await message_content(
        app, original, as_user_id=remote.ghost_user_id,
        header_plain=f"🔁 reposted {author_label}'s post:",
        header_html=f'🔁 reposted {html.escape(author_label)}\'s <a href="{link}">post</a>:',
        media=await _matrix_media(app, target[0], target[1]) if target else None,
    )
    if app.state.config.bridge.set_msc4501_relates_to and target is not None:
        content[SOCIAL_RELATES_TO_FIELD] = {
            "rel_type": "org.matrix.msc4501.social.repost", "event_id": target[1],
            "room_id": target[0], "sender": target[2],
            "via": [app.state.config.homeserver.server_name],
            **({"content_inline": True} if app.state.config.bridge.use_msc4501_content_inline else {}),
        }
    event_id = await _send(
        app, remote.room_id, _with_extras(app, content, raw, remote, link=event_url(app, original)), sender=remote.ghost_user_id,
        ts=int(raw["created_at"]) * 1000, event_type=_post_event_type(app),
    )
    await _store(app, raw, room_id=remote.room_id, matrix_event_id=event_id, scope="public" if reaction else "followed")
    if original_row is not None:
        await repository.put_mirrored_event(event_id, original_row.id, remote.room_id)
    if reaction is not None:
        await repository.put_mirrored_event(reaction[1], raw["id"], reaction[0])
    return IngestResult(True, "", stored=True)


async def repair_repost_cards(app: FastAPI) -> int:
    """Startup pass: reposts of local posts by followed accounts that only
    got a 🔁 on the post (before such reposts were also shown in the
    reposter's room) get their card. Returns how many were repaired."""
    from bridge.event_sources import rebuild

    repository = app.state.repository
    repaired = 0
    for row in await repository.list_events_of_kinds(tuple(REPOST_KINDS)):
        if not row.room_id or not row.matrix_event_id or await repository.list_mirrored_copies(row.id):
            continue
        local = await repository.get_id_by_pubkey(row.pubkey)
        if local is not None:
            # A local user's 🔁 from before reposts left a card.
            if await _repair_own_repost_card(app, local, row):
                repaired += 1
            continue
        remote = await repository.get_remote_account(row.pubkey)
        if remote is None or not remote.room_id or row.room_id == remote.room_id:
            continue
        if not await repository.list_followers_of_remote(row.pubkey):
            continue
        rebuilt = await rebuild(app, row)
        if rebuilt.status != "ok" or rebuilt.event is None:
            continue
        raw = rebuilt.event
        target_id = reaction_target(raw)
        if not target_id:
            continue
        reaction = (row.room_id, row.matrix_event_id)
        await repository.delete_event(row.id)
        try:
            result = await _repost_card(app, raw, remote, target_id, reaction)
        except Exception:
            result = None
            logger.warning("Could not repair the repost %s", row.id, exc_info=True)
        if result is None or not result.stored:
            # Leave it as it was.
            await repository.put_event(dataclasses.replace(row, tags=index_tags(raw.get("tags") or [])))
            continue
        repaired += 1
    if repaired:
        logger.info("Showed %d earlier repost(s) in their reposters' rooms", repaired)
    return repaired


async def _repair_own_repost_card(app: FastAPI, local: NostrId, row: EventRow) -> bool:
    from bridge.interactions import post_repost_card

    if not local.profile_room_id or row.room_id == local.profile_room_id:
        return False
    try:
        reaction = await app.state.homeserver.get_event(row.room_id, row.matrix_event_id, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return False
    relates = ((reaction or {}).get("content") or {}).get("m.relates_to") or {}
    if (reaction or {}).get("type") != "m.reaction" or not isinstance(relates.get("event_id"), str):
        return False
    target = await app.state.repository.get_event_by_matrix_event(relates["event_id"])
    if target is None:
        return False
    await post_repost_card(app, local, target, room_id=row.room_id, target_event=relates["event_id"], repost=row)
    return bool(await app.state.repository.list_mirrored_copies(row.id))


async def _original_of_repost(app: FastAPI, raw: dict[str, Any], target_id: str) -> dict[str, Any] | None:
    try:
        embedded = json.loads(raw.get("content") or "")
        if isinstance(embedded, dict) and embedded.get("id") == target_id:
            nostr_event.verify(embedded)
            return embedded
    except (ValueError, nostr_event.InvalidEvent):
        pass
    return await fetch_event(app, target_id, hints=[t[2] for t in _tags(raw, "e") if len(t) >= 3 and t[2]])


async def fetch_event(app: FastAPI, event_id: str, *, hints: list[str] = (), author: str | None = None) -> dict[str, Any] | None:
    """Look an event up on relays (hints, its author's relays, fallbacks)."""
    row = await app.state.repository.get_event(event_id)
    if row is not None:
        from bridge.event_sources import rebuild

        rebuilt = await rebuild(app, row)
        if rebuilt.status == "ok":
            return rebuilt.event
    relays = [h for h in hints if isinstance(h, str) and h.startswith(("wss://", "ws://"))]
    if author:
        remote = await app.state.repository.get_remote_account(author)
        relays += remotes.write_relays_for(app, remote)
    relays += list(app.state.config.nostr.fallback_relays)
    for found in await app.state.relay_pool.query(relays, [{"ids": [event_id]}], timeout=6.0):
        if found.get("id") == event_id:
            try:
                nostr_event.verify(found)
                return found
            except nostr_event.InvalidEvent:
                continue
    return None


async def _author_label(app: FastAPI, pubkey: str) -> str:
    local = await app.state.repository.get_id_by_pubkey(pubkey)
    if local is not None:
        return f"{local.name}@{app.state.config.bridge.domain}"
    remote = await remotes.get_or_create_remote(app, pubkey)
    return await _ghost_label(app, remote)


async def _author_mxid(app: FastAPI, pubkey: str) -> str:
    local = await app.state.repository.get_id_by_pubkey(pubkey)
    if local is not None:
        return local.matrix_user_id
    return remotes.ghost_user_id(app, pubkey)


_OWN_NOTE_PATH = re.compile(r"/actor/[^/]+/notes/[0-9A-Za-z_-]+")
_MATRIX_ID = {"room_id": "!", "event_id": "$", "sender": "@"}


async def matrix_source(app: FastAPI, raw: dict[str, Any]) -> tuple[str, str, str] | None:
    """For a post another bridge (Mostr) republished from this domain's
    ActivityPub bridge (a ``proxy`` tag naming one of its notes): the
    Matrix post it came from, as (room_id, event_id, sender), asked of the
    ActivityPub bridge at ``<note>/matrix``. None for anything else."""
    from bridge.safe_fetch import FetchError, UnsafeAddress

    domain = app.state.config.bridge.domain.lower()
    for tag in _tags(raw, "proxy"):
        if len(tag) < 3 or tag[2] != "activitypub":
            continue
        parts = urlsplit(tag[1])
        if parts.scheme != "https" or (parts.hostname or "") != domain or parts.query or not _OWN_NOTE_PATH.fullmatch(parts.path):
            continue
        try:
            fetched = await app.state.fetcher.get(tag[1] + "/matrix", max_bytes=16 * 1024, accept="application/json")
            found = json.loads(fetched.content) if fetched.status == 200 else None
        except (FetchError, UnsafeAddress, ValueError):
            return None
        if not isinstance(found, dict) or not all(
            isinstance(found.get(k), str) and found[k].startswith(p) and (k == "event_id" or ":" in found[k])
            for k, p in _MATRIX_ID.items()
        ):
            return None
        return found["room_id"], found["event_id"], found["sender"]
    return None


async def _matrix_media(app: FastAPI, room_id: str, event_id: str) -> dict[str, Any] | None:
    """The image/video/audio of a post already in Matrix, for a repost card
    to show without downloading it again (a big video can take longer than
    the fetch allows a second time)."""
    try:
        event = await app.state.homeserver.get_event(room_id, event_id, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return None
    content = (event or {}).get("content") or {}
    url = content.get("url")
    if content.get("msgtype") not in ("m.image", "m.video", "m.audio") or not isinstance(url, str) or not url.startswith("mxc://"):
        return None
    info = content.get("info") if isinstance(content.get("info"), dict) else {}
    filename = content.get("filename") or content.get("body") or "file"
    return {"url": url, "info": info, "filename": filename if isinstance(filename, str) else "file"}


async def _matrix_name(app: FastAPI, user_id: str) -> str:
    try:
        name = (await app.state.homeserver.get_profile(user_id)).get("displayname")
    except HomeserverError:
        name = None
    return name if isinstance(name, str) and name.strip() else user_id


async def _ensure_post_in_matrix(app: FastAPI, raw: dict[str, Any]) -> EventRow | None:
    """A post brought into Matrix so something can refer to it (a repost, the
    start of a conversation): in its author's Remote User Room, which is
    created if needed (nobody is invited to it just for this)."""
    repository = app.state.repository
    existing = await repository.get_event(raw["id"])
    if existing is not None:
        return existing
    if await repository.get_id_by_pubkey(raw["pubkey"]) is not None:
        return None  # a local post that isn't in Matrix: deleted
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    remote = await remotes.ensure_room(app, remote)
    assert remote.room_id is not None
    content = _with_extras(app, await message_content(app, raw, as_user_id=remote.ghost_user_id), raw, remote, link=event_url(app, raw))
    event_id = await _send(app, remote.room_id, content, sender=remote.ghost_user_id, ts=int(raw["created_at"]) * 1000, event_type=_post_event_type(app))
    await _store(app, raw, room_id=remote.room_id, matrix_event_id=event_id, scope="followed")
    return await repository.get_event(raw["id"])


async def _post(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    repository = app.state.repository
    author = raw["pubkey"]
    parent_id, root_id = reply_targets(raw)
    parent = await repository.get_event(parent_id) if parent_id else None
    followed = bool(await repository.list_followers_of_remote(author))
    mentioned = await _local_ids_for(app, [t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] in ("p", "P")])
    quoted_ids = [t[1] for t in _tags(raw, "q")]

    if parent is None and parent_id and followed:
        # A followed account answering a post that isn't in Matrix: bring
        # the post in first, so the reply has something to thread under.
        original = await fetch_event(app, parent_id, hints=[t[2] for t in _tags(raw, "e") if len(t) >= 3 and t[2]])
        if original is not None:
            parent = await _ensure_post_in_matrix(app, original)

    if parent is not None and parent.room_id and parent.matrix_event_id:
        reaction = await _reaction_context(app, parent) if parent.kind in REACTION_KINDS else None
        if parent.kind in REACTION_KINDS and reaction is None:
            return IngestResult(False, "restricted: the reaction it answers isn't in Matrix")
        return await _reply_in_matrix(app, raw, parent, followed=followed, mentioned=mentioned, reaction=reaction)
    if followed:
        return await _followed_post(app, raw, parent_id=parent_id)
    if mentioned:
        return await _mention(app, raw, mentioned, quoted_ids)
    return REJECTED


@dataclass(frozen=True)
class _Reacted:
    """What a reply to a reaction is about: the Matrix event reacted to, and
    the reaction as text ("👍", ":blobcat:") and, for a custom emoji, its
    image."""

    event_id: str
    key: str
    mxc: str | None


async def _reaction_context(app: FastAPI, reaction: EventRow) -> _Reacted | None:
    """For a reply to a reaction (a NIP-22 comment on a kind 7): the Matrix
    event that was reacted to, which the reply threads under (a reaction
    can't have a thread of its own), and the reaction."""
    try:
        event = await app.state.homeserver.get_event(reaction.room_id or "", reaction.matrix_event_id or "",
                                                     as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return None
    content = (event or {}).get("content") or {}
    relation = content.get("m.relates_to") or {}
    target = relation.get("event_id")
    if not isinstance(target, str):
        return None
    key = str(relation.get("key") or "")
    mxc = key if key.startswith("mxc://") else None
    # A custom emoji's name: MSC4027's "shortcode", or Beeper's field for it.
    name = content.get("shortcode") or content.get("com.beeper.reaction.shortcode")
    text = str(name) if isinstance(name, str) and name else ("a custom emoji" if mxc or not key else key)
    return _Reacted(target, text, mxc)


def _reaction_html(reaction: _Reacted) -> str:
    """The reaction as shown: a custom emoji as its image, else its text."""
    if reaction.mxc:
        name = html.escape(reaction.key, quote=True)
        return f'<img data-mx-emoticon height="32" src="{html.escape(reaction.mxc, quote=True)}" alt="{name}" title="{name}">'
    return html.escape(f'"{reaction.key}"')


async def _reacted_post_author(app: FastAPI, event_id: str, local: NostrId) -> str | None:
    """Whose post a reaction was on, as a notification names them: None when
    it's the reactor's own."""
    row = await app.state.repository.get_event_by_matrix_event(event_id)
    if row is None or row.pubkey == local.pubkey:
        return None
    return await _author_label(app, row.pubkey)


async def _reply_in_matrix(
    app: FastAPI, raw: dict[str, Any], parent: EventRow, *, followed: bool, mentioned: list[NostrId],
    reaction: _Reacted | None = None,
) -> IngestResult:
    """``reaction``: the parent is a reaction, so the reply goes in the thread
    of what was reacted to, under a "Reply to X's reaction" line."""
    repository = app.state.repository
    parent_local = await repository.get_id_by_pubkey(parent.pubkey)
    if parent_local is not None and await repository.is_blocked_by_any(raw["pubkey"], [parent_local.id]):
        return IngestResult(False, "blocked: not accepted")
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    assert parent.room_id and parent.matrix_event_id
    await remotes.ensure_ghost_in_room(app, remote.ghost_user_id, parent.room_id)
    anchor = reaction.event_id if reaction else parent.matrix_event_id
    thread_root = await _thread_root_of(app, parent.room_id, anchor)
    header_plain = header_html = ""
    if reaction is not None:
        reactor = await _author_label(app, parent.pubkey)
        header_plain = f'⤵️ Reply to {reactor}\'s reaction "{reaction.key}":'
        reactor_pill = _pill(await _author_mxid(app, parent.pubkey), reactor)
        header_html = f"⤵️ Reply to {reactor_pill}'s reaction " + _reaction_html(reaction) + ":"
    content = await message_content(app, raw, as_user_id=remote.ghost_user_id,
                                    header_plain=header_plain, header_html=header_html)
    content["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": thread_root,
        # A reply to a reaction is in the post's thread, not an answer to the
        # post itself: the reply part is only for clients without threads.
        "is_falling_back": reaction is not None,
        "m.in_reply_to": {"event_id": anchor},
    }
    event_id = await _send(
        app, parent.room_id, _with_extras(app, content, raw, remote, link=event_url(app, raw)), sender=remote.ghost_user_id,
        ts=int(raw["created_at"]) * 1000,
    )
    await _store(app, raw, room_id=parent.room_id, matrix_event_id=event_id, scope="public" if parent_local or mentioned else "followed")
    notified: set[int] = set()
    theirs = (parent.room_id, event_id)
    if parent_local is not None and reaction is not None:
        await notify_format.send(app, parent_local, "reaction_reply", raw["pubkey"], own=(parent.room_id, anchor),
                                 theirs=theirs, post_author=await _reacted_post_author(app, anchor, parent_local))
        notified.add(parent_local.id)
    elif parent_local is not None:
        await notify_format.send(
            app, parent_local, "reply", raw["pubkey"], own=(parent.room_id, parent.matrix_event_id), theirs=theirs
        )
        notified.add(parent_local.id)
    for local in mentioned:
        if local.id not in notified and not await repository.is_blocked_by_any(raw["pubkey"], [local.id]):
            await notify_format.send(app, local, "mention", raw["pubkey"], theirs=theirs)
    if followed:
        # A copy in the replier's own room keeps it a full timeline.
        remote = await remotes.ensure_room(app, remote)
        if remote.room_id and remote.room_id != parent.room_id:
            parent_author = await _author_label(app, parent.pubkey)
            link = _event_link(parent.room_id, anchor)
            if reaction is not None:
                header_plain = f'⤵️ Reply to {parent_author}\'s reaction "{reaction.key}":'
                header_html = (f"⤵️ Reply to {_pill(await _author_mxid(app, parent.pubkey), parent_author)}'s "
                               f"<a href=\"{link}\">reaction</a> " + _reaction_html(reaction) + ":")
            else:
                header_plain = f"⤵️ Reply to {parent_author}'s post:"
                header_html = f'⤵️ Reply to {html.escape(parent_author)}\'s <a href="{link}">post</a>:'
            copy = await message_content(app, raw, as_user_id=remote.ghost_user_id,
                                         header_plain=header_plain, header_html=header_html)
            if app.state.config.bridge.set_msc4501_relates_to:
                copy[SOCIAL_RELATES_TO_FIELD] = {
                    "rel_type": "org.matrix.msc4501.social.reply", "event_id": anchor,
                    "room_id": parent.room_id, "sender": await _author_mxid(app, parent.pubkey),
                    "via": [app.state.config.homeserver.server_name],
                }
            copy["external_url"] = event_url(app, raw)
            copy_id = await _send(app, remote.room_id, copy, sender=remote.ghost_user_id, ts=int(raw["created_at"]) * 1000)
            await repository.put_mirrored_event(copy_id, raw["id"], remote.room_id)
    return IngestResult(True, "", stored=True)


async def _quote_footer(app: FastAPI, raw: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    """For a quote-post: "🔁 reposted X's post" and a preview of the quoted
    post, as matrix-appservice-atproto shows them, plus the MSC4501
    relation. Whether a quoted post that isn't in Matrix is brought in
    follows ``bridge.quote_import_policy``: "known" (only if its author
    already has a room here), "always", or "never"."""
    quoted_id = next((t[1] for t in _tags(raw, "q")), None)
    if not quoted_id:
        return "", "", None
    repository = app.state.repository
    row = await repository.get_event(quoted_id)
    quoted_raw = None
    if row is None:
        hints = [t[2] for t in _tags(raw, "q") if len(t) >= 3 and t[2]]
        quoted_raw = await fetch_event(app, quoted_id, hints=hints)
        if quoted_raw is not None:
            policy = app.state.config.bridge.quote_import_policy
            known = await repository.get_remote_account(quoted_raw["pubkey"]) is not None
            if policy == "always" or (policy == "known" and known):
                row = await _ensure_post_in_matrix(app, quoted_raw)
    author_pubkey = row.pubkey if row is not None else (quoted_raw or {}).get("pubkey")
    if not author_pubkey:
        return "", "", None
    author = await _author_label(app, author_pubkey)
    from bridge.notify_format import event_link, post_preview, quote_block

    relation = None
    if row is not None and row.room_id and row.matrix_event_id:
        link = event_link(app, row.room_id, row.matrix_event_id)
        preview, _media = await post_preview(app, row.room_id, row.matrix_event_id)
        if app.state.config.bridge.set_msc4501_relates_to:
            relation = {
                "rel_type": "org.matrix.msc4501.social.repost", "event_id": row.matrix_event_id, "room_id": row.room_id,
                "sender": await _author_mxid(app, author_pubkey), "via": [app.state.config.homeserver.server_name],
            }
    else:
        link = event_url(app, quoted_raw) if quoted_raw else _viewer_link(app, quoted_id)
        preview = ((quoted_raw or {}).get("content") or "")[:200]
    quoted_plain, quoted_html = quote_block(preview)
    plain = f"🔁 reposted {author}'s post\n{quoted_plain}".strip()
    rich = f'🔁 reposted {html.escape(author)}\'s <a href="{html.escape(link, quote=True)}">post</a>{quoted_html}'
    return plain, rich, relation


async def _followed_post(app: FastAPI, raw: dict[str, Any], *, parent_id: str | None) -> IngestResult:
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    remote = await remotes.ensure_room(app, remote)
    assert remote.room_id is not None
    header_plain = header_html = ""
    if parent_id:
        link = _viewer_link(app, parent_id)
        header_plain = "⤵️ Reply to a post:"
        header_html = f'⤵️ Reply to a <a href="{html.escape(link)}">post</a>:'
    footer_plain, footer_html, relation = await _quote_footer(app, raw)
    content = await message_content(
        app, raw, as_user_id=remote.ghost_user_id, header_plain=header_plain, header_html=header_html,
        footer_plain=footer_plain, footer_html=footer_html,
    )
    if relation is not None:
        content[SOCIAL_RELATES_TO_FIELD] = relation
    event_id = await _send(
        app, remote.room_id, _with_extras(app, content, raw, remote, link=event_url(app, raw)), sender=remote.ghost_user_id,
        ts=int(raw["created_at"]) * 1000, event_type=_post_event_type(app),
    )
    await _store(app, raw, room_id=remote.room_id, matrix_event_id=event_id, scope="followed")
    mentioned = await _local_ids_for(app, [t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] in ("p", "P")])
    for local in mentioned:
        if not await app.state.repository.is_blocked_by_any(raw["pubkey"], [local.id]):
            await notify_format.send(app, local, "mention", raw["pubkey"], theirs=(remote.room_id, event_id))
    return IngestResult(True, "", stored=True)


async def _mention(app: FastAPI, raw: dict[str, Any], mentioned: list[NostrId], quoted_ids: list[str]) -> IngestResult:
    """A post tagging local IDs that has no other home: the notification
    is its home (it carries the raw event)."""
    repository = app.state.repository
    rendered = await render(app, raw)
    quoted_local = None
    for quoted in quoted_ids:
        row = await repository.get_event(quoted)
        if row is not None and await repository.get_id_by_pubkey(row.pubkey) is not None:
            quoted_local = row
    stored = False
    for local in mentioned:
        if await repository.is_blocked_by_any(raw["pubkey"], [local.id]):
            continue
        is_quote = quoted_local is not None and quoted_local.pubkey == local.pubkey
        own = (quoted_local.room_id, quoted_local.matrix_event_id) if is_quote and quoted_local.room_id and quoted_local.matrix_event_id else None
        event_id = await notify_format.send(
            app, local, "quote" if is_quote else "mention", raw["pubkey"], own=own, their_text=rendered.body,
            extra={EMBED_FIELD: raw} if not stored else None,
        )
        if event_id and not stored:
            room = (await repository.get_user_rooms(local.matrix_user_id)).notifications_room_id
            await _store(app, raw, room_id=room or "", matrix_event_id=event_id, scope="public")
            stored = True
    return IngestResult(True, "", stored=stored) if stored else IngestResult(False, "blocked: not accepted")


# -- events by local IDs, from their other clients ------------------------------------


async def _per_message_profile(app: FastAPI, local: NostrId) -> dict[str, Any]:
    """MSC4144: the bot's message shown as the user, as the sibling bridges
    do it (their Matrix ID), with the ID's profile: its Profile Room's name
    and avatar."""
    profile: dict[str, Any] = {"id": local.matrix_user_id}
    bot = bot_mxid(app.state.config)
    name = avatar = None
    if local.profile_room_id:
        try:
            name = (await app.state.homeserver.get_room_state(local.profile_room_id, "m.room.name", as_user_id=bot) or {}).get("name")
            avatar = (await app.state.homeserver.get_room_state(local.profile_room_id, "m.room.avatar", as_user_id=bot) or {}).get("url")
        except HomeserverError:
            pass
    profile["displayname"] = name if isinstance(name, str) and name.strip() else f"{local.name}@{app.state.config.bridge.domain}"
    if isinstance(avatar, str) and avatar.startswith("mxc://"):
        profile["avatar_url"] = avatar
    return profile


async def _from_local_author(app: FastAPI, raw: dict[str, Any], local: NostrId, *, origin: str) -> IngestResult:
    """An event signed by a local ID that the bridge didn't make: posted from
    the user's other Nostr app (an imported key). Posts are mirrored into the
    ID's Profile Room by the bot, shown as the user (MSC4144); reactions and
    deletions are applied."""
    kind = raw["kind"]
    if kind == 5:
        return await _deletion(app, raw)
    if kind not in POST_KINDS or not local.profile_room_id:
        return IngestResult(False, "restricted: the bridge manages this ID's other events itself")
    room_id = local.profile_room_id
    content = await message_content(app, raw, as_user_id=bot_mxid(app.state.config))
    if app.state.config.bridge.msc4144_per_message_profiles:
        content[PER_MESSAGE_PROFILE_FIELD] = await _per_message_profile(app, local)
    # Where the post is on the web, as for every post the bridge shows.
    content["external_url"] = event_url(app, raw)
    parent_id, _ = reply_targets(raw)
    parent = await app.state.repository.get_event(parent_id) if parent_id else None
    if parent is not None and parent.room_id == room_id and parent.matrix_event_id:
        content["m.relates_to"] = {
            "rel_type": "m.thread", "event_id": await _thread_root_of(app, room_id, parent.matrix_event_id),
            "is_falling_back": False, "m.in_reply_to": {"event_id": parent.matrix_event_id},
        }
    content[EMBED_FIELD] = raw
    event_id = await _send(app, room_id, content, sender=bot_mxid(app.state.config), ts=int(raw["created_at"]) * 1000)
    await _store(app, raw, room_id=room_id, matrix_event_id=event_id, scope="public")
    return IngestResult(True, "", stored=True)
