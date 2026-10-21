"""Who is who: the bot, the bridge's ghosts, and local users."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any


def bot_mxid(config: Any) -> str:
    return f"@{config.appservice.bot_localpart}:{config.homeserver.server_name}"


def is_bridge_user(config: Any, user_id: str) -> bool:
    """Whether ``user_id`` is the bot or one of the bridge's ghosts, whose
    own messages must never be acted on."""
    return user_id == bot_mxid(config) or user_id.startswith(f"@{config.appservice.user_prefix}")


@lru_cache(maxsize=32)
def _ghost_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


def is_other_bot(config: Any, user_id: str) -> bool:
    """Another bot or another bridge's ghost sharing rooms with this one
    (``appservice.other_bot_users`` / ``other_bridge_users``)."""
    section = config.appservice
    if user_id in section.other_bot_users:
        return True
    return any(p.fullmatch(user_id) for p in _ghost_patterns(tuple(section.other_bridge_users)))


def is_local_user(config: Any, user_id: str) -> bool:
    """A real user of this bridge's own homeserver."""
    return (
        user_id.startswith("@")
        and user_id.rsplit(":", 1)[-1] == config.homeserver.server_name
        and not is_bridge_user(config, user_id)
    )


def localpart(user_id: str) -> str:
    return user_id[1:].split(":", 1)[0] if user_id.startswith("@") else user_id
