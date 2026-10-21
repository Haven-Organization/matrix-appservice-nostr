"""Turning Matrix messages into Nostr notes (kind 1) and redactions into
deletions (kind 5), the same way every time.

The bridge doesn't keep event content (``bridge.event_sources``), so every
conversion here runs at least twice: when the Matrix event arrives, and
again whenever the event is requested, straight from the homeserver. Both
runs must give identical bytes. So each converter is a frozen contract:

- It may depend only on the Matrix event and its recipe, never on the
  clock, config, or who is in the room now. Anything else it needs (which
  key a pill pointed at, the published media URL, what the post replies
  to, a relay hint) is decided once and written into the recipe.
- Once events exist under a version it must never change. To convert
  differently, add ``..._v2`` and write new events with ``"v": 2``; old
  ones keep rebuilding under the rules they were made with.

Tests pin every version's output byte for byte (``tests/vectors/note-v1-golden.json``).

Note recipe, version 1::

    {"kind": "note", "v": 1, "room_id", "event_id",
     "mentions": [{"user_id", "pubkey"}],   # pills to rewrite as nostr:npub
     "p": [pubkey, ...],                     # p tags, in order
     "reply": {"root", "root_pubkey", "parent", "parent_pubkey"} | None,
     "relay_hint": "wss://...",
     "media": {"url", "m", "x", "size", "dim"} | None}
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import unquote

from bridge.nostr import nip19

NOTE_KIND = 1
DELETION_KIND = 5

MEDIA_MSGTYPES = ("m.image", "m.video", "m.audio", "m.file")
POSTABLE_MSGTYPES = ("m.text", "m.emote", *MEDIA_MSGTYPES)

_MX_REPLY_RE = re.compile(r"<mx-reply>.*?</mx-reply>", re.DOTALL | re.IGNORECASE)
_PILL_RE = re.compile(
    r"""<a\s[^>]*?href\s*=\s*["']https://matrix\.to/#/([^"'?]+)[^"']*["'][^>]*>(.*?)</a>""", re.DOTALL | re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPOILER_RE = re.compile(r"""data-mx-spoiler(?:\s*=\s*["']([^"']*)["'])?""", re.IGNORECASE)


def is_redacted(event: dict[str, Any] | None) -> bool:
    return not event or "redacted_because" in (event.get("unsigned") or {}) or not event.get("content")


def message_body(content: dict[str, Any]) -> str:
    """A message's body without the quoted "> ..." fallback some clients
    prepend to a reply: the text the sender actually wrote."""
    body = content.get("body")
    if not isinstance(body, str):
        return ""
    if ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id"):
        lines = body.split("\n")
        while lines and lines[0].startswith(">"):
            lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
        body = "\n".join(lines)
    return body


def _formatted(content: dict[str, Any]) -> str:
    if content.get("format") != "org.matrix.custom.html":
        return ""
    formatted = content.get("formatted_body")
    return _MX_REPLY_RE.sub("", formatted) if isinstance(formatted, str) else ""


def pills(content: dict[str, Any]) -> list[tuple[str, str]]:
    """The user pills in a message, in order: (user ID, link text)."""
    found = []
    for href, inner in _PILL_RE.findall(_formatted(content)):
        target = unquote(href)
        if target.startswith("@"):
            found.append((target, html.unescape(_TAG_RE.sub("", inner))))
    return found


def has_caption(content: dict[str, Any]) -> bool:
    """A media message has a caption when its body isn't just the file name
    (MSC2530: the name is then in ``filename``)."""
    filename = content.get("filename")
    return isinstance(filename, str) and bool(filename) and filename != content.get("body")


def spoiler_reason(content: dict[str, Any]) -> str | None:
    """The reason of the first spoiler in the message ("" for none given),
    or ``None`` if it has no spoiler."""
    match = _SPOILER_RE.search(_formatted(content))
    if match is None:
        return None
    return html.unescape(match.group(1) or "")


# -- kind 1, version 1 -------------------------------------------------------


def _replace_pills_v1(text: str, content: dict[str, Any], mentions: list[dict[str, str]]) -> str:
    """Each pill of a mentioned user, found in order in ``text``, replaced
    by ``nostr:npub...`` (NIP-27). Pills whose text can't be found are left
    as they are."""
    keys = {m["user_id"]: m["pubkey"] for m in mentions}
    position = 0
    for user_id, label in pills(content):
        pubkey = keys.get(user_id)
        if not pubkey or not label:
            continue
        index = text.find(label, position)
        if index < 0:
            continue
        replacement = "nostr:" + nip19.npub_encode(pubkey)
        text = text[:index] + replacement + text[index + len(label):]
        position = index + len(replacement)
    return text


def note_v1(event: dict[str, Any], recipe: dict[str, Any]) -> tuple[str, list[list[str]]]:
    """(content, tags) of the note a Matrix message becomes."""
    content = event.get("content") or {}
    msgtype = content.get("msgtype")
    if msgtype in MEDIA_MSGTYPES:
        text = message_body(content) if has_caption(content) else ""
    else:
        text = message_body(content)
    text = _replace_pills_v1(text, content, recipe.get("mentions") or []).strip()
    if msgtype == "m.emote" and text:
        # "/me waves": italics in the many clients that render markdown.
        text = f"*{text}*"
    media = recipe.get("media")
    if media:
        text = f"{text}\n{media['url']}" if text else media["url"]

    tags: list[list[str]] = []
    reply = recipe.get("reply")
    hint = recipe.get("relay_hint") or ""
    if reply:
        # NIP-10 marked e tags.
        tags.append(["e", reply["root"], hint, "root", reply["root_pubkey"]])
        if reply["parent"] != reply["root"]:
            tags.append(["e", reply["parent"], hint, "reply", reply["parent_pubkey"]])
    for pubkey in recipe.get("p") or []:
        tags.append(["p", pubkey])
    reason = spoiler_reason(content)
    if reason is not None:
        tags.append(["content-warning", reason] if reason else ["content-warning"])
    if media:
        imeta = ["imeta", f"url {media['url']}", f"m {media['m']}", f"x {media['x']}", f"size {media['size']}"]
        if media.get("dim"):
            imeta.append(f"dim {media['dim']}")
        tags.append(imeta)
    return text, tags


# -- kind 1, version 2: v1 plus a quoted post ------------------------------------
# Recipe: v1's, plus "quote": {"id", "pubkey", "link"}. The link to the quoted
# post (as the user pasted it) becomes a nostr:nevent reference, and a q tag
# (NIP-18) names the post.


def nevent_reference(event_id: str, pubkey: str, hint: str) -> str:
    return "nostr:" + nip19.nevent_encode(nip19.EventPointer(event_id, (hint,) if hint else (), pubkey))


def note_v2(event: dict[str, Any], recipe: dict[str, Any]) -> tuple[str, list[list[str]]]:
    text, tags = note_v1(event, recipe)
    quote = recipe.get("quote")
    if not quote:
        return text, tags
    hint = recipe.get("relay_hint") or ""
    reference = nevent_reference(quote["id"], quote["pubkey"], hint)
    link = quote.get("link") or ""
    if link and link in text:
        text = text.replace(link, reference, 1)
    else:
        text = f"{text}\n{reference}" if text else reference
    tags = [*tags, ["q", quote["id"], hint, quote["pubkey"]]]
    return text, tags


# -- kind 1111 (NIP-22 comment), version 1 -----------------------------------------
# For replies to anything that isn't a kind 1 note, and to comments (which
# NIP-22 says are always answered with comments). Recipe: v1's note recipe,
# with "comment": {"root", "root_kind", "root_pubkey", "parent", "parent_kind",
# "parent_pubkey"} in place of "reply".


def comment_v1(event: dict[str, Any], recipe: dict[str, Any]) -> tuple[str, list[list[str]]]:
    text, base_tags = note_v1(event, {**recipe, "reply": None, "p": []})
    c = recipe["comment"]
    hint = recipe.get("relay_hint") or ""
    tags: list[list[str]] = [
        ["E", c["root"], hint, c["root_pubkey"]],
        ["K", str(c["root_kind"])],
        ["P", c["root_pubkey"]],
        ["e", c["parent"], hint, c["parent_pubkey"]],
        ["k", str(c["parent_kind"])],
        ["p", c["parent_pubkey"]],
    ]
    for pubkey in recipe.get("p") or []:
        if pubkey != c["parent_pubkey"]:
            tags.append(["p", pubkey])
    tags += base_tags  # content-warning, imeta
    return text, tags


# -- kind 7 (NIP-25 reaction), version 1 ---------------------------------------------
# Recipe: {"kind": "reaction", "v": 1, "room_id", "event_id" (the m.reaction),
# "target", "target_pubkey", "target_kind", "relay_hint", "emoji": {"shortcode",
# "url"} | None}. The reaction's key comes from the Matrix event.

LIKE_KEYS = ("👍",)


def reaction_v1(event: dict[str, Any], recipe: dict[str, Any]) -> tuple[str, list[list[str]]]:
    key = ((event.get("content") or {}).get("m.relates_to") or {}).get("key") or ""
    emoji = recipe.get("emoji")
    if emoji:
        content = f":{emoji['shortcode']}:"
    elif key.startswith(LIKE_KEYS):
        content = "+"
    else:
        content = key
    hint = recipe.get("relay_hint") or ""
    tags = [
        ["e", recipe["target"], hint, recipe["target_pubkey"]],
        ["p", recipe["target_pubkey"]],
        ["k", str(recipe["target_kind"])],
    ]
    if emoji:
        tags.append(["emoji", emoji["shortcode"], emoji["url"]])
    return content, tags


# -- kinds 6/16 (NIP-18 repost), version 1 -------------------------------------------
# Recipe: {"kind": "repost", "v": 1, "room_id", "event_id", "target",
# "target_pubkey", "target_kind", "relay_hint"}. Content is left empty (NIP-18
# allows it): a copy of the reposted event couldn't be rebuilt once that
# event was deleted.


def repost_v1(event: dict[str, Any], recipe: dict[str, Any]) -> tuple[str, list[list[str]]]:
    hint = recipe.get("relay_hint") or ""
    tags = [["e", recipe["target"], hint], ["p", recipe["target_pubkey"]]]
    if recipe["target_kind"] != 1:
        tags.append(["k", str(recipe["target_kind"])])
    return "", tags


def repost_kind(recipe: dict[str, Any]) -> int:
    return 6 if recipe["target_kind"] == 1 else 16


# -- kind 1068 (NIP-88 poll), version 1 ---------------------------------------------
# Recipe: {"kind": "poll", "v": 1, "room_id", "event_id", "relay_hint"}. Built
# from a Matrix poll (MSC3381, stable or unstable names): the question is the
# content, each answer an option tag, single or multiple choice from
# max_selections.

POLL_START_TYPES = ("m.poll.start", "org.matrix.msc3381.poll.start")
POLL_RESPONSE_TYPES = ("m.poll.response", "org.matrix.msc3381.poll.response")


def _poll_body(content: dict[str, Any]) -> dict[str, Any]:
    return content.get("m.poll") or content.get("org.matrix.msc3381.poll.start") or {}


def _text_of(value: Any) -> str:
    """MSC1767 text in any of its shapes: a string, {"org.matrix.msc1767.text": s},
    or {"m.text": [{"body": s}]}."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("org.matrix.msc1767.text", "m.text", "body"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                return str(inner[0].get("body") or "")
    return ""


def poll_v1(event: dict[str, Any], recipe: dict[str, Any]) -> tuple[str, list[list[str]]]:
    poll = _poll_body(event.get("content") or {})
    question = _text_of(poll.get("question")).strip()
    tags: list[list[str]] = []
    for answer in poll.get("answers") or []:
        if isinstance(answer, dict) and isinstance(answer.get("id"), str):
            tags.append(["option", answer["id"], _text_of(answer).strip()])
    multiple = int(poll.get("max_selections") or 1) > 1
    tags.append(["polltype", "multiplechoice" if multiple else "singlechoice"])
    if recipe.get("relay_hint"):
        tags.append(["relay", recipe["relay_hint"]])
    return question, tags


# -- kind 1018 (NIP-88 vote), version 1 ---------------------------------------------
# Recipe: {"kind": "vote", "v": 1, "room_id", "event_id", "poll", "poll_pubkey"}.
# The chosen answers come from the Matrix poll response.


def vote_v1(event: dict[str, Any], recipe: dict[str, Any]) -> tuple[str, list[list[str]]]:
    content = event.get("content") or {}
    response = content.get("m.selections") or (content.get("org.matrix.msc3381.poll.response") or {}).get("answers") or []
    tags = [["e", recipe["poll"]], ["p", recipe["poll_pubkey"]]]
    tags += [["response", answer] for answer in response if isinstance(answer, str)]
    return "", tags


# -- kind 5, version 1 -------------------------------------------------------


def deletion_v1(redaction: dict[str, Any], recipe: dict[str, Any]) -> tuple[str, list[list[str]]]:
    """(content, tags) of the NIP-09 deletion a redaction becomes. The
    redaction's reason, if any, is the deletion's content."""
    reason = (redaction.get("content") or {}).get("reason")
    content = reason.strip() if isinstance(reason, str) else ""
    tags = [["e", target["id"]] for target in recipe["targets"]]
    for kind in dict.fromkeys(str(target["kind"]) for target in recipe["targets"]):
        tags.append(["k", kind])
    return content, tags


CONVERTERS = {
    ("note", 1): note_v1,
    ("note", 2): note_v2,
    ("comment", 1): comment_v1,
    ("reaction", 1): reaction_v1,
    ("repost", 1): repost_v1,
    ("deletion", 1): deletion_v1,
    ("poll", 1): poll_v1,
    ("vote", 1): vote_v1,
}
EVENT_KINDS = {"note": NOTE_KIND, "comment": 1111, "reaction": 7, "deletion": DELETION_KIND, "poll": 1068, "vote": 1018}


def event_kind_of(recipe: dict[str, Any]) -> int:
    if recipe.get("kind") == "repost":
        return repost_kind(recipe)
    return EVENT_KINDS[recipe["kind"]]


def matrix_event_id_of(recipe: dict[str, Any]) -> str:
    """The Matrix event a recipe rebuilds from."""
    return recipe["redaction_event"] if recipe.get("kind") == "deletion" else recipe["event_id"]
