"""Bot commands: how a Matrix user drives the bridge.

Same structure as the sibling bridges' commands. A message is a command
when it either

- tags the bot (a Matrix mention, or its MXID typed out) followed by the
  command (``@nostrbot:example.org help``), or
- starts with the command prefix immediately followed by a command
  (``[help``; ``appservice.command_prefix``). matrix-appservice-activitypub
  uses ``;`` and matrix-appservice-atproto ``]``, so all three can share a
  room without answering each other's commands.

Only the first line is looked at, and matching is case-insensitive.
Anything that addresses the bot is consumed here, recognized or not, so a
mistyped command sent in a Profile Room is never published as a post.

Commands are grouped into sections; ``help`` lists every section and
``help <section>`` one section. ``_help_sections`` and COMMANDS.md follow
the same breakdown: keep both in sync when adding a command.
"""

from __future__ import annotations

import html
import logging
import re
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Request

from bridge import confirm, id_deletion, ids, room_links
from bridge.admins import is_admin
from bridge.homeserver_client import HomeserverError
from bridge.identity_events import sync_identity
from bridge.messages import notice, room_link_html
from bridge.nostr import nip19
from bridge.profile_rooms import create_profile_room, ensure_user_rooms
from bridge.repository import NostrId
from bridge.users import bot_mxid, is_bridge_user, is_local_user, localpart

logger = logging.getLogger(__name__)

# Longer keywords first where one is a prefix of another.
_COMMAND_KEYWORDS = (
    "profiles|create|switch|rename|unlink|link|delete|play|join|id|unfollow|following|follow|unblock|block|backfill|dm|unban|bans|ban|"
    "widget|import|export|status|help"
)

_COMMAND_RE = re.compile(rf"\b({_COMMAND_KEYWORDS})\b\s*(.*)", re.IGNORECASE)

CREATE_ID_ACTION = "create_id"
RENAME_ID_ACTION = "rename_id"


def _prefixed_command_re(prefix: str) -> re.Pattern[str]:
    # Anchored to the start of the line and requiring a known keyword right
    # after the prefix: a "[" followed by anything else is someone's
    # ordinary message ("[citation needed]"), which must not be swallowed.
    return re.compile(rf"^{re.escape(prefix)}\s*({_COMMAND_KEYWORDS})\b\s*(.*)", re.IGNORECASE)


def reply_target_event_id(content: dict) -> str | None:
    """The event a message is a reply to, if it is one."""
    target = ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
    return target if isinstance(target, str) and target else None


def first_line(content: dict) -> str:
    """The message's first line, past the quoted "> ..." fallback some
    clients prepend to a reply."""
    body = content.get("body")
    if not isinstance(body, str):
        return ""
    lines = body.split("\n")
    if reply_target_event_id(content) is not None:
        while lines and lines[0].startswith(">"):
            lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return lines[0] if lines else ""


def _extract_prefixed_command(content: dict, config: Any) -> tuple[str, str] | None:
    match = _prefixed_command_re(config.appservice.command_prefix).match(first_line(content))
    return (match.group(1).lower(), match.group(2).strip()) if match else None


def _tags_bot(content: dict, config: Any) -> bool:
    """A proper Matrix mention of the bot, or its bare MXID in the text
    (some clients never send intentional mentions)."""
    mxid = bot_mxid(config)
    mentioned = (content.get("m.mentions") or {}).get("user_ids") or []
    return mxid in mentioned or mxid in first_line(content)


def message_addresses_bot(content: dict, config: Any) -> bool:
    """Whether a message is directed at the bot. Also used to make sure
    such a message is never published as a post."""
    return _tags_bot(content, config) or _extract_prefixed_command(content, config) is not None


@lru_cache(maxsize=32)
def _other_prefix_re(prefixes: tuple[str, ...]) -> re.Pattern[str] | None:
    # A prefix straight away followed by a word and then a space or the end
    # of the line: "]follow alice" or ";help", but not "[Update] ...".
    if not prefixes:
        return None
    alternatives = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    return re.compile(rf"^(?:{alternatives})[A-Za-z][\w-]*(?:\s|$)")


def message_addresses_other_bot(content: dict, config: Any) -> bool:
    """Whether a message is a command for another bot sharing the room
    (``appservice.other_command_prefixes`` and ``other_bot_users``)."""
    section = config.appservice
    pattern = _other_prefix_re(tuple(section.other_command_prefixes))
    line = first_line(content)
    if pattern is not None and pattern.match(line):
        return True
    if section.other_bot_users:
        mentioned = set((content.get("m.mentions") or {}).get("user_ids") or [])
        return any(bot in mentioned or bot in line for bot in section.other_bot_users)
    return False


def message_for_a_bot(content: dict, config: Any) -> bool:
    """Whether a message is meant for this bot or another one: either way,
    it's never published."""
    return message_addresses_bot(content, config) or message_addresses_other_bot(content, config)


def _handle(app: FastAPI, nostr_id: NostrId) -> str:
    return f"{nostr_id.name}@{app.state.config.bridge.domain}"


# -- help ---------------------------------------------------------------------
#
# Laid out like matrix-appservice-activitypub's ;help: bare "[help" shows a
# table of sections with a line about each, "[help <section>" that section's
# commands, "[help all" every section but Admin. Anything that asks for a
# "confirm" before acting lives in the Danger Zone. COMMANDS.md follows the
# same sections: keep both in sync when adding a command.


def _help_sections(config: Any) -> dict[str, tuple[str, str, list[tuple[str, str]]]]:
    """``{key: (title, blurb, [(command, description)])}``; ``[help <key>``."""
    p = config.appservice.command_prefix
    domain = config.bridge.domain
    return {
        "profile": (
            "Profile",
            "Setting up and maintaining your own Nostr identities.",
            [
                (f"{p}create profile [<id>]",
                 f"Set up a new Nostr identity (<id>@{domain}, or your Matrix username without <id>) and its "
                 "Profile Room in one step. Run it again for one you have to be invited back into your rooms."),
                (f"{p}id",
                 "List your Nostr identities (numbered, Active one marked), or switch which one is Active: "
                 f'"{p}id <number|id>". Active is used for anything that doesn\'t already have an identity '
                 "tied to it; in a Profile Room, that room's own identity is always used."),
                (f"{p}create room <number|id>",
                 "Give an identity that has no Profile Room (after unlinking it) a new one, starting from its "
                 "current profile."),
            ],
        ),
        "general": (
            "General",
            "Following, messaging, and everyday commands.",
            [
                (f"{p}help", "Show this message, or run with a section name to see its commands."),
                (f"{p}follow [<account>]",
                 "Follow a Nostr account (npub, nprofile, name@domain or a profile link) as your Active identity, "
                 f'in a room of its own for its posts. Undo it with "{p}unfollow", or by leaving that room. Run '
                 "in an account's room, the account can be left out (the same goes for unfollow, block, unblock "
                 "and dm)."),
                (f"{p}following", "List every account your Active identity follows."),
                (f"{p}dm [<account>]", "Start, or get back into, a private DM with a Nostr account."),
                (f"{p}join <invite link>",
                 "Join a Concord community (Armada, Vector, …) from its invite link, as your Active identity: a "
                 "Space with a room per channel. Its messages are stored readable on this homeserver."),
                *([(f"{p}play [<link>]",
                    "Reply to a game post (a webxdc mini app, marked 🎮), or give a link to one, to open it in "
                    "your Games room. Games need a Matrix client with widgets, such as Element Web or Desktop.")]
                  if config.webxdc.enabled else []),
                (f"{p}import <link|nevent|note|naddr>",
                 "Bring one Nostr post (or article) into Matrix by its link, without following its author. A reply "
                 "is threaded into its conversation; anything else lands in its author's room. You're invited, "
                 "and I reply with a link to it."),
            ],
        ),
        "advanced": (
            "Advanced/Maintenance",
            "Keys, moderation of your own feed, and other things almost nobody needs day to day.",
            [
                (f"{p}backfill [N]",
                 "Run in an account's room: bring in its latest N posts (20 by default). Run inside a thread "
                 "(or as a reply to a post) of a Nostr conversation: bring in that conversation's replies instead. "
                 "Ones already here are skipped."),
                (f"{p}block [<account>]",
                 "Block an account for your Active identity: its replies, mentions and reactions stop reaching "
                 f'you. "{p}unblock" undoes it.'),
                (f"{p}widget",
                 "Add the bridge's widget to this room: buttons for your identities, following, and bringing "
                 "your own key, for clients that support Matrix widgets."),
                (f"{p}import key",
                 "How to bring a Nostr key you already have. It's done in the widget over HTTPS, never in chat."),
                (f"{p}export key", "How to take an identity's key with you (also only in the widget)."),
                (f"{p}status", "Your Nostr identities, and whether the bridge's handles and relay are working."),
            ],
        ),
        "admin": (
            "Admin",
            "Moderating the bridge's relay, gated to bridge admins.",
            [
                (f"{p}ban <npub|handle|IP> [1h|6h|1d|7d|30d|forever] [purge] [reason]",
                 "Ban an account or address from the relay (7 days by default). purge also removes their "
                 "events from Matrix."),
                (f"{p}unban <npub|handle|IP>", "Lift a ban."),
                (f"{p}bans", "List current bans."),
            ],
        ),
        "danger": (
            "⚠️ Danger Zone",
            "Commands that change what a room or an identity IS, in ways that are hard to undo. Each sends a "
            'warning and requires a "confirm" reply before anything actually happens.',
            [
                (f"{p}link <number|id> [keep]",
                 "Run in an existing room you admin (invite me first): make it that identity's Profile Room. "
                 "Everything you post there is then published on Nostr. The room takes on the identity's "
                 "profile, or with keep, the identity takes on the room's name, topic and avatar."),
                (f"{p}unlink <number|id>",
                 "Stop a room being the identity's Profile Room. The room and its history stay; the identity "
                 f'keeps its key, handle and followers, but has no Profile Room until "{p}link" or '
                 f'"{p}create room".'),
                (f"{p}delete profile [<number|id>]",
                 "Delete an identity for good: every relay is asked to delete everything it posted (NIP-62), its "
                 "profile is marked deleted, the bridge destroys its key, and its handle is retired. Irreversible, "
                 "unlike everything else here. Its Profile Room stays, with its history. With more than one "
                 "identity, says which to pick rather than guessing."),
                (f"{p}rename <number|id> <new id>",
                 f"Change an identity's handle (<new id>@{domain}). Followers and posts stay, the old handle "
                 "keeps working for a while, and an identity can only be renamed once a week."),
            ],
        ),
    }


def _render_help_table(intro: str, intro_html: str, commands: list[tuple[str, str]]) -> tuple[str, str]:
    body = intro + "\n\n" + "\n\n".join(f"{cmd}\n  - {desc}" for cmd, desc in commands)
    rows = "".join(f"<tr><td>{html.escape(cmd)}</td><td>{html.escape(desc)}</td></tr>" for cmd, desc in commands)
    formatted = (f"<p>{intro_html}</p><table><thead><tr><th>Command</th><th>Description</th></tr></thead>"
                 f"<tbody>{rows}</tbody></table>")
    return body, formatted


async def _handle_help(app: FastAPI, room_id: str, argument: str = "", sender: str = "") -> None:
    """Sent as an ordinary message, not a notice, so it stands out."""
    config = app.state.config
    p = config.appservice.command_prefix
    sections = _help_sections(config)
    key = argument.strip().lower()
    intro = (f'Hi, I\'m the Nostr bridge! Start a message with "{p}" to give me a command (e.g. "{p}help"). '
             f'You can also tag me in place of "{p}":')
    intro_html = (f"Hi, I'm the Nostr bridge! Start a message with <code>{html.escape(p)}</code> to give me a "
                  f"command (e.g. <code>{html.escape(p)}help</code>). You can also tag me in place of "
                  f"<code>{html.escape(p)}</code>:")
    if key == "admin":
        if not await is_admin(config, app.state.homeserver, sender):
            await notice(app, room_id, "Only a bridge admin can use this.")
            return
        rows = sections["admin"][2]
    elif key == "all":
        rows = [row for k, (_t, _b, commands) in sections.items() if k != "admin" for row in commands]
    elif key in sections:
        rows = sections[key][2]
    else:
        rows = [(f"{p}help {k}", blurb) for k, (_title, blurb, _cmds) in sections.items()]
        intro += f'\n\nRun "{p}help <section>" to see a section\'s own commands.'
        intro_html += f"</p><p>Run <code>{html.escape(p)}help &lt;section&gt;</code> to see a section's own commands."
    body, formatted = _render_help_table(intro, intro_html, rows)
    await notice(app, room_id, body, html_message=formatted, msgtype="m.text")


# -- create profile -----------------------------------------------------------


async def _finish_creating(
    app: FastAPI, room_id: str, sender: str, nostr_id: NostrId, *, profile_event: dict | None = None
) -> None:
    """After the ID row exists: its Profile Room, the user's other rooms,
    the identity events, and telling the user."""
    repository = app.state.repository
    owned = await repository.list_ids_for_user(sender)
    first = len(owned) == 1
    handle = _handle(app, nostr_id)
    prefix = app.state.config.appservice.command_prefix
    try:
        nostr_id = await create_profile_room(app, nostr_id, first=first)
    except HomeserverError:
        logger.warning("Could not create the Profile Room of %s", handle, exc_info=True)
        await notice(
            app,
            room_id,
            f"{handle} exists, but I couldn't create its Profile Room. "
            f'Run "{prefix}create profile {nostr_id.name}" again to retry.',
        )
        return
    await ensure_user_rooms(app, sender)
    if profile_event is not None:
        from bridge.keys import seed_profile_room

        try:
            await seed_profile_room(app, nostr_id, profile_event)
        except HomeserverError:
            logger.info("Could not start %s's Profile Room from its existing profile", handle, exc_info=True)
    try:
        await sync_identity(app, nostr_id)
    except HomeserverError:
        logger.warning("Could not publish the identity of %s yet", handle, exc_info=True)
    npub = nip19.npub_encode(nostr_id.pubkey)
    explanation = (
        "Its name, topic and avatar are your Nostr profile. Anything published from it is public on Nostr, "
        "even though the room itself is private."
    )
    active = await ids.active_id(app, sender)
    if active is not None and active.id == nostr_id.id:
        active_line = "It's your Active identity."
    else:
        number = ids.id_number(await repository.list_ids_for_user(sender), nostr_id)
        active_line = f'It isn\'t your Active identity; make it so with "{prefix}id {number}".'
    assert nostr_id.profile_room_id is not None
    await notice(
        app,
        room_id,
        f"Created your Nostr ID {handle} ({npub}) and invited you to its Profile Room {nostr_id.profile_room_id}. "
        f"{explanation} {active_line}",
        html_message=f"<p>Created your Nostr ID <strong>{html.escape(handle)}</strong> "
        f"(<code>{html.escape(npub)}</code>) and invited you to its Profile Room "
        f"{room_link_html(nostr_id.profile_room_id)}.</p><p>{html.escape(explanation)} {html.escape(active_line)}</p>",
    )


async def _already_yours(app: FastAPI, room_id: str, sender: str, nostr_id: NostrId) -> None:
    """Re-running create for an ID you have: the way back into your rooms."""
    await ensure_user_rooms(app, sender)
    nostr_id = await app.state.repository.get_id(nostr_id.id) or nostr_id
    handle = _handle(app, nostr_id)
    if nostr_id.profile_room_id:
        await notice(
            app,
            room_id,
            f"{handle} is already yours. I've re-invited you to its Profile Room {nostr_id.profile_room_id} "
            "and your other bridge rooms.",
            html_message=f"<strong>{html.escape(handle)}</strong> is already yours. I've re-invited you to its "
            f"Profile Room {room_link_html(nostr_id.profile_room_id)} and your other bridge rooms.",
        )
    else:
        await notice(app, room_id, f"{handle} is already yours, but I couldn't create its Profile Room. Try again in a minute.")


async def _create(app: FastAPI, room_id: str, sender: str, name: str) -> None:
    try:
        nostr_id = await ids.create_id(app, sender, name)
    except ids.IdRejected as exc:
        await notice(app, room_id, f"Couldn't create {name}@{app.state.config.bridge.domain}: {exc.rejection.message}")
        return
    await _finish_creating(app, room_id, sender, nostr_id)


async def _offer_suggestion(app: FastAPI, room_id: str, sender: str, wanted: str, problem: ids.Rejection) -> None:
    """The default ID can't be used: say why, offer a close name to accept
    with "confirm", and show how to pick any name instead."""
    config = app.state.config
    prefix = config.appservice.command_prefix
    domain = config.bridge.domain
    if problem.kind is ids.RejectionKind.INVALID:
        headline = f'"{wanted}" can\'t be used as a Nostr ID: {problem.message}'
    else:
        headline = problem.message
    suggestion = await ids.suggest_available(app, wanted, sender)
    create_any = f"{prefix}create profile <id>"
    if suggestion is None:
        await notice(app, room_id, f"⚠️ {headline}\nPick any ID you like: {create_any}")
        return
    plain = (
        f"⚠️ {headline}\n"
        f"1. Use {suggestion}@{domain} instead: reply \"confirm\" to this message.\n"
        f"2. Or pick any ID you like: {create_any}"
    )
    rich = (
        f"<p>⚠️ {html.escape(headline)}</p><ol>"
        f"<li>Use <strong>{html.escape(suggestion)}@{html.escape(domain)}</strong> instead: reply "
        "<strong>confirm</strong> to this message.</li>"
        f"<li>Or pick any ID you like: <code>{html.escape(create_any)}</code></li></ol>"
    )
    await confirm.ask(
        app, room_id, action=CREATE_ID_ACTION, user=sender, args={"name": suggestion}, plain=plain, rich=rich,
        add_instruction=False,
    )


async def _handle_create(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    config = app.state.config
    prefix = config.appservice.command_prefix
    words = argument.split()
    if words and words[0].lower() == "room":
        await room_links.handle_create_room(app, room_id, sender, words[1] if len(words) > 1 else "")
        return
    if not words or words[0].lower() not in ("profile", "id"):
        await notice(app, room_id, f'Did you mean "{prefix}create profile"?')
        return
    if not ids.user_allowed(config, sender):
        await notice(app, room_id, "This server hasn't enabled Nostr IDs for your account.")
        return
    requested = words[1] if len(words) > 1 else None
    if requested is not None:
        name = ids.normalize_requested(requested, config.bridge.domain)
        if name is None:
            await notice(app, room_id, f"Nostr IDs here are always @{config.bridge.domain}.")
            return
    else:
        name = localpart(sender)
    own = await app.state.repository.get_id_by_name(name)
    if own is not None and own.matrix_user_id == sender:
        await _already_yours(app, room_id, sender, own)
        return
    limit = await ids.limit_problem(app, sender, is_admin=await is_admin(config, app.state.homeserver, sender))
    if limit is not None:
        await notice(app, room_id, limit.message)
        return
    problem = await ids.check_new_name(app, name, sender)
    if problem is None:
        await _create(app, room_id, sender, name)
    elif requested is None:
        await _offer_suggestion(app, room_id, sender, name, problem)
    else:
        await notice(app, room_id, f"Couldn't create {name}@{config.bridge.domain}: {problem.message}")


async def _execute_create(app: FastAPI, room_id: str, sender: str, args: dict[str, Any]) -> None:
    """A "confirm" to a suggested ID: everything is checked again."""
    name = args.get("name")
    if not isinstance(name, str):
        return
    own = await app.state.repository.get_id_by_name(name)
    if own is not None and own.matrix_user_id == sender:
        await _already_yours(app, room_id, sender, own)
        return
    config = app.state.config
    limit = await ids.limit_problem(app, sender, is_admin=await is_admin(config, app.state.homeserver, sender))
    if limit is not None:
        await notice(app, room_id, limit.message)
        return
    await _create(app, room_id, sender, name)


# -- id: listing your identities, and which one is Active ------------------------
# As matrix-appservice-activitypub's ;id. "[profiles" and "[switch <x>" are
# kept as old spellings of "[id" and "[id <x>".


async def _handle_id(app: FastAPI, room_id: str, sender: str, argument: str = "") -> None:
    config = app.state.config
    p = config.appservice.command_prefix
    owned = await app.state.repository.list_ids_for_user(sender)
    if not owned:
        await notice(app, room_id, f'You don\'t have a Nostr identity yet -- run "{p}create profile" first.')
        return
    arg = argument.strip()
    if not arg or arg.lower() == "list":
        active = await ids.active_id(app, sender)
        lines = ["Your Nostr identities:"]
        for number, nostr_id in enumerate(owned, start=1):
            marker = " (active)" if active is not None and nostr_id.id == active.id else ""
            lines.append(f"{number}. {_handle(app, nostr_id)}{marker}")
        if len(owned) > 1:
            lines.append(f'Switch with "{p}id <number|id>".')
        await notice(app, room_id, "\n".join(lines))
        return
    target = await ids.find_own_id(app, sender, arg.split()[0])
    if target is None:
        await notice(app, room_id, f'No identity matching "{arg}" -- run "{p}id list" to see yours.')
        return
    await app.state.repository.set_active_id(sender, target.id)
    await notice(app, room_id, f"Active identity is now {_handle(app, target)}.")


async def _handle_switch(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    """The old "[switch <x>" (and the widget's switch button)."""
    if not argument.strip():
        argument = "list"
    await _handle_id(app, room_id, sender, argument)


# -- rename -------------------------------------------------------------------


async def _handle_rename(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    config = app.state.config
    prefix = config.appservice.command_prefix
    words = argument.split()
    if len(words) != 2:
        await notice(app, room_id, f'Usage: "{prefix}rename <number or ID> <new ID>", e.g. "{prefix}rename 2 qart".')
        return
    target = await ids.find_own_id(app, sender, words[0])
    if target is None:
        await notice(app, room_id, f'You don\'t have an ID "{words[0]}". "{prefix}id" lists yours.')
        return
    new_name = ids.normalize_requested(words[1], config.bridge.domain)
    if new_name is None:
        await notice(app, room_id, f"Nostr IDs here are always @{config.bridge.domain}.")
        return
    problem = await ids.rename_problem(app, target, new_name)
    if problem is not None:
        await notice(app, room_id, f"Can't rename {_handle(app, target)}: {problem.message}")
        return
    old_handle = _handle(app, target)
    new_handle = f"{new_name}@{config.bridge.domain}"
    days = config.bridge.renamed_id_grace_days
    details = (
        "Your followers, posts and Profile Room stay; Nostr apps will show the new handle once they see your "
        f"updated profile. The old handle keeps working for {days} days, and stays reserved for you after that."
    )
    await confirm.ask(
        app,
        room_id,
        action=RENAME_ID_ACTION,
        user=sender,
        args={"id": target.id, "from": target.name, "to": new_name},
        plain=f"Rename {old_handle} to {new_handle}?\n{details}",
        rich=f"<p>Rename <strong>{html.escape(old_handle)}</strong> to <strong>{html.escape(new_handle)}</strong>?</p>"
        f"<p>{html.escape(details)}</p>",
    )


async def _execute_rename(app: FastAPI, room_id: str, sender: str, args: dict[str, Any]) -> None:
    id_ref, old, new = args.get("id"), args.get("from"), args.get("to")
    if not isinstance(id_ref, int) or not isinstance(old, str) or not isinstance(new, str):
        return
    target = await app.state.repository.get_id(id_ref)
    domain = app.state.config.bridge.domain
    if target is None or target.matrix_user_id != sender or target.name != old:
        await notice(app, room_id, f"{old}@{domain} has changed since; nothing was renamed.")
        return
    try:
        renamed = await ids.rename_id(app, target, new)
    except ids.IdRejected as exc:
        await notice(app, room_id, f"Can't rename {old}@{domain}: {exc.rejection.message}")
        return
    try:
        await sync_identity(app, renamed)
    except HomeserverError:
        logger.warning("Could not republish the profile of %s", renamed.name, exc_info=True)
    await notice(app, room_id, f"Renamed {old}@{domain} to {renamed.name}@{domain}.")


confirm.register(CREATE_ID_ACTION, _execute_create)
confirm.register(RENAME_ID_ACTION, _execute_rename)


# -- following ----------------------------------------------------------------


async def _actor(app: FastAPI, room_id: str, sender: str) -> NostrId | None:
    from bridge.interactions import acting_id

    actor = await acting_id(app, sender, room_id)
    if actor is None:
        prefix = app.state.config.appservice.command_prefix
        await notice(app, room_id, f'You need a Nostr ID first: "{prefix}create profile".')
    return actor


async def _resolve(app: FastAPI, room_id: str, argument: str):
    """The account a command names, or with none named, the one the room is
    about: a Nostr account's room, or the other side of a DM room."""
    from bridge.follows import Reference, ReferenceError, resolve_reference

    if not argument.strip():
        repository = app.state.repository
        remote = await repository.get_remote_account_for_room(room_id)
        if remote is not None:
            return Reference(remote.pubkey, remote.write_relays, remote.nip05)
        dm = await repository.get_dm_room_info(room_id)
        if dm is not None:
            remote = await repository.get_remote_account(dm[1])
            return Reference(dm[1], remote.write_relays if remote else (), remote.nip05 if remote else None)
        await notice(app, room_id, "Which account? Give an npub, an nprofile, a handle (name@domain) or a profile link, "
                                   "or run this in that account's room.")
        return None
    try:
        return await resolve_reference(app, argument.split()[0])
    except ReferenceError as exc:
        await notice(app, room_id, str(exc))
        return None


async def _account_label(app: FastAPI, pubkey: str, nip05: str | None = None) -> str:
    from bridge.remote import short_npub

    remote = await app.state.repository.get_remote_account(pubkey)
    return nip05 or (remote.nip05 if remote else None) or short_npub(pubkey)


async def _handle_follow(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    from bridge.follows import safe_follow

    actor = await _actor(app, room_id, sender)
    reference = await _resolve(app, room_id, argument) if actor else None
    if actor is None or reference is None:
        return
    if await app.state.repository.get_id_by_pubkey(reference.pubkey) is not None:
        await notice(app, room_id, "That's an ID on this server: join its Profile Room to follow it in Matrix.")
        return
    await notice(app, room_id, f"Following {await _account_label(app, reference.pubkey, reference.nip05)}…")
    remote, new, problem = await safe_follow(app, actor, reference)
    if remote is None:
        await notice(app, room_id, problem or "Couldn't follow that account.")
        return
    label = await _account_label(app, reference.pubkey, reference.nip05)
    handle = _handle(app, actor)
    what = "Now following" if new else "Already following"
    await notice(
        app, room_id, f"{what} {label} as {handle}. Its posts arrive in {remote.room_id}.",
        html_message=f"{what} <strong>{html.escape(label)}</strong> as {html.escape(handle)}. Its posts arrive in "
        f"{room_link_html(remote.room_id or '', label)}.",
    )


async def _handle_unfollow(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    from bridge.follows import unfollow

    actor = await _actor(app, room_id, sender)
    reference = await _resolve(app, room_id, argument) if actor else None
    if actor is None or reference is None:
        return
    label = await _account_label(app, reference.pubkey, reference.nip05)
    if await unfollow(app, actor, reference.pubkey):
        await notice(app, room_id, f"{_handle(app, actor)} no longer follows {label}.")
    else:
        await notice(app, room_id, f"{_handle(app, actor)} doesn't follow {label}.")


async def _handle_following(app: FastAPI, room_id: str, sender: str) -> None:
    actor = await _actor(app, room_id, sender)
    if actor is None:
        return
    pubkeys = await app.state.repository.list_follows(actor.id)
    if not pubkeys:
        await notice(app, room_id, f"{_handle(app, actor)} doesn't follow anyone yet.")
        return
    plain = [f"{_handle(app, actor)} follows:"]
    rich = [f"<p>{html.escape(_handle(app, actor))} follows:</p><ul>"]
    for pubkey in pubkeys:
        label = await _account_label(app, pubkey)
        remote = await app.state.repository.get_remote_account(pubkey)
        plain.append(f"- {label}")
        link = room_link_html(remote.room_id, label) if remote and remote.room_id else html.escape(label)
        rich.append(f"<li>{link}</li>")
    rich.append("</ul>")
    await notice(app, room_id, "\n".join(plain), html_message="".join(rich))


async def _handle_block(app: FastAPI, room_id: str, sender: str, argument: str, *, blocked: bool) -> None:
    from bridge.follows import block

    actor = await _actor(app, room_id, sender)
    reference = await _resolve(app, room_id, argument) if actor else None
    if actor is None or reference is None:
        return
    label = await _account_label(app, reference.pubkey, reference.nip05)
    changed = await block(app, actor, reference.pubkey, blocked)
    if blocked:
        await notice(app, room_id, f"{'Blocked' if changed else 'Already blocked'} {label} for {_handle(app, actor)}.")
    else:
        await notice(app, room_id, f"{'Unblocked' if changed else 'Not blocked:'} {label} for {_handle(app, actor)}.")


async def _handle_backfill(app: FastAPI, room_id: str, sender: str, argument: str, content: dict | None = None) -> None:
    """In an account's room: its latest posts. Inside a thread (or as a
    reply to a post) that's a Nostr conversation: that conversation's
    replies, as matrix-appservice-activitypub's ;backfill does."""
    from bridge.follows import backfill_posts, backfill_thread, thread_root_row
    from bridge.tasks import run_in_background

    count = int(argument.split()[0]) if argument.split() and argument.split()[0].isdigit() else 20
    if count > 100 and not await is_admin(app.state.config, app.state.homeserver, sender):
        count = 100
    root = await thread_root_row(app, room_id, content or {})
    if root is not None:
        await notice(app, room_id, f"Bringing in up to {count} replies in this conversation…")

        async def run_thread() -> None:
            found = await backfill_thread(app, root, count)
            await notice(app, room_id, f"Done: {found} replies brought in." if found else "No more replies found.")

        run_in_background(app, run_thread(), name=f"backfill thread {root.id[:8]}")
        return
    remote = await app.state.repository.get_remote_account_for_room(room_id)
    if remote is None:
        await notice(app, room_id, "Run this in a Nostr account's room, or inside a thread of a Nostr post.")
        return
    await notice(app, room_id, f"Bringing in up to {count} posts…")

    async def run() -> None:
        found = await backfill_posts(app, remote, count)
        await notice(app, room_id, f"Done: {found} posts brought in." if found else "No more posts found.")

    run_in_background(app, run(), name=f"backfill {remote.pubkey[:8]}")


async def _handle_dm(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    from bridge.dms import open_dm

    actor = await _actor(app, room_id, sender)
    reference = await _resolve(app, room_id, argument) if actor else None
    if actor is None or reference is None:
        return
    if await app.state.repository.get_id_by_pubkey(reference.pubkey) is not None:
        await notice(app, room_id, "That's an ID on this server: message its owner directly in Matrix instead.")
        return
    try:
        dm_room = await open_dm(app, actor, reference.pubkey)
    except HomeserverError:
        await notice(app, room_id, "Couldn't create the DM room just now; try again in a minute.")
        return
    label = await _account_label(app, reference.pubkey, reference.nip05)
    await notice(
        app, room_id, f"DMs with {label}, as {_handle(app, actor)}: {dm_room}",
        html_message=f"DMs with <strong>{html.escape(label)}</strong>, as {html.escape(_handle(app, actor))}: "
        f"{room_link_html(dm_room, 'open the room')}",
    )


# -- admin: bans ----------------------------------------------------------------

_DURATIONS = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800, "30d": 2592000}


async def _handle_ban(app: FastAPI, room_id: str, sender: str, argument: str, *, ban: bool) -> None:
    import ipaddress
    from datetime import timedelta

    if not await is_admin(app.state.config, app.state.homeserver, sender):
        await notice(app, room_id, "Only bridge admins can do that.")
        return
    guard = getattr(app.state, "spam", None)
    words = argument.split()
    if guard is None or not words:
        await notice(app, room_id, "Give an npub, a handle or an IP address.")
        return
    target = words[0]
    try:
        ipaddress.ip_address(target)
        kind, key, label = "ip", target, target
    except ValueError:
        reference = await _resolve(app, room_id, target)
        if reference is None:
            return
        kind, key, label = "pubkey", reference.pubkey, await _account_label(app, reference.pubkey, reference.nip05)
    if not ban:
        await guard.unban(kind, key)
        await notice(app, room_id, f"Unbanned {label}.")
        return
    rest = words[1:]
    duration = None
    if rest and (rest[0] in _DURATIONS or rest[0] == "forever"):
        duration = timedelta(seconds=_DURATIONS[rest[0]]) if rest[0] != "forever" else None
        rest = rest[1:]
    else:
        duration = timedelta(days=7)
    purge = bool(rest) and rest[0] == "purge"
    if purge:
        rest = rest[1:]
    reason = " ".join(rest) or f"banned by {sender}"
    await guard.ban(kind, key, duration, reason)
    removed = await purge_pubkey(app, key) if purge and kind == "pubkey" else 0
    until = "for good" if duration is None else f"for {words[1] if len(words) > 1 and words[1] in _DURATIONS else '7d'}"
    await notice(app, room_id, f"Banned {label} {until}." + (f" Removed {removed} of their events." if purge else ""))


async def purge_pubkey(app: FastAPI, pubkey: str) -> int:
    """Redact everything an account has in bridge rooms (and so remove it
    from the relay). Returns how many events went."""
    repository = app.state.repository
    rows = await repository.query_events({"authors": [pubkey]}, limit=10000, now=0,
                                         dm_readers=frozenset({"*"}), followed_readers=frozenset())
    removed = 0
    for row in rows:
        places = [(row.room_id, row.matrix_event_id)] if row.room_id and row.matrix_event_id else []
        places += await repository.list_mirrored_copies(row.id)
        for place_room, place_event in places:
            try:
                await app.state.homeserver.redact_event(place_room, place_event, as_user_id=bot_mxid(app.state.config), reason="Banned from the relay")
            except HomeserverError:
                logger.info("Could not redact %s", place_event, exc_info=True)
        await repository.delete_event(row.id)
        removed += 1
    return removed


async def _handle_bans(app: FastAPI, room_id: str, sender: str) -> None:
    if not await is_admin(app.state.config, app.state.homeserver, sender):
        await notice(app, room_id, "Only bridge admins can do that.")
        return
    guard = getattr(app.state, "spam", None)
    bans = await guard.list_bans() if guard else []
    if not bans:
        await notice(app, room_id, "No bans.")
        return
    lines = ["Bans:"] + [f"- {b.kind} {b.key}: until {b.until or 'forever'} ({b.reason})" for b in bans]
    await notice(app, room_id, "\n".join(lines))


# -- status -------------------------------------------------------------------


async def _handle_status(app: FastAPI, room_id: str, sender: str) -> None:
    config = app.state.config
    domain = config.bridge.domain
    lines: list[str] = []
    owned = await app.state.repository.list_ids_for_user(sender)
    if owned:
        active = await ids.active_id(app, sender)
        lines.append("Your Nostr IDs:")
        for number, nostr_id in enumerate(owned, start=1):
            marker = "  ← active" if active is not None and nostr_id.id == active.id else ""
            lines.append(f"{number}. {nostr_id.name}@{domain} ({nip19.npub_encode(nostr_id.pubkey)}){marker}")
    else:
        lines.append("You don't have a Nostr ID yet.")
    lines.append("")
    lines.append(f"Handles: name@{domain}")
    check = getattr(app.state, "nip05_self_check", None)
    if check is None:
        lines.append("Handle check: not run yet")
    else:
        ok, explanation, _when = check
        lines.append(f"Handle check: {'working' if ok else 'NOT working'} ({explanation})")
    lines.append(f"Relay: {config.nostr.relay_url}")
    if await is_admin(config, app.state.homeserver, sender):
        lines.append("")
        lines.append("Outgoing relay connections (admin):")
        connections = app.state.relay_pool.connections()
        if not connections:
            lines.append("  none open")
        for connection in connections:
            state = "connected" if connection.is_connected else f"not connected ({connection.last_error or 'idle'})"
            lines.append(f"  {connection.url}: {state}")
    await notice(app, room_id, "\n".join(lines))


# -- dispatch -----------------------------------------------------------------


async def maybe_handle_command(request: Request, event: dict) -> bool:
    """Returns True if the message addressed the bot (and so is handled,
    whether or not a recognized command was found in it)."""
    if event.get("type") != "m.room.message":
        return False
    app = request.app
    config = app.state.config
    content = event.get("content") or {}
    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    if not sender or not room_id:
        return False
    if not message_addresses_bot(content, config):
        # Another bot's command: not ours to answer, and never a post.
        return message_addresses_other_bot(content, config)
    if is_bridge_user(config, sender):
        return True

    prefix = config.appservice.command_prefix
    parsed = _extract_prefixed_command(content, config)
    if parsed is None:
        # Tagged: the keyword can be anywhere on the first line.
        match = _COMMAND_RE.search(first_line(content))
        parsed = (match.group(1).lower(), match.group(2).strip()) if match else None
    if parsed is None:
        residue = first_line(content).replace(bot_mxid(config), "")
        for name in (config.appservice.bot_display_name, config.appservice.bot_localpart):
            if name:
                residue = residue.replace(name, "")
        if residue.strip(" \t@:,"):
            await notice(app, room_id, f'Not sure what you mean. Try "{prefix}help" to see what I can do.')
        else:
            await _handle_help(app, room_id)
        return True

    keyword, argument = parsed
    if not is_local_user(config, sender):
        await notice(app, room_id, f"Only users of {config.homeserver.server_name} can use this bridge.")
        return True
    if keyword == "help":
        await _handle_help(app, room_id, argument, sender)
    elif keyword == "status":
        await _handle_status(app, room_id, sender)
    elif keyword == "create":
        await _handle_create(app, room_id, sender, argument)
    elif keyword in ("id", "profiles"):
        await _handle_id(app, room_id, sender, argument)
    elif keyword == "switch":
        await _handle_switch(app, room_id, sender, argument)
    elif keyword == "rename":
        await _handle_rename(app, room_id, sender, argument)
    elif keyword == "join":
        from bridge.concord import handle_join

        await handle_join(app, room_id, sender, argument)
    elif keyword == "play":
        from bridge.games_room import handle_play

        await handle_play(app, room_id, sender, content, argument)
    elif keyword == "delete":
        await id_deletion.handle_delete(app, room_id, sender, argument)
    elif keyword in ("link", "unlink"):
        handler = room_links.handle_link if keyword == "link" else room_links.handle_unlink
        await handler(app, room_id, sender, argument)
    elif keyword == "follow":
        await _handle_follow(app, room_id, sender, argument)
    elif keyword == "unfollow":
        await _handle_unfollow(app, room_id, sender, argument)
    elif keyword == "following":
        await _handle_following(app, room_id, sender)
    elif keyword in ("block", "unblock"):
        await _handle_block(app, room_id, sender, argument, blocked=keyword == "block")
    elif keyword == "backfill":
        await _handle_backfill(app, room_id, sender, argument, content)
    elif keyword == "dm":
        await _handle_dm(app, room_id, sender, argument)
    elif keyword in ("ban", "unban"):
        await _handle_ban(app, room_id, sender, argument, ban=keyword == "ban")
    elif keyword == "bans":
        await _handle_bans(app, room_id, sender)
    elif keyword == "widget":
        from bridge.room_widget import add_bridge_widget

        added = await add_bridge_widget(app, room_id)
        await notice(app, room_id, "Added the Nostr Bridge widget to this room." if added else
                     "Couldn't add the widget here: the bot needs permission to change room settings.")
    elif keyword == "import" and (not argument.split() or argument.split()[0].lower() not in ("key", "keys")):
        from bridge.importing import handle_import

        await handle_import(app, room_id, sender, argument)
    elif keyword in ("import", "export"):
        await notice(
            app, room_id,
            f"Keys never go through chat messages: a homeserver stores those unencrypted. Open the Nostr Bridge widget "
            f'(add it with "{prefix}widget" if this room doesn\'t have it) to {"import a key or connect a remote signer" if keyword == "import" else "export a key"}.',
        )
    return True
