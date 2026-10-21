"""Catching a Nostr secret key pasted into chat.

A room message is stored by the homeserver unencrypted, and anyone in the
room (and the homeserver's admins) can read it: a pasted ``nsec`` gives that
identity away. Keys only ever go through the widget. When a message the bot
can see contains a valid ``nsec``, the bridge redacts it at once (where it
has the power to), never publishes it, and tells the user what to do.
"""

from __future__ import annotations

import logging
import re

from fastapi import Request

from bridge.homeserver_client import HomeserverError
from bridge.messages import notice
from bridge.nostr import nip19
from bridge.users import bot_mxid, is_bridge_user

logger = logging.getLogger(__name__)

_NSEC_RE = re.compile(r"nsec1[02-9ac-hj-np-z]{58}")


def contains_secret(text: str) -> bool:
    for match in _NSEC_RE.finditer(text.lower()):
        try:
            nip19.nsec_decode(match.group(0))
            return True
        except nip19.Nip19Error:
            continue
    return False


async def maybe_catch_secret(request: Request, event: dict) -> bool:
    if event.get("type") != "m.room.message":
        return False
    app = request.app
    if is_bridge_user(app.state.config, event.get("sender", "")):
        return False
    content = event.get("content") or {}
    text = " ".join(str(content.get(k) or "") for k in ("body", "formatted_body"))
    if not contains_secret(text):
        return False
    room_id = event.get("room_id", "")
    try:
        await app.state.homeserver.redact_event(
            room_id, event.get("event_id", ""), as_user_id=bot_mxid(app.state.config), reason="Contained a Nostr secret key"
        )
        removed = "I've removed the message."
    except HomeserverError:
        removed = "I couldn't remove the message here: please delete it yourself, now."
    logger.warning("Redacted a pasted Nostr secret key in %s", room_id)
    await notice(
        app, room_id,
        "⚠️ That message contained a Nostr secret key (nsec). Anyone who saw it can act as that identity. "
        f"{removed} If the key is one you use, consider it exposed. To bring a key to the bridge, use the "
        "Nostr Bridge widget, never chat.",
    )
    return True
