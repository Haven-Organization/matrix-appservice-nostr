"""Publishing from Profile Rooms: Matrix messages become Nostr notes, and
redacting one deletes the note.

What gets published (DESIGN.md, "Profile Rooms: who posts"):

- The owner's text, emote and media messages, as the room's ID. A reply
  or thread message answering one of the ID's own posts becomes a NIP-10
  reply to it; a reply to anything else is simply a post.
- Another local user's reply to a post here, as *their* active ID (they're
  answering on Nostr, not posting on the owner's behalf). Their other
  messages are conversation and stay in Matrix.
- Not edits (Nostr notes are immutable), commands, notices, or anything
  from the bot or a ghost.

Each note goes into the index with a recipe (``bridge.notes``) and no
content, then out to the public relays (``bridge.publishing``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request

from bridge import ids, shadow_ids
from bridge.commands import message_for_a_bot
from bridge.media import MediaProblem, publish_matrix_media
from bridge.messages import notice
from bridge.inbound import index_tags
from bridge.interactions import extra_relays_for, find_quote, notify_local_author, reply_context
from bridge.notes import (
    CONVERTERS,
    DELETION_KIND,
    MEDIA_MSGTYPES,
    POSTABLE_MSGTYPES,
    deletion_v1,
    edited_view,
    event_kind_of,
    is_redacted,
    message_body,
    pills,
    COMMENT_VERSION,
    NOTE_VERSION,
)
from bridge.remote import pubkey_of_ghost
from bridge.publishing import NotifyTarget, publish_in_background
from bridge.repository import EventRow, NostrId
from bridge.homeserver_client import HomeserverError
from bridge.users import bot_mxid, is_bridge_user, is_other_bot

logger = logging.getLogger(__name__)

# What a reply in Matrix can answer on Nostr.
REPLYABLE_KINDS = frozenset({1, 20, 21, 22, 1063, 1111, 30023, 34235, 34236})


def _relates_to(content: dict[str, Any]) -> dict[str, Any]:
    relates = content.get("m.relates_to")
    return relates if isinstance(relates, dict) else {}


def _is_edit(content: dict[str, Any]) -> bool:
    return _relates_to(content).get("rel_type") == "m.replace" or "m.new_content" in content


def _reply_targets(content: dict[str, Any]) -> list[str]:
    """What a message answers, most specific first: the event it replies
    to, then its thread's root. A reply picked out on purpose inside a
    thread answers just that event, not the thread's root."""
    relates = _relates_to(content)
    targets = []
    in_reply_to = (relates.get("m.in_reply_to") or {}).get("event_id")
    if isinstance(in_reply_to, str):
        targets.append(in_reply_to)
    if relates.get("rel_type") == "m.thread" and isinstance(relates.get("event_id"), str):
        if targets and relates.get("is_falling_back") is False:
            return targets
        targets.append(relates["event_id"])
    return targets


async def _replied_post(app: FastAPI, content: dict[str, Any]) -> EventRow | None:
    """The Nostr post (in the index) this message answers, if any."""
    repository = app.state.repository
    for target in _reply_targets(content):
        row = await repository.get_event_by_matrix_event(target)
        if row is not None and row.kind in REPLYABLE_KINDS:
            return row
    return None


def _is_xdc(content: dict[str, Any]) -> bool:
    name = str(content.get("filename") or content.get("body") or "").lower()
    mime = str((content.get("info") or {}).get("mimetype") or "").lower()
    return content.get("msgtype") == "m.file" and (name.endswith(".xdc") or mime == "application/x-webxdc")


_APP_FILE = "webxdc:file:{}"  # a Matrix post of a webxdc app -> its kind 1063


async def _xdc_details(app: FastAPI, content: dict[str, Any]) -> dict[str, Any]:
    """The app's name (its manifest's) and picture (its icon, made public),
    read from inside it."""
    from bridge.games_room import _icon_of
    from bridge.media import PROFILE_IMAGE_MAX_BYTES, publish_matrix_image
    from bridge.webxdc import manifest_name

    # Nice to have: whatever goes wrong here (a slow homeserver timing out,
    # say), the app is still posted.
    try:
        download = await app.state.homeserver.download_media(content["url"])
    except Exception:
        logger.info("Could not read the app being posted", exc_info=True)
        return {}
    if download is None:
        return {}
    out: dict[str, Any] = {}
    name = manifest_name(download.content)
    if name:
        out["name"] = name
    icon = _icon_of(download.content)
    for _attempt in range(4) if icon else ():
        # Tried again on failure: a homeserver whose media store has folders
        # it can't write to fails some uploads, at random, by their new name.
        try:
            mxc = await app.state.homeserver.upload_media(icon, "image/png", "icon.png", as_user_id=bot_mxid(app.state.config))
            published = await publish_matrix_image(app, mxc, max_bytes=PROFILE_IMAGE_MAX_BYTES)
        except Exception:
            logger.info("Could not upload the icon of an app; trying again", exc_info=True)
            continue
        if published is not None:
            out["image"] = published.url
        break
    return out


async def _app_file_of(app: FastAPI, matrix_event_id: str) -> EventRow | None:
    file_id = await app.state.repository.get_state(_APP_FILE.format(matrix_event_id))
    return await app.state.repository.get_event(file_id) if file_id else None


SOCIAL_RELATES_TO = "org.matrix.msc4501.social.relates_to"


def _social_reply(content: dict[str, Any]) -> dict[str, Any] | None:
    """MSC4501's reply relation, as another bridge cross-posting a reply sets it."""
    relation = content.get(SOCIAL_RELATES_TO)
    if (isinstance(relation, dict) and relation.get("rel_type") == "org.matrix.msc4501.social.reply"
            and isinstance(relation.get("event_id"), str)):
        return relation
    return None


async def _reply_heading(app: FastAPI, relation: dict[str, Any]) -> dict[str, str]:
    """Who the reply answers, and where their post is on the web."""
    inline = relation.get("content") if isinstance(relation.get("content"), dict) else {}
    url = inline.get("external_url")
    name = relation.get("displayname")
    if not isinstance(url, str) or not isinstance(name, str):
        try:
            original = await app.state.homeserver.get_event(str(relation.get("room_id") or ""), relation["event_id"],
                                                            as_user_id=bot_mxid(app.state.config)) or {}
        except HomeserverError:
            original = {}
        url = url if isinstance(url, str) else (original.get("content") or {}).get("external_url")
        if not isinstance(name, str) and original.get("sender"):
            try:
                name = (await app.state.homeserver.get_profile(original["sender"])).get("displayname")
            except HomeserverError:
                name = None
    return {"name": str(name or "").strip()[:100],
            "url": url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""}


async def _mentions(app: FastAPI, content: dict[str, Any]) -> list[dict[str, str]]:
    """The message's pills of Nostr accounts: local users with an ID (the
    key their active ID has now) and ghosts of remote accounts."""
    config = app.state.config
    found: dict[str, str] = {}
    for user_id, _label in pills(content):
        if user_id in found:
            continue
        ghost_key = pubkey_of_ghost(app, user_id)
        if ghost_key is not None:
            found[user_id] = ghost_key
            continue
        if is_bridge_user(config, user_id):
            continue
        active = await ids.active_id(app, user_id)
        if active is not None:
            found[user_id] = active.pubkey
    return [{"user_id": user, "pubkey": pubkey} for user, pubkey in found.items()]


async def _store_and_send(
    app: FastAPI, author: NostrId, *, kind: int, content: str, tags: list[list[str]], created_at: int,
    recipe: dict[str, Any], room_id: str, matrix_event_id: str, notify: NotifyTarget | None,
    extra_relays: list[str] | None = None,
) -> EventRow | None:
    event = await app.state.signer.sign(author.pubkey, kind=kind, content=content, created_at=created_at, tags=tags)
    if event is None:
        reason = app.state.signer.last_error.pop(author.pubkey, "the bridge doesn't hold this ID's key")
        logger.warning("Can't sign for %s: %s", author.name, reason)
        if notify is not None:
            await notice(
                app, notify.room_id, f"That wasn't published on Nostr: {reason}",
                extra={"m.relates_to": {"m.in_reply_to": {"event_id": notify.event_id}}},
            )
        return None
    row = EventRow(
        id=event.id,
        pubkey=event.pubkey,
        kind=kind,
        created_at=created_at,
        sig=event.sig,
        source=json.dumps(recipe, ensure_ascii=False, separators=(",", ":")),
        room_id=room_id,
        matrix_event_id=matrix_event_id,
        tags=index_tags(tags),
    )
    await app.state.repository.put_event(row)
    from bridge.webxdc import register_games

    await register_games(app, event.to_dict())
    publish_in_background(app, event, notify=notify, extra_relays=extra_relays)
    return row


async def maybe_publish_message(request: Request, event: dict[str, Any]) -> bool:
    """Returns True if this message was a post or reply (published or
    deliberately not): anything in a Profile Room, or a reply to a Nostr
    post in any room."""
    if event.get("type") != "m.room.message":
        return False
    app = request.app
    config = app.state.config
    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    content = event.get("content") or {}
    repository = app.state.repository
    profile_id = await repository.get_id_for_profile_room(room_id)
    if is_bridge_user(config, sender) or not sender.startswith("@"):
        return profile_id is not None
    if _is_edit(content) or message_for_a_bot(content, config):
        return profile_id is not None
    if content.get("msgtype") not in POSTABLE_MSGTYPES:
        return profile_id is not None

    parent = await _replied_post(app, content)
    social = _social_reply(content)
    if parent is None and social is not None:
        # A reply cross-posted by another bridge (MSC4501): to a post that's
        # on Nostr, it's a Nostr reply; otherwise it gets an "In reply to" line.
        row = await app.state.repository.get_event_by_matrix_event(social["event_id"])
        if row is not None and row.kind in REPLYABLE_KINDS:
            parent = row
    if profile_id is not None and sender == profile_id.matrix_user_id:
        author: NostrId | None = profile_id
        if not await shadow_ids.may_post_from_profile_room(app, profile_id):
            return True  # a third party's parked room: stays in Matrix
    elif parent is not None:
        # Answering a post anywhere else is done as the replier's own ID,
        # a shadow one made for them if they have none.
        author = await shadow_ids.ensure_shadow_id_and_tell(app, sender, room_id)
    else:
        return profile_id is not None  # conversation, not a post
    if author is None or not await shadow_ids.still_allowed(app, author, room_id):
        return True
    if parent is None and social is None and _reply_targets(content):
        # Answering something that isn't on Nostr (in a thread or not): a
        # guest in the room, another bridge's post or bot, or one of the
        # owner's own messages that was never posted. That's conversation.
        return True
    await _publish_post(app, event, author, parent)
    return True


async def _publish_post(app: FastAPI, event: dict[str, Any], author: NostrId, parent: EventRow | None,
                        *, edit: str | None = None, game_id: str | None = None) -> EventRow | None:
    """A Matrix post or reply as a Nostr note or comment. ``edit``: the
    Matrix edit this republishes ``event`` after (``event`` is then the
    edited view, ``notes.edited_view``)."""
    config = app.state.config
    room_id = event.get("room_id", "")
    content = event.get("content") or {}
    event_id = event.get("event_id", "")
    mentions = await _mentions(app, content)
    recipe: dict[str, Any] = {
        "kind": "note",
        "v": 1,
        "room_id": room_id,
        "event_id": event_id,
        "mentions": mentions,
        "p": [],
        "reply": None,
        "relay_hint": config.nostr.relay_url,
        "media": None,
    }
    if edit:
        recipe["edit"] = edit
    p_tags: list[str] = []
    if parent is not None:
        context = await reply_context(app, parent, author.pubkey)
        p_tags += context["p"]
        if "comment" in context:
            recipe["kind"] = "comment"
            recipe["v"] = COMMENT_VERSION
            recipe["comment"] = context["comment"]
        else:
            recipe["reply"] = context["reply"]
    p_tags += [m["pubkey"] for m in mentions]
    recipe["p"] = [pubkey for pubkey in dict.fromkeys(p_tags) if pubkey != author.pubkey]

    if content.get("msgtype") in MEDIA_MSGTYPES:
        try:
            media = await publish_matrix_media(app, content)
        except MediaProblem as exc:
            await notice(
                app,
                room_id,
                f"That wasn't published on Nostr: {exc}.",
                extra={"m.relates_to": {"m.in_reply_to": {"event_id": event_id}}},
            )
            return None
        recipe["media"] = {
            "url": media.url,
            "m": media.mime_type,
            "x": media.sha256,
            "size": media.size,
            "dim": f"{media.width}x{media.height}" if media.width and media.height else None,
        }
        if config.webxdc.enabled and _is_xdc(content):
            from bridge.webxdc import MIME, new_game_id

            # A webxdc app (NIP-DC): a game of its own, with a new identifier
            # (or, edited, the one it had), and its own name and picture.
            name = str(content.get("filename") or content.get("body") or "app").rsplit("/", 1)[-1]
            recipe["media"].update({"m": MIME, "webxdc": game_id or new_game_id(),
                                    "name": name[:-4] if name.lower().endswith(".xdc") else name})
            recipe["media"].update(await _xdc_details(app, content))

    if recipe["kind"] == "note":
        recipe["v"] = NOTE_VERSION
        if parent is None and _social_reply(content) is not None:
            recipe["reply_to"] = await _reply_heading(app, _social_reply(content))
        quote = await find_quote(app, message_body(content))
        if quote is not None:
            recipe["quote"] = quote
            if quote["pubkey"] != author.pubkey and quote["pubkey"] not in recipe["p"]:
                recipe["p"].append(quote["pubkey"])

    if recipe["kind"] == "note" and (recipe.get("media") or {}).get("webxdc"):
        # A webxdc app goes out as Ditto posts them: a NIP-94 file (kind
        # 1063) only. (A note too showed twice in Ditto.)
        recipe = {"kind": "file", "v": 1, "room_id": room_id, "event_id": event_id,
                  "mentions": recipe.get("mentions") or [], "media": recipe["media"], **({"edit": edit} if edit else {})}
    converter = CONVERTERS[(recipe["kind"], recipe["v"])]
    text, tags = converter(event, recipe)
    if not text and recipe["kind"] != "file":
        return None
    created_at = int(event.get("origin_server_ts", 0)) // 1000
    row = await _store_and_send(
        app, author, kind=event_kind_of(recipe), content=text, tags=tags, created_at=created_at, recipe=recipe,
        room_id=room_id, matrix_event_id=event_id,
        notify=NotifyTarget(room_id, event_id, "this post" if parent is None else "this reply"),
        extra_relays=await extra_relays_for(app, recipe.get("p") or []),
    )
    if row is not None:
        logger.info("Published %s from %s as %s@%s", row.id, event_id, author.name, config.bridge.domain)
        if parent is not None and not edit:
            await notify_local_author(app, author, parent, "reply", theirs=(room_id, event_id))
    return row


async def maybe_handle_redaction(request: Request, event: dict[str, Any]) -> bool:
    """Returns True if a published note's Matrix event was redacted: a
    NIP-09 deletion is signed by its author and sent out, and the note
    leaves the index (it can never be rebuilt again)."""
    if event.get("type") != "m.room.redaction":
        return False
    target = event.get("redacts") or (event.get("content") or {}).get("redacts")
    if not isinstance(target, str):
        return False
    app = request.app
    repository = app.state.repository
    row = await repository.get_event_by_matrix_event(target, include_copies=False)
    via_card = False
    if row is None:
        # A repost card (a copy of a local repost): redacting it undoes the
        # repost, as redacting the 🔁 does.
        copy_of = await repository.get_event_by_matrix_event(target)
        if copy_of is not None and copy_of.kind in (6, 16) and await repository.get_id_by_pubkey(copy_of.pubkey):
            row, via_card = copy_of, True
    if row is None or row.kind == DELETION_KIND:
        return False
    if is_other_bot(app.state.config, event.get("sender", "")) and json.loads(row.source).get("kind") != "share":
        return True  # another bridge may only undo the reposts made through it
    author = await repository.get_id_by_pubkey(row.pubkey)
    if author is not None and row.kind in (6, 16):
        # A repost: its card and its 🔁, whichever wasn't the one redacted.
        others = await repository.list_mirrored_copies(row.id)
        if via_card and row.room_id and row.matrix_event_id:
            others.append((row.room_id, row.matrix_event_id))
        for other_room, other_event in others:
            if other_event == target:
                continue
            try:
                await app.state.homeserver.redact_event(other_room, other_event, as_user_id=bot_mxid(app.state.config),
                                                        reason="Repost undone")
            except HomeserverError:
                logger.info("Could not redact %s", other_event, exc_info=True)
    await repository.delete_event(row.id)
    hub = getattr(app.state, "relay_hub", None)
    if hub is not None:
        hub.cache.drop(row.id)
    companion = await _app_file_of(app, target)
    if companion is not None:
        await repository.delete_event(companion.id)
        await repository.delete_state(_APP_FILE.format(target))
        if hub is not None:
            hub.cache.drop(companion.id)
    if author is None:
        # Someone else's event, removed in Matrix (a moderator, or the
        # Profile Room's owner): it leaves the relay, and counts against
        # its author in the spam rules.
        guard = getattr(app.state, "spam", None)
        if guard is not None and row.room_id and await repository.get_id_for_profile_room(row.room_id) is not None:
            await guard.record_report(row.pubkey, 1)
        return True
    await _send_deletion(app, author, row, event, also=companion)
    return True


async def _send_deletion(app: FastAPI, author: NostrId, row: EventRow, event: dict[str, Any],
                         also: EventRow | None = None) -> None:
    """A NIP-09 deletion of ``row``, made from ``event`` (a redaction, or
    the edit that replaces the note)."""
    room_id = event.get("room_id", "")
    recipe = {
        "kind": "deletion",
        "v": 1,
        "room_id": room_id,
        "redaction_event": event.get("event_id", ""),
        "targets": [{"id": r.id, "kind": r.kind} for r in (row, also) if r is not None],
    }
    content, tags = deletion_v1(event, recipe)
    deletion = await _store_and_send(
        app, author, kind=DELETION_KIND, content=content, tags=tags,
        created_at=int(event.get("origin_server_ts", 0)) // 1000, recipe=recipe, room_id=room_id,
        matrix_event_id=event.get("event_id", ""), notify=None,
    )
    if deletion is not None:
        logger.info("Deleted %s (Matrix event %s)", row.id, event.get("event_id"))


async def maybe_handle_edit(request: Request, event: dict[str, Any]) -> bool:
    """Returns True if this was an edit (m.replace) of a local user's own
    published note or comment. Nostr notes can't change, so the edit
    replaces it: a NIP-09 deletion of the old one, then a new one with the
    edited text, as the same identity, still answering what it answered.
    Edits of anything else fall through (and are never posted themselves)."""
    content = event.get("content") or {}
    relates = _relates_to(content)
    if event.get("type") != "m.room.message" or relates.get("rel_type") != "m.replace":
        return False
    target = relates.get("event_id")
    app = request.app
    config = app.state.config
    sender = event.get("sender", "")
    if not isinstance(target, str) or is_bridge_user(config, sender) or not sender.startswith("@"):
        return False
    repository = app.state.repository
    row = await repository.get_event_by_matrix_event(target, include_copies=False)
    if row is None or (json.loads(row.source).get("kind") not in ("note", "comment", "file")):
        return False
    author = await repository.get_id_by_pubkey(row.pubkey)
    if author is None or author.matrix_user_id != sender:
        return False
    room_id = event.get("room_id", "")
    if not await shadow_ids.still_allowed(app, author, room_id) or (
        row.room_id == author.profile_room_id and not await shadow_ids.may_post_from_profile_room(app, author)
    ):
        return True  # access taken away: the edit stays in Matrix
    try:
        original = await app.state.homeserver.get_event(row.room_id or room_id, target, as_user_id=bot_mxid(config))
    except HomeserverError:
        logger.info("Could not fetch %s to edit it", target, exc_info=True)
        return True
    edited = edited_view(event, target)
    if original is None or edited is None or is_redacted(original):
        return True
    new = edited["content"]
    if new.get("msgtype") not in POSTABLE_MSGTYPES or message_for_a_bot(new, config):
        return True
    if (message_body(new).strip(), new.get("url")) == (message_body(original.get("content") or {}).strip(),
                                                        (original.get("content") or {}).get("url")):
        return True  # nothing a note shows has changed
    # What it answered comes from the original: an edit doesn't restate it.
    parent = await _replied_post(app, original.get("content") or {})
    await repository.delete_event(row.id)
    hub = getattr(app.state, "relay_hub", None)
    if hub is not None:
        hub.cache.drop(row.id)
    companion = await _app_file_of(app, target)
    if companion is not None:
        await repository.delete_event(companion.id)
        if hub is not None:
            hub.cache.drop(companion.id)
    await _send_deletion(app, author, row, event, also=companion)
    # A game keeps its identifier through an edit, so its moves still count.
    game_id = (json.loads(row.source).get("media") or {}).get("webxdc")
    republished = await _publish_post(app, edited, author, parent, edit=event.get("event_id", ""), game_id=game_id)
    if republished is not None:
        logger.info("Edited %s: now %s", row.id, republished.id)
    return True
