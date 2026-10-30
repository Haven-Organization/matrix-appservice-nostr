"""Confirm-by-reply: actions that wait for the user to say "confirm".

Same pattern as the sibling bridges' Danger Zone. The bot posts a message
saying exactly what will happen, carrying the pending action in a content
field (``CONFIRM_FIELD``); nothing changes until the person it was meant
for replies "confirm" to that very message. The action lives on the
message, not in the database: a warning nobody answers leaves nothing
behind, and one that is answered has everything re-checked before acting.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request

from bridge.homeserver_client import HomeserverError
from bridge.messages import notice
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

# On the bot's message: {"action": ..., "user": who may confirm, "args": {...}},
# and "reactions": true when a 👍 counts as well as a reply.
CONFIRM_FIELD = "software.haven.nostr.confirm"

# Reactions that confirm, where a message allows them: a thumbs up, any skin
# tone, compared without the emoji variation selector (U+FE0F).
CONFIRM_REACTIONS = frozenset({"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿"})


def is_confirm_reaction(key: str) -> bool:
    return key.replace("\ufe0f", "").strip() in CONFIRM_REACTIONS

# (app, room id, sender, args)
Executor = Callable[[FastAPI, str, str, dict[str, Any]], Awaitable[None]]
_EXECUTORS: dict[str, Executor] = {}


def register(action: str, executor: Executor) -> None:
    _EXECUTORS[action] = executor


def _reply_target(content: dict) -> str | None:
    target = ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
    return target if isinstance(target, str) and target else None


def _first_line_after_fallback(content: dict) -> str:
    body = content.get("body")
    if not isinstance(body, str):
        return ""
    lines = [line for line in body.split("\n") if not line.startswith(">")]
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines[0] if lines else ""


async def ask(
    app: FastAPI,
    room_id: str,
    *,
    action: str,
    user: str,
    args: dict[str, Any],
    plain: str,
    rich: str,
    add_instruction: bool = True,
    reactions: bool = False,
    extra: dict[str, Any] | None = None,
) -> str | None:
    """Post a message that a later "confirm" reply from ``user`` acts on
    (or, with ``reactions``, a 👍 from them on it: only for actions
    that are easy to live with, never a deletion). ``plain``/``rich`` say
    what will happen; the instruction to reply is added here unless the
    text already says it (``add_instruction``). ``extra``: more content
    fields (a thread, say)."""
    instruction = 'Reply "confirm" to THIS message to go ahead.'
    if add_instruction:
        plain = f"{plain}\n\n{instruction}"
        rich = f"{rich}<p>{html.escape(instruction)}</p>"
    return await notice(
        app,
        room_id,
        plain,
        html_message=rich,
        extra={**(extra or {}),
               CONFIRM_FIELD: {"action": action, "user": user, "args": args, **({"reactions": True} if reactions else {})}},
        msgtype="m.text",
    )


async def maybe_handle_confirmation(request: Request, event: dict) -> bool:
    """Returns True if this was a "confirm" reply to one of the bot's own
    pending actions (handled, whether or not it went through). Only the
    person it was meant for can confirm; anyone else's "confirm" is
    claimed and ignored."""
    content = event.get("content") or {}
    reaction = event.get("type") == "m.reaction"
    if reaction:
        relation = content.get("m.relates_to") or {}
        if relation.get("rel_type") != "m.annotation" or not is_confirm_reaction(str(relation.get("key") or "")):
            return False
        target = relation.get("event_id") if isinstance(relation.get("event_id"), str) else None
    elif event.get("type") == "m.room.message" and _first_line_after_fallback(content).strip().lower() == "confirm":
        target = _reply_target(content)
    else:
        return False
    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    if not target or not room_id or not sender:
        return False
    app = request.app
    bot = bot_mxid(app.state.config)
    try:
        pending_event = await app.state.homeserver.get_event(room_id, target, as_user_id=bot)
    except HomeserverError:
        return False
    pending = ((pending_event or {}).get("content") or {}).get(CONFIRM_FIELD)
    if (pending_event or {}).get("sender") != bot or not isinstance(pending, dict):
        return False
    if reaction and not pending.get("reactions"):
        return False  # a 👍 on a warning about something drastic is just a reaction
    if pending.get("user") != sender:
        return True
    args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
    executor = _EXECUTORS.get(str(pending.get("action")))
    if executor is None:
        logger.warning("Confirmation for unknown action %r", pending.get("action"))
        return True
    await executor(app, room_id, sender, args)
    return True
