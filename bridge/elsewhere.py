"""Events a local ID signs somewhere else: the user exported the key, or
brought their own, and used it in another Nostr app. Whatever they can do
there shows in Matrix as if they had done it here, sent by the bot on their
behalf and shown as them (MSC4144), since the bridge can't speak as a Matrix
user of its homeserver:

- a post: in their Profile Room; a reply: in the thread of the post it
  answers, in whichever room that post is shown (their Profile Room if the
  post isn't in Matrix);
- a reaction: on the post it reacts to, as a Matrix reaction would be;
- a repost: a 🔁 on the post, and the repost card in their Profile Room;
- a deletion: what it deletes is redacted;
- a profile (kind 0): the Profile Room's name, topic, avatar, banner and
  extra fields, from which the profile is then published as usual;
- a follow list or mute list (kinds 3 and 10000): the follows and blocks
  here are made to match, without publishing the list again over theirs;
- a DM they sent (their own gift-wrapped copy): in the DM room, as theirs
  (``bridge.dms``).

They arrive through the bridge's own relay, or the ``own`` filter on the
publish and fallback relays (``bridge.feeds``). What the bridge signed
itself is already stored and never gets this far, and an older version of
a list or profile than the one the bridge has is ignored, so its own echoes
are never applied twice.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import FastAPI

from bridge.event_sources import EMBED_FIELD, inline_source
from bridge.homeserver_client import HomeserverError
from bridge.repository import EventRow, NostrId
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

FOLLOW_LIST_KIND = 3
MUTE_LIST_KIND = 10000
PROFILE_KIND = 0
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_LIST = 2000
# A follow list adding more than this at once (an app restoring a backup)
# doesn't fetch everyone's recent posts.
_BACKFILL_UP_TO = 5


async def receive(app: FastAPI, raw: dict[str, Any], local: NostrId) -> Any:
    """An event signed by ``local``'s key that the bridge didn't make."""
    from bridge import inbound

    kind = raw["kind"]
    if kind == 5:
        return await inbound._deletion(app, raw)
    if kind in inbound.POST_KINDS:
        return await _post(app, raw, local)
    if kind == 7:
        return await _reaction(app, raw, local)
    if kind in inbound.REPOST_KINDS:
        return await _repost(app, raw, local)
    if kind == PROFILE_KIND:
        return await _profile(app, raw, local)
    if kind in (FOLLOW_LIST_KIND, MUTE_LIST_KIND):
        return await _list(app, raw, local)
    return inbound.IngestResult(False, "restricted: the bridge manages this ID's other events itself")


async def as_them(app: FastAPI, local: NostrId) -> dict[str, Any]:
    """The MSC4144 per-message profile that shows the bot's message as theirs."""
    from bridge.inbound import _per_message_profile
    from bridge.msc4501 import PER_MESSAGE_PROFILE_FIELD

    if not app.state.config.bridge.msc4144_per_message_profiles:
        return {}
    return {PER_MESSAGE_PROFILE_FIELD: await _per_message_profile(app, local)}


# -- posts and replies ------------------------------------------------------------------


async def _post(app: FastAPI, raw: dict[str, Any], local: NostrId):
    from bridge import inbound
    from bridge.interactions import notify_local_author

    repository = app.state.repository
    bot = bot_mxid(app.state.config)
    parent_id, _ = inbound.reply_targets(raw)
    parent = await repository.get_event(parent_id) if parent_id else None
    if parent is not None and not (parent.room_id and parent.matrix_event_id):
        parent = None
    room_id = parent.room_id if parent is not None else local.profile_room_id
    if not room_id:
        return inbound.IngestResult(False, "restricted: this ID has no Profile Room to show it in")
    content = await inbound.message_content(app, raw, as_user_id=bot)
    content.update(await as_them(app, local))
    # Where the post is on the web, as for every post the bridge shows.
    content["external_url"] = inbound.event_url(app, raw)
    if parent is not None:
        content["m.relates_to"] = {
            "rel_type": "m.thread", "event_id": await inbound._thread_root_of(app, room_id, parent.matrix_event_id),
            "is_falling_back": False, "m.in_reply_to": {"event_id": parent.matrix_event_id},
        }
    content[EMBED_FIELD] = raw
    try:
        event_id = await inbound._send(app, room_id, content, sender=bot, ts=int(raw["created_at"]) * 1000,
                                       event_type=inbound._post_event_type(app))
    except HomeserverError:
        if parent is None or not local.profile_room_id:
            raise
        # The post's room won't have the bot (left, say): a post of their own then.
        content.pop("m.relates_to", None)
        room_id = local.profile_room_id
        event_id = await inbound._send(app, room_id, content, sender=bot, ts=int(raw["created_at"]) * 1000,
                                       event_type=inbound._post_event_type(app))
    await inbound._store(app, raw, room_id=room_id, matrix_event_id=event_id, scope="public")
    if parent is not None and room_id == parent.room_id:
        await notify_local_author(app, local, parent, "reply", theirs=(room_id, event_id))
    return inbound.IngestResult(True, "", stored=True)


# -- reactions and reposts --------------------------------------------------------------


async def _shown_target(app: FastAPI, raw: dict[str, Any]) -> EventRow | None:
    """What a reaction or repost is on, as shown in Matrix: brought in if
    it's a post the bridge can find, ``None`` if it can't be shown."""
    from bridge import inbound

    target_id = inbound.reaction_target(raw)
    if not target_id:
        return None
    target = await app.state.repository.get_event(target_id)
    if target is None and raw["kind"] in inbound.REPOST_KINDS:
        found = await inbound._original_of_repost(app, raw, target_id)
        target = await inbound._ensure_post_in_matrix(app, found) if found else None
    if target is None or not target.room_id or not target.matrix_event_id:
        return None
    return target


async def _reaction(app: FastAPI, raw: dict[str, Any], local: NostrId):
    from bridge import inbound
    from bridge.interactions import notify_local_author

    target = await _shown_target(app, raw)
    if target is None or target.kind in (4, 5, 7, 1059):
        return inbound.IngestResult(False, "restricted: what it reacts to isn't shown here")
    key = inbound.reaction_key(raw)
    if key is None:
        return inbound.IngestResult(False, "restricted: dislikes aren't shown")
    reaction, extra = key
    bot = bot_mxid(app.state.config)
    emoji_url = extra.pop("_emoji_url", None)
    if emoji_url and app.state.config.bridge.msc4027_custom_reactions:
        mxc = await inbound._emoji_mxc(app, emoji_url, bot)
        if mxc is None:
            extra = {}
        else:
            reaction = mxc
    else:
        extra = {}
    content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": target.matrix_event_id, "key": reaction},
               **extra, **(await as_them(app, local)), EMBED_FIELD: raw}
    reaction_id = await inbound._send(app, target.room_id, content, sender=bot, ts=None, event_type="m.reaction")
    await inbound._store(app, raw, room_id=target.room_id, matrix_event_id=reaction_id, scope="public")
    shown = None if reaction == "👍" else raw.get("content", "")
    await notify_local_author(app, local, target, "like", reaction=shown)
    return inbound.IngestResult(True, "", stored=True)


async def _repost(app: FastAPI, raw: dict[str, Any], local: NostrId):
    from bridge import inbound
    from bridge.interactions import REPOST_KEY, notify_local_author, post_repost_card

    target = await _shown_target(app, raw)
    if target is None:
        return inbound.IngestResult(False, "restricted: the reposted post couldn't be found")
    bot = bot_mxid(app.state.config)
    content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": target.matrix_event_id, "key": REPOST_KEY},
               **(await as_them(app, local)), EMBED_FIELD: raw}
    reaction_id = await inbound._send(app, target.room_id, content, sender=bot, ts=None, event_type="m.reaction")
    await inbound._store(app, raw, room_id=target.room_id, matrix_event_id=reaction_id, scope="public")
    row = await app.state.repository.get_event(raw["id"])
    if row is not None:
        # The card is a copy of the repost: deleting the repost redacts it too.
        await post_repost_card(app, local, target, room_id=target.room_id, target_event=target.matrix_event_id, repost=row)
    await notify_local_author(app, local, target, "repost")
    return inbound.IngestResult(True, "", stored=True)


# -- profile ------------------------------------------------------------------------------


async def _newer_than_ours(app: FastAPI, raw: dict[str, Any], kind: int) -> bool:
    latest = await app.state.repository.get_latest_event(raw["pubkey"], kind)
    return latest is None or raw["created_at"] > latest.created_at


async def _profile(app: FastAPI, raw: dict[str, Any], local: NostrId):
    """Their profile, edited elsewhere, onto the Profile Room. The room
    changing republishes the profile from it, as any edit there does."""
    from bridge import inbound
    from bridge.identity_events import PROFILE_EXTRA_STATE_TYPE, _room_state
    from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE
    from bridge.remote import _fetch_image, parse_profile

    if not local.profile_room_id:
        return inbound.IngestResult(False, "restricted: a shadow ID's profile is its Matrix profile")
    if not await _newer_than_ours(app, raw, PROFILE_KIND):
        return inbound.IngestResult(False, "restricted: older than this ID's profile")
    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    room = local.profile_room_id
    fields = parse_profile(raw)
    state = await _room_state(app, room)
    ours = await _current_profile(app, local)

    def current(event_type: str, key: str) -> Any:
        return ((state.get(event_type) or {}).get("content") or {}).get(key)

    changed = 0
    if fields.get("name") and fields["name"] != current("m.room.name", "name"):
        await homeserver.send_state_event(room, "m.room.name", {"name": fields["name"]}, as_user_id=bot)
        changed += 1
    if (fields.get("about") or "") != (current("m.room.topic", "topic") or ""):
        await homeserver.send_state_event(room, "m.room.topic", {"topic": fields.get("about") or ""}, as_user_id=bot)
        changed += 1
    for field, event_type in (("picture", "m.room.avatar"), ("banner", PROFILE_BANNER_STATE_TYPE)):
        url = fields.get(field)
        if not url or url == ours.get(field):
            continue  # the same image the bridge published: nothing to copy
        image = await _fetch_image(app, url)
        if image is None:
            continue
        mxc = await homeserver.upload_media(image[0], image[1], field, as_user_id=bot)
        await homeserver.send_state_event(room, event_type, {"url": mxc}, as_user_id=bot)
        changed += 1
    extra = {k: fields[k] for k in ("website", "lud16") if fields.get(k)}
    old_extra = {k: v for k, v in ((state.get(PROFILE_EXTRA_STATE_TYPE) or {}).get("content") or {}).items() if v}
    if extra != old_extra:
        await homeserver.send_state_event(room, PROFILE_EXTRA_STATE_TYPE, extra, as_user_id=bot)
        changed += 1
    logger.info("Applied a profile %s made elsewhere to %s (%d changes)", raw["id"], local.name, changed)
    return inbound.IngestResult(True, "")


async def _current_profile(app: FastAPI, local: NostrId) -> dict[str, Any]:
    from bridge.interactions import raw_of

    latest = await app.state.repository.get_latest_event(local.pubkey, PROFILE_KIND)
    raw = await raw_of(app, latest) if latest is not None else None
    try:
        parsed = json.loads((raw or {}).get("content") or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# -- follow and mute lists ----------------------------------------------------------------


async def _list(app: FastAPI, raw: dict[str, Any], local: NostrId):
    """A follow or mute list from elsewhere: follows and blocks here are
    made to match. Kept as the latest list, so the bridge's next one starts
    from it (keeping what it can't model, like followed hashtags)."""
    from bridge import inbound
    from bridge.follows import Reference, follow

    kind = raw["kind"]
    if not await _newer_than_ours(app, raw, kind):
        return inbound.IngestResult(False, "restricted: older than this ID's list")
    repository = app.state.repository
    wanted = list(dict.fromkeys(
        t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "p" and _HEX_RE.match(t[1]) and t[1] != local.pubkey
    ))[:_MAX_LIST]
    hints = {t[1]: (t[2],) for t in raw.get("tags") or [] if len(t) >= 3 and t[0] == "p" and str(t[2]).startswith("wss://")}
    if kind == FOLLOW_LIST_KIND:
        have = set(await repository.list_follows(local.id))
        added = [p for p in wanted if p not in have]
        removed = [p for p in have if p not in set(wanted)]
        for pubkey in added:
            try:
                await follow(app, local, Reference(pubkey, hints.get(pubkey, ())), backfill=len(added) <= _BACKFILL_UP_TO,
                             publish=False)
            except Exception:
                logger.info("Could not follow %s for %s", pubkey, local.name, exc_info=True)
        for pubkey in removed:
            await repository.remove_follow(local.id, pubkey)
    else:
        have = set(await repository.list_blocks(local.id))
        added = [p for p in wanted if p not in have]
        removed = [p for p in have if p not in set(wanted)]
        for pubkey in added:
            await repository.set_block(local.id, pubkey, True)
        for pubkey in removed:
            await repository.set_block(local.id, pubkey, False)
    await repository.put_event(
        EventRow(
            id=raw["id"], pubkey=raw["pubkey"], kind=kind, created_at=int(raw["created_at"]), sig=raw["sig"],
            source=_json(inline_source(kind, raw.get("tags") or [], raw.get("content") or "")),
            tags=tuple((t[0], t[1]) for t in raw.get("tags") or [] if len(t) >= 2 and len(t[0]) == 1),
        ),
        replace=True,
    )
    app.state.feeds.changed()
    logger.info("Applied a %s made elsewhere to %s: +%d -%d", "follow list" if kind == FOLLOW_LIST_KIND else "mute list",
                local.name, len(added), len(removed))
    return inbound.IngestResult(True, "", stored=True)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
