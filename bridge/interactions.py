"""Local users acting on Nostr posts from Matrix: reactions, reposts and
replies, in any room the post appears in.

Which ID acts (DESIGN.md, "Which ID an action uses"): inside one of the
user's own Profile Rooms, that room's ID; inside a DM room, that DM's ID;
anywhere else, their active ID. An undo (redacting the reaction) is signed
by whichever ID made it, since the index remembers its author.

- Reacting with 🔁 reposts (kind 6, or 16 for anything but a note).
- Reacting with 👍 (any skin tone) likes ("+"); any other emoji is sent as
  that emoji, and a custom emoji (MSC4027) as ``:shortcode:`` with its image.
- Replying (rich reply or thread) to a post replies on Nostr: a NIP-10
  kind 1 to a note, a NIP-22 comment (kind 1111) to a comment or to
  anything else.

Interactions are also sent to the post author's own inbox relays (their
NIP-65 read relays), where their clients look for them.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import FastAPI, Request

from bridge import ids
from bridge import remote as remotes
from bridge.event_sources import rebuild
from bridge.inbound import index_tags, reply_targets
from bridge.media import PROFILE_IMAGE_MAX_BYTES, publish_matrix_image
from bridge.notes import reaction_v1, repost_kind, repost_v1
from bridge.notifications import notify
from bridge.publishing import NotifyTarget, publish_in_background
from bridge.repository import EventRow, NostrId
from bridge.users import is_bridge_user, is_local_user

logger = logging.getLogger(__name__)

REPOST_KEY = "🔁"


async def acting_id(app: FastAPI, sender: str, room_id: str) -> NostrId | None:
    repository = app.state.repository
    profile = await repository.get_id_for_profile_room(room_id)
    if profile is not None and profile.matrix_user_id == sender:
        return profile
    dm = await repository.get_dm_room_info(room_id)
    if dm is not None:
        dm_id = await repository.get_id(dm[0])
        if dm_id is not None and dm_id.matrix_user_id == sender:
            return dm_id
    return await ids.active_id(app, sender)


async def raw_of(app: FastAPI, row: EventRow) -> dict[str, Any] | None:
    result = await rebuild(app, row)
    return result.event if result.status == "ok" else None


async def extra_relays_for(app: FastAPI, pubkeys: list[str]) -> list[str]:
    """Inbox relays of the remote accounts an event is aimed at."""
    relays: list[str] = []
    for pubkey in dict.fromkeys(pubkeys):
        remote = await app.state.repository.get_remote_account(pubkey)
        relays += remotes.inbox_relays_for(app, remote)[:3]
    return relays


async def notify_local_author(
    app: FastAPI, actor: NostrId, target: EventRow, reason: str, *, reaction: str | None = None,
    theirs: tuple[str, str] | None = None,
) -> None:
    """A local user acting on another local user's post: the bridge made the
    event itself, so it never comes back in to notify them; tell them here."""
    from bridge import notify_format

    owner = await app.state.repository.get_id_by_pubkey(target.pubkey)
    if owner is None or owner.matrix_user_id == actor.matrix_user_id or not target.room_id or not target.matrix_event_id:
        return
    await notify_format.send(
        app, owner, reason, actor.pubkey, own=(target.room_id, target.matrix_event_id), theirs=theirs, reaction=reaction,
    )


async def _sign_and_store(
    app: FastAPI, author: NostrId, *, kind: int, content: str, tags: list[list[str]], created_at: int,
    recipe: dict[str, Any], room_id: str, matrix_event_id: str, extra_relays: list[str], notify_target: NotifyTarget | None,
) -> EventRow | None:
    event = await app.state.signer.sign(author.pubkey, kind=kind, content=content, created_at=created_at, tags=tags)
    if event is None:
        return None
    row = EventRow(
        id=event.id, pubkey=event.pubkey, kind=kind, created_at=created_at, sig=event.sig,
        source=json.dumps(recipe, ensure_ascii=False, separators=(",", ":")),
        room_id=room_id, matrix_event_id=matrix_event_id, tags=index_tags(tags),
    )
    await app.state.repository.put_event(row)
    publish_in_background(app, event, notify=notify_target, extra_relays=extra_relays)
    return row


async def maybe_handle_reaction(request: Request, event: dict[str, Any]) -> bool:
    """Returns True if this was a local user's reaction to a Nostr post."""
    if event.get("type") != "m.reaction":
        return False
    app = request.app
    config = app.state.config
    sender = event.get("sender", "")
    if is_bridge_user(config, sender) or not is_local_user(config, sender):
        return False
    relates = (event.get("content") or {}).get("m.relates_to") or {}
    target_event = relates.get("event_id")
    key = relates.get("key")
    if relates.get("rel_type") != "m.annotation" or not isinstance(target_event, str) or not isinstance(key, str):
        return False
    target = await app.state.repository.get_event_by_matrix_event(target_event)
    if target is None or target.kind in (4, 5, 7, 1059):
        return False  # not something a reaction can go to (a DM's wrap, say)
    room_id = event.get("room_id", "")
    actor = await acting_id(app, sender, room_id)
    if actor is None:
        return True
    hint = config.nostr.relay_url
    base = {
        "v": 1, "room_id": room_id, "event_id": event.get("event_id", ""), "target": target.id,
        "target_pubkey": target.pubkey, "target_kind": target.kind, "relay_hint": hint,
    }
    if key == REPOST_KEY:
        recipe = {"kind": "repost", **base}
        content, tags = repost_v1(event, recipe)
        kind = repost_kind(recipe)
        reason, shown = "repost", None
    else:
        emoji = None
        shortcode = (event.get("content") or {}).get("com.beeper.reaction.shortcode") or (event.get("content") or {}).get("shortcode")
        if key.startswith("mxc://") and isinstance(shortcode, str):
            published = await publish_matrix_image(app, key, max_bytes=PROFILE_IMAGE_MAX_BYTES)
            if published is not None:
                emoji = {"shortcode": re.sub(r"[^a-zA-Z0-9_-]", "", shortcode.strip(":")) or "emoji", "url": published.url}
        if key.startswith("mxc://") and emoji is None:
            return True  # an image reaction that can't be published
        recipe = {"kind": "reaction", **base, "emoji": emoji}
        content, tags = reaction_v1(event, recipe)
        kind = 7
        reason, shown = "like", (None if content == "+" else content)
    created_at = int(event.get("origin_server_ts", 0)) // 1000
    row = await _sign_and_store(
        app, actor, kind=kind, content=content, tags=tags, created_at=created_at, recipe=recipe,
        room_id=room_id, matrix_event_id=event.get("event_id", ""),
        extra_relays=await extra_relays_for(app, [target.pubkey]), notify_target=None,
    )
    if row is not None:
        await notify_local_author(app, actor, target, reason, reaction=shown)
    return True


# -- replies --------------------------------------------------------------------------


def _first_tag(raw: dict[str, Any], name: str) -> list[str] | None:
    return next((t for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == name), None)


async def reply_context(app: FastAPI, parent: EventRow, author_pubkey: str) -> dict[str, Any]:
    """How a reply to ``parent`` must be shaped: ``{"reply": ...}`` for a
    NIP-10 note, or ``{"comment": ...}`` for a NIP-22 comment; plus the
    pubkeys to tag."""
    repository = app.state.repository
    source = json.loads(parent.source)
    raw = None if source.get("kind") in ("note", "comment") else await raw_of(app, parent)
    p_tags = [parent.pubkey]
    if parent.kind == 1:
        if source.get("kind") == "note" and source.get("reply"):
            root, root_pubkey = source["reply"]["root"], source["reply"]["root_pubkey"]
        elif raw is not None:
            _, root_id = reply_targets(raw)
            root = root_id or parent.id
            root_tag = next((t for t in raw.get("tags") or [] if len(t) >= 5 and t[0] == "e" and t[1] == root), None)
            root_row = await repository.get_event(root) if root != parent.id else parent
            root_pubkey = (root_tag[4] if root_tag else None) or (root_row.pubkey if root_row else "")
            p_tags += [t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "p"][:20]
        else:
            root, root_pubkey = parent.id, parent.pubkey
        p_tags.append(root_pubkey)
        reply = {"root": root, "root_pubkey": root_pubkey, "parent": parent.id, "parent_pubkey": parent.pubkey}
        context: dict[str, Any] = {"reply": reply}
    else:
        if parent.kind == 1111:
            if source.get("kind") == "comment":
                c = source["comment"]
                root, root_kind, root_pubkey = c["root"], c["root_kind"], c["root_pubkey"]
            elif raw is not None:
                e_root = _first_tag(raw, "E")
                root = e_root[1] if e_root else parent.id
                k_root = _first_tag(raw, "K")
                root_kind = int(k_root[1]) if k_root and k_root[1].isdigit() else 1
                p_root = _first_tag(raw, "P")
                root_pubkey = p_root[1] if p_root else parent.pubkey
            else:
                root, root_kind, root_pubkey = parent.id, parent.kind, parent.pubkey
        else:
            root, root_kind, root_pubkey = parent.id, parent.kind, parent.pubkey
        p_tags.append(root_pubkey)
        context = {"comment": {
            "root": root, "root_kind": root_kind, "root_pubkey": root_pubkey,
            "parent": parent.id, "parent_kind": parent.kind, "parent_pubkey": parent.pubkey,
        }}
    context["p"] = [p for p in dict.fromkeys(p_tags) if p and p != author_pubkey]
    return context


_QUOTE_RE = re.compile(
    r"(?:https?://[^\s<>\"']*?/)?(?:nostr:)?((?:note|nevent)1[02-9ac-hj-np-z]{20,})"
    r"|https://matrix\.to/#/(![^/\s]+)/(\$[^\s?<>\"']+)"
)


async def find_quote(app: FastAPI, text: str) -> dict[str, str] | None:
    """The first Nostr post a message links to (a nostr:/note/nevent link on
    any site, or a matrix.to link to a bridged post): ``{"id", "pubkey",
    "link"}`` for a quote, or ``None``."""
    from bridge.nostr import nip19

    for match in _QUOTE_RE.finditer(text):
        link = match.group(0)
        if match.group(1):
            try:
                decoded = nip19.decode(match.group(1))
            except nip19.Nip19Error:
                continue
            event_id = decoded.id if isinstance(decoded, nip19.EventPointer) else decoded if isinstance(decoded, str) else None
            author = decoded.author if isinstance(decoded, nip19.EventPointer) else None
            if not event_id:
                continue
            row = await app.state.repository.get_event(event_id)
            pubkey = row.pubkey if row else author
            if not pubkey:
                from bridge.inbound import fetch_event

                fetched = await fetch_event(app, event_id)
                pubkey = fetched["pubkey"] if fetched else None
            if pubkey:
                return {"id": event_id, "pubkey": pubkey, "link": link}
        else:
            row = await app.state.repository.get_event_by_matrix_event(match.group(3))
            if row is not None and row.kind in (1, 20, 21, 22, 1111, 30023):
                return {"id": row.id, "pubkey": row.pubkey, "link": link}
    return None
