"""Concord communities (Armada, Vector, ...): end-to-end encrypted, Discord-
style communities over Nostr, as a Matrix Space with a room per channel.

Spec: https://github.com/concord-protocol/concord (CORD-01..08). Phase 1:

- ``[join <invite link>`` (CORD-05): the link's fragment unlocks the invite
  bundle on relays, which holds the community's key; the bridge checks the
  owner commitment, reads the Control Plane for the channels (CORD-02/03),
  makes the Space and rooms, announces the Join on the Guestbook, and brings
  in each channel's recent messages.
- Messages, threaded replies, reactions, edits and deletes come in live from
  the community's relays, decrypted and shown by the authors' ghosts; what
  members post in a channel room goes out the same way, sealed and signed
  with their identity's key and wrapped with the channel's.
- Not yet: rekeys and refoundings (CORD-06: a community that re-founds stops
  being readable here until re-joined), roles, moderation, voice (CORD-07),
  disappearing messages (CORD-08).

Messages are decrypted into Matrix, where they are stored as plain text like
any bridged room; ``[join`` says so. The community's keys are kept in the
bridge's database, encrypted with the key vault like Nostr keys.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Request

from bridge.homeserver_client import HomeserverError
from bridge.messages import notice, room_link_html
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19, nip44, secp256k1
from bridge.users import bot_mxid, is_bridge_user, is_local_user

logger = logging.getLogger(__name__)

INVITE_KIND = 33301
WRAP_KIND = 1059
SEAL_ENCRYPTED = 20013
SEAL_PLAINTEXT = 20014
EDITION_KIND = 3308
JOIN_KIND = 3306
EDIT_KIND = 3302
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_ZERO = b"\0" * 32
# CORD-05 §3: the relay dictionary (format version 4).
RELAY_DICTIONARY = {1: "wss://jskitty.com/nostr", 2: "wss://asia.vectorapp.io/nostr",
                    3: "wss://relay.ditto.pub", 4: "wss://relay.dreamith.to"}
_MAX_CHANNELS = 256
# CORD-04 §3: permission bits.
MANAGE_ROLES, MANAGE_CHANNELS, MANAGE_METADATA, KICK, BAN, MANAGE_MESSAGES = 1, 2, 4, 8, 16, 32
_ALL = (1 << 63) - 1
_ROLELESS = 1 << 32
_MAX_RELAYS = 5
_BACKFILL = 50
_STATE = "concord:c:{}"
_ROOM = "concord:room:{}"
_MSG = "concord:msg:{}"
_MX = "concord:mx:{}"
WARNING = (
    "⚠️ Concord communities are end-to-end encrypted on Nostr, but this bridge can't encrypt Matrix rooms: its "
    "messages are stored readable on this homeserver, like any bridged room."
)


class ConcordProblem(Exception):
    """Why a community can't be joined or used; safe to show."""


# -- derivations (CORD-02 Appendix A) ------------------------------------------------


def hkdf(secret: bytes, label: str, ident: bytes = _ZERO, epoch: int | None = None, extra: bytes = b"") -> bytes:
    info = label.encode() + b"\0" + ident + (epoch.to_bytes(8, "big") if epoch is not None else b"") + extra
    return nip44._hkdf_expand(nip44._hkdf_extract(b"", secret), info, 32)


@dataclass(frozen=True)
class GroupKey:
    sk: bytes
    pk: str
    conv: bytes


def group_key(label: str, secret: bytes, ident: bytes, epoch: int | None) -> GroupKey:
    seed = hkdf(secret, label, ident, epoch)
    counter = 0
    while not 1 <= int.from_bytes(seed, "big") < _N:
        seed = hkdf(secret, label, ident, epoch, bytes([counter]))
        counter += 1
    pk = secp256k1.public_key(seed).hex()
    return GroupKey(seed, pk, nip44.get_conversation_key(seed, pk))


def community_id(owner: str, salt: str) -> str:
    return hashlib.sha256(b"concord/community" + bytes.fromhex(owner) + bytes.fromhex(salt)).hexdigest()


# -- invites (CORD-05) ---------------------------------------------------------------


@dataclass(frozen=True)
class Invite:
    signer: str
    relays: tuple[str, ...]
    token: bytes


def parse_invite(text: str) -> Invite:
    """``…/invite/<naddr>#<fragment>``."""
    text = text.strip().strip("<>")
    path, _, fragment = text.partition("#")
    naddr = next((p for p in reversed(unquote(path).split("/")) if p.startswith("naddr1")), "")
    if not naddr or not fragment:
        raise ConcordProblem("That doesn't look like a community invite link (…/invite/naddr1…#…).")
    try:
        address = nip19.decode(naddr)
        raw = base64.urlsafe_b64decode(fragment + "=" * (-len(fragment) % 4))
    except (nip19.Nip19Error, ValueError) as exc:
        raise ConcordProblem("That invite link is damaged.") from exc
    if not isinstance(address, nip19.Address) or address.kind != INVITE_KIND:
        raise ConcordProblem("That isn't a community invite.")
    if len(raw) < 18 or raw[0] < 4:
        raise ConcordProblem("That invite link is from an older version of Concord this bridge can't read.")
    flags, pos, relays = raw[1], 2, []
    if flags & 1:
        relays = list(RELAY_DICTIONARY.values())
    else:
        count = raw[pos]
        pos += 1
        for _ in range(min(count, 3)):
            entry = raw[pos]
            pos += 1
            if 1 <= entry <= 254:
                if entry in RELAY_DICTIONARY:
                    relays.append(RELAY_DICTIONARY[entry])
            else:
                length = raw[pos]
                literal = raw[pos + 1:pos + 1 + length].decode("utf-8", "replace")
                pos += 1 + length
                relays.append(("wss://" + literal) if entry == 0 else literal)
    token = raw[pos:pos + 16]
    if len(token) != 16:
        raise ConcordProblem("That invite link is damaged.")
    return Invite(address.pubkey, tuple(r for r in relays if r.startswith(("wss://", "ws://"))), token)


async def fetch_bundle(app: FastAPI, invite: Invite) -> dict[str, Any]:
    relays = [*invite.relays, *RELAY_DICTIONARY.values()]
    found = await app.state.relay_pool.query(relays, [{"kinds": [INVITE_KIND], "authors": [invite.signer], "#d": [""]}],
                                             timeout=10.0)
    newest = None
    for raw in found:
        try:
            nostr_event.verify(raw)
        except nostr_event.InvalidEvent:
            continue
        if raw.get("pubkey") == invite.signer and (newest is None or raw["created_at"] > newest["created_at"]):
            newest = raw
    if newest is None:
        raise ConcordProblem("I couldn't find that invite on its relays.")
    if ["vsk", "9"] in [list(t) for t in newest.get("tags") or []]:
        raise ConcordProblem("That invite link has been revoked.")
    try:
        bundle = json.loads(nip44.decrypt(newest["content"], hkdf(invite.token, "concord/invite-key")))
    except (ValueError, KeyError) as exc:
        raise ConcordProblem("I couldn't open that invite (is the link complete?).") from exc
    for key in ("community_id", "owner", "owner_salt", "community_root"):
        if not isinstance(bundle.get(key), str) or len(bundle[key]) != 64:
            raise ConcordProblem("That invite is malformed.")
    if community_id(bundle["owner"], bundle["owner_salt"]) != bundle["community_id"]:
        raise ConcordProblem("That invite doesn't prove who owns the community, so it can't be trusted.")
    if len(bundle.get("channels") or []) > _MAX_CHANNELS:
        raise ConcordProblem("That invite is malformed (too many channels).")
    expires = bundle.get("expires_at")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        raise ConcordProblem("That invite has expired.")
    return bundle


# -- a community's state ---------------------------------------------------------------


@dataclass
class Community:
    id: str
    owner: str
    root: str
    epoch: int
    control_pk: str | None
    relays: list[str]
    name: str
    description: str
    picture: str
    # channel_id -> {"name", "private", "key" (private only), "epoch", "room"}
    channels: dict[str, dict[str, Any]]
    space: str | None
    members: list[int]
    # The folded roster (CORD-04): role_id -> {"position", "permissions"};
    # member pubkey -> role ids; banned pubkeys.
    roles: dict[str, dict[str, int]] = field(default_factory=dict)
    grants: dict[str, list[str]] = field(default_factory=dict)
    banned: list[str] = field(default_factory=list)
    # Disappearing messages (CORD-08): seconds, 0 = off.
    timer: int = 0
    # Who's in the community (CORD-02 §5, the Guestbook fold): pubkeys.
    present: list[str] = field(default_factory=list)
    # Rotations seen but not yet complete (CORD-06): "rotator:epoch:commit" -> chunk numbers.
    pending: dict[str, list[int]] = field(default_factory=dict)
    removed: bool = False

    # -- the roster (CORD-04 §3) --
    def rank(self, pubkey: str) -> int:
        if pubkey == self.owner:
            return 0
        positions = [self.roles[r]["position"] for r in self.grants.get(pubkey, []) if r in self.roles]
        return min(positions) if positions else _ROLELESS

    def permissions(self, pubkey: str) -> int:
        if pubkey == self.owner:
            return _ALL
        bits = 0
        for role_id in self.grants.get(pubkey, []):
            bits |= self.roles.get(role_id, {}).get("permissions", 0)
        return bits

    def can(self, pubkey: str, permission: int, *, over: str | None = None) -> bool:
        """Holds ``permission`` (and strictly outranks ``over``)."""
        if pubkey in self.banned:
            return False
        if not self.permissions(pubkey) & permission and pubkey != self.owner:
            return False
        return over is None or self.rank(pubkey) < self.rank(over)

    def control(self) -> GroupKey:
        """The Control Plane's read key (and, before the split, its address)."""
        return group_key("concord/control", bytes.fromhex(self.root), bytes.fromhex(self.id), self.epoch)

    def control_address(self) -> str:
        return self.control_pk or self.control().pk

    def channel_key(self, channel_id: str) -> GroupKey:
        info = self.channels[channel_id]
        if info.get("private") and info.get("key"):
            return group_key("concord/channel", bytes.fromhex(info["key"]), bytes.fromhex(channel_id), int(info.get("epoch") or 0))
        return group_key("concord/channel", bytes.fromhex(self.root), bytes.fromhex(channel_id), self.epoch)

    def guestbook(self) -> GroupKey:
        return group_key("concord/guestbook", bytes.fromhex(self.root), bytes.fromhex(self.id), self.epoch)

    def readable(self) -> list[str]:
        return [c for c, info in self.channels.items()
                if not info.get("deleted") and (not info.get("private") or info.get("key"))]


async def load(app: FastAPI, cid: str) -> Community | None:
    stored = await app.state.repository.get_state(_STATE.format(cid))
    if not stored:
        return None
    data = json.loads(app.state.vault.decrypt(stored, f"concord:{cid}"))
    return Community(**data)


async def save(app: FastAPI, community: Community) -> None:
    data = json.dumps(community.__dict__, separators=(",", ":")).encode()
    await app.state.repository.set_state(_STATE.format(community.id), app.state.vault.encrypt(data, f"concord:{community.id}"))
    _index(app).clear()


async def all_communities(app: FastAPI) -> list[Community]:
    out = []
    for key, _value in await app.state.repository.list_state(_STATE.format("")):
        community = await load(app, key.rsplit(":", 1)[1])
        if community is not None:
            out.append(community)
    return out


def _index(app: FastAPI) -> dict[str, tuple[str, str | None, GroupKey]]:
    """Stream address -> (community id, channel id or None for Control, key)."""
    return app.state.__dict__.setdefault("concord_index", {})


async def addresses(app: FastAPI) -> dict[str, tuple[str, str | None, GroupKey]]:
    index = _index(app)
    if not index:
        for community in await all_communities(app):
            control = community.control()
            index[community.control_address()] = (community.id, None, control)
            for channel_id in community.readable():
                key = community.channel_key(channel_id)
                index[key.pk] = (community.id, channel_id, key)
    return index


# -- the envelope (CORD-01) ------------------------------------------------------------


def open_wrap(wrap: dict[str, Any], key: GroupKey) -> dict[str, Any] | None:
    """The rumor inside a stream event, its author's seal checked; or None."""
    try:
        nostr_event.verify(wrap)
        seal = json.loads(nip44.decrypt(wrap["content"], key.conv))
        nostr_event.verify(seal)
        if seal.get("kind") == SEAL_ENCRYPTED:
            rumor = json.loads(nip44.decrypt(seal["content"], key.conv))
        elif seal.get("kind") == SEAL_PLAINTEXT:
            rumor = json.loads(seal["content"])
        else:
            return None
    except (ValueError, KeyError, TypeError, nostr_event.InvalidEvent):
        return None
    if not isinstance(rumor, dict) or rumor.get("pubkey") != seal.get("pubkey"):
        return None
    rumor["id"] = nostr_event.compute_id(rumor["pubkey"], int(rumor.get("created_at", 0)), int(rumor.get("kind", 0)),
                                         rumor.get("tags") or [], rumor.get("content") or "")
    return rumor


def make_wrap(key: GroupKey, author_secret: bytes, kind: int, content: str, tags: list[list[str]]) -> tuple[nostr_event.Event, str]:
    """(the kind 1059 stream event, the rumor's id) for a chat rumor."""
    now = time.time()
    created_at = int(now)
    tags = [*tags, ["ms", str(int(now * 1000) % 1000)]]
    author = secp256k1.public_key(author_secret).hex()
    rumor_id = nostr_event.compute_id(author, created_at, kind, tags, content)
    rumor = {"id": rumor_id, "pubkey": author, "created_at": created_at, "kind": kind, "tags": tags, "content": content}
    seal = nostr_event.build(author_secret, kind=SEAL_ENCRYPTED, created_at=created_at, tags=[],
                             content=nip44.encrypt(json.dumps(rumor, ensure_ascii=False, separators=(",", ":")), key.conv))
    ephemeral = secp256k1.public_key(secp256k1.generate_secret_key()).hex()
    wrap = nostr_event.build(key.sk, kind=WRAP_KIND, created_at=created_at, tags=[["p", ephemeral]],
                             content=nip44.encrypt(json.dumps(seal.to_dict(), ensure_ascii=False, separators=(",", ":")), key.conv))
    return wrap, rumor_id


# -- the Control Plane (CORD-02 §5, CORD-04) ----------------------------------------------------


def _editions(rumors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for rumor in rumors:
        if rumor.get("kind") != EDITION_KIND:
            continue
        tags = {t[0]: t[1] for t in rumor.get("tags") or [] if len(t) >= 2}
        version = tags.get("ev", "")
        if not tags.get("vsk") or not tags.get("eid") or not version.isdigit():
            continue
        try:
            content = json.loads(rumor.get("content") or "null")
        except ValueError:
            continue
        out.append({"vsk": tags["vsk"], "eid": tags["eid"], "version": int(version), "content": content,
                    "actor": rumor.get("pubkey", ""), "id": rumor.get("id", "")})
    return out


def _heads(community: Community, editions: list[dict[str, Any]], vsk: str, allowed) -> dict[str, Any]:
    """The current head of each entity of one type: the highest version an
    authorized actor published; at the same version, the higher-ranked actor,
    then the lower rumor id (CORD-04 §1). (Chain links aren't checked yet.)"""
    best: dict[str, dict[str, Any]] = {}
    for edition in editions:
        if edition["vsk"] != vsk or not allowed(edition):
            continue
        current = best.get(edition["eid"])
        key = (-edition["version"], community.rank(edition["actor"]), edition["id"])
        if current is None or key < (-current["version"], community.rank(current["actor"]), current["id"]):
            best[edition["eid"]] = edition
    return best


def fold_control(community: Community, rumors: list[dict[str, Any]]) -> None:
    """The Control Plane folded (CORD-04): the roster first, outward from the
    owner until it settles, then metadata and channels by permission."""
    editions = _editions(rumors)
    community.roles, community.grants, community.banned = {}, {}, []
    for _round in range(8):
        before = (json.dumps(community.roles, sort_keys=True), json.dumps(community.grants, sort_keys=True), list(community.banned))

        def role_ok(e: dict[str, Any]) -> bool:
            position = (e["content"] or {}).get("position") if isinstance(e["content"], dict) else None
            return (isinstance(position, int) and position > 0 and community.can(e["actor"], MANAGE_ROLES)
                    and community.rank(e["actor"]) < position)

        roles = {}
        for eid, head in _heads(community, editions, "1", role_ok).items():
            content = head["content"]
            if (content.get("scope") or {"kind": "server"}).get("kind") != "server":
                continue  # channel-scoped roles: a later phase
            try:
                bits = int(str(content.get("permissions") or "0"))
            except ValueError:
                bits = 0
            roles[eid] = {"position": int(content["position"]), "permissions": bits}

        def grant_ok(e: dict[str, Any]) -> bool:
            content = e["content"] if isinstance(e["content"], dict) else {}
            given = [r for r in content.get("role_ids") or [] if isinstance(r, str)]
            return community.can(e["actor"], MANAGE_ROLES) and all(
                community.rank(e["actor"]) < roles.get(r, {}).get("position", 0) for r in given)

        grants = {}
        for _eid, head in _heads(community, editions, "3", grant_ok).items():
            content = head["content"]
            member = content.get("member")
            if isinstance(member, str) and len(member) == 64 and member != community.owner:
                grants[member] = [r for r in content.get("role_ids") or [] if r in roles][:64]
        banned = _heads(community, editions, "4", lambda e: community.can(e["actor"], BAN))
        community.roles, community.grants = roles, grants
        heads = list(banned.values())
        community.banned = sorted({p for p in (heads[0]["content"] if heads and isinstance(heads[0]["content"], list) else [])
                                   if isinstance(p, str) and len(p) == 64 and p != community.owner})
        if before == (json.dumps(community.roles, sort_keys=True), json.dumps(community.grants, sort_keys=True), community.banned):
            break
    meta = _heads(community, editions, "0", lambda e: e["eid"] == community.id and community.can(e["actor"], MANAGE_METADATA))
    for head in meta.values():
        content = head["content"] if isinstance(head["content"], dict) else {}
        community.name = str(content.get("name") or community.name)[:64]
        community.description = str(content.get("description") or "")[:2000]
        picture = content.get("picture")
        community.picture = picture if isinstance(picture, str) and picture.startswith("https://") else community.picture
        relays = [r for r in content.get("relays") or [] if isinstance(r, str) and r.startswith(("wss://", "ws://"))]
        if relays:
            community.relays = relays[:_MAX_RELAYS]
        timer = content.get("message_expiration")
        community.timer = timer if isinstance(timer, int) and timer > 0 else 0
    channels = _heads(community, editions, "2", lambda e: len(e["eid"]) == 64 and community.can(e["actor"], MANAGE_CHANNELS))
    for eid, head in channels.items():
        if len(community.channels) >= _MAX_CHANNELS and eid not in community.channels:
            continue
        content = head["content"] if isinstance(head["content"], dict) else {}
        channel = community.channels.setdefault(eid, {"epoch": 0})
        channel["name"] = str(content.get("name") or "channel")[:64]
        channel["private"] = bool(content.get("private"))
        channel["deleted"] = bool(content.get("deleted"))


async def read_control(app: FastAPI, community: Community) -> None:
    control = community.control()
    wraps = await app.state.relay_pool.query(
        community.relays, [{"kinds": [WRAP_KIND], "authors": [community.control_address()]}], timeout=12.0
    )
    rumors = [r for w in wraps if (r := open_wrap(w, control)) is not None]
    fold_control(community, rumors)


# -- Matrix rooms -------------------------------------------------------------------------


async def _ensure_rooms(app: FastAPI, community: Community, users: list[str]) -> None:
    """The community's Space and a room for every channel the bridge can read."""
    homeserver = app.state.homeserver
    config = app.state.config
    bot = bot_mxid(config)
    server = config.homeserver.server_name
    topic = (community.description + "\n\n" if community.description else "") + (
        "A Concord community, bridged from Nostr. " + WARNING.lstrip("⚠️ "))
    if community.space is None:
        community.space = await homeserver.create_room(
            as_user_id=bot, name=community.name, topic=topic, invite=users, room_type="m.space",
            preset="private_chat", history_visibility="shared",
        )
        await _set_avatar(app, community.space, community.picture)
    for channel_id in community.readable():
        info = community.channels[channel_id]
        name = f"#{info.get('name') or 'channel'}"
        if not info.get("room"):
            info["room"] = await homeserver.create_room(
                as_user_id=bot, name=name, topic=f"{name} in {community.name}. {WARNING.lstrip('⚠️ ')}", invite=users,
                preset="private_chat", history_visibility="shared",
                initial_state=[{"type": "m.space.parent", "state_key": community.space, "content": {"via": [server], "canonical": True}}],
            )
            await homeserver.send_state_event(community.space, "m.space.child", {"via": [server], "suggested": True},
                                              as_user_id=bot, state_key=info["room"])
            await app.state.repository.set_state(_ROOM.format(info["room"]), f"{community.id}|{channel_id}")
            await _set_avatar(app, info["room"], community.picture)
        else:
            current = await homeserver.get_room_state(info["room"], "m.room.name", as_user_id=bot)
            if (current or {}).get("name") != name:
                await homeserver.send_state_event(info["room"], "m.room.name", {"name": name}, as_user_id=bot)
    from bridge.messages import invite_to_room
    from bridge.spaces import add_room_to_space

    for user in users:
        await invite_to_room(app, community.space, user)
        await add_room_to_space(app, user, community.space)
        for channel_id in community.readable():
            await invite_to_room(app, community.channels[channel_id]["room"], user)


async def _set_avatar(app: FastAPI, room_id: str, url: str) -> None:
    if not url:
        return
    from bridge.render import Attachment, copy_attachment

    try:
        copied = await copy_attachment(app, Attachment(url, "image/png"), as_user_id=bot_mxid(app.state.config))
        if copied:
            await app.state.homeserver.send_state_event(room_id, "m.room.avatar", {"url": copied["url"]},
                                                        as_user_id=bot_mxid(app.state.config))
    except HomeserverError:
        logger.info("Could not set the avatar of %s", room_id, exc_info=True)


# -- joining --------------------------------------------------------------------------------


async def handle_join(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    from bridge import ids

    p = app.state.config.appservice.command_prefix
    if not argument.strip():
        await notice(app, room_id, f'Usage: "{p}join <community invite link>" (an Armada, Vector or other Concord invite).')
        return
    member = await ids.active_id(app, sender)
    if member is None:
        await notice(app, room_id, f'You need a Nostr identity first: "{p}create profile".')
        return
    if member.key_origin == "bunker":
        await notice(app, room_id, f"{member.name}@{app.state.config.bridge.domain} signs through a remote signer, which "
                                   "Concord communities can't work with yet. Switch to another identity to join.")
        return
    try:
        invite = parse_invite(argument.split()[0])
        bundle = await fetch_bundle(app, invite)
    except ConcordProblem as exc:
        await notice(app, room_id, str(exc))
        return
    await notice(app, room_id, f"Joining {bundle.get('name') or 'the community'}… {WARNING}")
    community = await load(app, bundle["community_id"])
    if community is None:
        community = Community(
            id=bundle["community_id"], owner=bundle["owner"], root=bundle["community_root"],
            epoch=int(bundle.get("root_epoch") or 0), control_pk=bundle.get("control_pk") or None,
            relays=[r for r in bundle.get("relays") or invite.relays if isinstance(r, str)][:_MAX_RELAYS] or list(invite.relays),
            name=str(bundle.get("name") or "Community")[:64], description="", picture="",
            channels={}, space=None, members=[],
        )
    for channel in bundle.get("channels") or []:
        if isinstance(channel, dict) and len(str(channel.get("id", ""))) == 64:
            community.channels[channel["id"]] = {"name": str(channel.get("name") or "channel")[:64], "private": True,
                                                 "key": channel.get("key"), "epoch": int(channel.get("epoch") or 0)}
    await read_control(app, community)
    if not community.readable():
        await notice(app, room_id, "I opened the invite, but couldn't read any of the community's channels.")
        return
    new_member = member.id not in community.members
    if new_member:
        community.members.append(member.id)
    users = sorted({m.matrix_user_id for i in community.members if (m := await app.state.repository.get_id(i))})
    try:
        await _ensure_rooms(app, community, users)
    except HomeserverError:
        logger.warning("Could not make the rooms of %s", community.id, exc_info=True)
        await notice(app, room_id, "I couldn't make the community's rooms just now; try again in a minute.")
        return
    await save(app, community)
    if new_member:
        await _announce_join(app, community, member, bundle)
    for channel_id in community.readable():
        await backfill_channel(app, community, channel_id)
    app.state.feeds.changed()
    names = ", ".join(f"#{community.channels[c].get('name')}" for c in community.readable())
    await notice(app, room_id, f"Joined {community.name} as {member.name}@{app.state.config.bridge.domain}: {names}. "
                               f"Its space: {community.space}.",
                 html_message=f"Joined <strong>{html.escape(community.name)}</strong> as "
                 f"{html.escape(member.name)}@{html.escape(app.state.config.bridge.domain)}: {html.escape(names)}. "
                 f"Its space: {room_link_html(community.space or '', community.name)}.")


async def _announce_join(app: FastAPI, community: Community, member, bundle: dict[str, Any]) -> None:
    secret = await app.state.signer.secret_key(member.pubkey)
    if secret is None:
        return
    tags = []
    if isinstance(bundle.get("creator_npub"), str) and len(bundle["creator_npub"]) == 64:
        tags.append(["invite", bundle["creator_npub"], str(bundle.get("label") or "")])
    wrap, _rumor = make_wrap(community.guestbook(), secret, JOIN_KIND, "join", tags)
    await app.state.relay_pool.publish(wrap, community.relays)


# -- incoming --------------------------------------------------------------------------------


async def backfill_channel(app: FastAPI, community: Community, channel_id: str) -> int:
    key = community.channel_key(channel_id)
    wraps = await app.state.relay_pool.query(community.relays, [{"kinds": [WRAP_KIND], "authors": [key.pk], "limit": _BACKFILL}],
                                             timeout=12.0)
    brought = 0
    for wrap in sorted(wraps, key=lambda w: w.get("created_at", 0)):
        if await _receive_chat(app, community, channel_id, key, wrap):
            brought += 1
    return brought


async def receive(app: FastAPI, wrap: dict[str, Any]) -> None:
    """A stream event from a community's relays."""
    if wrap.get("kind") != WRAP_KIND or not app.state.seen.add("concord:" + str(wrap.get("id"))):
        return
    entry = (await addresses(app)).get(wrap.get("pubkey", ""))
    if entry is None:
        return
    cid, channel_id, key = entry
    community = await load(app, cid)
    if community is None:
        return
    if channel_id is None:
        rumor = open_wrap(wrap, key)
        if rumor is not None:
            await _refresh(app, community)
        return
    await _receive_chat(app, community, channel_id, key, wrap)


async def _refresh(app: FastAPI, community: Community) -> None:
    """The Control Plane changed: channels and metadata again."""
    await read_control(app, community)
    users = sorted({m.matrix_user_id for i in community.members if (m := await app.state.repository.get_id(i))})
    try:
        await _ensure_rooms(app, community, users)
    except HomeserverError:
        logger.info("Could not update the rooms of %s", community.id, exc_info=True)
    await save(app, community)
    app.state.feeds.changed()


async def _receive_chat(app: FastAPI, community: Community, channel_id: str, key: GroupKey, wrap: dict[str, Any]) -> bool:
    from bridge import remote as remotes
    from bridge.inbound import _per_message_profile

    rumor = open_wrap(wrap, key)
    if rumor is None:
        return False
    tags = rumor.get("tags") or []
    first = {t[0]: t for t in reversed(tags) if isinstance(t, list) and len(t) >= 2}
    if (first.get("channel") or [None, None])[1] != channel_id:
        return False
    repository = app.state.repository
    if await repository.get_state(_MSG.format(rumor["id"])):
        return False  # already here (or sent from here)
    room = community.channels[channel_id].get("room")
    if not room:
        return False
    author = rumor["pubkey"]
    local = await repository.get_id_by_pubkey(author)
    if local is not None:
        sender, extra = bot_mxid(app.state.config), {"com.beeper.per_message_profile": await _per_message_profile(app, local)}
    else:
        ghost = await remotes.get_or_create_remote(app, author)
        await remotes.ensure_ghost_in_room(app, ghost.ghost_user_id, room)
        sender, extra = ghost.ghost_user_id, {}
    kind = rumor.get("kind")
    ts = int(rumor.get("created_at", 0)) * 1000 + int((first.get("ms") or [0, "0"])[1] or 0) % 1000
    homeserver = app.state.homeserver
    try:
        if kind in (9, 1111):
            content = await _message(app, rumor, sender)
            content.update(extra)
            if kind == 1111:
                root = await _lookup(app, (first.get("E") or [None, None])[1])
                parent = await _lookup(app, (first.get("e") or [None, None])[1])
                if root or parent:
                    content["m.relates_to"] = {"rel_type": "m.thread", "event_id": (root or parent)[1],
                                               "is_falling_back": False,
                                               "m.in_reply_to": {"event_id": (parent or root)[1]}}
            event_id = await homeserver.send_message_event(room, content, as_user_id=sender, ts=ts)
        elif kind == 7:
            target = await _lookup(app, (first.get("e") or [None, None])[1])
            if target is None:
                return False
            reaction = (rumor.get("content") or "+").strip()
            reaction = "👍" if reaction in ("", "+") else reaction[:64]
            event_id = await homeserver.send_message_event(
                room, {"m.relates_to": {"rel_type": "m.annotation", "event_id": target[1], "key": reaction}},
                as_user_id=sender, ts=ts, event_type="m.reaction")
        elif kind == 5:
            for tag in tags:
                if len(tag) >= 2 and tag[0] == "e":
                    target = await _lookup(app, tag[1])
                    if target is not None and target[2] == author:
                        await homeserver.redact_event(target[0], target[1], as_user_id=sender, reason="Deleted on Nostr")
            await repository.set_state(_MSG.format(rumor["id"]), json.dumps([room, "", author]))
            return True
        elif kind == EDIT_KIND:
            target = await _lookup(app, (first.get("e") or [None, None])[1])
            if target is None or target[2] != author:
                return False
            new = await _message(app, rumor, sender)
            event_id = await homeserver.send_message_event(room, {
                "msgtype": "m.text", "body": "* " + new["body"], "m.new_content": new,
                "m.relates_to": {"rel_type": "m.replace", "event_id": target[1]}, **extra}, as_user_id=sender, ts=ts)
        else:
            return False
    except HomeserverError:
        logger.info("Could not show a message from %s", community.id, exc_info=True)
        return False
    await repository.set_state(_MSG.format(rumor["id"]), json.dumps([room, event_id, author]))
    await repository.set_state(_MX.format(event_id), json.dumps([rumor["id"], author, kind]))
    return True


async def _lookup(app: FastAPI, rumor_id: str | None) -> tuple[str, str, str] | None:
    """(room, Matrix event, author) of a message, by its rumor id."""
    if not rumor_id:
        return None
    stored = await app.state.repository.get_state(_MSG.format(rumor_id))
    if not stored:
        return None
    room, event_id, author = json.loads(stored)
    return (room, event_id, author) if event_id else None


def _imeta(tag: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in tag[1:]:
        if isinstance(item, str) and " " in item:
            key, _, value = item.partition(" ")
            fields.setdefault(key, value.strip())
    return fields


async def _encrypted_attachment(app: FastAPI, fields: dict[str, str], caption: str, sender: str) -> dict[str, Any]:
    """An attachment encrypted for the community (AES-GCM, key and nonce in
    its imeta, as Armada sends them): fetched, decrypted, put in Matrix."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        fetched = await app.state.fetcher.get(fields["url"], max_bytes=app.state.config.nostr.max_media_mb * 1024 * 1024)
        data = fetched.content
        if fields.get("x") and hashlib.sha256(data).hexdigest() != fields["x"].lower():
            raise ValueError("hash mismatch")
        data = AESGCM(bytes.fromhex(fields["decryption-key"])).decrypt(bytes.fromhex(fields["decryption-nonce"]), data, None)
        mime = fields.get("m") or "application/octet-stream"
        mxc = await app.state.homeserver.upload_media(data, mime, "attachment", as_user_id=sender)
    except Exception as exc:
        logger.info("Could not fetch a community attachment: %s", exc)
        return {"msgtype": "m.text", "body": (caption + "\n" if caption else "") + "(an attachment that couldn't be fetched)"}
    msgtype = {"image": "m.image", "video": "m.video", "audio": "m.audio"}.get(mime.split("/")[0], "m.file")
    filename = "attachment." + (mime.split("/")[-1] or "bin")
    content: dict[str, Any] = {"msgtype": msgtype, "body": caption or filename, "filename": filename, "url": mxc,
                               "info": {"mimetype": mime, "size": len(data)}}
    if fields.get("dim") and "x" in fields["dim"]:
        width, _, height = fields["dim"].partition("x")
        if width.isdigit() and height.isdigit():
            content["info"].update({"w": int(width), "h": int(height)})
    return content


async def _message(app: FastAPI, rumor: dict[str, Any], sender: str) -> dict[str, Any]:
    from bridge.render import message_content

    for tag in rumor.get("tags") or []:
        if isinstance(tag, list) and tag and tag[0] == "imeta":
            fields = _imeta(tag)
            if fields.get("encryption-algorithm") == "aes-gcm" and fields.get("url") and fields.get("decryption-key"):
                caption = (rumor.get("content") or "").replace(fields["url"], "").strip()
                return await _encrypted_attachment(app, fields, caption, sender)

    raw = {"id": rumor["id"], "pubkey": rumor["pubkey"], "kind": 1, "created_at": rumor.get("created_at", 0),
           "content": rumor.get("content") or "", "tags": [t for t in rumor.get("tags") or [] if t and t[0] in ("imeta", "emoji", "p")]}
    content = await message_content(app, raw, as_user_id=sender)
    content.pop("external_url", None)
    return content


# -- outgoing ---------------------------------------------------------------------------------------


async def maybe_publish(request: Request, event: dict[str, Any]) -> bool:
    """A message, reaction or redaction in a community's room, from a member here."""
    app = request.app
    config = app.state.config
    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    kind_type = event.get("type")
    if kind_type not in ("m.room.message", "m.reaction", "m.room.redaction"):
        return False
    mapped = await app.state.repository.get_state(_ROOM.format(room_id))
    if not mapped:
        return False
    if is_bridge_user(config, sender) or not is_local_user(config, sender):
        return True
    from bridge.commands import message_for_a_bot

    content = event.get("content") or {}
    if kind_type == "m.room.message" and message_for_a_bot(content, config):
        return False
    cid, channel_id = mapped.split("|", 1)
    community = await load(app, cid)
    if community is None or channel_id not in community.channels:
        return True
    member = None
    for id_ref in community.members:
        candidate = await app.state.repository.get_id(id_ref)
        if candidate is not None and candidate.matrix_user_id == sender:
            member = candidate
            break
    if member is None:
        await notice(app, room_id, f'Join the community first: "{config.appservice.command_prefix}join <invite link>".')
        return True
    secret = await app.state.signer.secret_key(member.pubkey)
    if secret is None:
        return True
    info = community.channels[channel_id]
    epoch = int(info.get("epoch") or 0) if info.get("private") else community.epoch
    base_tags = [["channel", channel_id], ["epoch", str(epoch)]]
    built = await _outgoing(app, event, content, base_tags)
    if built is None:
        return True
    kind, text, tags = built
    key = community.channel_key(channel_id)
    wrap, rumor_id = make_wrap(key, secret, kind, text, tags)
    # Recorded before it goes out: the relays echo it straight back, and an
    # unrecorded message by a member here would be mirrored in a second time.
    app.state.seen.add("concord:" + wrap.id)
    matrix_event = event.get("event_id", "")
    await app.state.repository.set_state(_MSG.format(rumor_id), json.dumps([room_id, matrix_event if kind != 5 else "", member.pubkey]))
    if kind != 5:
        await app.state.repository.set_state(_MX.format(matrix_event), json.dumps([rumor_id, member.pubkey, kind]))
    await app.state.relay_pool.publish(wrap, community.relays)
    return True


async def _outgoing(app: FastAPI, event: dict[str, Any], content: dict[str, Any], tags: list[list[str]]):
    """(kind, content, tags) of the rumor a Matrix event becomes, or None."""
    repository = app.state.repository

    async def rumor_of(matrix_event: str | None):
        stored = await repository.get_state(_MX.format(matrix_event)) if matrix_event else None
        return json.loads(stored) if stored else None

    kind_type = event.get("type")
    if kind_type == "m.reaction":
        relates = content.get("m.relates_to") or {}
        target = await rumor_of(relates.get("event_id"))
        key = relates.get("key")
        if target is None or not isinstance(key, str):
            return None
        return 7, ("+" if key == "👍" else key), [*tags, ["e", target[0]], ["p", target[1]], ["k", str(target[2])]]
    if kind_type == "m.room.redaction":
        target = await rumor_of(event.get("redacts") or content.get("redacts"))
        if target is None:
            return None
        return 5, str(content.get("reason") or ""), [*tags, ["e", target[0]], ["k", str(target[2])]]
    from bridge.notes import MEDIA_MSGTYPES, message_body

    relates = content.get("m.relates_to") or {}
    if relates.get("rel_type") == "m.replace":
        target = await rumor_of(relates.get("event_id"))
        new = content.get("m.new_content") or {}
        if target is None or not isinstance(new.get("body"), str):
            return None
        return EDIT_KIND, new["body"], [*tags, ["e", target[0]]]
    text = message_body(content)
    has_media = False
    if content.get("msgtype") in MEDIA_MSGTYPES:
        # Encrypted for the community, as Armada does: a plain link would make
        # the file public.
        from bridge.dms import _encrypted_file
        from bridge.media import MediaProblem

        try:
            url, file_tags = await _encrypted_file(app, content)
        except MediaProblem:
            return None
        fields = {t[0]: t[1] for t in file_tags}
        caption = text if content.get("filename") and content.get("filename") != content.get("body") else ""
        text = caption
        tags = [*tags, ["imeta", f"url {url}", f"m {fields['file-type']}", "encryption-algorithm aes-gcm",
                        f"decryption-key {fields['decryption-key']}", f"decryption-nonce {fields['decryption-nonce']}",
                        f"x {fields['x']}", f"ox {fields['ox']}"]]
        has_media = True
    if not text and not has_media:
        return None
    parent_event = (relates.get("m.in_reply_to") or {}).get("event_id")
    thread_root = relates.get("event_id") if relates.get("rel_type") == "m.thread" else None
    parent = await rumor_of(parent_event if not relates.get("is_falling_back") else None) or await rumor_of(thread_root)
    if parent is None:
        return 9, text, tags
    root = await rumor_of(thread_root) or parent
    return 1111, text, [*tags, ["K", str(root[2])], ["E", root[0], "", root[1]], ["P", root[1]],
                        ["k", str(parent[2])], ["e", parent[0], "", parent[1]], ["p", parent[1]]]


# -- subscriptions ----------------------------------------------------------------------------------


async def desired(app: FastAPI) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """For bridge.feeds: each community's streams on its relays."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    from bridge.nostr.relay_pool import normalize_relay_url

    for community in await all_communities(app):
        authors = [community.control_address(), *(community.channel_key(c).pk for c in community.readable())]
        for relay in community.relays[:_MAX_RELAYS]:
            out[(normalize_relay_url(relay), f"concord:{community.id[:16]}")] = [
                {"kinds": [WRAP_KIND], "authors": sorted(set(authors))}]
    return out
