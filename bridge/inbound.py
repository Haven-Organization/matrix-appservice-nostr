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
import html
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from bridge import remote as remotes
from bridge.event_sources import EMBED_FIELD, embedded_source
from bridge.homeserver_client import HomeserverError
from bridge.msc4501 import EXTERNAL_HANDLE_FIELD, external_handle_value
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19
from bridge.notifications import notify
from bridge.render import message_content, render
from bridge.repository import EventRow, NostrId, RemoteAccount
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

INDEXED_TAG_LETTERS = frozenset("epaqkdEPAK")
POST_KINDS = frozenset({1, 20, 21, 22, 1111, 30023})
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
            return content, {"com.beeper.reaction.shortcode": content, "_emoji_url": url}
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
    hub = getattr(app.state, "relay_hub", None)
    if hub is not None:
        hub.broadcast(raw, scope)


def _expiration(raw: dict[str, Any]) -> int | None:
    value = next((t[1] for t in _tags(raw, "expiration")), None)
    return int(value) if value and value.isdigit() else None


def _with_extras(app: FastAPI, content: dict[str, Any], raw: dict[str, Any], remote: RemoteAccount) -> dict[str, Any]:
    content = {**content, EMBED_FIELD: raw}
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
    if not app.state.seen.add(event.id):
        return IngestResult(True, "duplicate: already seen")

    guard = getattr(app.state, "spam", None)
    if guard is not None and origin in ("relay", "subscription"):
        verdict = await guard.check_event(
            event, ip=ip, tier=await _tier(app, event.pubkey), targets=targets,
            # An old interaction written to the relay now is suspicious; one
            # the bridge fetched itself (catching up) is not.
            interaction=origin == "relay" and event.kind in (1, 6, 7, 16, 1111, ZAP_RECEIPT_KIND),
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
                remote = await remotes.get_or_create_remote(app, raw["pubkey"])
                label = await _ghost_label(app, remote)
                await notify(
                    app, local, f"{label} followed you", f"{_pill(remote.ghost_user_id, label)} followed you"
                )
    return IngestResult(False, "restricted: not stored") if not touched else IngestResult(False, "restricted: noted, not stored")


async def _zap_receipt(app: FastAPI, raw: dict[str, Any]) -> IngestResult:
    """A zap receipt (NIP-57) for a local ID: a notification, which is also
    the receipt's home."""
    locals_ = await _local_ids_for(app, [t[1] for t in _tags(raw, "p")])
    if not locals_:
        return REJECTED
    description = next((t[1] for t in _tags(raw, "description")), "")
    try:
        request = json.loads(description) if description else {}
    except ValueError:
        request = {}
    sender = request.get("pubkey") if isinstance(request, dict) else None
    amount_msats = next((t[1] for t in (request.get("tags") or []) if isinstance(t, list) and len(t) >= 2 and t[0] == "amount"), None) if isinstance(request, dict) else None
    sats = int(amount_msats) // 1000 if amount_msats and str(amount_msats).isdigit() else None
    who_plain, who_html = "Someone", "Someone"
    if isinstance(sender, str) and len(sender) == 64:
        remote = await remotes.get_or_create_remote(app, sender)
        who_plain = await _ghost_label(app, remote)
        who_html = _pill(remote.ghost_user_id, who_plain)
    amount = f" {sats:,} sats" if sats else ""
    note = (request.get("content") or "").strip() if isinstance(request, dict) else ""
    local = locals_[0]
    event_id = await notify(
        app, local,
        f"⚡ {who_plain} zapped you{amount}" + (f": {note}" if note else ""),
        f"⚡ {who_html} zapped you{html.escape(amount)}" + (f": {html.escape(note)}" if note else ""),
        extra={EMBED_FIELD: raw},
    )
    if event_id:
        room = (await app.state.repository.get_user_rooms(local.matrix_user_id)).notifications_room_id
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
    if emoji_url:
        mxc = await _emoji_mxc(app, emoji_url, remote.ghost_user_id)
        if mxc is None:
            reaction, extra = "👍", {}
        else:
            reaction = mxc
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
        label = await _ghost_label(app, remote)
        shown = reaction if not reaction.startswith("mxc://") else raw.get("content", "")
        link = _event_link(target.room_id, target.matrix_event_id)
        await notify(
            app, local,
            f"{label} reacted {shown} to your post: {link}",
            f'{_pill(remote.ghost_user_id, label)} reacted {html.escape(shown)} to <a href="{link}">your post</a>',
        )
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
    if local is not None and target is not None and target.room_id and target.matrix_event_id:
        if await repository.is_blocked_by_any(raw["pubkey"], [local.id]):
            return IngestResult(False, "blocked: not accepted")
        await remotes.ensure_ghost_in_room(app, remote.ghost_user_id, target.room_id)
        content = _with_extras(
            app, {"m.relates_to": {"rel_type": "m.annotation", "event_id": target.matrix_event_id, "key": "🔁"}}, raw, remote
        )
        reaction_id = await _send(app, target.room_id, content, sender=remote.ghost_user_id, ts=None, event_type="m.reaction")
        await _store(app, raw, room_id=target.room_id, matrix_event_id=reaction_id, scope="public")
        label = await _ghost_label(app, remote)
        link = _event_link(target.room_id, target.matrix_event_id)
        await notify(
            app, local, f"{label} reposted your post: {link}",
            f'{_pill(remote.ghost_user_id, label)} reposted <a href="{link}">your post</a>',
        )
        return IngestResult(True, "", stored=True)
    if not followed:
        return REJECTED
    original = await _original_of_repost(app, raw, target_id)
    if original is None:
        return IngestResult(False, "restricted: the reposted post couldn't be found")
    original_row = await _ensure_post_in_matrix(app, original)
    remote = await remotes.ensure_room(app, remote)
    assert remote.room_id is not None
    author_label = await _author_label(app, original["pubkey"])
    if original_row is not None and original_row.room_id and original_row.matrix_event_id:
        link = _event_link(original_row.room_id, original_row.matrix_event_id)
    else:
        link = _viewer_link(app, original["id"])
    content = await message_content(
        app, original, as_user_id=remote.ghost_user_id,
        header_plain=f"🔁 reposted {author_label}'s post:",
        header_html=f'🔁 reposted {html.escape(author_label)}\'s <a href="{link}">post</a>:',
    )
    if app.state.config.bridge.set_msc4501_relates_to and original_row is not None and original_row.matrix_event_id:
        content[SOCIAL_RELATES_TO_FIELD] = {
            "rel_type": "org.matrix.msc4501.social.repost", "event_id": original_row.matrix_event_id,
            "room_id": original_row.room_id, "sender": await _author_mxid(app, original["pubkey"]),
            "via": [app.state.config.homeserver.server_name],
            **({"content_inline": True} if app.state.config.bridge.use_msc4501_content_inline else {}),
        }
    event_id = await _send(
        app, remote.room_id, _with_extras(app, content, raw, remote), sender=remote.ghost_user_id,
        ts=int(raw["created_at"]) * 1000, event_type=_post_event_type(app),
    )
    await _store(app, raw, room_id=remote.room_id, matrix_event_id=event_id, scope="followed")
    if original_row is not None:
        await repository.put_mirrored_event(event_id, original_row.id, remote.room_id)
    return IngestResult(True, "", stored=True)


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
    content = _with_extras(app, await message_content(app, raw, as_user_id=remote.ghost_user_id), raw, remote)
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
        return await _reply_in_matrix(app, raw, parent, followed=followed, mentioned=mentioned)
    if followed:
        return await _followed_post(app, raw, parent_id=parent_id)
    if mentioned:
        return await _mention(app, raw, mentioned, quoted_ids)
    return REJECTED


async def _reply_in_matrix(
    app: FastAPI, raw: dict[str, Any], parent: EventRow, *, followed: bool, mentioned: list[NostrId]
) -> IngestResult:
    repository = app.state.repository
    parent_local = await repository.get_id_by_pubkey(parent.pubkey)
    if parent_local is not None and await repository.is_blocked_by_any(raw["pubkey"], [parent_local.id]):
        return IngestResult(False, "blocked: not accepted")
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    assert parent.room_id and parent.matrix_event_id
    await remotes.ensure_ghost_in_room(app, remote.ghost_user_id, parent.room_id)
    thread_root = await _thread_root_of(app, parent.room_id, parent.matrix_event_id)
    content = await message_content(app, raw, as_user_id=remote.ghost_user_id)
    content["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": thread_root,
        "is_falling_back": False,
        "m.in_reply_to": {"event_id": parent.matrix_event_id},
    }
    event_id = await _send(
        app, parent.room_id, _with_extras(app, content, raw, remote), sender=remote.ghost_user_id,
        ts=int(raw["created_at"]) * 1000,
    )
    await _store(app, raw, room_id=parent.room_id, matrix_event_id=event_id, scope="public" if parent_local or mentioned else "followed")
    label = await _ghost_label(app, remote)
    rendered = await render(app, raw)
    notified: set[int] = set()
    if parent_local is not None:
        link = _event_link(parent.room_id, event_id)
        await notify(
            app, parent_local,
            f"{label} replied to your post: {rendered.body}\n{link}",
            f'{_pill(remote.ghost_user_id, label)} <a href="{link}">replied</a> to your post: {rendered.html}',
        )
        notified.add(parent_local.id)
    for local in mentioned:
        if local.id not in notified and not await repository.is_blocked_by_any(raw["pubkey"], [local.id]):
            link = _event_link(parent.room_id, event_id)
            await notify(
                app, local, f"{label} mentioned you: {rendered.body}\n{link}",
                f'{_pill(remote.ghost_user_id, label)} <a href="{link}">mentioned you</a>: {rendered.html}',
            )
    if followed:
        # A copy in the replier's own room keeps it a full timeline.
        remote = await remotes.ensure_room(app, remote)
        if remote.room_id and remote.room_id != parent.room_id:
            parent_author = await _author_label(app, parent.pubkey)
            link = _event_link(parent.room_id, parent.matrix_event_id)
            copy = await message_content(
                app, raw, as_user_id=remote.ghost_user_id,
                header_plain=f"⤵️ Reply to {parent_author}'s post:",
                header_html=f'⤵️ Reply to {html.escape(parent_author)}\'s <a href="{link}">post</a>:',
            )
            if app.state.config.bridge.set_msc4501_relates_to:
                copy[SOCIAL_RELATES_TO_FIELD] = {
                    "rel_type": "org.matrix.msc4501.social.reply", "event_id": parent.matrix_event_id,
                    "room_id": parent.room_id, "sender": await _author_mxid(app, parent.pubkey),
                    "via": [app.state.config.homeserver.server_name],
                }
            copy_id = await _send(app, remote.room_id, copy, sender=remote.ghost_user_id, ts=int(raw["created_at"]) * 1000)
            await repository.put_mirrored_event(copy_id, raw["id"], remote.room_id)
    return IngestResult(True, "", stored=True)


async def _followed_post(app: FastAPI, raw: dict[str, Any], *, parent_id: str | None) -> IngestResult:
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    remote = await remotes.ensure_room(app, remote)
    assert remote.room_id is not None
    header_plain = header_html = ""
    if parent_id:
        link = _viewer_link(app, parent_id)
        header_plain = "⤵️ Reply to a post:"
        header_html = f'⤵️ Reply to a <a href="{html.escape(link)}">post</a>:'
    content = await message_content(app, raw, as_user_id=remote.ghost_user_id, header_plain=header_plain, header_html=header_html)
    event_id = await _send(
        app, remote.room_id, _with_extras(app, content, raw, remote), sender=remote.ghost_user_id,
        ts=int(raw["created_at"]) * 1000, event_type=_post_event_type(app),
    )
    await _store(app, raw, room_id=remote.room_id, matrix_event_id=event_id, scope="followed")
    mentioned = await _local_ids_for(app, [t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] in ("p", "P")])
    if mentioned:
        label = await _ghost_label(app, remote)
        rendered = await render(app, raw)
        link = _event_link(remote.room_id, event_id)
        for local in mentioned:
            await notify(
                app, local, f"{label} mentioned you: {rendered.body}\n{link}",
                f'{_pill(remote.ghost_user_id, label)} <a href="{link}">mentioned you</a>: {rendered.html}',
            )
    return IngestResult(True, "", stored=True)


async def _mention(app: FastAPI, raw: dict[str, Any], mentioned: list[NostrId], quoted_ids: list[str]) -> IngestResult:
    """A post tagging local IDs that has no other home: the notification
    is its home."""
    repository = app.state.repository
    remote = await remotes.get_or_create_remote(app, raw["pubkey"])
    label = await _ghost_label(app, remote)
    rendered = await render(app, raw)
    link = _viewer_link(app, raw["id"])
    quoted_local = None
    for quoted in quoted_ids:
        row = await repository.get_event(quoted)
        if row is not None and await repository.get_id_by_pubkey(row.pubkey) is not None:
            quoted_local = row
    stored = False
    for local in mentioned:
        if await repository.is_blocked_by_any(raw["pubkey"], [local.id]):
            continue
        verb = "quoted your post" if quoted_local is not None and quoted_local.pubkey == local.pubkey else "mentioned you"
        event_id = await notify(
            app, local, f"{label} {verb}: {rendered.body}\n{link}",
            f'{_pill(remote.ghost_user_id, label)} <a href="{html.escape(link)}">{verb}</a>: {rendered.html}',
            extra={EMBED_FIELD: raw} if not stored else None,
        )
        if event_id and not stored:
            room = (await repository.get_user_rooms(local.matrix_user_id)).notifications_room_id
            await _store(app, raw, room_id=room or "", matrix_event_id=event_id, scope="public")
            stored = True
    return IngestResult(True, "", stored=stored) if stored else IngestResult(False, "blocked: not accepted")


# -- events by local IDs, from their other clients ------------------------------------


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
        content["com.beeper.per_message_profile"] = {
            "id": local.pubkey, "displayname": f"{local.name}@{app.state.config.bridge.domain}",
        }
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
