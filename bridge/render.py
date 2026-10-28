"""Showing a Nostr event in Matrix: text, formatting and media.

Kind 1 content is plain text (some clients write markdown, but that isn't
standard, so it isn't rendered). The bridge:

- links URLs,
- turns ``nostr:npub``/``nprofile`` into a pill when the key is a local ID
  (its owner) or has a ghost here, and otherwise into a link,
- turns ``nostr:note``/``nevent``/``naddr`` into a matrix.to link when the
  event is in Matrix, and otherwise into a link to ``nostr.web_viewer_url``,
- shows NIP-30 ``:shortcode:`` custom emoji as inline images,
- turns a ``content-warning`` tag into a spoiler.

Media is found from ``imeta`` tags (NIP-92), and in posts without them from
URLs ending in a media extension. As in matrix-appservice-atproto, the first
image or video becomes the Matrix message (copied into the media
repository), captioned with the text; any further attachments are listed
as links. Picture and video posts (kinds 20, 21, 22, and the addressable videos 34235
and 34236) work the same way.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.nostr import nip19
from bridge.safe_fetch import FetchError, UnsafeAddress
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
# A leading "@" is part of the match: many clients write a mention as
# "@nostr:npub1...", and the pill it becomes replaces the "@" too.
_NOSTR_RE = re.compile(r"@?(?:nostr:)?((?:npub|nprofile|note|nevent|naddr)1[02-9ac-hj-np-z]{20,})")
_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_-]+):")
_TRAILING = ".,;:!?)]}'\""
_MEDIA_EXTENSIONS = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    ".avif": "image/avif", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".wav": "audio/wav",
}
_MATRIX_MSGTYPE = {"image": "m.image", "video": "m.video", "audio": "m.audio"}


@dataclass
class Attachment:
    url: str
    mime: str | None = None
    sha256: str | None = None
    dim: str | None = None
    alt: str | None = None

    @property
    def family(self) -> str:
        return (self.mime or "").split("/", 1)[0]


@dataclass
class Rendered:
    body: str
    html: str
    attachments: list[Attachment] = field(default_factory=list)


def attachments_of(raw: dict[str, Any]) -> list[Attachment]:
    """Media an event carries: its imeta tags, else media URLs in its text.
    A NIP-94 file (kind 1063) carries its file in flat tags, and maybe a
    preview ``image``, shown in its place when the file isn't a picture or
    a video itself (a webxdc game's icon, say)."""
    found: list[Attachment] = []
    seen: set[str] = set()
    if raw.get("kind") == 1063:
        flat: dict[str, str] = {}
        for tag in raw.get("tags") or []:
            if isinstance(tag, list) and len(tag) >= 2 and isinstance(tag[1], str):
                flat.setdefault(tag[0], tag[1].strip())
        url = flat.get("url", "")
        if url.startswith(("https://", "http://")):
            file = Attachment(url, flat.get("m"), flat.get("x"), flat.get("dim"), flat.get("alt"))
            preview = flat.get("image") or flat.get("thumb") or ""
            if file.family not in ("image", "video") and preview.startswith(("https://", "http://")):
                found.append(Attachment(preview, "image/" + (preview.rsplit(".", 1)[-1].lower() if "." in preview[-6:] else "png")))
                seen.add(preview)
            found.append(file)
            seen.add(url)
    for tag in raw.get("tags") or []:
        if not isinstance(tag, list) or not tag or tag[0] != "imeta":
            continue
        fields: dict[str, str] = {}
        for item in tag[1:]:
            if isinstance(item, str) and " " in item:
                key, _, value = item.partition(" ")
                fields.setdefault(key, value)
        url = fields.get("url")
        if url and url.startswith(("https://", "http://")) and url not in seen:
            seen.add(url)
            found.append(Attachment(url, fields.get("m"), fields.get("x"), fields.get("dim"), fields.get("alt")))
    for match in _URL_RE.finditer(raw.get("content") or ""):
        url = match.group(0).rstrip(_TRAILING)
        lower = url.lower().split("?", 1)[0]
        mime = next((m for ext, m in _MEDIA_EXTENSIONS.items() if lower.endswith(ext)), None)
        if mime and url not in seen:
            seen.add(url)
            found.append(Attachment(url, mime))
    return found


def _emoji_map(raw: dict[str, Any]) -> dict[str, str]:
    return {
        t[1]: t[2]
        for t in raw.get("tags") or []
        if isinstance(t, list) and len(t) >= 3 and t[0] == "emoji" and str(t[2]).startswith("https://")
    }


async def _pill_for_pubkey(app: FastAPI, pubkey: str) -> tuple[str, str] | None:
    """(MXID, label) for a key the bridge knows: a local ID's owner, or a ghost."""
    repository = app.state.repository
    local = await repository.get_id_by_pubkey(pubkey)
    if local is not None:
        return local.matrix_user_id, f"{local.name}@{app.state.config.bridge.domain}"
    remote = await repository.get_remote_account(pubkey)
    if remote is not None:
        try:
            name = (await app.state.homeserver.get_profile(remote.ghost_user_id)).get("displayname")
        except HomeserverError:
            name = None
        return remote.ghost_user_id, name or remote.nip05 or nip19.npub_encode(pubkey)[:16]
    return None


async def matrix_link_for_event(app: FastAPI, event_id: str) -> str | None:
    row = await app.state.repository.get_event(event_id)
    if row is None or not row.room_id or not row.matrix_event_id:
        return None
    return f"https://matrix.to/#/{row.room_id}/{row.matrix_event_id}"


async def render(app: FastAPI, raw: dict[str, Any], *, skip_urls: set[str] | frozenset[str] = frozenset()) -> Rendered:
    """``raw``'s text as Matrix body and HTML. URLs in ``skip_urls`` (media
    shown as the message itself) are left out of the text."""
    viewer = app.state.config.nostr.web_viewer_url
    content = raw.get("content") or ""
    if raw.get("kind") in (20, 21, 22, 34235, 34236):
        title = next((t[1] for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == "title"), "")
        if title and title not in content:
            content = f"{title}\n{content}" if content else title
    emoji = _emoji_map(raw)
    plain: list[str] = []
    rich: list[str] = []
    position = 0
    tokens = sorted(
        [(m.start(), m.end(), "url", m.group(0)) for m in _URL_RE.finditer(content)]
        + [(m.start(), m.end(), "nostr", m.group(1)) for m in _NOSTR_RE.finditer(content)]
        + [(m.start(), m.end(), "emoji", m.group(1)) for m in _SHORTCODE_RE.finditer(content) if m.group(1) in emoji]
    )
    for start, end, kind, value in tokens:
        if start < position:
            continue
        text_before = content[position:start]
        plain.append(text_before)
        rich.append(html.escape(text_before))
        token_text = content[start:end]
        if kind == "url":
            url = value.rstrip(_TRAILING)
            end = start + len(url)
            if url in skip_urls:
                position = end
                continue
            plain.append(url)
            rich.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>')
        elif kind == "emoji":
            plain.append(token_text)
            rich.append(
                f'<img data-mx-emoticon height="32" src="{html.escape(emoji[value], quote=True)}" '
                f'alt=":{html.escape(value)}:" title=":{html.escape(value)}:">'
            )
        else:
            if token_text.startswith("@") and not value.startswith(("npub", "nprofile")):
                # Only a person's "@" belongs to the pill; keep any other.
                plain.append("@")
                rich.append("@")
            label, link = await _nostr_reference(app, value, viewer)
            plain.append(label)
            rich.append(link)
        position = end
    plain.append(content[position:])
    rich.append(html.escape(content[position:]))
    body = "".join(plain).strip()
    formatted = "".join(rich).strip().replace("\n", "<br>")
    warning = next(
        (t for t in raw.get("tags") or [] if isinstance(t, list) and t and t[0] == "content-warning"), None
    )
    if warning is not None and formatted:
        reason = html.escape(warning[1]) if len(warning) > 1 and warning[1] else ""
        attribute = f' data-mx-spoiler="{reason}"' if reason else " data-mx-spoiler"
        formatted = f"<span{attribute}>{formatted}</span>"
    return Rendered(body, formatted, attachments_of(raw))


async def _nostr_reference(app: FastAPI, bech: str, viewer: str) -> tuple[str, str]:
    """(plain label, HTML) for a nostr: reference."""
    try:
        decoded = nip19.decode(bech)
    except nip19.Nip19Error:
        return bech, html.escape(bech)
    if isinstance(decoded, nip19.Profile) or (bech.startswith("npub") and isinstance(decoded, str)):
        pubkey = decoded.pubkey if isinstance(decoded, nip19.Profile) else decoded
        pill = await _pill_for_pubkey(app, pubkey)
        if pill is not None:
            mxid, label = pill
            return label, f'<a href="https://matrix.to/#/{html.escape(mxid)}">{html.escape(label)}</a>'
        npub = nip19.npub_encode(pubkey)
        short = f"{npub[:12]}…"
        return short, f'<a href="{html.escape(viewer + npub, quote=True)}">{html.escape(short)}</a>'
    event_id = decoded.id if isinstance(decoded, nip19.EventPointer) else decoded if bech.startswith("note") else None
    if isinstance(event_id, str):
        link = await matrix_link_for_event(app, event_id)
        if link:
            return "post", f'<a href="{html.escape(link, quote=True)}">post</a>'
    return "post", f'<a href="{html.escape(viewer + bech, quote=True)}">post</a>'


async def copy_attachment(app: FastAPI, attachment: Attachment, *, as_user_id: str) -> dict[str, Any] | None:
    """Download an attachment into the Matrix media repository. Returns the
    ``url``/``info`` for a Matrix media message, or ``None`` if it can't be
    fetched, is too big, or its hash doesn't match its ``imeta``."""
    limit = app.state.config.nostr.max_media_mb * 1024 * 1024
    try:
        fetched = await app.state.fetcher.get(attachment.url, max_bytes=limit)
    except (FetchError, UnsafeAddress) as exc:
        logger.info("Not copying %s: %s", attachment.url, exc)
        return None
    if fetched.status != 200 or not fetched.content:
        return None
    if attachment.sha256 and hashlib.sha256(fetched.content).hexdigest() != attachment.sha256.lower():
        logger.info("Not copying %s: hash doesn't match its imeta", attachment.url)
        return None
    mime = (attachment.mime or fetched.content_type or "application/octet-stream").split(";")[0].strip()
    name = attachment.url.rsplit("/", 1)[-1].split("?", 1)[0] or "file"
    try:
        mxc = await app.state.homeserver.upload_media(fetched.content, mime, name, as_user_id=as_user_id)
    except HomeserverError:
        logger.info("Could not store %s", attachment.url, exc_info=True)
        return None
    info: dict[str, Any] = {"mimetype": mime, "size": len(fetched.content)}
    if attachment.dim and "x" in attachment.dim:
        width, _, height = attachment.dim.partition("x")
        if width.isdigit() and height.isdigit():
            info["w"], info["h"] = int(width), int(height)
    return {"url": mxc, "info": info, "filename": name}


def _long_form(app: FastAPI, raw: dict[str, Any]) -> dict[str, Any]:
    """A long-form article (NIP-23) as a short post: its title and summary,
    its cover image, and a link to read it, rather than pages of markdown."""
    tags = {t[0]: t[1] for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2}
    d = tags.get("d", "")
    link = app.state.config.nostr.web_viewer_url + nip19.naddr_encode(nip19.Address(d, raw["pubkey"], int(raw["kind"])))
    title = tags.get("title") or "Untitled article"
    summary = tags.get("summary") or (raw.get("content") or "")[:280].strip()
    text = f"📰 {title}\n{summary}\nRead: {link}" if summary else f"📰 {title}\nRead: {link}"
    shadow_tags = [t for t in raw.get("tags") or [] if isinstance(t, list) and t and t[0] in ("content-warning", "emoji")]
    if tags.get("image", "").startswith("https://"):
        shadow_tags.append(["imeta", f"url {tags['image']}"])
    return {**raw, "kind": 1, "content": text, "tags": shadow_tags}


async def message_content(
    app: FastAPI,
    raw: dict[str, Any],
    *,
    as_user_id: str,
    header_plain: str = "",
    header_html: str = "",
    footer_plain: str = "",
    footer_html: str = "",
) -> dict[str, Any]:
    """A complete Matrix message for a Nostr post: its first image or video
    as the message (when it can be copied), the text as body/caption, and
    any further attachments listed as links. ``header_*`` go above the text
    ("🔁 reposted X's post:"); ``footer_*`` below it (a quoted post)."""
    if raw.get("kind") == 30023:
        raw = _long_form(app, raw)
    attachments = attachments_of(raw)
    main = next((a for a in attachments if a.family in _MATRIX_MSGTYPE), None)
    copied = await copy_attachment(app, main, as_user_id=as_user_id) if main is not None else None
    skip = {main.url} if copied is not None and main is not None else set()
    rendered = await render(app, raw, skip_urls=skip)
    others = [a for a in attachments if copied is None or a is not main]
    body, formatted = rendered.body, rendered.html
    games = []
    if app.state.config.webxdc.enabled:
        from bridge import webxdc

        games = webxdc.apps_of(raw)
        game_urls = {g.url for g in games}
        others = [a for a in others if a.url not in game_urls]
    listed = [a for a in others if a.url not in body]
    if listed:
        body += "\n\nOther Attachments:\n" + "\n".join(a.url for a in listed)
        formatted += "<br><br>Other Attachments:<br>" + "<br>".join(
            f'<a href="{html.escape(a.url, quote=True)}">{html.escape(a.url)}</a>' for a in listed
        )
    if header_plain:
        body = f"{header_plain}\n{body}" if body else header_plain
        formatted = f"{header_html}<br>{formatted}" if formatted else header_html
    for game in games[:1]:
        line_plain, line_html = webxdc.game_line(game, app.state.config.appservice.command_prefix)
        body = f"{body}\n\n{line_plain}" if body.strip() else line_plain
        formatted = f"{formatted}<br><br>{line_html}" if formatted else line_html
    if footer_plain:
        body = f"{body}\n\n{footer_plain}" if body.strip() else footer_plain
        formatted = f"{formatted}<br><br>{footer_html}" if formatted else footer_html
    content: dict[str, Any] = {"msgtype": "m.text", "body": body or " ", "format": "org.matrix.custom.html", "formatted_body": formatted}
    if copied is not None and main is not None:
        content = {
            "msgtype": _MATRIX_MSGTYPE[main.family],
            "body": body or copied["filename"],
            "filename": copied["filename"],
            "url": copied["url"],
            "info": copied["info"],
        }
        if body:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = formatted
    if games:
        content[webxdc.GAME_FIELD] = webxdc.game_field(games[0])
    return content


def bot(app: FastAPI) -> str:
    return bot_mxid(app.state.config)
