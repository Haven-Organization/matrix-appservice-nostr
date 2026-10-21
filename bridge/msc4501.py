"""MSC4501 ("Rooms as Social Media Pages") and related proposals, kept
identical to the sibling bridges' use of them, so a client that
understands one bridge's rooms understands all three.

https://github.com/matrix-org/matrix-spec-proposals/pull/4501

Everything is under its proposal's unstable name.
"""

from __future__ import annotations

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
