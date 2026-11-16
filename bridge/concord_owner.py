"""Owning a Concord community: ``[concord``.

``bridge.concord`` joins communities others made and carries their
messages. This is the owner's side, for a community made here:

- ``[concord create <name>`` (CORD-02 §1): mints the community's secrets
  (``owner_salt``, ``community_root``, ``control_root``), publishes its
  genesis on the Control Plane (the metadata and a public ``#general``, both
  owner-signed), activates Roles v2 (CORD-04 §2: ``@everyone`` with the
  default bits), announces the owner's Join on the Guestbook, and records
  the membership in the owner's Community List (CORD-02 §8) so Armada or
  Vector signed in as the same npub shows it too.
- ``[concord channel <name>``, in an existing Matrix room: a new public
  Channel (CORD-03 §2), with that room as its Matrix side, in a community
  made here or one where the user's ID holds MANAGE_CHANNELS and the bridge
  can write the Control Plane (a legacy one, whose read key signs too; the
  edition cites the actor's Grant, CORD-04 §5). The room keeps
  its name, avatar, join rules and power levels; it's added to the
  community's Space. Messages from then on go to the Channel.
- ``[concord rename <name>``, in a channel's room: the Channel's next
  edition, on the head the plane holds, its other fields carried as they
  are. An attached room keeps its own Matrix name.
- An attached room's topic is the channel's: taken when it's attached,
  sent again when a manager changes it in Matrix, or on ``[concord topic``.
- ``[concord link [label]`` (CORD-05): a shareable link. Its bundle goes on
  relays under a fresh link signer, encrypted by a token only the link's
  fragment carries; the link is listed in the owner's Registry (vsk 8) and
  Invite List (kind 13303). ``[concord links`` lists them, ``[concord
  revoke <n>`` retires one (a tombstone at the same coordinate).
- ``[concord invite <who>`` (CORD-05 §6): a Direct Invite, the bundle itself
  giftwrapped (NIP-59, ``k`` 3313) to one npub, by any member, of a
  community made here or joined. ``<who>`` is an npub, a NIP-05 name, a
  pill or MXID of a Nostr account's ghost, or a reply to one of its
  messages. Inviting a ghost to the community's Space or a channel room
  in Matrix does the same. Someone with an ID on this server is invited
  to the Space in Matrix instead.

Not here: roles, bans, private channels, rekeys. Retiring the last link
should refound a private community (CORD-05 §2, CORD-06); this bridge
doesn't refound, so the members already in keep their keys.

Every Control edition is the owner's, so it cites no Grant (CORD-04 §5).
The chain head of each entity the bridge wrote is kept in
``Community.mine`` so the next edition links to it.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import struct
import time
from typing import Any

from fastapi import FastAPI

from bridge import concord
from bridge.concord import Community, ConcordProblem, GroupKey, RELAY_DICTIONARY, group_key, hkdf
from bridge.homeserver_client import HomeserverError
from bridge.messages import notice, room_link_html
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19, nip44, secp256k1
from bridge.nostr.relay_pool import normalize_relay_url
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

INVITE_BASE = "https://armada.buzz"
INVITE_LIST_KIND = 13303
COMMUNITY_LIST_KIND = 33302
# CORD-04 §3: what @everyone holds at the switch to Roles v2. VIEW_CHANNEL
# through VIDEO (bits 13 to 21) and CHANGE_NICKNAME (bit 29).
EVERYONE_DEFAULTS = sum(1 << b for b in range(13, 22)) | (1 << 29)
_EDITION_LABEL = b"vector-community/v1/edition"
_EVENT_CEILING = 56 * 1024  # what a self-encrypted list is packed under (CORD-02 §8)
_NAME_BYTES = 64


# -- derivations (CORD-02 A.6, CORD-04 §1) -----------------------------------------------


def locator(community_id: str, label: str, ident: bytes = bytes(32)) -> str:
    """A derived coordinate (``concord/everyone``, ``concord/invite-links``, ...)."""
    return hkdf(bytes.fromhex(community_id), label, ident).hex()


def edition_hash(eid: str, version: int, prev: str | None, content: str, signer: str | None) -> str:
    """CORD-04 §1: the hash the next edition's ``ep`` names; marked when
    ``signer`` is given."""
    body = content.encode()
    preimage = (struct.pack(">Q", len(_EDITION_LABEL)) + _EDITION_LABEL + bytes.fromhex(eid) + struct.pack(">Q", version)
                + (b"\x01" + bytes.fromhex(prev) if prev else b"\x00" + bytes(32))
                + struct.pack(">Q", len(body)) + body)
    if signer is not None:
        preimage += b"concord/edition-signer" + bytes.fromhex(signer)
    return hashlib.sha256(preimage).hexdigest()


def control_signer(community: Community) -> GroupKey:
    return group_key("concord/control-signer", bytes.fromhex(community.control_root), bytes.fromhex(community.id),
                     community.epoch)


def _b64u(hex_value: str) -> str:
    return base64.urlsafe_b64encode(bytes.fromhex(hex_value)).decode().rstrip("=")


def _cut(name: str) -> str:
    """At most 64 bytes of UTF-8, cut at a whole character."""
    return name.encode()[:_NAME_BYTES].decode("utf-8", "ignore").strip()


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# -- the Control Plane ------------------------------------------------------------------


def can_write(community: Community) -> bool:
    """Whether the bridge can mint a wrap at the Control Plane's address:
    with the staff key (made here), or in a legacy Community from before the
    split, where the read key signs too (CORD-02 §5)."""
    return bool(community.control_root) or community.control_pk is None


def _plane_signer(community: Community) -> GroupKey:
    return control_signer(community) if community.control_root else community.control()


async def publish_edition(app: FastAPI, community: Community, secret: bytes, vsk: str, eid: str, content: Any,
                          *, marked: bool = True, vac: list[str] | None = None,
                          after: tuple[int, str] | None = None) -> str:
    """An edition (CORD-04 §1) of one entity, chained on the last one
    written here, sealed in plaintext (kind 20014) by the actor and wrapped
    at the Control Plane's address. ``vac``: the Grant a non-owner acts
    under (CORD-04 §5). ``after``: the (version, hash) of the head it
    supersedes, when that's known from the plane rather than from what was
    written here. Returns its hash."""
    owner = secp256k1.public_key(secret).hex()
    head = community.mine.get(eid) or {}
    version = int(head.get("v") or 0) + 1
    prev = head.get("h") or None
    if after is not None:
        version, prev = after[0] + 1, after[1]
    text = _compact(content)
    tags = [["vsk", vsk], ["eid", eid], ["ev", str(version)]]
    if prev:
        tags.append(["ep", prev])
    if vac:
        tags.append(["vac", *vac])
    if marked:
        tags += [["eh", "2"], ["eb", locator(community.id, "concord/edition-binding")]]
    created_at = int(time.time())
    rumor = {"id": nostr_event.compute_id(owner, created_at, concord.EDITION_KIND, tags, text), "pubkey": owner,
             "created_at": created_at, "kind": concord.EDITION_KIND, "tags": tags, "content": text}
    seal = nostr_event.build(secret, kind=concord.SEAL_PLAINTEXT, created_at=created_at, tags=[], content=_compact(rumor))
    signer, read = _plane_signer(community), community.control()
    ephemeral = secp256k1.public_key(secp256k1.generate_secret_key()).hex()
    wrap = nostr_event.build(signer.sk, kind=concord.WRAP_KIND, created_at=created_at, tags=[["p", ephemeral]],
                             content=nip44.encrypt(_compact(seal.to_dict()), read.conv))
    digest = edition_hash(eid, version, prev, text, owner if marked else None)
    community.mine[eid] = {"v": version, "h": digest}
    results = await app.state.relay_pool.publish(wrap, community.relays)
    if not any(r.accepted for r in results):
        logger.warning("No relay took edition %s of %s (vsk %s): %s", version, eid, vsk,
                       "; ".join(f"{r.relay}: {r.message}" for r in results))
    return digest


# -- who owns what -----------------------------------------------------------------------


async def owned(app: FastAPI, sender: str) -> list[Community]:
    """The communities ``sender`` owns through one of their IDs, and the
    bridge can write for (made here: it holds the control_root)."""
    out = []
    for community in await concord.all_communities(app):
        if community.removed or not community.control_root:
            continue
        owner = await app.state.repository.get_id_by_pubkey(community.owner)
        if owner is not None and owner.matrix_user_id == sender:
            out.append(community)
    return out


async def _owner_secret(app: FastAPI, community: Community) -> bytes | None:
    return await app.state.signer.secret_key(community.owner)


def _split_in(argument: str, names: list[str]) -> tuple[str, str | None]:
    """``what in <community>``: the community named last, if it's one of
    ``names``; otherwise all of it is ``what`` (a name may say "in" too)."""
    text = argument.strip()
    head, sep, tail = text.rpartition(" in ")
    if sep and tail.strip().casefold() in {n.casefold() for n in names}:
        return head.strip(), tail.strip()
    return text, None


def _pick(communities: list[Community], name: str | None) -> Community | None:
    if name:
        wanted = name.strip().casefold()
        return next((c for c in communities if c.name.casefold() == wanted), None)
    return communities[0] if len(communities) == 1 else None


# -- self-encrypted lists (CORD-02 §8, CORD-05 §4) -------------------------------------------


def _list_relays(app: FastAPI, community: Community) -> list[str]:
    return list(dict.fromkeys([*app.state.config.nostr.publish_relays, *RELAY_DICTIONARY.values(), *community.relays]))


async def _read_own(app: FastAPI, secret: bytes, kind: int, relays: list[str]) -> list[tuple[dict[str, Any], Any]]:
    """Every copy of one of the owner's own self-encrypted lists: (event,
    its parsed plaintext). Raises if a copy signed by them doesn't open,
    since it may hold what a write would lose."""
    owner = secp256k1.public_key(secret).hex()
    found = await app.state.relay_pool.query(relays, [{"kinds": [kind], "authors": [owner]}], timeout=10.0)
    key = nip44.get_conversation_key(secret, owner)
    out = []
    for raw in found:
        try:
            nostr_event.verify(raw)
        except nostr_event.InvalidEvent:
            continue
        if raw.get("pubkey") != owner or raw.get("kind") != kind:
            continue
        try:
            out.append((raw, json.loads(nip44.decrypt(raw["content"], key))))
        except (ValueError, KeyError) as exc:
            raise ConcordProblem("a copy of it on the relays wouldn't open") from exc
    return out


def _self_encrypted(secret: bytes, kind: int, plaintext: Any, created_at: int, tags: list[list[str]]) -> nostr_event.Event:
    owner = secp256k1.public_key(secret).hex()
    content = nip44.encrypt(_compact(plaintext), nip44.get_conversation_key(secret, owner))
    return nostr_event.build(secret, kind=kind, created_at=created_at, tags=tags, content=content)


async def _update_invite_list(app: FastAPI, community: Community, secret: bytes, *, add: dict[str, Any] | None = None,
                              retire: str | None = None) -> None:
    """Read, merge, write (CORD-05 §4): the token is the merge key, entries
    never change once minted, tombstones union and beat entries."""
    relays = _list_relays(app, community)
    copies = await _read_own(app, secret, INVITE_LIST_KIND, relays)
    entries: dict[str, dict[str, Any]] = {}
    tombstones: dict[str, dict[str, Any]] = {}
    newest = 0
    for raw, plain in copies:
        newest = max(newest, int(raw.get("created_at") or 0))
        if not isinstance(plain, dict):
            raise ConcordProblem("a copy of it on the relays isn't a list")
        for entry in plain.get("entries") or []:
            if isinstance(entry, dict) and isinstance(entry.get("token"), str):
                entries.setdefault(entry["token"], entry)
        for grave in plain.get("tombstones") or []:
            if isinstance(grave, dict) and isinstance(grave.get("token"), str):
                tombstones.setdefault(grave["token"], grave)
    if add is not None:
        entries.setdefault(add["token"], add)
    if retire is not None:
        grave = {"token": retire, "community_id": community.id}
        tombstones.setdefault(retire, grave)
    now = int(time.time())

    def live(item: dict[str, Any]) -> bool:
        expires = item.get("expires_at")
        return not isinstance(expires, int) or expires > now

    plaintext = {"entries": [e for t, e in entries.items() if t not in tombstones and live(e)],
                 "tombstones": [g for g in tombstones.values() if live(g)]}
    event = _self_encrypted(secret, INVITE_LIST_KIND, plaintext, max(now, newest + 1), [])
    if len(_compact(event.to_dict()).encode()) > _EVENT_CEILING:
        raise ConcordProblem("your list of invite links is full")
    await app.state.relay_pool.publish(event, relays)


def _join_material(community: Community) -> dict[str, Any]:
    """The owner's membership (CORD-02 §8): every 32-byte value unpadded
    base64url; public Channels need no key, so none are listed."""
    material: dict[str, Any] = {
        "owner": _b64u(community.owner), "owner_salt": _b64u(community.owner_salt),
        "community_root": _b64u(community.root), "root_epoch": community.epoch,
        "control_pk": _b64u(community.control_pk or control_signer(community).pk), "channels": [],
        "relays": community.relays, "name": community.name,
    }
    if community.control_root:
        material["control_root"] = _b64u(community.control_root)
    return material


async def _add_to_community_list(app: FastAPI, community: Community, secret: bytes) -> None:
    """The owner's Community List gains this community, so their other
    clients show it. Written only from a whole read; a fragment that would
    pass the event ceiling is left alone (and logged)."""
    relays = list(dict.fromkeys([*app.state.config.nostr.publish_relays, *RELAY_DICTIONARY.values()]))
    copies = await _read_own(app, secret, COMMUNITY_LIST_KIND, relays)
    fragments: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    for raw, plain in copies:
        d = next((t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "d"), "")
        if not d.isdigit() or not isinstance(plain, dict):
            continue
        index = int(d)
        if index not in fragments or raw["created_at"] > fragments[index][0]["created_at"]:
            fragments[index] = (raw, plain)
    frags = 1
    if fragments:
        newest = max(fragments.values(), key=lambda f: (f[0]["created_at"], int(f[1].get("frags") or 1)))
        frags = max(1, int(newest[1].get("frags") or 1))
        if any(i not in fragments for i in range(frags)):
            raise ConcordProblem("part of it couldn't be found on the relays")
    key = _b64u(community.id)
    target = next((i for i, (_raw, plain) in fragments.items()
                   if any(isinstance(e, dict) and e.get("community_id") == key for e in plain.get("entries") or [])),
                  frags - 1)
    raw, plain = fragments.get(target, ({"created_at": 0}, {"frags": frags, "entries": [], "tombstones": []}))
    plain = dict(plain)
    entries = [e for e in plain.get("entries") or [] if not (isinstance(e, dict) and e.get("community_id") == key)]
    entries.append({"community_id": key, "current": _join_material(community), "added_at": int(time.time() * 1000)})
    plain["entries"] = entries
    plain["frags"] = frags
    plain.setdefault("tombstones", [])
    event = _self_encrypted(secret, COMMUNITY_LIST_KIND, plain, max(int(time.time()), int(raw["created_at"]) + 1),
                            [["d", str(target)]])
    if len(_compact(event.to_dict()).encode()) > _EVENT_CEILING:
        raise ConcordProblem("its fragment is full")
    await app.state.relay_pool.publish(event, relays)


# -- [concord create ----------------------------------------------------------------------


async def create(app: FastAPI, room_id: str, sender: str, name: str) -> None:
    from bridge.shadow_ids import ensure_shadow_id_and_tell

    p = app.state.config.appservice.command_prefix
    name = _cut(name)
    if not name:
        await notice(app, room_id, f'Usage: "{p}concord create <name>".')
        return
    member = await ensure_shadow_id_and_tell(app, sender, room_id)
    if member is None:
        await notice(app, room_id, f'You need a Nostr identity first: "{p}create profile".')
        return
    secret = await app.state.signer.secret_key(member.pubkey)
    if secret is None or member.key_origin == "bunker":
        await notice(app, room_id, f"{member.name}@{app.state.config.bridge.domain} signs through a remote signer, which "
                                   "Concord communities can't work with yet. Switch to another identity to make one.")
        return
    salt, root, control_root = secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32)
    cid = concord.community_id(member.pubkey, salt)
    community = Community(id=cid, owner=member.pubkey, root=root, epoch=0, control_pk=None,
                          relays=list(RELAY_DICTIONARY.values()), name=name, description="", picture="",
                          channels={}, space=None, members=[member.id], present=[member.pubkey],
                          owner_salt=salt, control_root=control_root)
    community.control_pk = control_signer(community).pk
    general = secrets.token_hex(32)
    # Genesis (CORD-02 §1): the metadata and one public #general, then
    # Roles v2 switched on (CORD-04 §2).
    await publish_edition(app, community, secret, "0", cid, {"name": name, "relays": community.relays}, marked=False)
    await publish_edition(app, community, secret, "2", general, {"name": "general", "private": False}, marked=False)
    await publish_edition(app, community, secret, "13", locator(cid, "concord/everyone"),
                          {"permissions": str(EVERYONE_DEFAULTS), "legacy": [general]})
    community.channels[general] = {"name": "general", "private": False, "epoch": 0}
    try:
        await concord._ensure_rooms(app, community, [sender])
    except HomeserverError:
        logger.warning("Could not make the rooms of %s", cid, exc_info=True)
        await save_and_follow(app, community)
        await notice(app, room_id, "The community is made, but I couldn't make its Matrix rooms just now. They'll be "
                                   "made when the bridge next restarts.")
        return
    await save_and_follow(app, community)
    await concord._announce_join(app, community, member, {})
    listed = await _record_membership(app, community, secret)
    await notice(app, room_id,
                 f"Made {name}, owned by {member.name}@{app.state.config.bridge.domain}, with #general. Its space: "
                 f"{community.space}. {concord.WARNING}\n"
                 f'Run "{p}concord channel <name>" in a room to make it a channel, "{p}concord link" for a link '
                 f'to share, and "{p}concord invite <npub or name@domain>" to invite someone.{listed}',
                 html_message=f"Made <strong>{html.escape(name)}</strong>, owned by "
                 f"{html.escape(member.name)}@{html.escape(app.state.config.bridge.domain)}, with #general. Its space: "
                 f"{room_link_html(community.space or '', name)}. {html.escape(concord.WARNING)}<br>"
                 f"Run <code>{html.escape(p)}concord channel &lt;name&gt;</code> in a room to make it a channel, and "
                 f"<code>{html.escape(p)}concord link</code> for a link to share, and <code>{html.escape(p)}concord invite "
                 f"&lt;npub or name@domain&gt;</code> to invite someone.{html.escape(listed)}")


async def _record_membership(app: FastAPI, community: Community, secret: bytes) -> str:
    try:
        await _add_to_community_list(app, community, secret)
    except ConcordProblem as exc:
        logger.warning("Didn't add %s to its owner's Community List: %s", community.id, exc)
        return (f" (I couldn't add it to your Concord community list: {exc}. Armada or Vector signed in as you "
                "won't list it until you open an invite link there.)")
    return ""


async def save_and_follow(app: FastAPI, community: Community) -> None:
    await concord.save(app, community)
    app.state.feeds.changed()


# -- [concord channel ---------------------------------------------------------------------


async def add_channel(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    from bridge.commands import bridged_as

    p = app.state.config.appservice.command_prefix
    managing = await managed(app, sender)
    mine = [c for c, _ in managing]
    name, which = _split_in(argument, [c.name for c in mine])
    name = _cut(name.strip().lstrip("#"))
    if not name:
        await notice(app, room_id, f'Usage: run "{p}concord channel <name>" in the room that becomes the channel.')
        return
    if not mine:
        await notice(app, room_id, "You don't manage the channels of any community I can write for. Make one with "
                                   f'"{p}concord create <name>", or get a role that can manage channels in one you\'re in.')
        return
    community = _pick(mine, which) or await _by_space(app, mine, room_id)
    if community is None:
        names = ", ".join(c.name for c in mine)
        await notice(app, room_id, f'Which community ({names})? Say "{p}concord channel {name} in <community>".')
        return
    already = await bridged_as(app, room_id)
    if already is not None:
        await notice(app, room_id, f"This room is already {already}, so it can't be a channel too.")
        return
    homeserver, bot = app.state.homeserver, bot_mxid(app.state.config)
    try:
        encrypted = await homeserver.get_room_state(room_id, "m.room.encryption", as_user_id=bot) is not None
    except HomeserverError:
        encrypted = False
    if encrypted:
        await notice(app, room_id, "This room is end-to-end encrypted, and I can't read encrypted rooms, so it can't "
                                   "be a channel.")
        return
    actor = next(m for c, m in managing if c.id == community.id)
    secret = await app.state.signer.secret_key(actor.pubkey)
    if secret is None or community.space is None:
        await notice(app, room_id, f"I can't write for {community.name} right now.")
        return
    try:
        marked, vac, _rumors = await _authority(app, community, actor.pubkey)
    except ConcordProblem as exc:
        await notice(app, room_id, f"I can't add a channel to {community.name}: {exc}.")
        return
    channel_id = secrets.token_hex(32)
    topic = await _room_topic(app, room_id)
    # The room stays as it is (name, avatar, who may join, power levels):
    # it's only added to the Space. Saved before the edition goes out: the
    # relays echo it straight back, and a community read then must already
    # know the channel's room, or it makes one.
    community.channels[channel_id] = {"name": name, "private": False, "epoch": 0, "room": room_id,
                                      "attached": True, "open": True}
    await app.state.repository.set_state(concord._ROOM.format(room_id), f"{community.id}|{channel_id}")
    await concord.save(app, community)
    await publish_edition(app, community, secret, "2", channel_id,
                          {"name": name, "private": False, **({"topic": topic} if topic else {})}, marked=marked, vac=vac)
    await concord.save(app, community)
    server = app.state.config.homeserver.server_name
    try:
        await homeserver.send_state_event(community.space, "m.space.child", {"via": [server], "suggested": True},
                                          as_user_id=bot, state_key=room_id)
    except HomeserverError:
        logger.info("Could not add %s to the Space of %s", room_id, community.id, exc_info=True)
    try:
        await homeserver.send_state_event(room_id, "m.space.parent", {"via": [server], "canonical": True},
                                          as_user_id=bot, state_key=community.space)
    except HomeserverError:
        pass  # only a pointer back; the Space lists the room without it
    users = sorted({m.matrix_user_id for i in community.members if (m := await app.state.repository.get_id(i))})
    try:
        await concord._ensure_rooms(app, community, users)
        await concord._power_levels(app, community)
    except HomeserverError:
        logger.info("Could not update the rooms of %s", community.id, exc_info=True)
    await save_and_follow(app, community)
    await notice(app, room_id,
                 f"This room is now #{name} in {community.name}, in its Space: what's said here from now on goes to the community "
                 "(earlier messages stay in Matrix). Anyone on this homeserver who talks here joins the community "
                 f'with their Nostr identity, or a shadow one made for them. For people from other homeservers, '
                 f'"{p}allow room" here lets them in too. {concord.WARNING}')


def _channel_head(community: Community, rumors: list[dict[str, Any]], channel_id: str) -> tuple[dict[str, Any], dict[str, str]] | None:
    """A Channel's current edition on the plane: the highest version by
    someone who may manage channels; at one version, the lower id."""
    best = None
    for rumor in rumors:
        tags = {t[0]: t[1] for t in rumor.get("tags") or [] if len(t) >= 2}
        if (rumor.get("kind") != concord.EDITION_KIND or tags.get("vsk") != "2" or tags.get("eid") != channel_id
                or not tags.get("ev", "").isdigit() or not community.can(rumor.get("pubkey", ""), concord.MANAGE_CHANNELS)):
            continue
        key = (-int(tags["ev"]), rumor.get("id", ""))
        if best is None or key < best[0]:
            best = (key, rumor, tags)
    return (best[1], best[2]) if best else None


def _topic(text: Any) -> str:
    """A Channel ``topic``: at most 1024 bytes, cut at a whole character
    (CORD-03 §2); empty means none."""
    return str(text or "").strip().encode()[:1024].decode("utf-8", "ignore").strip()


async def update_channel(app: FastAPI, community: Community, actor, channel_id: str,
                         change: dict[str, Any]) -> dict[str, Any]:
    """The Channel's next edition, on the head the plane holds, with
    ``change`` applied (a ``None`` value removes the field) and everything
    else carried as it is (CORD-02 §6). Returns the content it had before;
    raises ``ConcordProblem`` if it can't be written."""
    secret = await app.state.signer.secret_key(actor.pubkey)
    if secret is None:
        raise ConcordProblem(f"I can't write for {community.name} right now")
    marked, vac, rumors = await _authority(app, community, actor.pubkey)
    head = _channel_head(community, rumors, channel_id)
    if head is None:
        raise ConcordProblem("I couldn't find this channel's current details on its relays, so I left it alone "
                             "(writing without them would lose them)")
    rumor, tags = head
    try:
        before = json.loads(rumor.get("content") or "")
    except ValueError:
        before = None
    if not isinstance(before, dict) or "sealed" in before or before.get("deleted"):
        raise ConcordProblem("this channel's details are private or it's deleted")
    content = dict(before)
    for key, value in change.items():
        if value is None:
            content.pop(key, None)
        else:
            content[key] = value
    if content == before:
        return before
    current = edition_hash(channel_id, int(tags["ev"]), tags.get("ep"), rumor.get("content") or "",
                           rumor["pubkey"] if tags.get("eh") == "2" else None)
    if "name" in change:
        community.channels.setdefault(channel_id, {"epoch": 0})["name"] = change["name"]
        await concord.save(app, community)
    await publish_edition(app, community, secret, "2", channel_id, content, marked=marked, vac=vac,
                          after=(int(tags["ev"]), current))
    await concord.save(app, community)
    return before


async def _channel_here(app: FastAPI, room_id: str, sender: str, what: str):
    """(community, the sender's managing ID, channel id) for this channel
    room, or ``None`` after saying why not."""
    mapped = await app.state.repository.get_state(concord._ROOM.format(room_id))
    if not mapped:
        return None
    cid, _, channel_id = mapped.partition("|")
    found = next(((c, m) for c, m in await managed(app, sender) if c.id == cid), None)
    if found is None:
        await notice(app, room_id, f"You can't {what} channels in this community (it takes a role that manages "
                                   "channels, in a community I can write for).")
        return None
    return found[0], found[1], channel_id


async def rename_channel(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    name = _cut(argument.strip().lstrip("#"))
    if not name or not await app.state.repository.get_state(concord._ROOM.format(room_id)):
        await notice(app, room_id, f'Usage: run "{p}concord rename <new name>" in the channel\'s room.')
        return
    here = await _channel_here(app, room_id, sender, "rename")
    if here is None:
        return
    community, actor, channel_id = here
    try:
        before = await update_channel(app, community, actor, channel_id, {"name": name})
    except ConcordProblem as exc:
        await notice(app, room_id, f"I didn't rename this channel: {exc}.")
        return
    attached = community.channels[channel_id].get("attached")
    await notice(app, room_id, f"Renamed #{before.get('name') or ''} to #{name} in {community.name}."
                               + (" This room's own name stays as it is." if attached else ""))


async def _room_topic(app: FastAPI, room_id: str) -> str:
    try:
        state = await app.state.homeserver.get_room_state(room_id, "m.room.topic", as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        return ""
    return _topic((state or {}).get("topic"))


async def push_topic(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    """``[concord topic``: the room's Matrix topic onto its channel."""
    p = app.state.config.appservice.command_prefix
    if not await app.state.repository.get_state(concord._ROOM.format(room_id)):
        await notice(app, room_id, f'Run "{p}concord topic" in a channel\'s room: it gives the channel the room\'s topic.')
        return
    here = await _channel_here(app, room_id, sender, "change")
    if here is None:
        return
    community, actor, channel_id = here
    topic = await _room_topic(app, room_id)
    try:
        await update_channel(app, community, actor, channel_id, {"topic": topic or None})
    except ConcordProblem as exc:
        await notice(app, room_id, f"I didn't change the channel's topic: {exc}.")
        return
    name = community.channels.get(channel_id, {}).get("name") or "channel"
    await notice(app, room_id, f"#{name} in {community.name} now has this room's topic." if topic
                 else f"#{name} in {community.name} has no topic now, like this room.")


async def maybe_sync_topic(request: Any, event: dict[str, Any]) -> bool:
    """A room made a channel with ``[concord channel`` keeps its own topic,
    and the channel follows it: a topic change by someone who manages the
    channels goes out as the channel's. Never claims the event."""
    if event.get("type") != "m.room.topic" or event.get("state_key") != "":
        return False
    app = request.app
    room_id, sender = event.get("room_id", ""), event.get("sender", "")
    mapped = await app.state.repository.get_state(concord._ROOM.format(room_id))
    if not mapped or sender == bot_mxid(app.state.config):
        return False
    cid, _, channel_id = mapped.partition("|")
    found = next(((c, m) for c, m in await managed(app, sender) if c.id == cid), None)
    if found is None or not found[0].channels.get(channel_id, {}).get("attached"):
        return False
    topic = _topic((event.get("content") or {}).get("topic"))
    try:
        await update_channel(app, found[0], found[1], channel_id, {"topic": topic or None})
    except ConcordProblem as exc:
        logger.info("Didn't carry the topic of %s to its channel: %s", room_id, exc)
    return False


async def managed(app: FastAPI, sender: str) -> list[tuple[Community, Any]]:
    """Each community where one of ``sender``'s IDs may manage channels
    (the owner, or a role with MANAGE_CHANNELS in the folded roster) and
    the bridge can write: (community, that ID)."""
    out = []
    for community in await concord.all_communities(app):
        if community.removed or not can_write(community):
            continue
        for id_ref in dict.fromkeys([*community.members]):
            member = await app.state.repository.get_id(id_ref)
            if (member is not None and member.matrix_user_id == sender
                    and community.can(member.pubkey, concord.MANAGE_CHANNELS)):
                out.append((community, member))
                break
        else:
            owner = await app.state.repository.get_id_by_pubkey(community.owner)
            if owner is not None and owner.matrix_user_id == sender and community.control_root:
                out.append((community, owner))
    return out


async def _authority(app: FastAPI, community: Community, actor: str,
                     ) -> tuple[bool, list[str] | None, list[dict[str, Any]]]:
    """How an edition by ``actor`` must be written here: marked (the
    Community is on Roles v2), and the Grant it cites (none for the owner).
    Read from the Control Plane as it stands."""
    wraps = await app.state.relay_pool.query(
        community.relays, [{"kinds": [concord.WRAP_KIND], "authors": [community.control_address()]}], timeout=12.0)
    rumors = [r for w in wraps if (r := concord.open_wrap(w, community.control())) is not None]
    everyone = locator(community.id, "concord/everyone")
    on_v2 = bool(community.control_root) or any(
        r.get("kind") == concord.EDITION_KIND and r.get("pubkey") == community.owner
        and ["vsk", "13"] in r.get("tags", []) and ["eid", everyone] in r.get("tags", []) for r in rumors)
    if actor == community.owner:
        return on_v2, None, rumors
    grant = locator(community.id, "concord/grant", bytes.fromhex(actor))
    head = None
    for rumor in rumors:
        tags = {t[0]: t[1] for t in rumor.get("tags") or [] if len(t) >= 2}
        if rumor.get("kind") != concord.EDITION_KIND or tags.get("vsk") != "3" or tags.get("eid") != grant:
            continue
        if not tags.get("ev", "").isdigit() or not community.can(rumor.get("pubkey", ""), concord.MANAGE_ROLES):
            continue
        if head is None or int(tags["ev"]) > int(head[1]["ev"]):
            head = (rumor, tags)
    if head is None:
        raise ConcordProblem("I couldn't find the role grant that lets you manage channels on its relays")
    rumor, tags = head
    digest = edition_hash(grant, int(tags["ev"]), tags.get("ep"), rumor.get("content") or "",
                          rumor["pubkey"] if tags.get("eh") == "2" else None)
    return on_v2, [grant, tags["ev"], digest], rumors


async def _by_space(app: FastAPI, communities: list[Community], room_id: str) -> Community | None:
    """The community whose Space already lists this room."""
    bot = bot_mxid(app.state.config)
    for community in communities:
        if not community.space:
            continue
        try:
            child = await app.state.homeserver.get_room_state(community.space, "m.space.child", room_id, as_user_id=bot)
        except HomeserverError:
            continue
        if child and child.get("via"):
            return community
    return None


async def _joined(app: FastAPI, room_id: str) -> list[str]:
    try:
        return list(await app.state.homeserver.get_joined_members(room_id, as_user_id=bot_mxid(app.state.config)))
    except HomeserverError:
        return []


# -- [concord invite / invites / revoke ------------------------------------------------------


def _fragment(token: bytes) -> str:
    """CORD-05 §3: format 4, the stock relay set, the token."""
    return base64.urlsafe_b64encode(bytes([4, 0x01]) + token).decode().rstrip("=")


def _bundle(community: Community, label: str, creator: str | None = None) -> dict[str, Any]:
    channels = [{"id": cid, "key": community.root, "epoch": community.epoch, "name": info.get("name") or "channel"}
                for cid, info in community.channels.items() if not info.get("deleted") and not info.get("private")]
    bundle: dict[str, Any] = {
        "community_id": community.id, "owner": community.owner, "owner_salt": community.owner_salt,
        "community_root": community.root, "root_epoch": community.epoch,
        "control_pk": community.control_pk or control_signer(community).pk, "channels": channels[:concord._MAX_CHANNELS],
        "relays": community.relays, "name": community.name, "creator_npub": creator or community.owner,
    }
    if label:
        bundle["label"] = label
    return bundle


async def _publish_registry(app: FastAPI, community: Community, secret: bytes) -> None:
    """The owner's invite Registry (CORD-05 §5): the live links' signers."""
    live = [link["signer_pk"] for link in community.links if not link.get("revoked")]
    await publish_edition(app, community, secret, "8",
                          locator(community.id, "concord/invite-links", bytes.fromhex(community.owner)), live)


async def make_link(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    label, which = _split_in(argument, [c.name for c in await owned(app, sender)])
    community = await _owned_one(app, room_id, sender, which, f"{p}concord link [label] in <community>")
    if community is None:
        return
    secret = await _owner_secret(app, community)
    if secret is None:
        await notice(app, room_id, f"I can't write for {community.name} right now.")
        return
    label = label.strip().encode()[:64].decode("utf-8", "ignore")
    token = secrets.token_bytes(16)
    signer_secret = secp256k1.generate_secret_key()
    signer_pk = secp256k1.public_key(signer_secret).hex()
    naddr = nip19.naddr_encode(nip19.Address("", signer_pk, concord.INVITE_KIND))
    url = f"{INVITE_BASE}/invite/{naddr}#{_fragment(token)}"
    created_at = int(time.time())
    entry = {"token": token.hex(), "signer_sk": signer_secret.hex(), "community_id": community.id, "url": url,
             "created_at": created_at, "epoch": community.epoch, **({"label": label} if label else {})}
    # Recorded before the bundle goes out: a link the list can't record is
    # one no other device of the owner could retire (CORD-05 §4).
    try:
        await _update_invite_list(app, community, secret, add=entry)
    except ConcordProblem as exc:
        await notice(app, room_id, f"I didn't make the link: I couldn't update your list of invite links ({exc}).")
        return
    content = nip44.encrypt(_compact(_bundle(community, label)), hkdf(token, "concord/invite-key"))
    bundle_event = nostr_event.build(signer_secret, kind=concord.INVITE_KIND, created_at=created_at,
                                     tags=[["d", ""], ["vsk", "6"]], content=content)
    results = await app.state.relay_pool.publish(bundle_event, list(dict.fromkeys([*RELAY_DICTIONARY.values(),
                                                                                    *community.relays])))
    bootstrap = {normalize_relay_url(r) for r in RELAY_DICTIONARY.values()}
    if not any(normalize_relay_url(r.relay) in bootstrap and r.accepted for r in results):
        await notice(app, room_id, "None of the relays an invite link points to took it, so the link wouldn't work. "
                                   "Try again in a while.")
        return
    community.links.append({"token": token.hex(), "signer_sk": signer_secret.hex(), "signer_pk": signer_pk, "url": url,
                            "label": label, "created_at": created_at})
    await _publish_registry(app, community, secret)
    await concord.save(app, community)
    await notice(app, room_id, f"An invite link to {community.name}{f' ({label})' if label else ''}, for Armada, "
                               f"Vector or this bridge (\"{p}join <link>\"):\n{url}\n"
                               f'Anyone with it can join. "{p}concord revoke {len(_live(community))}" retires it.',
                 html_message=f"An invite link to <strong>{html.escape(community.name)}</strong>"
                 f"{html.escape(f' ({label})' if label else '')}, for Armada, Vector or this bridge "
                 f"(<code>{html.escape(p)}join &lt;link&gt;</code>):<br><a href=\"{html.escape(url)}\">"
                 f"{html.escape(url)}</a><br>Anyone with it can join. <code>{html.escape(p)}concord revoke "
                 f"{len(_live(community))}</code> retires it.")


def _live(community: Community) -> list[dict[str, Any]]:
    return [link for link in community.links if not link.get("revoked")]


async def _owned_one(app: FastAPI, room_id: str, sender: str, which: str | None, usage: str) -> Community | None:
    p = app.state.config.appservice.command_prefix
    mine = await owned(app, sender)
    if not mine:
        await notice(app, room_id, f'You don\'t own a community made here. Make one with "{p}concord create <name>".')
        return None
    community = _pick(mine, which)
    if community is None:
        community = await _by_room(app, mine, room_id)
    if community is None:
        await notice(app, room_id, f'Which community ({", ".join(c.name for c in mine)})? Say "{usage}".')
    return community


async def _by_room(app: FastAPI, communities: list[Community], room_id: str) -> Community | None:
    """The community whose channel or Space this room is."""
    for community in communities:
        if room_id == community.space or any(i.get("room") == room_id for i in community.channels.values()):
            return community
    return None


async def list_links(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    community = await _owned_one(app, room_id, sender, argument.strip() or None, f"{p}concord links <community>")
    if community is None:
        return
    live = _live(community)
    if not live:
        await notice(app, room_id, f'{community.name} has no invite links. "{p}concord link" makes one.')
        return
    lines = [f"Invite links to {community.name}:"]
    for number, link in enumerate(live, 1):
        made = time.strftime("%Y-%m-%d", time.gmtime(link.get("created_at") or 0))
        lines.append(f"{number}. {link.get('label') or '(no label)'}, made {made}: {link['url']}")
    lines.append(f'"{p}concord revoke <number>" retires one.')
    await notice(app, room_id, "\n".join(lines))


async def revoke(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    reference, which = _split_in(argument, [c.name for c in await owned(app, sender)])
    community = await _owned_one(app, room_id, sender, which, f"{p}concord revoke <number> in <community>")
    if community is None:
        return
    live = _live(community)
    link = None
    if reference.isdigit() and 1 <= int(reference) <= len(live):
        link = live[int(reference) - 1]
    elif reference:
        link = next((x for x in live if reference in (x.get("label"), x.get("url"))), None)
    if link is None:
        await notice(app, room_id, f'Which link? "{p}concord links" lists them by number.')
        return
    secret = await _owner_secret(app, community)
    if secret is None:
        await notice(app, room_id, f"I can't write for {community.name} right now.")
        return
    grave = nostr_event.build(bytes.fromhex(link["signer_sk"]), kind=concord.INVITE_KIND, created_at=int(time.time()),
                              tags=[["d", ""], ["vsk", "9"]], content="")
    await app.state.relay_pool.publish(grave, list(dict.fromkeys([*RELAY_DICTIONARY.values(), *community.relays])))
    link["revoked"] = True
    await _publish_registry(app, community, secret)
    try:
        await _update_invite_list(app, community, secret, retire=link["token"])
    except ConcordProblem as exc:
        logger.warning("Couldn't mark the revoked link in the Invite List of %s: %s", community.owner, exc)
    await concord.save(app, community)
    await notice(app, room_id, f"Retired that link to {community.name}: it no longer lets anyone join. People who "
                               "already joined with it stay members.")


# -- direct invites (CORD-05 §6) -------------------------------------------------------------


async def member_of(app: FastAPI, sender: str) -> list[tuple[Community, Any]]:
    """Each community ``sender`` is in through one of their IDs (the one
    that's a member), that an invite can be made for: its owner's salt is
    known (made here, or joined since the bridge kept it)."""
    out = []
    for community in await concord.all_communities(app):
        if community.removed or not community.owner_salt:
            continue
        for id_ref in community.members:
            member = await app.state.repository.get_id(id_ref)
            if member is not None and member.matrix_user_id == sender:
                out.append((community, member))
                break
    return out


async def send_direct_invite(app: FastAPI, community: Community, inviter, pubkey: str) -> str:
    """Invite ``pubkey`` as ``inviter`` (a member's ID). Returns what
    happened, to tell the user; raises ``ConcordProblem`` when it didn't."""
    from bridge.messages import invite_to_room
    from bridge.nostr import nip59
    from bridge import remote as remotes

    local = await app.state.repository.get_id_by_pubkey(pubkey)
    if local is not None:
        # An ID on this server: its owner is invited to the Space in Matrix.
        # They join the channels they want from there, and the community by
        # talking in one.
        if community.space:
            await invite_to_room(app, community.space, local.matrix_user_id)
            done = community.invited.setdefault(community.space, [])
            if local.matrix_user_id not in done:
                done.append(local.matrix_user_id)
        await concord.save(app, community)
        return f"{local.matrix_user_id} is on this server, so I invited them to its Space in Matrix instead."
    if pubkey in community.banned:
        raise ConcordProblem("they're banned from it")
    secret = await app.state.signer.secret_key(inviter.pubkey)
    if secret is None:
        raise ConcordProblem(f"{inviter.name}@{app.state.config.bridge.domain} signs through a remote signer, which "
                             "Concord invites can't work with yet")
    rumor = nip59.make_rumor(inviter.pubkey, 3313, _compact(_bundle(community, "", creator=inviter.pubkey)), [],
                             int(time.time()))
    wrap = nip59.gift_wrap(nip59.seal(rumor, secret, pubkey), pubkey, extra_tags=[["k", "3313"]])
    # Their DM relays (NIP-17); without those, ours and their inbox.
    try:
        remote = await remotes.get_or_create_remote(app, pubkey)
        if not remote.dm_relays:
            remote = await remotes.refresh(app, remote, force=True)
    except HomeserverError:
        remote = await app.state.repository.get_remote_account(pubkey)
    if remote is not None and remote.dm_relays:
        relays = list(remote.dm_relays)
    else:
        relays = list(dict.fromkeys([*app.state.config.nostr.publish_relays, *remotes.inbox_relays_for(app, remote)]))
    results = await app.state.relay_pool.publish(wrap, relays)
    if not any(r.accepted for r in results):
        raise ConcordProblem("none of their relays took it (" + "; ".join(f"{r.relay}: {r.message or 'no answer'}"
                                                                          for r in results) + ")")
    logger.info("%s invited %s to %s", inviter.name, pubkey, community.id)
    return ""


def _label(remote, pubkey: str) -> str:
    from bridge.remote import short_npub

    return (remote.nip05 if remote is not None and remote.nip05 else None) or short_npub(pubkey)


async def invite(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    from bridge.commands import _resolve

    p = app.state.config.appservice.command_prefix
    memberships = await member_of(app, sender)
    if not memberships:
        await notice(app, room_id, f'You\'re not in a community I can invite to. "{p}concord create <name>" makes one.')
        return
    who, which = _split_in(argument, [c.name for c, _ in memberships])
    chosen = [m for m in memberships if which is None or m[0].name.casefold() == which.casefold()]
    if len(chosen) > 1:
        here = await _by_room(app, [c for c, _ in chosen], room_id)
        chosen = [m for m in chosen if here is not None and m[0].id == here.id]
    if len(chosen) != 1:
        names = ", ".join(c.name for c, _ in memberships)
        await notice(app, room_id, f'Which community ({names})? Say "{p}concord invite <who> in <community>".')
        return
    community, inviter = chosen[0]
    reference = await _resolve(app, room_id, who)
    if reference is None:
        return
    remote = await app.state.repository.get_remote_account(reference.pubkey)
    label = reference.nip05 or _label(remote, reference.pubkey)
    try:
        other = await send_direct_invite(app, community, inviter, reference.pubkey)
    except ConcordProblem as exc:
        await notice(app, room_id, f"Couldn't invite {label} to {community.name}: {exc}.")
        return
    await notice(app, room_id, other or f"Invited {label} to {community.name}, as {inviter.name}@"
                                        f"{app.state.config.bridge.domain}. The invite reaches them in Armada, Vector "
                                        "or another Concord app.")


async def maybe_invite_ghost(request: Any, event: dict[str, Any]) -> bool:
    """A Matrix user inviting a Nostr account's ghost to a community's Space
    or channel room: the account gets a Direct Invite, as with
    ``[concord invite``."""
    from bridge.remote import pubkey_of_ghost
    from bridge.users import is_local_user

    if event.get("type") != "m.room.member" or (event.get("content") or {}).get("membership") != "invite":
        return False
    app = request.app
    pubkey = pubkey_of_ghost(app, event.get("state_key", ""))
    sender, room_id = event.get("sender", ""), event.get("room_id", "")
    if pubkey is None or not is_local_user(app.state.config, sender):
        return False
    memberships = await member_of(app, sender)
    community = await _by_room(app, [c for c, _ in memberships], room_id)
    if community is None:
        return False
    inviter = next(m for c, m in memberships if c.id == community.id)
    remote = await app.state.repository.get_remote_account(pubkey)
    try:
        await send_direct_invite(app, community, inviter, pubkey)
    except ConcordProblem as exc:
        await notice(app, room_id, f"Couldn't send {_label(remote, pubkey)} an invite to {community.name} on Nostr: {exc}.")
        return True
    await notice(app, room_id, f"Sent {_label(remote, pubkey)} an invite to {community.name} on Nostr, as "
                               f"{inviter.name}@{app.state.config.bridge.domain}.")
    return True


# -- the command -----------------------------------------------------------------------------


async def handle_command(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    verb, _, rest = argument.strip().partition(" ")
    verb = verb.lower()
    if verb == "create":
        await create(app, room_id, sender, rest)
    elif verb == "channel":
        await add_channel(app, room_id, sender, rest)
    elif verb == "link":
        await make_link(app, room_id, sender, rest)
    elif verb == "links":
        await list_links(app, room_id, sender, rest)
    elif verb == "invite":
        await invite(app, room_id, sender, rest)
    elif verb == "revoke":
        await revoke(app, room_id, sender, rest)
    elif verb == "rename":
        await rename_channel(app, room_id, sender, rest)
    elif verb == "topic":
        await push_topic(app, room_id, sender, rest)
    else:
        mine = await owned(app, sender)
        have = f" You own: {', '.join(c.name for c in mine)}." if mine else ""
        await notice(app, room_id,
                     f'Concord communities you own: "{p}concord create <name>" makes one; "{p}concord channel <name>", '
                     f'run in a room, makes that room a channel of it ("{p}concord rename <name>" in a channel\'s '
                     f'room renames it); "{p}concord link [label]" makes an invite '
                     f'link, "{p}concord links" lists them and "{p}concord revoke <number>" retires one; '
                     f'"{p}concord invite <npub, name@domain or a mention>" invites someone to a community you\'re '
                     f"in (so does inviting their Matrix ghost to its Space or a channel room).{have}")
