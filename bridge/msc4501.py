"""MSC4501 ("Rooms as Social Media Pages") and related proposals, kept
identical to the sibling bridges' use of them, so a client that
understands one bridge's rooms understands all three.

https://github.com/matrix-org/matrix-spec-proposals/pull/4501

Everything is under its proposal's unstable name.
"""

from __future__ import annotations

import html
from typing import Any

# Room type of every room representing a profile: a Profile Room, and
# (later) a Remote User Room. A room's type can't change after creation.
SOCIAL_PROFILE_ROOM_TYPE = "org.matrix.msc4501.social.profile"

# Whose profile a room is. Only the bot may set it: at the default level
# any moderator could reassign a profile out from under its owner.
SOCIAL_PROFILE_USER_ID_STATE_TYPE = "org.matrix.msc4501.social.profile_user_id"
SOCIAL_PROFILE_USER_ID_POWER_LEVEL = 100

# MSC4144: lets a message the bot sends on a user's behalf show as theirs.
PER_MESSAGE_PROFILE_FIELD = "com.beeper.per_message_profile"

# MSC4221 (Room Banners), under that proposal's own unstable name. Content
# mirrors m.room.avatar's: {"url": "mxc://..."}.
PROFILE_BANNER_STATE_TYPE = "page.codeberg.everypizza.room.banner"

# MSC4503 (External Protocol Handles): a ghost's real handle on Nostr, which
# neither its display name nor its MXID carries. Set on ghosts (and their
# events) per bridge.msc4503_external_handle, as the sibling bridges do, once
# ghosts exist (step 5 of the build order).
EXTERNAL_HANDLE_FIELD = "org.matrix.msc4503.external_handle"


def external_handle_value(config, *, pubkey: str, nip05: str | None = None) -> dict:
    """The MSC4503 value for a Nostr account: its verified NIP-05 handle if
    it has one, else its npub, with the bot's avatar as the protocol icon."""
    from bridge.nostr import nip19

    npub = nip19.npub_encode(pubkey)
    protocol: dict = {"id": "nostr", "displayname": "Nostr"}
    if config.appservice.bot_avatar_mxc:
        protocol["avatar_url"] = config.appservice.bot_avatar_mxc
    return {"handle": nip05 or npub, "protocol": protocol, "url": f"{config.nostr.web_viewer_url}{npub}"}


# Points one event at another: a repost, a quote-post, or the copy of a reply
# in the replier's own room.
SOCIAL_RELATES_TO_FIELD = "org.matrix.msc4501.social.relates_to"
REPOST_REL_TYPE = "org.matrix.msc4501.social.repost"
REPLY_REL_TYPE = "org.matrix.msc4501.social.reply"

# What a client that understands the relation shows in place of body and
# formatted_body: the author's own words, without the "⤵️ Reply to X's post:"
# or "🔁 reposted X's post:" lines the bridge adds for other clients. Only
# set together with a relation, and left out when there are no words.
SOCIAL_BODY_FIELD = "org.matrix.msc4501.social.body"
SOCIAL_FORMATTED_BODY_FIELD = "org.matrix.msc4501.social.formatted_body"


def set_social_body(content: dict[str, Any], plain: str, rich: str) -> None:
    plain = (plain or "").strip()
    if plain:
        content[SOCIAL_BODY_FIELD] = plain
        content[SOCIAL_FORMATTED_BODY_FIELD] = rich or html.escape(plain)


def own_words(content: dict[str, Any]) -> tuple[str, str]:
    """An event's own words for ``social.body``: its own ``social.body`` if
    it has one (a quote-post's or reply's text alone), else its body."""
    if SOCIAL_BODY_FIELD in content:
        plain = str(content.get(SOCIAL_BODY_FIELD) or "")
        return plain, str(content.get(SOCIAL_FORMATTED_BODY_FIELD) or "")
    if content.get("filename") and content.get("body") == content.get("filename"):
        return "", ""  # a file with no caption: the body is only its name
    return str(content.get("body") or ""), str(content.get("formatted_body") or "")


async def social_relation(
    app: Any, rel_type: str, *, room_id: str, event_id: str, sender: str,
    displayname: str | None = None, content_inline: bool = False,
) -> dict[str, Any] | None:
    """The relation to the event ``event_id`` in ``room_id``, if
    ``bridge.set_msc4501_relates_to`` is on. ``content_inline`` says the
    event carrying it already is a copy of that event (a plain repost);
    otherwise that event's own content goes inside, so a client can show it
    without access to its room. Never ``content_inline`` for a reply or a
    quote-post: their content is the author's own words."""
    from bridge.homeserver_client import HomeserverError
    from bridge.users import bot_mxid

    config = app.state.config
    if not config.bridge.set_msc4501_relates_to:
        return None
    value: dict[str, Any] = {"rel_type": rel_type, "event_id": event_id, "room_id": room_id, "sender": sender,
                             "via": [config.homeserver.server_name]}
    if displayname:
        value["displayname"] = displayname
    if content_inline:
        value["content_inline"] = True
        return value
    try:
        event = await app.state.homeserver.get_event(room_id, event_id, as_user_id=bot_mxid(config))
    except HomeserverError:
        event = None
    content = (event or {}).get("content")
    if isinstance(content, dict) and content:
        value["content"] = content
    return value
