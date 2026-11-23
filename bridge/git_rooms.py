"""Git repositories of a Concord community (NIP-34), as Matrix rooms.

Armada attaches git repositories to a community's channels (a channel's
``custom["armada.git"]``). Their issues, pull requests and patches aren't
encrypted for the community: they're public NIP-34 events tagging the
repository (``["a", "30617:<owner>:<id>"]``), discussed in NIP-22 comments.

In Matrix, the community's Space gets a **Git** Space, and in it one room
per repository:

- an issue, pull request or patch is a message (a thread root): its title,
  labels and body, from its author (the bot showing a local ID's owner);
- its comments are replies in its thread;
- a status (open, resolved/merged, closed, draft) from its author or one of
  the repository's maintainers is a line in the thread and a marker on the
  root (the bot's one reaction, replaced when it changes).

From Matrix: replying in an issue's thread comments on it, reactions react
(both the usual way, ``bridge.profile_posts`` and ``bridge.interactions``),
and a new message that isn't a reply opens an issue, its first line the
title and the rest the body.

When a repository's room is made, its open issues and anything active in
the last 30 days are brought in, with their comments and statuses.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Request

from bridge.event_sources import EMBED_FIELD, inline_source
from bridge.homeserver_client import HomeserverError
from bridge.repository import EventRow
from bridge.users import bot_mxid, is_bridge_user

logger = logging.getLogger(__name__)

REPO_KIND = 30617
PATCH_KIND = 1617
PR_KIND = 1618
ISSUE_KIND = 1621
COMMENT_KIND = 1111
ROOT_KINDS = frozenset({PATCH_KIND, PR_KIND, ISSUE_KIND})
STATUS = {1630: ("open", "🟢", "reopened this"), 1631: ("resolved", "✅", "marked this resolved"),
          1632: ("closed", "⛔", "closed this"), 1633: ("draft", "📝", "marked this a draft")}
_ICON = {ISSUE_KIND: "🐛", PR_KIND: "🔀", PATCH_KIND: "🩹"}
_WHAT = {ISSUE_KIND: "issue", PR_KIND: "pull request", PATCH_KIND: "patch"}
_ROOM = "git:room:{}"  # room -> "community id|repository address"
_ROOTS = "git:roots:{}"  # address -> JSON list of root event ids shown (newest last)
_BACKFILLED = "git:backfilled:{}"
_MARKER = "git:marker:{}"  # root id -> JSON {"event", "at"}
_RECENT_DAYS = 30
_MAX_ROOTS = 1000
_SUBSCRIBED_ROOTS = 400
_PATCH_SHOWN = 3000
_RELAYS_PER_REPO = 4


# -- which repositories ---------------------------------------------------------------------


def channel_repositories(content: dict[str, Any]) -> list[dict[str, Any]]:
    """The repositories attached to a channel, from its edition's content
    (Armada's ``custom["armada.git"]``), leaving out detached ones."""
    custom = content.get("custom") if isinstance(content.get("custom"), dict) else {}
    git = custom.get("armada.git") if isinstance(custom.get("armada.git"), dict) else {}
    out = []
    for repo in git.get("repositories") or []:
        if not isinstance(repo, dict) or not isinstance(repo.get("address"), str):
            continue
        address = repo["address"]
        if address.count(":") < 2 or not address.startswith(f"{REPO_KIND}:"):
            continue
        attached, detached = repo.get("attachedAt") or 0, repo.get("detachedAt") or 0
        if isinstance(detached, int) and isinstance(attached, int) and detached and detached >= attached:
            continue
        hints = [r for r in repo.get("relayHints") or [] if isinstance(r, str) and r.startswith(("wss://", "ws://"))]
        out.append({"address": address, "relays": hints[:_RELAYS_PER_REPO]})
    return out


def wanted(community) -> dict[str, list[str]]:
    """Address -> relay hints, for every repository attached to a channel
    the bridge can read."""
    out: dict[str, list[str]] = {}
    for channel_id in community.readable():
        for repo in community.channels[channel_id].get("git") or []:
            out.setdefault(repo["address"], [])
            out[repo["address"]] = list(dict.fromkeys([*out[repo["address"]], *repo.get("relays", [])]))
    return out


def _relays(community, info: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys([*info.get("relays", []), *community.relays]))[:6]


async def _announcement(app: FastAPI, address: str, relays: list[str]) -> dict[str, Any] | None:
    _, owner, ident = address.split(":", 2)
    found = await app.state.relay_pool.query(relays, [{"kinds": [REPO_KIND], "authors": [owner], "#d": [ident]}],
                                             timeout=10.0)
    found = [e for e in found if e.get("pubkey") == owner]
    return max(found, key=lambda e: e.get("created_at", 0), default=None)


def _tag(raw: dict[str, Any], name: str) -> str | None:
    return next((t[1] for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == name), None)


def _tag_values(raw: dict[str, Any], name: str) -> list[str]:
    return [v for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == name for v in t[1:]
            if isinstance(v, str) and v]


# -- rooms --------------------------------------------------------------------------------------


async def ensure_rooms(app: FastAPI, community) -> None:
    """The community's Git Space and a room per repository attached to its
    channels. Called with the rest of the community's rooms; the caller
    saves the community."""
    repos = wanted(community)
    if not repos or community.space is None:
        return
    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    server = app.state.config.homeserver.server_name
    from bridge.concord import _space_members

    if not community.git_space:
        community.git_space = await homeserver.create_room(
            as_user_id=bot, name="Git", topic=f"The git repositories of {community.name}, bridged from Nostr (NIP-34).",
            room_type="m.space", preset="private_chat", history_visibility="shared",
            initial_state=[{"type": "m.space.parent", "state_key": community.space, "content": {"via": [server], "canonical": True}},
                           {"type": "m.room.join_rules", "state_key": "", "content": _space_members(community.space)}],
        )
        await homeserver.send_state_event(community.space, "m.space.child", {"via": [server], "suggested": True},
                                          as_user_id=bot, state_key=community.git_space)
    for address, hints in repos.items():
        info = community.git.setdefault(address, {})
        info["relays"] = list(dict.fromkeys([*hints, *info.get("relays", [])]))[:_RELAYS_PER_REPO]
        if info.get("room"):
            continue
        announcement = await _announcement(app, address, _relays(community, info))
        ident = address.split(":", 2)[2]
        name = (_tag(announcement, "name") if announcement else None) or ident
        description = (_tag(announcement, "description") if announcement else None) or ""
        web = (_tag(announcement, "web") if announcement else None) or ""
        if announcement:
            info["maintainers"] = [m for m in _tag_values(announcement, "maintainers") if len(m) == 64][:50]
            info["relays"] = list(dict.fromkeys([*info["relays"], *_tag_values(announcement, "relays")]))[:_RELAYS_PER_REPO]
        info["name"] = name[:100]
        topic = " ".join(part for part in (description, web) if part) + (
            "\n\n" if description or web else "") + (
            "A git repository, bridged from Nostr (NIP-34). Each issue is a thread: reply in it to comment. "
            "A new message here (not a reply) opens an issue; its first line is the title.")
        info["room"] = await homeserver.create_room(
            as_user_id=bot, name=info["name"], topic=topic, preset="private_chat", history_visibility="shared",
            initial_state=[{"type": "m.space.parent", "state_key": community.git_space, "content": {"via": [server], "canonical": True}},
                           {"type": "m.room.join_rules", "state_key": "", "content": _space_members(community.space)}],
        )
        await homeserver.send_state_event(community.git_space, "m.space.child", {"via": [server], "suggested": True},
                                          as_user_id=bot, state_key=info["room"])
        await app.state.repository.set_state(_ROOM.format(info["room"]), f"{community.id}|{address}")
        logger.info("Made the room of git repository %s in %s: %s", address, community.name, info["room"])
    from bridge.tasks import run_in_background

    for address, info in community.git.items():
        if info.get("room") and not await app.state.repository.get_state(_BACKFILLED.format(address)):
            await app.state.repository.set_state(_BACKFILLED.format(address), "started")
            # Given what it needs: the caller saves the community only after this.
            run_in_background(app, backfill(app, address, dict(info), _relays(community, info)),
                              name=f"git backfill {address[-20:]}")


async def _all_repos(app: FastAPI) -> dict[str, tuple[Any, dict[str, Any]]]:
    from bridge.concord import all_communities

    out = {}
    for community in await all_communities(app):
        if community.removed:
            continue
        for address, info in community.git.items():
            if info.get("room"):
                out[address] = (community, info)
    return out


async def _roots(app: FastAPI, address: str) -> list[str]:
    stored = await app.state.repository.get_state(_ROOTS.format(address))
    return json.loads(stored) if stored else []


async def _add_root(app: FastAPI, address: str, root_id: str) -> None:
    roots = [r for r in await _roots(app, address) if r != root_id] + [root_id]
    await app.state.repository.set_state(_ROOTS.format(address), json.dumps(roots[-_MAX_ROOTS:]))


async def desired(app: FastAPI) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """For bridge.feeds: each repository's issues, patches and statuses, and
    the comments on what's shown, from its relays and its community's."""
    from bridge.nostr.relay_pool import normalize_relay_url

    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for address, (community, info) in (await _all_repos(app)).items():
        roots = (await _roots(app, address))[-_SUBSCRIBED_ROOTS:]
        filters: list[dict[str, Any]] = [{"kinds": sorted([*ROOT_KINDS, *STATUS]), "#a": [address]}]
        if roots:
            filters.append({"kinds": [COMMENT_KIND], "#E": roots})
        for relay in _relays(community, info):
            key = (normalize_relay_url(relay), f"git:{address.rsplit(':', 1)[1][:24]}:{address.split(':')[1][:8]}")
            out[key] = filters
    return out


# -- Nostr to Matrix --------------------------------------------------------------------------


async def _sender(app: FastAPI, pubkey: str, room_id: str) -> tuple[str, dict[str, Any]]:
    from bridge import remote as remotes
    from bridge.elsewhere import as_them

    local = await app.state.repository.get_id_by_pubkey(pubkey)
    if local is not None:
        return bot_mxid(app.state.config), await as_them(app, local)
    ghost = await remotes.get_or_create_remote(app, pubkey)
    await remotes.ensure_ghost_in_room(app, ghost.ghost_user_id, room_id)
    return ghost.ghost_user_id, {}


def _subject(raw: dict[str, Any]) -> str:
    subject = _tag(raw, "subject") or _tag(raw, "title")
    if not subject and raw["kind"] == PATCH_KIND:
        line = next((ln for ln in (raw.get("content") or "").splitlines() if ln.startswith("Subject:")), "")
        subject = line.removeprefix("Subject:").strip()
    if not subject:
        subject = next((ln.strip() for ln in (raw.get("content") or "").splitlines() if ln.strip()), "")
    return subject[:200] or f"(untitled {_WHAT.get(raw['kind'], 'issue')})"


async def receive(app: FastAPI, raw: dict[str, Any], *, verified: bool = False, live: bool = True) -> bool:
    """An issue, pull request, patch, status or comment for a repository
    with a room here. ``False``: not one of those (a comment on something
    else, say), for the caller to handle."""
    kind = raw.get("kind")
    if kind not in ROOT_KINDS and kind not in STATUS and kind != COMMENT_KIND:
        return False
    repository = app.state.repository
    if kind in ROOT_KINDS:
        repos = await _all_repos(app)
        address = next((a for a in _tag_values(raw, "a") if a in repos), None)
        if address is None:
            return False
    else:
        root_id = _tag(raw, "E") if kind == COMMENT_KIND else _status_root(raw)
        root = await repository.get_event(root_id) if root_id else None
        if root is None or root.kind not in ROOT_KINDS or not root.room_id:
            return False
    if not verified:
        from bridge.nostr import event as nostr_event

        try:
            await asyncio.to_thread(nostr_event.verify, raw)
        except nostr_event.InvalidEvent:
            return True  # ours to judge, and not shown
    if await repository.get_event(raw["id"]) is not None:
        return True
    try:
        if kind in ROOT_KINDS:
            community, info = repos[address]
            await _show_root(app, raw, address, info)
        elif kind in STATUS:
            await _show_status(app, raw, root)
        else:
            await _show_comment(app, raw, root, live=live)
    except HomeserverError:
        logger.info("Could not show git event %s", raw["id"], exc_info=True)
    return True


def _status_root(raw: dict[str, Any]) -> str | None:
    marked = next((t[1] for t in raw.get("tags") or [] if len(t) >= 4 and t[0] == "e" and t[3] == "root"), None)
    return marked or _tag(raw, "e")


async def _receive_root(app: FastAPI, raw: dict[str, Any], address: str, info: dict[str, Any]) -> bool:
    if await app.state.repository.get_event(raw["id"]) is not None:
        return False
    try:
        await _show_root(app, raw, address, info)
    except HomeserverError:
        logger.info("Could not show git event %s", raw["id"], exc_info=True)
        return False
    return True


async def _show_root(app: FastAPI, raw: dict[str, Any], address: str, info: dict[str, Any]) -> None:
    from bridge.inbound import _store, event_url
    from bridge.render import message_content

    room = info["room"]
    sender, extra = await _sender(app, raw["pubkey"], room)
    subject = _subject(raw)
    labels = [t for t in _tag_values(raw, "t") if t not in ("root", "revision-root")][:10]
    icon = _ICON.get(raw["kind"], "🐛")
    label_plain = "".join(f" [{t}]" for t in labels)
    label_html = "".join(f" <code>{html.escape(t)}</code>" for t in labels)
    shown = raw if raw["kind"] != PATCH_KIND else {**raw, "content": _patch_preview(raw.get("content") or "")}
    content = await message_content(
        app, shown, as_user_id=sender,
        header_plain=f"{icon} {subject}{label_plain}",
        header_html=f"{icon} <strong>{html.escape(subject)}</strong>{label_html}",
    )
    content.update(extra)
    content["external_url"] = event_url(app, raw)
    content[EMBED_FIELD] = raw
    event_id = await app.state.homeserver.send_message_event(room, content, as_user_id=sender, ts=int(raw["created_at"]) * 1000)
    await _store(app, raw, room_id=room, matrix_event_id=event_id, scope="public")
    await _add_root(app, address, raw["id"])
    app.state.feeds.changed()


def _patch_preview(text: str) -> str:
    if len(text) <= _PATCH_SHOWN:
        return f"```\n{text}\n```"
    return f"```\n{text[:_PATCH_SHOWN]}\n```\n(… {len(text) - _PATCH_SHOWN} more characters)"


async def _may_set_status(app: FastAPI, raw: dict[str, Any], root: EventRow) -> bool:
    if raw["pubkey"] == root.pubkey:
        return True
    for address, (_community, info) in (await _all_repos(app)).items():
        if info.get("room") == root.room_id:
            return raw["pubkey"] == address.split(":")[1] or raw["pubkey"] in info.get("maintainers", [])
    return False


async def _show_status(app: FastAPI, raw: dict[str, Any], root: EventRow) -> None:
    from bridge.inbound import _store, _thread_root_of

    if not await _may_set_status(app, raw, root):
        return
    state, emoji, said = STATUS[raw["kind"]]
    homeserver = app.state.homeserver
    sender, extra = await _sender(app, raw["pubkey"], root.room_id)
    note = (raw.get("content") or "").strip()
    content = {"msgtype": "m.notice", "body": f"{emoji} {said}" + (f": {note}" if note else ""),
               "m.relates_to": {"rel_type": "m.thread", "event_id": await _thread_root_of(app, root.room_id, root.matrix_event_id),
                                "is_falling_back": True, "m.in_reply_to": {"event_id": root.matrix_event_id}},
               **extra, EMBED_FIELD: raw}
    event_id = await homeserver.send_message_event(root.room_id, content, as_user_id=sender, ts=int(raw["created_at"]) * 1000)
    await _store(app, raw, room_id=root.room_id, matrix_event_id=event_id, scope="public")
    # The marker on the issue: the latest status only.
    repository = app.state.repository
    stored = await repository.get_state(_MARKER.format(root.id))
    marker = json.loads(stored) if stored else {}
    if marker and int(marker.get("at", 0)) > int(raw["created_at"]):
        return
    bot = bot_mxid(app.state.config)
    if marker.get("event"):
        try:
            await homeserver.redact_event(root.room_id, marker["event"], as_user_id=bot, reason="Status changed")
        except HomeserverError:
            pass
    marker_id = await homeserver.send_message_event(
        root.room_id, {"m.relates_to": {"rel_type": "m.annotation", "event_id": root.matrix_event_id, "key": f"{emoji} {state}"}},
        as_user_id=bot, event_type="m.reaction")
    await repository.set_state(_MARKER.format(root.id), json.dumps({"event": marker_id, "at": raw["created_at"], "state": state}))


async def _show_comment(app: FastAPI, raw: dict[str, Any], root: EventRow, *, live: bool) -> None:
    from bridge import notify_format
    from bridge.inbound import _store, _thread_root_of, event_url
    from bridge.render import message_content

    repository = app.state.repository
    sender, extra = await _sender(app, raw["pubkey"], root.room_id)
    parent_id = _tag(raw, "e")
    parent = await repository.get_event(parent_id) if parent_id and parent_id != root.id else None
    answered = parent.matrix_event_id if parent is not None and parent.room_id == root.room_id else root.matrix_event_id
    content = await message_content(app, raw, as_user_id=sender)
    content.update(extra)
    content["external_url"] = event_url(app, raw)
    content["m.relates_to"] = {"rel_type": "m.thread", "event_id": await _thread_root_of(app, root.room_id, root.matrix_event_id),
                               "is_falling_back": answered == root.matrix_event_id, "m.in_reply_to": {"event_id": answered}}
    content[EMBED_FIELD] = raw
    event_id = await app.state.homeserver.send_message_event(root.room_id, content, as_user_id=sender,
                                                             ts=int(raw["created_at"]) * 1000)
    await _store(app, raw, room_id=root.room_id, matrix_event_id=event_id, scope="public")
    if not live:
        return
    # Someone here whose issue (or comment) this answers is told, as for a reply.
    answered_row = parent if parent is not None else root
    owner = await repository.get_id_by_pubkey(answered_row.pubkey)
    if owner is not None and answered_row.pubkey != raw["pubkey"] and answered_row.matrix_event_id:
        await notify_format.send(app, owner, "reply", raw["pubkey"], own=(answered_row.room_id, answered_row.matrix_event_id),
                                 theirs=(root.room_id, event_id))


# -- backfill ----------------------------------------------------------------------------------


async def backfill(app: FastAPI, address: str, info: dict[str, Any], relays: list[str]) -> int:
    """A new repository room's open issues (and pull requests and patches),
    and everything active in the last 30 days, with their statuses and
    comments. Returns how many were brought in."""
    from bridge.nostr import event as nostr_event

    pool = app.state.relay_pool
    events = await pool.query(relays, [{"kinds": sorted([*ROOT_KINDS, *STATUS]), "#a": [address], "limit": 5000}],
                              timeout=20.0)
    valid = []
    for raw in {e["id"]: e for e in events if isinstance(e, dict) and "id" in e}.values():
        try:
            nostr_event.verify(raw)
        except nostr_event.InvalidEvent:
            continue
        valid.append(raw)
    roots = {e["id"]: e for e in valid if e["kind"] in ROOT_KINDS and address in _tag_values(e, "a")}
    statuses = [e for e in valid if e["kind"] in STATUS and _status_root(e) in roots]
    comments = []
    ids = list(roots)
    for start in range(0, len(ids), 150):
        found = await pool.query(relays, [{"kinds": [COMMENT_KIND], "#E": ids[start:start + 150]}], timeout=20.0)
        comments += [e for e in found if isinstance(e, dict) and _tag(e, "E") in roots]
    comments = list({e["id"]: e for e in comments}.values())
    maintainers = {address.split(":")[1], *info.get("maintainers", [])}
    latest: dict[str, dict[str, Any]] = {}
    for status in sorted(statuses, key=lambda e: e["created_at"]):
        root = roots[_status_root(status)]
        if status["pubkey"] == root["pubkey"] or status["pubkey"] in maintainers:
            latest[root["id"]] = status
    activity: dict[str, int] = {root_id: raw["created_at"] for root_id, raw in roots.items()}
    for event in [*statuses, *comments]:
        root_id = _status_root(event) if event["kind"] in STATUS else _tag(event, "E")
        if root_id in activity:
            activity[root_id] = max(activity[root_id], event["created_at"])
    cutoff = time.time() - _RECENT_DAYS * 86400
    chosen = [r for r in roots.values()
              if STATUS.get(latest.get(r["id"], {}).get("kind"), ("open",))[0] in ("open", "draft") or activity[r["id"]] >= cutoff]
    chosen.sort(key=lambda e: e["created_at"])
    keep = {r["id"] for r in chosen}
    shown = 0
    for raw in chosen:
        shown += int(await _receive_root(app, raw, address, info))
    for event in sorted([e for e in [*statuses, *comments]
                         if (_status_root(e) if e["kind"] in STATUS else _tag(e, "E")) in keep],
                        key=lambda e: e["created_at"]):
        try:
            nostr_event.verify(event)
        except nostr_event.InvalidEvent:
            continue
        await receive(app, event, verified=True, live=False)
    await app.state.repository.set_state(_BACKFILLED.format(address), "done")
    app.state.feeds.changed()
    logger.info("Brought %d of %d issues of %s into its room", shown, len(roots), address)
    return shown


# -- Matrix to Nostr: a new issue ---------------------------------------------------------------


async def maybe_open_issue(request: Request, event: dict[str, Any]) -> bool:
    """A new message (not a reply or thread message) in a repository's room
    opens an issue: its first line the title, the rest the body."""
    if event.get("type") != "m.room.message":
        return False
    app = request.app
    config = app.state.config
    room_id = event.get("room_id", "")
    mapped = await app.state.repository.get_state(_ROOM.format(room_id))
    if not mapped:
        return False
    sender = event.get("sender", "")
    content = event.get("content") or {}
    if is_bridge_user(config, sender) or not sender.startswith("@"):
        return True
    from bridge.commands import message_for_a_bot

    if message_for_a_bot(content, config):
        return False
    relates = content.get("m.relates_to") if isinstance(content.get("m.relates_to"), dict) else {}
    if relates or "m.new_content" in content:
        return False  # a reply, thread message or edit: the usual handlers
    from bridge.messages import notice

    if content.get("msgtype") not in ("m.text", "m.emote", "m.notice"):
        await notice(app, room_id, "To open an issue, send a text message: its first line is the title, the rest the "
                                   "description. Add pictures and files as replies in its thread.")
        return True
    from bridge import shadow_ids
    from bridge.concord import load
    from bridge.notes import message_body
    from bridge.publishing import publish_in_background

    community_id, address = mapped.split("|", 1)
    community = await load(app, community_id)
    info = (community.git if community else {}).get(address)
    if info is None:
        return True
    author = await shadow_ids.ensure_shadow_id_and_tell(app, sender, room_id)
    if author is None or not await shadow_ids.still_allowed(app, author, room_id):
        return True
    text = message_body(content).strip()
    subject, _, body = text.partition("\n")
    subject = subject.strip()[:200]
    if not subject:
        return True
    owner = address.split(":")[1]
    hint = (info.get("relays") or community.relays or [config.nostr.relay_url])[0]
    tags = [["a", address, hint], *(["p", p] for p in dict.fromkeys([owner, *info.get("maintainers", [])]) if p != author.pubkey),
            ["subject", subject], ["alt", f"git repository issue: {subject}"]]
    created_at = int(event.get("origin_server_ts", 0)) // 1000 or int(time.time())
    signed = await app.state.signer.sign(author.pubkey, kind=ISSUE_KIND, content=body.strip(), created_at=created_at, tags=tags)
    if signed is None:
        return True
    from bridge.inbound import index_tags

    await app.state.repository.put_event(EventRow(
        id=signed.id, pubkey=signed.pubkey, kind=ISSUE_KIND, created_at=created_at, sig=signed.sig,
        source=json.dumps(inline_source(ISSUE_KIND, [list(t) for t in signed.tags], signed.content),
                          ensure_ascii=False, separators=(",", ":")),
        room_id=room_id, matrix_event_id=event.get("event_id", ""), tags=index_tags([list(t) for t in signed.tags]),
    ))
    await _add_root(app, address, signed.id)
    publish_in_background(app, signed, extra_relays=_relays(community, info))
    app.state.feeds.changed()
    return True
