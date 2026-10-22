"""``[delete profile [<number|id>]``: deleting a Nostr identity for good.

A Nostr key can't be revoked, so "deleting" one means everything the
protocol allows, done in this order once the owner confirms:

1. Sign, before anything changes (a remote signer that can't be reached
   leaves everything as it was):
   - a request to vanish (NIP-62, kind 62) naming ``ALL_RELAYS``;
   - a last profile (kind 0) marked ``"deleted": true`` and an empty follow
     list (kind 3), one second newer than the request, so relays that keep
     anything keep these and apps show the account as gone.
2. Send them to every relay the bridge knows of (publish, fallback and
   discovery relays).
3. Clear the identity from the bridge's own relay (``bridge.vanish``).
4. Unlink its Profile Room, which stays with its history.
5. Forget the identity: its key (the bridge's copy is destroyed), follows,
   blocks and DM rooms. Its name is retired for good: the handle stops
   answering, and nobody else can ever take it.

Gated like every Danger Zone command: the bot spells out what will happen
and waits for a "confirm" reply to that very message, from the same person,
within ``_CONFIRM_WINDOW_SECONDS``; everything is checked again then.
"""

from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI

from bridge import confirm, ids
from bridge.event_sources import inline_source
from bridge.homeserver_client import HomeserverError
from bridge.messages import notice
from bridge.nostr.event import Event
from bridge.publishing import publish_in_background
from bridge.repository import EventRow, NostrId, RetiredName
from bridge.vanish import ALL_RELAYS, VANISH_KIND, apply_vanish

logger = logging.getLogger(__name__)

DELETE_ACTION = "delete_id"
_CONFIRM_WINDOW_SECONDS = 600
DELETED_PROFILE = {"name": "Deleted account", "about": "This account has been deleted.", "deleted": True}


def _handle(app: FastAPI, nostr_id: NostrId) -> str:
    return f"{nostr_id.name}@{app.state.config.bridge.domain}"


async def handle_delete(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    words = argument.split()
    if not words or words[0].lower() != "profile":
        await notice(app, room_id, f'Did you mean "{p}delete profile [<number|id>]"?')
        return
    owned = await app.state.repository.list_ids_for_user(sender)
    if not owned:
        await notice(app, room_id, f'You don\'t have a Nostr identity -- nothing to delete.')
        return
    if len(words) > 1:
        target = await ids.find_own_id(app, sender, words[1])
        if target is None:
            await notice(app, room_id, f'No identity matching "{words[1]}" -- run "{p}id list" to see yours.')
            return
    elif len(owned) == 1:
        target = owned[0]
    else:
        # Never guess which one to delete.
        lines = ["Which identity? You have more than one:"]
        lines += [f"{n}. {_handle(app, i)}" for n, i in enumerate(owned, start=1)]
        lines.append(f'Run "{p}delete profile <number|id>".')
        await notice(app, room_id, "\n".join(lines))
        return
    await _ask(app, room_id, sender, target)


async def _ask(app: FastAPI, room_id: str, sender: str, target: NostrId) -> None:
    from bridge.keys import KEY_ORIGIN_BUNKER, KEY_ORIGIN_IMPORTED

    handle = _handle(app, target)
    minutes = _CONFIRM_WINDOW_SECONDS // 60
    if target.key_origin == KEY_ORIGIN_IMPORTED:
        key_line = ("The bridge destroys its copy of the key. The key you brought still exists wherever you keep "
                    "it, and anyone holding it can keep posting as this account.")
    elif target.key_origin == KEY_ORIGIN_BUNKER:
        key_line = ("The bridge disconnects from your remote signer. The key stays in your signer, and anyone "
                    "holding it can keep posting as this account.")
    else:
        key_line = "Its key is destroyed: nobody, including you, can post as this account again."
    points = [
        "Every relay is asked to delete everything it ever posted (a NIP-62 request to vanish). Relays that don't "
        "support that may keep copies, and people who saved its posts still have them.",
        "Its profile is replaced with one marked deleted, and its follow list is emptied.",
        key_line,
        f"Its handle {handle} stops working, and is never given to anyone else.",
        "Its followers, follows, blocks and DM rooms are forgotten by the bridge.",
        "Its Profile Room stays, with its history, but nothing said there is published any more.",
    ]
    head = f"⚠️ Delete {handle} for good? This can't be undone."
    tail = f'Reply "confirm" to THIS message within {minutes} minutes to delete it. Anything else does nothing.'
    plain = "\n".join([head, *(f"- {point}" for point in points), tail])
    rich = (f"<p>⚠️ <strong>Delete {html.escape(handle)} for good?</strong> This can't be undone.</p><ul>"
            + "".join(f"<li>{html.escape(point)}</li>" for point in points)
            + f"</ul><p>{html.escape(tail)}</p>")
    await confirm.ask(
        app, room_id, action=DELETE_ACTION, user=sender, add_instruction=False, plain=plain, rich=rich,
        args={"id": target.id, "name": target.name, "pubkey": target.pubkey, "asked_at": int(time.time())},
    )


async def _execute_delete(app: FastAPI, room_id: str, sender: str, args: dict[str, Any]) -> None:
    repository = app.state.repository
    id_ref = args.get("id")
    target = await repository.get_id(id_ref) if isinstance(id_ref, int) else None
    asked_at = args.get("asked_at")
    if (
        target is None or target.matrix_user_id != sender
        or target.name != args.get("name") or target.pubkey != args.get("pubkey")
    ):
        await notice(app, room_id, "That identity has changed since; nothing was deleted.")
        return
    if not isinstance(asked_at, int) or time.time() - asked_at > _CONFIRM_WINDOW_SECONDS:
        p = app.state.config.appservice.command_prefix
        await notice(app, room_id, f'That confirmation has expired; nothing was deleted. Run "{p}delete profile '
                                   f'{target.name}" again if you still want to.')
        return
    handle = _handle(app, target)
    now = int(time.time())
    vanish = await _sign(app, target, VANISH_KIND, [["relay", ALL_RELAYS]], "This account was deleted.", now)
    profile = await _sign(app, target, 0, [], json.dumps(DELETED_PROFILE, separators=(",", ":")), now + 1)
    follows = await _sign(app, target, 3, [], "", now + 1)
    if vanish is None or profile is None or follows is None:
        await notice(app, room_id, f"I couldn't sign for {handle} (is your remote signer reachable?). "
                                   "Nothing was deleted.")
        return

    # Kept in the index (and served by this relay) after everything else goes.
    for event, replace in ((vanish, False), (profile, True), (follows, True)):
        await repository.put_event(
            EventRow(id=event.id, pubkey=event.pubkey, kind=event.kind, created_at=event.created_at, sig=event.sig,
                     source=json.dumps(inline_source(event.kind, [list(t) for t in event.tags], event.content),
                                       ensure_ascii=False, separators=(",", ":")), room_id=None),
            replace=replace,
        )
    config = app.state.config.nostr
    relays = list(dict.fromkeys([*config.publish_relays, *config.fallback_relays, *config.discovery_relays]))
    for event in (vanish, profile, follows):
        publish_in_background(app, event, relays=relays)
    await apply_vanish(app, target.pubkey, now, redact=False, keep=(vanish.id,))

    if target.profile_room_id:
        from bridge.room_links import _detach

        await _detach(app, target, target.profile_room_id, moved_to=None, message=(
            f"{handle} was deleted. This room stays, with its history, but nothing said here is published to "
            "Nostr any more."
        ))
    was_active = await repository.get_active_id(sender) == target.id
    await repository.delete_id(target.id, RetiredName(
        name=target.name, matrix_user_id=sender, id_ref=target.id, reason="deleted",
        retired_at=datetime.now(timezone.utc).isoformat(), resolves_until=None,
    ))
    remaining = await repository.list_ids_for_user(sender)
    if was_active and remaining:
        await repository.set_active_id(sender, remaining[0].id)
    feeds = getattr(app.state, "feeds", None)
    if feeds is not None:
        feeds.changed()
    logger.info("Deleted %s (%s) for %s", handle, target.pubkey, sender)
    after = f" Your Active identity is now {_handle(app, remaining[0])}." if was_active and remaining else ""
    try:
        await notice(app, room_id, f"Deleted {handle}. Relays have been asked to remove everything it posted.{after}")
    except HomeserverError:
        pass


async def _sign(app: FastAPI, target: NostrId, kind: int, tags: list[list[str]], content: str,
                created_at: int) -> Event | None:
    return await app.state.signer.sign(target.pubkey, kind=kind, content=content, created_at=created_at, tags=tags)


confirm.register(DELETE_ACTION, _execute_delete)
