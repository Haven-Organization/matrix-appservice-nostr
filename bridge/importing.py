"""``[import <post>``: bring one Nostr post into Matrix, as
matrix-appservice-activitypub's ``;import <url>`` does, without following
its author.

``<post>`` is a ``nevent1…``, ``note1…`` or ``naddr1…`` (a long-form
article), bare, with ``nostr:``, or inside a link to any Nostr website
(njump.me, ditto.pub, primal.net, ...).

- A post that's already in Matrix isn't brought in twice: the bot links to
  it.
- A reply is threaded where its conversation is: its ancestors are fetched
  up the thread (at most ``_MAX_ANCESTORS``) until one is already in Matrix,
  or the root is reached and brought into its author's room; then each is
  threaded under the one before. ``[backfill`` in that room covers the rest.
- Anything else goes into its author's room, made if needed.

The user is invited to the room it lands in, and the bot replies with a link.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from fastapi import FastAPI

from bridge.messages import invite_to_room, notice, room_link_html
from bridge.nostr import nip19

logger = logging.getLogger(__name__)

_BECH_RE = re.compile(r"\b((?:nevent|note|naddr)1[02-9ac-hj-np-z]+)", re.IGNORECASE)
_MAX_ANCESTORS = 10
IMPORTABLE_KINDS = frozenset({1, 20, 21, 22, 1111, 30023, 34235, 34236})


class ImportProblem(Exception):
    """Why a post couldn't be imported; safe to show."""


def parse_post_reference(text: str) -> nip19.EventPointer | nip19.Address:
    match = _BECH_RE.search(text.strip())
    if match is None:
        raise ImportProblem("Give a link to a Nostr post, or its nevent, note or naddr.")
    code = match.group(1).lower()
    try:
        decoded = nip19.decode(code)
    except nip19.Nip19Error as exc:
        raise ImportProblem(f"That post reference isn't valid ({exc}).") from exc
    if isinstance(decoded, str):  # note1…: just the id
        return nip19.EventPointer(decoded)
    if isinstance(decoded, (nip19.EventPointer, nip19.Address)):
        return decoded
    raise ImportProblem("That's not a post reference.")


async def fetch_post(app: FastAPI, reference: nip19.EventPointer | nip19.Address) -> dict[str, Any]:
    from bridge.inbound import fetch_event
    from bridge.nostr import event as nostr_event
    from bridge.remote import write_relays_for

    if isinstance(reference, nip19.EventPointer):
        found = await fetch_event(app, reference.id, hints=list(reference.relays), author=reference.author)
        if found is None:
            raise ImportProblem("I couldn't find that post on any relay I tried.")
        return found
    # An address (a long-form article): the newest version of it.
    remote = await app.state.repository.get_remote_account(reference.pubkey)
    relays = [*reference.relays, *write_relays_for(app, remote), *app.state.config.nostr.fallback_relays]
    query = {"kinds": [reference.kind], "authors": [reference.pubkey], "#d": [reference.identifier]}
    newest = None
    for raw in await app.state.relay_pool.query(relays, [query], timeout=8.0):
        try:
            nostr_event.verify(raw)
        except nostr_event.InvalidEvent:
            continue
        if raw.get("pubkey") == reference.pubkey and (newest is None or raw["created_at"] > newest["created_at"]):
            newest = raw
    if newest is None:
        raise ImportProblem("I couldn't find that article on any relay I tried.")
    return newest


async def import_post(app: FastAPI, raw: dict[str, Any]):
    """The post's row once it's in Matrix (brought in now, or already)."""
    from bridge.inbound import _ensure_post_in_matrix, _reply_in_matrix, fetch_event, reply_targets

    repository = app.state.repository
    if int(raw.get("kind", -1)) not in IMPORTABLE_KINDS:
        raise ImportProblem("That isn't a post (a note, picture, video or article).")
    existing = await repository.get_event(raw["id"])
    if existing is not None and existing.room_id and existing.matrix_event_id:
        return existing
    # Up the thread until something is already here, or the root.
    chain = [raw]
    while len(chain) <= _MAX_ANCESTORS:
        parent_id, _root = reply_targets(chain[-1])
        if not parent_id:
            break
        known = await repository.get_event(parent_id)
        if known is not None and known.room_id and known.matrix_event_id:
            break
        hints = [t[2] for t in chain[-1].get("tags") or [] if len(t) >= 3 and t[0] == "e" and t[1] == parent_id and t[2]]
        parent = await fetch_event(app, parent_id, hints=hints)
        if parent is None:
            break  # a deleted or unreachable ancestor: thread from what we have
        chain.append(parent)
    top = chain[-1]
    parent_id, _root = reply_targets(top)
    anchor = await repository.get_event(parent_id) if parent_id else None
    if anchor is None or not anchor.room_id or not anchor.matrix_event_id:
        anchor = await _ensure_post_in_matrix(app, top)
        chain.pop()
    for post in reversed(chain):
        if anchor is None or not anchor.room_id or not anchor.matrix_event_id:
            anchor = await _ensure_post_in_matrix(app, post)
        else:
            await _reply_in_matrix(app, post, anchor, followed=False, mentioned=[])
            anchor = await repository.get_event(post["id"])
    row = await repository.get_event(raw["id"])
    if row is None or not row.room_id or not row.matrix_event_id:
        raise ImportProblem("I found the post but couldn't bring it into Matrix.")
    return row


async def handle_import(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    if not argument.strip():
        await notice(app, room_id, f'Usage: "{p}import <link to a Nostr post, or its nevent/note/naddr>". '
                                   f'(To bring a key you already have: "{p}import key".)')
        return
    try:
        reference = parse_post_reference(argument)
        raw = await fetch_post(app, reference)
        already = await app.state.repository.get_event(raw["id"])
        row = await import_post(app, raw)
    except ImportProblem as exc:
        await notice(app, room_id, str(exc))
        return
    await invite_to_room(app, row.room_id, sender)
    link = f"https://matrix.to/#/{row.room_id}/{row.matrix_event_id}"
    what = "Already here" if already is not None and already.matrix_event_id else "Imported"
    await notice(
        app, room_id, f"{what}: {link}",
        html_message=f'{what}: <a href="{html.escape(link, quote=True)}">the post</a>, in '
        f"{room_link_html(row.room_id)}.",
    )
