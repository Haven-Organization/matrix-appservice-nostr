"""What a notification looks like: the same shape as the sibling bridges'.

    👍 Alice (alice@example.com) liked your post
    > the post's text

Every notification leads with an emoji for its type (👤 follow, 👍 like,
🔁 repost, 💬 reply, 💬 mention, 🔁 quote, ⚡ zap; a reaction leads with the
reaction itself), then a pill of who did it (their name as its text, their
handle as its title), then what they did, with "your post" linking to the
post in Matrix. Under that, a quote: their words for a reply, mention or
quote; your post's for a like, reaction or repost. If the post quoted has
an image or video, the notification is that image or video at preview
size, with the text as its caption.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.notifications import notify
from bridge.repository import NostrId
from bridge.users import bot_mxid

PREVIEW_TEXT_LIMIT = 200
PREVIEW_IMAGE_SIZE = 96
PREVIEW_VIDEO_MAX_DIMENSION = 240

TYPE_EMOJI = {"follow": "👤", "like": "👍", "repost": "🔁", "reply": "💬", "reaction_reply": "💬", "mention": "💬",
              "quote": "🔁", "zap": "⚡"}
PHRASES = {
    "follow": "followed you",
    "like": "liked your post",
    "repost": "reposted your post",
    "reply": "replied to your post",
    "mention": "mentioned you",
    "quote": "quoted your post",
}
# Whose words a notification quotes: the actor's own post, or yours.
_THEIR_TEXT = ("reply", "reaction_reply", "mention", "quote")


def event_link(app: FastAPI, room_id: str, event_id: str) -> str:
    return f"https://matrix.to/#/{room_id}/{event_id}?via={app.state.config.homeserver.server_name}"


def quote_block(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    return (
        "\n".join(f"> {line}" for line in text.split("\n")),
        f"<blockquote>{html.escape(text).replace(chr(10), '<br>')}</blockquote>",
    )


def _scaled(width: Any, height: Any, limit: int) -> tuple[int, int]:
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return limit, limit
    if width >= height:
        return limit, max(1, round(height * limit / width))
    return max(1, round(width * limit / height)), limit


async def post_preview(app: FastAPI, room_id: str, event_id: str) -> tuple[str, dict[str, Any] | None]:
    """(text, media) of a post's Matrix event for a notification: its text,
    cut short, and its image or video (scaled to preview size), if any."""
    try:
        event = await app.state.homeserver.get_event(room_id, event_id, as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return "", None
    content = (event or {}).get("content") or {}
    body = str(content.get("org.matrix.msc4501.social.body") or content.get("body") or "").strip()
    filename = str(content.get("filename") or "").strip()
    msgtype = content.get("msgtype")
    if msgtype in ("m.image", "m.video") and (not filename or body == filename):
        body = ""
    if len(body) > PREVIEW_TEXT_LIMIT:
        body = body[:PREVIEW_TEXT_LIMIT].rstrip() + "…"
    media = None
    url = content.get("url")
    if isinstance(url, str) and msgtype in ("m.image", "m.video"):
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        limit = PREVIEW_IMAGE_SIZE if msgtype == "m.image" else PREVIEW_VIDEO_MAX_DIMENSION
        width, height = _scaled(info.get("w"), info.get("h"), limit)
        media = {
            "msgtype": msgtype,
            "url": url,
            "filename": filename or ("image" if msgtype == "m.image" else "video"),
            "info": {"mimetype": info.get("mimetype") or ("image/jpeg" if msgtype == "m.image" else "video/mp4"), "w": width, "h": height},
        }
    return body, media


async def actor_parts(app: FastAPI, pubkey: str) -> tuple[str, str]:
    """(plain label, HTML pill) for whoever did something: a local ID's
    owner, or the account's ghost. The pill's text is their name, its title
    their handle; an empty pill would vanish from notification text."""
    from bridge import remote as remotes

    repository = app.state.repository
    local = await repository.get_id_by_pubkey(pubkey)
    if local is not None:
        handle = f"{local.name}@{app.state.config.bridge.domain}"
        mxid = local.matrix_user_id
        try:
            name = (await app.state.homeserver.get_profile(mxid)).get("displayname") or handle
        except HomeserverError:
            name = handle
    else:
        remote = await remotes.get_or_create_remote(app, pubkey)
        handle = remote.nip05 or remotes.short_npub(pubkey)
        mxid = remote.ghost_user_id
        try:
            name = (await app.state.homeserver.get_profile(mxid)).get("displayname") or handle
        except HomeserverError:
            name = handle
    label = f"{name} ({handle})" if name != handle else name
    pill = (
        f'<a href="https://matrix.to/#/{html.escape(mxid, quote=True)}" title="{html.escape(handle, quote=True)}">'
        f"{html.escape(name)}</a>"
    )
    return label, pill


async def send(
    app: FastAPI,
    local: NostrId,
    reason: str,
    actor_pubkey: str,
    *,
    own: tuple[str, str] | None = None,
    theirs: tuple[str, str] | None = None,
    their_text: str = "",
    reaction: str | None = None,
    reaction_mxc: str | None = None,
    sats: int | None = None,
    extra: dict[str, Any] | None = None,
    post_author: str | None = None,
) -> str | None:
    """Notify ``local``'s owner. ``own`` is (room, event) of their post the
    notification is about; ``theirs`` the actor's post in Matrix, if it has
    one there. For a "reaction_reply" (a reply to their reaction), ``own``
    is the post they reacted to and ``post_author`` whose it is (None:
    their own). Returns the notification's event ID."""
    label, pill = await actor_parts(app, actor_pubkey)
    if reason == "reaction_reply":
        emoji = TYPE_EMOJI[reason]
        whose = f"{post_author}'s" if post_author else "your"
        phrase = f"replied to your reaction on {whose} post"
    elif reaction is not None:
        emoji = reaction
        phrase = f"reacted to your post with {reaction}"
    elif reason == "zap":
        emoji = TYPE_EMOJI["zap"]
        amount = f" {sats:,} sats" if sats else ""
        phrase = f"zapped your post{amount}" if own else f"zapped you{amount}"
    else:
        emoji = TYPE_EMOJI[reason]
        phrase = PHRASES[reason]

    link = event_link(app, *own) if own else ""
    phrase_html = html.escape(phrase)
    if link and reason == "reaction_reply":
        # Only "post" is the link: to the post that was reacted to.
        phrase_html = phrase_html[: -len("post")] + f'<a href="{html.escape(link, quote=True)}">post</a>'
    elif link:
        phrase_html = phrase_html.replace("your post", f'<a href="{html.escape(link, quote=True)}">your post</a>')
    emoji_html = html.escape(emoji)
    if reaction_mxc:
        image = (
            f'<img data-mx-emoticon height="32" src="{html.escape(reaction_mxc, quote=True)}" '
            f'alt="{html.escape(emoji, quote=True)}" title="{html.escape(emoji, quote=True)}">'
        )
        emoji_html = image
        phrase_html = phrase_html.replace(html.escape(emoji), image)

    preview_text, media = "", None
    if reason in _THEIR_TEXT:
        if theirs is not None:
            preview_text, media = await post_preview(app, *theirs)
        if not preview_text:
            preview_text = their_text[:PREVIEW_TEXT_LIMIT] + ("…" if len(their_text) > PREVIEW_TEXT_LIMIT else "")
    elif own is not None and reason != "follow":
        preview_text, media = await post_preview(app, *own)
    if reason == "zap" and their_text:
        preview_text = their_text

    quoted_plain, quoted_html = quote_block(preview_text)
    plain = f"{emoji} {label} {phrase}"
    if quoted_plain:
        plain += f"\n{quoted_plain}"
    formatted = f"<p>{emoji_html} {pill} {phrase_html}</p>{quoted_html}"
    target_link = event_link(app, *theirs) if theirs else link
    if target_link:
        plain += f"\n{target_link}"
    return await notify(app, local, plain, formatted, extra=extra, media=media)
