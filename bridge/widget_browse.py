"""Browsing Nostr in the widget's Home section: feeds, threads, profiles
and search, fetched live from relays.

Nothing here is stored: like everything the bridge's relay serves (see
``bridge.event_sources``), event content stays out of the database. Results
are kept in memory for a minute (feeds) or ten (profiles), as the Games
widget keeps its catalog, so scrolling back and forth doesn't ask relays
again. Every event is verified before it's shown.

Relays that require NIP-42 authentication even to read work transparently:
queries authenticate as the user's Active identity, as the bridge does for
groups.

Posts come out in the shape the widget's post cards take, the same as
matrix-appservice-activitypub's browse cards: author, content as HTML,
media, counts, repost and reply lines, and whether (and where) the post is
already in Matrix.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI

from bridge.nostr import event as nostr_event
from bridge.nostr import nip19
from bridge.nostr.relay_pool import normalize_relay_url

logger = logging.getLogger(__name__)

FEED_KINDS = [1, 6]
PAGE = 30
FEED_SECONDS = 60.0
PROFILE_SECONDS = 600.0
_TIMEOUT = 8.0
_COUNT_TIMEOUT = 4.0
_MAX_AUTHORS = 500
_MAX_RELAYS = 10
_MAX_ANCESTORS = 10
_CACHE_ENTRIES = 300

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_NOSTR_RE = re.compile(r"(?:nostr:)?((?:npub|nprofile|note|nevent|naddr)1[02-9ac-hj-np-z]{20,})")
_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_-]+):")
_HASHTAG_RE = re.compile(r"(?<![\w&/])#(\w{1,64})")
_TOKEN_RE = re.compile(f"{_URL_RE.pattern}|{_NOSTR_RE.pattern}|{_SHORTCODE_RE.pattern}|{_HASHTAG_RE.pattern}")
_TRAILING = ".,;:!?)]}'\""
EVENT_REF_RE = re.compile(r"\b((?:nevent|note|naddr)1[02-9ac-hj-np-z]+)", re.IGNORECASE)


class BrowseProblem(Exception):
    """Why something couldn't be shown; safe to show."""


# -- cache ------------------------------------------------------------------------------------


def _cache(app: FastAPI) -> dict[str, Any]:
    return app.state.__dict__.setdefault("widget_browse", {"feeds": {}, "profiles": {}})


def _cached(store: dict[Any, tuple[float, Any]], key: Any, seconds: float) -> Any:
    entry = store.get(key)
    if entry is not None and time.time() - entry[0] < seconds:
        return entry[1]
    return None


def _keep(store: dict[Any, tuple[float, Any]], key: Any, value: Any) -> None:
    store[key] = (time.time(), value)
    if len(store) > _CACHE_ENTRIES:
        for old in sorted(store, key=lambda k: store[k][0])[: len(store) - _CACHE_ENTRIES]:
            store.pop(old, None)


# -- relays ------------------------------------------------------------------------------------


def _relays(*groups: Any) -> list[str]:
    out: list[str] = []
    for group in groups:
        for url in group or ():
            if isinstance(url, str) and url.startswith(("wss://", "ws://")):
                out.append(normalize_relay_url(url))
    return list(dict.fromkeys(out))


def configured_relays(app: FastAPI) -> list[str]:
    """The relays the Global feed reads, and the relay picker starts with."""
    nostr = app.state.config.nostr
    return _relays(nostr.publish_relays, nostr.fallback_relays)


def check_relay(url: str) -> str:
    from bridge.config import ConfigError, _relay_url

    try:
        return normalize_relay_url(_relay_url(url, "relay"))
    except ConfigError as exc:
        raise BrowseProblem("That isn't a relay address (wss://…).") from exc


async def _query(app: FastAPI, relays: list[str], filters: list[dict[str, Any]], auth: str | None,
                 timeout: float = _TIMEOUT) -> list[dict[str, Any]]:
    if not relays:
        return []
    try:
        return await app.state.relay_pool.query(relays, filters, timeout=timeout, auth_pubkey=auth)
    except Exception:
        logger.info("Browsing query failed", exc_info=True)
        return []


def _verified(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = {}
    for raw in events:
        if not isinstance(raw, dict):
            continue
        try:
            nostr_event.verify(raw)
        except (nostr_event.InvalidEvent, ValueError, KeyError, TypeError):
            continue
        out.setdefault(raw["id"], raw)
    return list(out.values())


# -- profiles -----------------------------------------------------------------------------------


async def profiles(app: FastAPI, pubkeys: list[str], relays: list[str], auth: str | None = None) -> dict[str, dict[str, str]]:
    """Kind 0 fields (``bridge.remote.parse_profile``) of ``pubkeys``, from
    the cache or the discovery relays and ``relays``."""
    from bridge.remote import parse_profile

    store = _cache(app)["profiles"]
    out: dict[str, dict[str, str]] = {}
    missing = []
    for pubkey in dict.fromkeys(pubkeys):
        cached = _cached(store, pubkey, PROFILE_SECONDS)
        if cached is not None:
            out[pubkey] = cached
        else:
            missing.append(pubkey)
    if missing:
        config = app.state.config
        found = await _query(app, _relays(config.nostr.discovery_relays, relays)[:_MAX_RELAYS],
                             [{"kinds": [0], "authors": missing[:_MAX_AUTHORS]}], auth)
        newest: dict[str, dict[str, Any]] = {}
        for raw in _verified(found):
            if raw.get("kind") == 0 and raw["created_at"] > newest.get(raw["pubkey"], {}).get("created_at", -1):
                newest[raw["pubkey"]] = raw
        for pubkey in missing:
            fields = parse_profile(newest.get(pubkey))
            _keep(store, pubkey, fields)
            out[pubkey] = fields
    return out


def _short(pubkey: str) -> str:
    npub = nip19.npub_encode(pubkey)
    return f"{npub[:12]}…{npub[-6:]}"


def author(pubkey: str, fields: dict[str, str]) -> dict[str, Any]:
    return {
        "pubkey": pubkey, "npub": nip19.npub_encode(pubkey),
        "display_name": fields.get("name") or fields.get("nip05") or _short(pubkey),
        "handle": fields.get("nip05") or _short(pubkey),
        "avatar_url": fields.get("picture") if (fields.get("picture") or "").startswith("https://") else None,
    }


# -- rendering ----------------------------------------------------------------------------------


def _emoji(raw: dict[str, Any]) -> dict[str, str]:
    return {t[1]: t[2] for t in raw.get("tags") or []
            if isinstance(t, list) and len(t) >= 3 and t[0] == "emoji" and str(t[2]).startswith("https://")}


def _mentioned(raw: dict[str, Any]) -> list[str]:
    out = []
    for match in _NOSTR_RE.finditer(raw.get("content") or ""):
        try:
            decoded = nip19.decode(match.group(1))
        except (nip19.Nip19Error, ValueError):
            continue
        if isinstance(decoded, nip19.Profile):
            out.append(decoded.pubkey)
        elif match.group(1).startswith("npub1") and isinstance(decoded, str):
            out.append(decoded)
    return out


def content_html(app: FastAPI, raw: dict[str, Any], names: dict[str, dict[str, str]], skip: set[str]) -> str:
    """A post's text as safe HTML: links, mentions (which open the account
    in the widget), event references, hashtags and custom emoji. Media URLs
    in ``skip`` are left out: they're shown as media."""
    text = raw.get("content") or ""
    if raw.get("kind") == 30023:
        title = next((t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "title"), "")
        text = (title + "\n\n" if title else "") + text[:1000]
    emoji = _emoji(raw)
    viewer = app.state.config.nostr.web_viewer_url
    out: list[str] = []
    last = 0
    for match in _TOKEN_RE.finditer(text):
        out.append(html.escape(text[last:match.start()]))
        last = match.end()
        token = match.group(0)
        if token.startswith(("http://", "https://")):
            url = token.rstrip(_TRAILING)
            rest = token[len(url):]
            if url not in skip:
                out.append(f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">{html.escape(url)}</a>')
            out.append(html.escape(rest))
        elif token.startswith(":") and token.endswith(":"):
            name = token[1:-1]
            if name in emoji:
                out.append(f'<img class="emoji" src="{html.escape(emoji[name], quote=True)}" alt="{html.escape(token, quote=True)}" '
                           f'title="{html.escape(token, quote=True)}">')
            else:
                out.append(html.escape(token))
        elif token.startswith("#"):
            out.append(f'<span class="hashtag">{html.escape(token)}</span>')
        else:
            bech = _NOSTR_RE.match(token).group(1)
            try:
                decoded = nip19.decode(bech)
            except (nip19.Nip19Error, ValueError):
                out.append(html.escape(token))
                continue
            pubkey = decoded.pubkey if isinstance(decoded, nip19.Profile) else (
                decoded if bech.startswith("npub1") and isinstance(decoded, str) else None)
            if pubkey:
                label = (names.get(pubkey) or {}).get("name") or _short(pubkey)
                out.append(f'<a href="{html.escape(viewer + nip19.npub_encode(pubkey), quote=True)}" target="_blank" '
                           f'rel="noopener" data-pubkey="{pubkey}">@{html.escape(label)}</a>')
            else:
                out.append(f'<a href="{html.escape(viewer + bech, quote=True)}" target="_blank" rel="noopener" '
                           f'data-ref="{html.escape(bech, quote=True)}">{html.escape(bech[:16])}…</a>')
    out.append(html.escape(text[last:]))
    return "<p>" + "".join(out).strip().replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def _media(raw: dict[str, Any]) -> list[dict[str, Any]]:
    from bridge.render import attachments_of

    media = []
    for attachment in attachments_of(raw):
        family = attachment.family
        if family not in ("image", "video", "audio") or not attachment.url.startswith("https://"):
            continue
        media.append({"type": family, "url": attachment.url, "full_url": attachment.url,
                      "description": attachment.alt or "", "thumb": attachment.thumb})
    return media


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(int(seconds), timezone.utc).isoformat()


def _reply_parent(raw: dict[str, Any]) -> str | None:
    from bridge.inbound import reply_targets

    if raw.get("kind") not in (1, 1111):
        return None
    parent, _root = reply_targets(raw)
    return parent


def _embedded(raw: dict[str, Any]) -> dict[str, Any] | None:
    """The post a repost (kind 6) carries, if it carries a valid one."""
    try:
        inner = json.loads(raw.get("content") or "")
    except ValueError:
        return None
    verified = _verified([inner]) if isinstance(inner, dict) else []
    return verified[0] if verified else None


async def cards(app: FastAPI, events: list[dict[str, Any]], relays: list[str], auth: str | None,
                *, counts: bool = True) -> list[dict[str, Any]]:
    """Post cards for verified events (reposts unwrapped), newest first as
    given. ``relays``: where to look for missing reposted posts, profiles
    and counts."""
    repository = app.state.repository
    shown: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    want: list[str] = []
    for raw in events:
        if raw.get("kind") == 6:
            inner = _embedded(raw)
            if inner is None:
                target = next((t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "e"), None)
                if target:
                    want.append(target)
                    shown.append(({"_missing": target}, raw))
                continue
            shown.append((inner, raw))
        else:
            shown.append((raw, None))
    if want:
        fetched = {e["id"]: e for e in _verified(await _query(app, relays, [{"ids": want}], auth))}
        shown = [((fetched.get(post["_missing"]) if "_missing" in post else post), booster) for post, booster in shown]
        shown = [(post, booster) for post, booster in shown if post is not None]
    seen: set[str] = set()
    unique = []
    for post, booster in shown:
        if post["id"] in seen:
            continue
        seen.add(post["id"])
        unique.append((post, booster))
    pubkeys = []
    for post, booster in unique:
        pubkeys += [post["pubkey"], *_mentioned(post)] + ([booster["pubkey"]] if booster else [])
    names = await profiles(app, pubkeys, relays, auth)
    tallies = await _counts(app, [p["id"] for p, _ in unique], relays, auth) if counts and unique else {}
    parents = [pid for p, _ in unique if (pid := _reply_parent(p))]
    parent_authors: dict[str, str] = {}
    for p, _ in unique:
        if p["id"] in parents:
            parent_authors[p["id"]] = p["pubkey"]
    viewer = app.state.config.nostr.web_viewer_url
    out = []
    for post, booster in unique:
        media = _media(post)
        skip = {m["url"] for m in media}
        parent = _reply_parent(post)
        row = await repository.get_event(post["id"])
        imported = row is not None and bool(row.room_id and row.matrix_event_id)
        nevent = nip19.nevent_encode(nip19.EventPointer(post["id"], tuple(relays[:2]), post["pubkey"], post.get("kind")))
        hint_author = next((t[1] for t in post.get("tags") or [] if len(t) >= 2 and t[0] == "p"), None)
        card = {
            "id": post["id"], "uri": post["id"], "nevent": nevent, "kind": post.get("kind"),
            "url": viewer + nevent, "instance": "the web",
            "created_at": _iso(post["created_at"]),
            "author": author(post["pubkey"], names.get(post["pubkey"]) or {}),
            "content_html": content_html(app, post, names, skip),
            "media": media,
            "counts": tallies.get(post["id"], {"replies": 0, "reposts": 0, "reactions": 0}),
            "in_reply_to": parent,
            "reply_hint_display_name": ((names.get(parent_authors.get(parent) or hint_author or "") or {}).get("name")
                                        if parent else None),
            "boosted_by": author(booster["pubkey"], names.get(booster["pubkey"]) or {}) if booster else None,
            "already_imported": imported,
            "matrix_room_id": row.room_id if imported else None,
            "matrix_event_id": row.matrix_event_id if imported else None,
        }
        out.append(card)
    return out


async def _counts(app: FastAPI, ids: list[str], relays: list[str], auth: str | None) -> dict[str, dict[str, int]]:
    found = await _query(app, relays, [{"kinds": [1, 6, 7], "#e": ids, "limit": 1000}], auth, timeout=_COUNT_TIMEOUT)
    tallies = {i: {"replies": 0, "reposts": 0, "reactions": 0} for i in ids}
    for raw in found:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        if kind == 1:
            parent = _reply_parent(raw)
            if parent in tallies:
                tallies[parent]["replies"] += 1
        else:
            targets = [t[1] for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == "e"]
            target = targets[-1] if kind == 7 and targets else (targets[0] if targets else None)
            if target in tallies:
                tallies[target]["reposts" if kind == 6 else "reactions"] += 1
    return tallies


# -- feeds --------------------------------------------------------------------------------------


async def _following_relays(app: FastAPI, authors: list[str]) -> list[str]:
    repository = app.state.repository
    relays: list[str] = []
    for pubkey in authors[:_MAX_AUTHORS]:
        remote = await repository.get_remote_account(pubkey)
        if remote is not None:
            relays += list(remote.write_relays[:2])
    counted: dict[str, int] = {}
    for relay in _relays(relays):
        counted[relay] = relays.count(relay)
    busiest = sorted(counted, key=lambda r: -counted[r])[:6]
    return _relays(busiest, app.state.config.nostr.fallback_relays)[:_MAX_RELAYS]


async def feed(app: FastAPI, *, source: str, relay: str = "", until: int | None = None,
               active=None) -> dict[str, Any]:
    """One page of a feed: ``source`` "following" (``active``'s follows),
    "global" (the configured relays) or "relay" (``relay``). ``until``: the
    cursor from the previous page."""
    auth = active.pubkey if active is not None else None
    base: dict[str, Any] = {"kinds": FEED_KINDS, "limit": PAGE}
    if until:
        base["until"] = int(until)
    if source == "following":
        if active is None:
            return {"posts": [], "next_until": None, "needs_id": True}
        authors = list(await app.state.repository.list_follows(active.id))
        if not authors:
            return {"posts": [], "next_until": None, "no_follows": True}
        relays = await _following_relays(app, authors)
        filters = [{**base, "authors": authors[:_MAX_AUTHORS]}]
    elif source == "relay":
        relays = [check_relay(relay)]
        filters = [base]
    else:
        relays = configured_relays(app)[:_MAX_RELAYS]
        filters = [base]
    key = (source, tuple(relays), until or 0, auth if source == "following" else None)
    store = _cache(app)["feeds"]
    cached = _cached(store, key, FEED_SECONDS)
    if cached is not None:
        return cached
    events = sorted(_verified(await _query(app, relays, filters, auth)), key=lambda e: -e["created_at"])[:PAGE]
    if source == "following":
        wanted = set(authors)
        events = [e for e in events if e["pubkey"] in wanted]
    posts = await cards(app, events, relays, auth)
    oldest = min((e["created_at"] for e in events), default=None)
    result = {"posts": posts, "next_until": (oldest - 1) if oldest and len(events) >= PAGE // 2 else None,
              "relays": relays}
    _keep(store, key, result)
    return result


# -- threads ------------------------------------------------------------------------------------


async def _resolve_event(app: FastAPI, reference: str, auth: str | None) -> dict[str, Any] | None:
    from bridge.inbound import fetch_event

    reference = reference.strip()
    if re.fullmatch(r"[0-9a-f]{64}", reference):
        return await fetch_event(app, reference)
    match = EVENT_REF_RE.search(reference)
    if match is None:
        raise BrowseProblem("Give a link to a Nostr post, or its nevent, note or naddr.")
    try:
        decoded = nip19.decode(match.group(1).lower())
    except (nip19.Nip19Error, ValueError) as exc:
        raise BrowseProblem(f"That post reference isn't valid ({exc}).") from exc
    if isinstance(decoded, str):
        return await fetch_event(app, decoded)
    if isinstance(decoded, nip19.EventPointer):
        return await fetch_event(app, decoded.id, hints=list(decoded.relays), author=decoded.author)
    if isinstance(decoded, nip19.Address):
        remote = await app.state.repository.get_remote_account(decoded.pubkey)
        from bridge.remote import write_relays_for

        relays = _relays(decoded.relays, write_relays_for(app, remote), app.state.config.nostr.fallback_relays)
        found = _verified(await _query(app, relays, [{"kinds": [decoded.kind], "authors": [decoded.pubkey],
                                                       "#d": [decoded.identifier]}], auth))
        found = [e for e in found if e["pubkey"] == decoded.pubkey]
        return max(found, key=lambda e: e["created_at"]) if found else None
    raise BrowseProblem("That's not a post reference.")


async def thread(app: FastAPI, reference: str, active=None) -> dict[str, Any]:
    """A post with its ancestors (up to the root, as far as relays have
    them) and its direct replies."""
    from bridge.inbound import fetch_event
    from bridge.remote import write_relays_for

    auth = active.pubkey if active is not None else None
    post = await _resolve_event(app, reference, auth)
    if post is None:
        raise BrowseProblem("I couldn't find that post on any relay I tried.")
    remote = await app.state.repository.get_remote_account(post["pubkey"])
    relays = _relays(write_relays_for(app, remote), configured_relays(app))[:_MAX_RELAYS]
    ancestors: list[dict[str, Any]] = []
    current = post
    while len(ancestors) < _MAX_ANCESTORS:
        parent_id = _reply_parent(current)
        if not parent_id:
            break
        hints = [t[2] for t in current.get("tags") or [] if len(t) >= 3 and t[0] == "e" and t[1] == parent_id and t[2]]
        parent = await fetch_event(app, parent_id, hints=hints)
        if parent is None:
            break
        ancestors.insert(0, parent)
        current = parent
    found = _verified(await _query(app, relays, [{"kinds": [1, 1111], "#e": [post["id"]], "limit": 100}], auth))
    replies = sorted((e for e in found if _reply_parent(e) == post["id"]), key=lambda e: e["created_at"])
    rendered = await cards(app, [*ancestors, post, *replies], relays, auth)
    by_id = {c["id"]: c for c in rendered}
    return {
        "ancestors": [by_id[e["id"]] for e in ancestors if e["id"] in by_id],
        "post": by_id.get(post["id"]),
        "replies": [by_id[e["id"]] for e in replies if e["id"] in by_id],
    }


# -- accounts -----------------------------------------------------------------------------------


async def _write_relays(app: FastAPI, pubkey: str, auth: str | None) -> list[str]:
    remote = await app.state.repository.get_remote_account(pubkey)
    if remote is not None and remote.write_relays:
        return list(remote.write_relays)
    store = _cache(app)["profiles"]
    cached = _cached(store, ("relays", pubkey), PROFILE_SECONDS)
    if cached is not None:
        return cached
    config = app.state.config
    found = [e for e in _verified(await _query(app, _relays(config.nostr.discovery_relays, config.nostr.fallback_relays),
                                               [{"kinds": [10002], "authors": [pubkey]}], auth))
             if e["pubkey"] == pubkey]
    relays: list[str] = []
    if found:
        newest = max(found, key=lambda e: e["created_at"])
        relays = [t[1] for t in newest.get("tags") or []
                  if len(t) >= 2 and t[0] == "r" and (len(t) < 3 or t[2] == "write")]
    relays = _relays(relays)[:6]
    _keep(store, ("relays", pubkey), relays)
    return relays


async def account(app: FastAPI, pubkey: str, active=None) -> dict[str, Any]:
    """A Nostr account's profile, and how the user relates to it."""
    repository = app.state.repository
    auth = active.pubkey if active is not None else None
    relays = _relays(await _write_relays(app, pubkey, auth), app.state.config.nostr.fallback_relays)
    fields = (await profiles(app, [pubkey], relays, auth)).get(pubkey) or {}
    card = author(pubkey, fields)
    local = await repository.get_id_by_pubkey(pubkey)
    remote = await repository.get_remote_account(pubkey)
    following = blocked = False
    if active is not None:
        following = pubkey in await repository.list_follows(active.id)
        blocked = pubkey in await repository.list_blocks(active.id)
    about = fields.get("about") or ""
    return {
        **card,
        "banner_url": fields.get("banner") if (fields.get("banner") or "").startswith("https://") else None,
        "bio_html": content_html(app, {"content": about, "tags": [], "kind": 0}, {}, set()) if about else "",
        "website": fields.get("website") or "", "lud16": fields.get("lud16") or "",
        "url": app.state.config.nostr.web_viewer_url + nip19.npub_encode(pubkey),
        "is_following": following, "is_blocked": blocked,
        "is_local": local is not None,
        "room_id": (local.profile_room_id if local is not None else (remote.room_id if remote else None)),
        "relays": relays,
    }


async def account_posts(app: FastAPI, pubkey: str, until: int | None = None, active=None) -> dict[str, Any]:
    auth = active.pubkey if active is not None else None
    relays = _relays(await _write_relays(app, pubkey, auth), app.state.config.nostr.fallback_relays)[:_MAX_RELAYS]
    base: dict[str, Any] = {"kinds": FEED_KINDS, "authors": [pubkey], "limit": 20}
    if until:
        base["until"] = int(until)
    key = ("author", pubkey, until or 0)
    store = _cache(app)["feeds"]
    cached = _cached(store, key, FEED_SECONDS)
    if cached is not None:
        return cached
    events = sorted((e for e in _verified(await _query(app, relays, [base], auth)) if e["pubkey"] == pubkey),
                    key=lambda e: -e["created_at"])[:20]
    oldest = min((e["created_at"] for e in events), default=None)
    result = {"posts": await cards(app, events, relays, auth),
              "next_until": (oldest - 1) if oldest and len(events) >= 10 else None}
    _keep(store, key, result)
    return result


async def search(app: FastAPI, query: str, active=None) -> dict[str, Any]:
    """Accounts (an npub, nprofile, name@domain or profile link; otherwise
    NIP-50 search) and posts (NIP-50 search). A post reference comes back
    as ``open`` for the widget to open its thread."""
    from bridge.follows import ReferenceError as NotAnAccount
    from bridge.follows import resolve_reference

    query = query.strip()
    if not query:
        return {"accounts": [], "posts": []}
    if EVENT_REF_RE.search(query):
        return {"open": EVENT_REF_RE.search(query).group(1), "accounts": [], "posts": []}
    auth = active.pubkey if active is not None else None
    if not re.search(r"\s", query):
        try:
            reference = await resolve_reference(app, query)
        except NotAnAccount:
            reference = None
        except Exception:
            logger.info("Account lookup failed for the widget", exc_info=True)
            reference = None
        if reference is not None:
            fields = (await profiles(app, [reference.pubkey], list(reference.relays), auth)).get(reference.pubkey) or {}
            return {"accounts": [author(reference.pubkey, fields)], "posts": []}
    relays = _relays(app.state.config.nostr.search_relays)
    found = _verified(await _query(app, relays, [{"kinds": [0], "search": query, "limit": 20},
                                                 {"kinds": [1], "search": query, "limit": 20}], auth))
    from bridge.remote import parse_profile

    accounts = []
    for raw in sorted((e for e in found if e.get("kind") == 0), key=lambda e: -e["created_at"]):
        if any(a["pubkey"] == raw["pubkey"] for a in accounts):
            continue
        fields = parse_profile(raw)
        _keep(_cache(app)["profiles"], raw["pubkey"], fields)
        accounts.append(author(raw["pubkey"], fields))
    notes = sorted((e for e in found if e.get("kind") == 1), key=lambda e: -e["created_at"])
    return {"accounts": accounts, "posts": await cards(app, notes, relays, auth, counts=False)}
