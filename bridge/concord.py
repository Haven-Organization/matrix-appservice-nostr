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
from bridge.users import bot_mxid, is_bridge_user

logger = logging.getLogger(__name__)

INVITE_KIND = 33301
WRAP_KIND = 1059
SEAL_ENCRYPTED = 20013
SEAL_PLAINTEXT = 20014
EDITION_KIND = 3308
JOIN_KIND = 3306
KICK_KIND = 3309
REKEY_KIND = 3303
TIMER_KIND = 1740
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
    # Room -> the Matrix users invited to it once already: nobody is invited
    # again (someone who declined, or left, stays out) until they "[join" again.
    invited: dict[str, list[str]] = field(default_factory=dict)
    # The community's icon and banner: {"url"} for a plain link, or with
    # "key", "nonce" (AES-GCM), "hash" (of the image) and "ext" when the
    # file is encrypted for the community, as Armada uploads them.
    icon: dict[str, str] = field(default_factory=dict)
    banner: dict[str, str] = field(default_factory=dict)
    # What's been put in Matrix of those: "icon"/"banner" -> the image it
    # was, "icon_mxc" -> its upload, "rooms" -> room -> the icon it shows.
    shown: dict[str, Any] = field(default_factory=dict)

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


def _index(app: FastAPI) -> dict[str, tuple[str, str, GroupKey]]:
    """Stream address -> (community id, plane, key). Planes: "control",
    "guestbook", "chat:<channel_id>", "rekey:<scope_id hex>"."""
    return app.state.__dict__.setdefault("concord_index", {})


def planes(community: Community) -> dict[str, tuple[str, GroupKey]]:
    """Every address the bridge follows for a community."""
    out: dict[str, tuple[str, GroupKey]] = {}
    if community.removed:
        return out
    out[community.control_address()] = ("control", community.control())
    guestbook = community.guestbook()
    out[guestbook.pk] = ("guestbook", guestbook)
    for channel_id in community.readable():
        key = community.channel_key(channel_id)
        out[key.pk] = (f"chat:{channel_id}", key)
    for scope, key in rekey_addresses(community).items():
        out[key.pk] = (f"rekey:{scope}", key)
    return out


async def addresses(app: FastAPI) -> dict[str, tuple[str, str, GroupKey]]:
    index = _index(app)
    if not index:
        for community in await all_communities(app):
            for address, (plane, key) in planes(community).items():
                index[address] = (community.id, plane, key)
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


def make_wrap(key: GroupKey, author_secret: bytes, kind: int, content: str, tags: list[list[str]],
              *, expire_after: int = 0) -> tuple[nostr_event.Event, str]:
    """(the kind 1059 stream event, the rumor's id) for a rumor. With
    ``expire_after`` (CORD-08), the rumor and its wrap carry the NIP-40
    expiration, except deletes and timer notices."""
    now = time.time()
    created_at = int(now)
    tags = [*tags, ["ms", str(int(now * 1000) % 1000)]]
    wrap_tags: list[list[str]] = []
    if expire_after and kind not in (5, 1740):
        expiration = ["expiration", str(created_at + expire_after)]
        tags.append(expiration)
        wrap_tags.append(expiration)
    author = secp256k1.public_key(author_secret).hex()
    rumor_id = nostr_event.compute_id(author, created_at, kind, tags, content)
    rumor = {"id": rumor_id, "pubkey": author, "created_at": created_at, "kind": kind, "tags": tags, "content": content}
    seal = nostr_event.build(author_secret, kind=SEAL_ENCRYPTED, created_at=created_at, tags=[],
                             content=nip44.encrypt(json.dumps(rumor, ensure_ascii=False, separators=(",", ":")), key.conv))
    ephemeral = secp256k1.public_key(secp256k1.generate_secret_key()).hex()
    wrap = nostr_event.build(key.sk, kind=WRAP_KIND, created_at=created_at, tags=[["p", ephemeral], *wrap_tags],
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


_IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _image_ref(value: Any) -> dict[str, str]:
    """A community image from its metadata: a plain https link, or an
    encrypted file ({"url", "key", "nonce", "hash", "ext"})."""
    if isinstance(value, str):
        return {"url": value} if value.startswith("https://") and len(value) <= 2000 else {}
    if not isinstance(value, dict) or not isinstance(value.get("url"), str) or not value["url"].startswith("https://"):
        return {}
    ref = {"url": value["url"][:2000]}
    for name, size in (("key", 64), ("nonce", 0), ("hash", 64)):
        item = value.get(name)
        if isinstance(item, str) and all(c in "0123456789abcdefABCDEF" for c in item) and item and (not size or len(item) == size):
            ref[name] = item.lower()
    if bool(ref.get("key")) != bool(ref.get("nonce")) or ("key" in ref and not 16 <= len(ref["nonce"]) <= 256):
        return {}
    ext = value.get("ext")
    if isinstance(ext, str) and ext.lower() in _IMAGE_TYPES:
        ref["ext"] = ext.lower()
    return ref


def fold_control(community: Community, rumors: list[dict[str, Any]]) -> None:
    """The Control Plane folded (CORD-04): the roster first, outward from the
    owner until it settles, then metadata and channels by permission."""
    editions = _editions(rumors)
    banlist = hkdf(bytes.fromhex(community.id), "concord/banlist").hex()  # its one coordinate (CORD-02 A.6)
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
            content = head["content"] if isinstance(head["content"], dict) else {}
            member = content.get("member")
            if isinstance(member, str) and len(member) == 64 and member != community.owner:
                grants[member] = [r for r in content.get("role_ids") or [] if r in roles][:64]
        banned = _heads(community, editions, "4", lambda e: e["eid"] == banlist and community.can(e["actor"], BAN))
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
        community.icon = _image_ref(content.get("icon")) or _image_ref(picture)
        community.banner = _image_ref(content.get("banner"))
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


async def _ensure_rooms(app: FastAPI, community: Community, users: list[str], again: str | None = None) -> None:
    """The community's Space and a room for every channel the bridge can read,
    each user invited to each once. ``again``: a user who asked ("[join"),
    invited again wherever they aren't."""
    homeserver = app.state.homeserver
    config = app.state.config
    bot = bot_mxid(config)
    server = config.homeserver.server_name
    topic = (community.description + "\n\n" if community.description else "") + (
        "A Concord community, bridged from Nostr. " + WARNING.lstrip("⚠️ "))
    made: set[str] = set()  # rooms made now, everyone invited with them
    if community.space is None:
        community.space = await homeserver.create_room(
            as_user_id=bot, name=community.name, topic=topic, invite=users, room_type="m.space",
            preset="private_chat", history_visibility="shared",
        )
        made.add(community.space)
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
            made.add(info["room"])
            await app.state.repository.set_state(_ROOM.format(info["room"]), f"{community.id}|{channel_id}")
        else:
            current = await homeserver.get_room_state(info["room"], "m.room.name", as_user_id=bot)
            if (current or {}).get("name") != name:
                await homeserver.send_state_event(info["room"], "m.room.name", {"name": name}, as_user_id=bot)
    from bridge.messages import invite_to_room
    from bridge.spaces import add_room_to_space

    rooms = [community.space, *(community.channels[c]["room"] for c in community.readable())]
    await _show_images(app, community, rooms)
    for room in rooms:
        if room not in community.invited:
            # Made just now (everyone was invited with it), or from before
            # invites were remembered (everyone here had theirs).
            community.invited[room] = list(users)
    for user in users:
        for room in rooms:
            done = community.invited.setdefault(room, [])
            if user in done and (user != again or room in made):
                continue
            await invite_to_room(app, room, user)
            if user not in done:
                done.append(user)
        await add_room_to_space(app, user, community.space)


async def _fetch_image(app: FastAPI, ref: dict[str, str]) -> tuple[bytes, str]:
    """The image's bytes and type: fetched, and decrypted if it's sealed for
    the community (checked against its hash)."""
    fetched = await app.state.fetcher.get(ref["url"], max_bytes=app.state.config.nostr.max_media_mb * 1024 * 1024)
    data = fetched.content
    if ref.get("key"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        data = AESGCM(bytes.fromhex(ref["key"])).decrypt(bytes.fromhex(ref["nonce"]), data, None)
    if ref.get("hash") and hashlib.sha256(data).hexdigest() != ref["hash"]:
        raise ValueError("hash mismatch")
    mime = _IMAGE_TYPES.get(ref.get("ext", ""))
    if mime is None:
        mime = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif"}.get(
            next((m for m in (b"\x89PNG", b"\xff\xd8\xff", b"GIF8") if data.startswith(m)), b""), "")
        if not mime and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
    if not mime:
        raise ValueError("not an image")
    return data, mime


async def _upload_image(app: FastAPI, ref: dict[str, str], name: str) -> str | None:
    from bridge.render import upload_once

    try:
        data, mime = await _fetch_image(app, ref)
        filename, bot = f"{name}.{mime.split('/')[1]}", bot_mxid(app.state.config)
        if ref.get("key"):
            # Sealed for the community: kept out of the shared copy records.
            return await app.state.homeserver.upload_media(data, mime, filename, as_user_id=bot)
        return await upload_once(app, data, mime, filename, source=str(ref.get("url") or ""), as_user_id=bot)
    except Exception as exc:
        logger.info("Could not fetch a community %s from %s: %s", name, ref.get("url"), exc)
        return None


async def _set_avatar(app: FastAPI, room_id: str, url: str) -> None:
    """A room's avatar from a plain image link (NIP-29 groups)."""
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


def _ref_key(ref: dict[str, str]) -> str:
    return json.dumps(ref, sort_keys=True) if ref else ""


async def _show_images(app: FastAPI, community: Community, rooms: list[str]) -> None:
    """The community's icon as the avatar of its Space and rooms, and its
    banner as the Space's banner: set when they change (or a room is new)."""
    from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE

    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    shown = community.shown
    icon = _ref_key(community.icon)
    if icon and shown.get("icon") != icon:
        # If it can't be fetched now, it's tried again at the next change
        # to the community, or the next "[join".
        mxc = await _upload_image(app, community.icon, "icon")
        if mxc is not None:
            shown.update(icon=icon, icon_mxc=mxc)
    elif not icon:
        shown.pop("icon", None)
        shown.pop("icon_mxc", None)
    seen = shown.setdefault("rooms", {})
    for room in rooms if shown.get("icon", "") == icon else ():
        if seen.get(room) == icon:
            continue
        try:
            await homeserver.send_state_event(room, "m.room.avatar", {"url": shown["icon_mxc"]} if icon else {},
                                              as_user_id=bot)
            seen[room] = icon
        except HomeserverError:
            logger.info("Could not set the avatar of %s", room, exc_info=True)
    banner = _ref_key(community.banner)
    if shown.get("banner", "") != banner and community.space:
        mxc = await _upload_image(app, community.banner, "banner") if banner else None
        if banner and mxc is None:
            return
        try:
            await homeserver.send_state_event(community.space, PROFILE_BANNER_STATE_TYPE, {"url": mxc} if mxc else {},
                                              as_user_id=bot, state_key="")
            shown["banner"] = banner
        except HomeserverError:
            logger.info("Could not set the banner of %s", community.space, exc_info=True)


# -- joining --------------------------------------------------------------------------------


async def handle_join(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    p = app.state.config.appservice.command_prefix
    if not argument.strip():
        await notice(app, room_id, f'Usage: "{p}join <community invite link>" (an Armada, Vector or other Concord invite).')
        return
    from bridge.shadow_ids import ensure_shadow_id_and_tell

    # Like [follow: makes a shadow ID for someone who has none.
    member = await ensure_shadow_id_and_tell(app, sender, room_id)
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
    community.removed = False
    await read_control(app, community)
    await catch_up(app, community)
    if community.removed or not community.readable():
        await notice(app, room_id, "I opened the invite, but couldn't read any of the community's channels.")
        return
    new_member = member.id not in community.members
    if new_member:
        community.members.append(member.id)
    users = sorted({m.matrix_user_id for i in community.members if (m := await app.state.repository.get_id(i))})
    try:
        await _ensure_rooms(app, community, users, again=sender)
        await read_guestbook(app, community)
        await _power_levels(app, community)
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


async def _join_quietly(app: FastAPI, community: Community, sender: str, room_id: str):
    """Someone talking in a channel room whose ID isn't a member yet: if
    they may send from this room (``bridge.shadow_ids``: a local user, or a
    third party with a grant, a room grant included), they join as the ID
    that acts for them here, a shadow one made if they have none. The
    bridge already holds the community's keys, so joining is just the
    guestbook entry. ``None`` if they may not (or the ID can't take part)."""
    from bridge.shadow_ids import ensure_shadow_id_and_tell, still_allowed

    member = await ensure_shadow_id_and_tell(app, sender, room_id)
    if member is None or not await still_allowed(app, member, room_id):
        return None
    if member.key_origin == "bunker":
        await notice(app, room_id, f"{member.name}@{app.state.config.bridge.domain} signs through a remote signer, "
                                   "which Concord communities can't work with yet.")
        return None
    if member.id not in community.members:
        community.members.append(member.id)
        await save(app, community)
        await _announce_join(app, community, member, {})
        logger.info("%s joined %s as %s from its room", sender, community.id, member.name)
    return member


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
    cid, plane, key = entry
    community = await load(app, cid)
    if community is None or community.removed:
        return
    if plane == "control":
        if open_wrap(wrap, key) is not None:
            await _refresh(app, community)
    elif plane == "guestbook":
        rumor = open_wrap(wrap, key)
        if rumor is not None and await apply_guestbook(app, community, [rumor]):
            await save(app, community)
    elif plane.startswith("rekey:"):
        rumor = open_wrap(wrap, key)
        if rumor is not None and await apply_rekey(app, community, plane[6:], rumor):
            await _after_rotation(app, community)
    elif plane.startswith("chat:") and plane[5:] in community.channels:
        await _receive_chat(app, community, plane[5:], key, wrap)


async def _refresh(app: FastAPI, community: Community) -> None:
    """The Control Plane changed: the roster, channels and metadata again."""
    await read_control(app, community)
    users = sorted({m.matrix_user_id for i in community.members if (m := await app.state.repository.get_id(i))})
    try:
        await _ensure_rooms(app, community, users)
        await _power_levels(app, community)
        for pubkey in community.banned:
            await _leave_rooms(app, community, pubkey)
            await _drop_member(app, community, pubkey, "was banned from the community")
    except HomeserverError:
        logger.info("Could not update the rooms of %s", community.id, exc_info=True)
    await save(app, community)
    app.state.feeds.changed()


async def refresh_all(app: FastAPI) -> None:
    """At startup: each community's metadata, channels and roster, for
    whatever changed while the bridge was away."""
    for community in await all_communities(app):
        if community.removed or community.space is None:
            continue
        try:
            await _refresh(app, community)
        except Exception:
            logger.exception("Could not refresh community %s", community.id)


# -- roles as power levels ------------------------------------------------------------------


async def _local_mxids(app: FastAPI, community: Community) -> dict[str, str]:
    out = {}
    for id_ref in community.members:
        member = await app.state.repository.get_id(id_ref)
        if member is not None:
            out[member.pubkey] = member.matrix_user_id
    return out


def _power_for(community: Community, pubkey: str) -> int:
    """A member's rank as a Matrix power level, for display: nothing is
    enforced from Matrix."""
    if pubkey == community.owner:
        return 90
    bits = community.permissions(pubkey)
    if bits & (BAN | MANAGE_ROLES):
        return 75
    if bits & (KICK | MANAGE_MESSAGES | MANAGE_CHANNELS | MANAGE_METADATA):
        return 50
    return 0


async def _power_levels(app: FastAPI, community: Community) -> None:
    from bridge.remote import ghost_user_id

    homeserver = app.state.homeserver
    bot = bot_mxid(app.state.config)
    locals_ = await _local_mxids(app, community)
    wanted = {locals_.get(pk) or ghost_user_id(app, pk): level
              for pk in {community.owner, *community.grants} if (level := _power_for(community, pk))}
    for channel_id in community.readable():
        room = community.channels[channel_id].get("room")
        if not room:
            continue
        levels = await homeserver.get_room_state(room, "m.room.power_levels", as_user_id=bot) or {}
        users = dict(levels.get("users") or {})
        changed = False
        for user, level in wanted.items():
            if users.get(user, 0) != level:
                users[user] = level
                changed = True
        for user in [u for u, level in users.items() if u not in wanted and u != bot and 0 < level < 100]:
            del users[user]
            changed = True
        if changed:
            await homeserver.send_state_event(room, "m.room.power_levels", {**levels, "users": users}, as_user_id=bot)


async def _leave_rooms(app: FastAPI, community: Community, pubkey: str) -> None:
    """A member's ghost out of the community's rooms."""
    from bridge.remote import ghost_user_id

    if await app.state.repository.get_id_by_pubkey(pubkey) is not None:
        return
    ghost = ghost_user_id(app, pubkey)
    for channel_id in community.readable():
        room = community.channels[channel_id].get("room")
        if room:
            try:
                await app.state.homeserver.leave_room(room, as_user_id=ghost)
            except HomeserverError:
                pass


# -- the Guestbook (CORD-02 §5) -----------------------------------------------------------------


def _ms_time(rumor: dict[str, Any]) -> int:
    ms = next((t[1] for t in rumor.get("tags") or [] if len(t) >= 2 and t[0] == "ms"), "0")
    return int(rumor.get("created_at", 0)) * 1000 + (int(ms) if ms.isdigit() and int(ms) < 1000 else 0)


async def apply_guestbook(app: FastAPI, community: Community, rumors: list[dict[str, Any]], *, live: bool = True) -> bool:
    """Joins, Leaves and authorized Kicks, coalesced per member (latest
    wins, ties to the lower rumor id): who's present, with their ghosts in
    the rooms. A member here who's kicked live leaves (history only says
    who's present: a rejoin follows an old kick). True if anything changed."""
    from bridge import remote as remotes

    horizon = (time.time() + 3600) * 1000
    latest: dict[str, tuple[int, str, str]] = {}
    for rumor in rumors:
        at, kind, actor = _ms_time(rumor), rumor.get("kind"), rumor.get("pubkey", "")
        if at > horizon:
            continue
        if kind == JOIN_KIND and rumor.get("content") in ("join", "leave"):
            target, verb = actor, rumor["content"]
        elif kind == KICK_KIND:
            target = next((t[1] for t in rumor.get("tags") or [] if len(t) >= 2 and t[0] == "p"), "")
            if not target or not community.can(actor, KICK, over=target):
                continue
            verb = "kick"
        else:
            continue
        current = latest.get(target)
        if current is None or at > current[0] or (at == current[0] and rumor["id"] < current[2]):
            latest[target] = (at, verb, rumor["id"])
    before = set(community.present)
    present = set(before)
    for pubkey, (_at, verb, _id) in latest.items():
        (present.add if verb == "join" else present.discard)(pubkey)
    present -= set(community.banned)
    community.present = sorted(present)[:5000]
    for pubkey in sorted(present - before):
        if await app.state.repository.get_id_by_pubkey(pubkey) is not None:
            continue
        ghost = await remotes.get_or_create_remote(app, pubkey)
        for channel_id in community.readable():
            room = community.channels[channel_id].get("room")
            if room:
                try:
                    await remotes.ensure_ghost_in_room(app, ghost.ghost_user_id, room)
                except HomeserverError:
                    pass
    for pubkey in before - present:
        await _leave_rooms(app, community, pubkey)
    kicked = [pk for pk, (_at, verb, _id) in latest.items() if verb == "kick"] if live else []
    for pubkey in kicked:
        await _drop_member(app, community, pubkey, "was removed from the community by its staff")
    return present != before or bool(kicked)


async def _drop_member(app: FastAPI, community: Community, pubkey: str, why: str) -> None:
    """A member here who's no longer in the community: told, and out of its rooms."""
    member = await app.state.repository.get_id_by_pubkey(pubkey)
    if member is None or member.id not in community.members:
        return
    community.members.remove(member.id)
    handle = f"{member.name}@{app.state.config.bridge.domain}"
    bot = bot_mxid(app.state.config)
    rooms = [community.space, *(community.channels[c].get("room") for c in community.readable())]
    for room in [r for r in rooms if r]:
        try:
            await notice(app, room, f"{handle} {why}.")
            await app.state.homeserver.kick_user(room, member.matrix_user_id, as_user_id=bot, reason=why)
        except HomeserverError:
            pass
    if not community.members:
        community.removed = True


async def read_guestbook(app: FastAPI, community: Community) -> None:
    key = community.guestbook()
    wraps = await app.state.relay_pool.query(community.relays, [{"kinds": [WRAP_KIND], "authors": [key.pk]}], timeout=12.0)
    await apply_guestbook(app, community, [r for w in wraps if (r := open_wrap(w, key)) is not None], live=False)


# -- rekeys and refoundings (CORD-06) ---------------------------------------------------------------


_BASE_SCOPE = "00" * 32


def rekey_addresses(community: Community) -> dict[str, GroupKey]:
    """Scope id (hex) -> where the next rotation of that scope lands: the
    base, and every channel (a public one's first rotation makes it private)."""
    root, cid = bytes.fromhex(community.root), bytes.fromhex(community.id)
    out = {_BASE_SCOPE: group_key("concord/base-rekey-pseudonym", root, cid, community.epoch + 1)}
    for channel_id, info in community.channels.items():
        if not info.get("deleted") and not info.get("lost"):
            out[channel_id] = group_key("concord/rekey-pseudonym", root, bytes.fromhex(channel_id),
                                        int(info.get("epoch") or 0) + 1)
    return out


def _commitment(epoch: int, key: bytes) -> str:
    return hashlib.sha256(b"concord/epoch-key-commitment" + epoch.to_bytes(8, "big") + key).hexdigest()


async def apply_rekey(app: FastAPI, community: Community, scope: str, rumor: dict[str, Any]) -> bool:
    """A kind 3303 rotation: the new key, if one of our members' blobs is in
    it; or, once every chunk is in and none is, the news that we were
    removed. True if the community changed."""
    if rumor.get("kind") != REKEY_KIND:
        return False
    tags = {t[0]: t[1:] for t in rumor.get("tags") or [] if t}
    rotator = rumor.get("pubkey", "")
    try:
        if tags["scope"][0] != scope:
            return False
        new_epoch, prev_epoch, prevcommit = int(tags["newepoch"][0]), int(tags["prevepoch"][0]), tags["prevcommit"][0]
        chunk, chunks = int(tags["chunk"][0]), int(tags["chunk"][1])
        blobs = json.loads(rumor.get("content") or "[]")
    except (KeyError, IndexError, ValueError):
        return False
    base = scope == _BASE_SCOPE
    info = None if base else community.channels.get(scope)
    if not base and info is None:
        return False
    if not community.can(rotator, BAN if base else MANAGE_CHANNELS):
        logger.info("Ignoring a rotation in %s by %s, who may not", community.id, rotator)
        return False
    current = community.epoch if base else int(info.get("epoch") or 0)
    if prev_epoch != current or new_epoch != current + 1:
        return False
    # A public Channel's key is the community root (CORD-02 A.6).
    held = bytes.fromhex(community.root if base or not info.get("key") else info["key"])
    if _commitment(prev_epoch, held) != prevcommit:
        logger.info("Ignoring a rotation in %s that doesn't continue the key held", community.id)
        return False
    wrapped_by_locator = {b.get("locator"): b.get("wrapped") for b in blobs if isinstance(b, dict)}
    scope_bytes = bytes.fromhex(scope)
    adopted = False
    for id_ref in list(community.members):
        member = await app.state.repository.get_id(id_ref)
        secret = await app.state.signer.secret_key(member.pubkey) if member else None
        if secret is None:
            continue
        locator = hkdf(bytes.fromhex(rotator) + bytes.fromhex(member.pubkey), "concord/recipient-pseudonym",
                       scope_bytes, new_epoch).hex()
        wrapped = wrapped_by_locator.get(locator)
        if not wrapped:
            continue
        try:
            plain = nip44.decrypt_bytes(wrapped, nip44.get_conversation_key(secret, rotator))
        except Exception:
            continue
        if plain[:32] != scope_bytes or int.from_bytes(plain[32:40], "big") != new_epoch:
            continue
        if base and len(plain) in (72, 104, 136):
            community.root, community.epoch = plain[40:72].hex(), new_epoch
            community.control_pk = plain[72:104].hex() if len(plain) >= 104 else None
        elif not base and len(plain) == 72:
            info.update({"key": plain[40:72].hex(), "epoch": new_epoch, "private": True})
        else:
            continue
        adopted = True
        break
    rotation = f"{scope}:{rotator}:{new_epoch}:{prevcommit}"
    if adopted:
        community.pending.pop(rotation, None)
        logger.info("Moved %s (%s) to epoch %d", community.id, "base" if base else scope[:8], new_epoch)
        return True
    seen = sorted(set(community.pending.get(rotation, [])) | {chunk})
    community.pending[rotation] = seen
    if len(seen) < chunks:
        await save(app, community)
        return False
    # Every chunk is in and none has a key for us: removed.
    community.pending.pop(rotation, None)
    if base:
        for id_ref in list(community.members):
            member = await app.state.repository.get_id(id_ref)
            if member is not None:
                await _drop_member(app, community, member.pubkey, "was removed from the community (it was re-founded without them)")
        community.removed = True
    else:
        info.update({"key": None, "lost": True, "private": True})
        room = info.get("room")
        if room:
            await notice(app, room, "This channel's key was changed without us, so nothing new will arrive here.")
    return True


async def _after_rotation(app: FastAPI, community: Community) -> None:
    """New keys: catch up on any further rotations, then the new epoch's planes."""
    await save(app, community)
    if not community.removed:
        await catch_up(app, community)
        await _refresh(app, community)
        await read_guestbook(app, community)
        await save(app, community)
    app.state.feeds.changed()


async def catch_up(app: FastAPI, community: Community) -> None:
    """Rotations missed while away, or before joining, in order."""
    for _round in range(16):
        moved = False
        for scope, key in rekey_addresses(community).items():
            wraps = await app.state.relay_pool.query(community.relays, [{"kinds": [WRAP_KIND], "authors": [key.pk]}], timeout=10.0)
            for wrap in wraps:
                rumor = open_wrap(wrap, key)
                if rumor is not None and await apply_rekey(app, community, scope, rumor):
                    moved = True
                    break
            if moved:
                break
        if not moved or community.removed:
            return
        await read_control(app, community)


# -- disappearing messages (CORD-08) -----------------------------------------------------------------


_EXPIRY = "concord:exp:{:012d}:{}"


async def expire_due(app: FastAPI) -> int:
    """Redact the community messages whose time is up. Returns how many."""
    repository = app.state.repository
    now = int(time.time())
    done = 0
    for key, room in await repository.list_state("concord:exp:"):
        when, event_id = key.split(":", 3)[2:]
        if int(when) > now:
            continue
        try:
            await app.state.homeserver.redact_event(room, event_id, as_user_id=bot_mxid(app.state.config),
                                                    reason="Disappearing message")
        except HomeserverError:
            pass
        await repository.delete_state(key)
        done += 1
    return done


async def expiry_loop(app: FastAPI) -> None:
    import asyncio

    while True:
        await asyncio.sleep(60)
        try:
            await expire_due(app)
        except Exception:
            logger.exception("Expiring community messages failed")


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
    if author in community.banned:
        return False
    expiration = (first.get("expiration") or [None, ""])[1]
    expires = int(expiration) if str(expiration).isdigit() else 0
    if expires and expires <= time.time():
        return False  # CORD-08: never stored once expired
    if author not in community.present and len(community.present) < 5000:
        community.present.append(author)
        await save(app, community)
    kind = rumor.get("kind")
    if kind == TIMER_KIND:
        return await _timer_notice(app, community, room, rumor, first)
    local = await repository.get_id_by_pubkey(author)
    if local is not None:
        sender, extra = bot_mxid(app.state.config), {"com.beeper.per_message_profile": await _per_message_profile(app, local)}
    else:
        ghost = await remotes.get_or_create_remote(app, author)
        await remotes.ensure_ghost_in_room(app, ghost.ghost_user_id, room)
        sender, extra = ghost.ghost_user_id, {}
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
            from bridge.inbound import _emoji_mxc, reaction_key

            found = reaction_key(rumor)
            if found is None:
                return False
            reaction, fields = found
            emoji_url = fields.pop("_emoji_url", None)
            mxc = await _emoji_mxc(app, emoji_url, sender) if emoji_url and app.state.config.bridge.msc4027_custom_reactions else None
            if mxc is None:
                fields = {}  # no image: the ":name:" text
            else:
                reaction = mxc
            event_id = await homeserver.send_message_event(
                room, {"m.relates_to": {"rel_type": "m.annotation", "event_id": target[1], "key": reaction}, **fields, **extra},
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
    if expires and kind != 5:
        await repository.set_state(_EXPIRY.format(expires, event_id), room)
    return True


async def _display_name(app: FastAPI, pubkey: str) -> str:
    from bridge import remote as remotes
    from bridge.inbound import _ghost_label

    local = await app.state.repository.get_id_by_pubkey(pubkey)
    if local is not None:
        return f"{local.name}@{app.state.config.bridge.domain}"
    return await _ghost_label(app, await remotes.get_or_create_remote(app, pubkey))


async def _timer_notice(app: FastAPI, community: Community, room: str, rumor: dict[str, Any], first: dict) -> bool:
    """CORD-08 §4: shown only from someone who may set the policy."""
    seconds = (first.get("timer") or [None, ""])[1]
    if not str(seconds).isdigit() or not community.can(rumor["pubkey"], MANAGE_METADATA):
        return False
    repository = app.state.repository
    who = await _display_name(app, rumor["pubkey"])
    seconds = int(seconds)
    if seconds == 0:
        text = f"{who} turned off disappearing messages."
    else:
        days, hours = divmod(seconds // 3600, 24)
        span = f"{days} day{'s' * (days != 1)}" if days and not hours else f"{seconds // 3600 or 1} hour{'s' * (seconds >= 7200)}"
        text = f"{who} set disappearing messages to {span}."
    event_id = await notice(app, room, text)
    await repository.set_state(_MSG.format(rumor["id"]), json.dumps([room, "", rumor["pubkey"]]))
    return bool(event_id)


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
    if is_bridge_user(config, sender) or not sender.startswith("@"):
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
    from bridge.shadow_ids import still_allowed

    if member is not None and not await still_allowed(app, member, room_id):
        return True  # a third party whose access was taken away: stays in Matrix
    if member is None:
        member = await _join_quietly(app, community, sender, room_id)
    if member is None:
        from bridge.shadow_ids import may_use_bridge

        if await may_use_bridge(app, sender):  # anyone else isn't told about commands they can't run
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
    wrap, rumor_id = make_wrap(key, secret, kind, text, tags, expire_after=community.timer)
    # Recorded before it goes out: the relays echo it straight back, and an
    # unrecorded message by a member here would be mirrored in a second time.
    app.state.seen.add("concord:" + wrap.id)
    matrix_event = event.get("event_id", "")
    await app.state.repository.set_state(_MSG.format(rumor_id), json.dumps([room_id, matrix_event if kind != 5 else "", member.pubkey]))
    if kind != 5:
        await app.state.repository.set_state(_MX.format(matrix_event), json.dumps([rumor_id, member.pubkey, kind]))
    # In the background: the homeserver sends the bridge one batch of events
    # at a time, so waiting here on a slow relay (up to its timeout) held up
    # every message behind this one.
    from bridge.tasks import run_in_background

    run_in_background(app, _send(app, wrap, community), name=f"concord send {wrap.id[:8]}")
    return True


async def _send(app: FastAPI, wrap, community: Community) -> None:
    for result in await app.state.relay_pool.publish(wrap, community.relays):
        if not result.accepted:
            logger.info("%s didn't take a message for %s: %s", result.relay, community.name, result.message)


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
        refs = [*tags, ["e", target[0]], ["p", target[1]], ["k", str(target[2])]]
        if key.startswith("mxc://"):
            # A custom emoji (MSC4027): published, and sent as NIP-30 ":name:".
            import re

            from bridge.media import PROFILE_IMAGE_MAX_BYTES, publish_matrix_image

            shortcode = content.get("shortcode") or content.get("com.beeper.reaction.shortcode")
            name = re.sub(r"[^a-zA-Z0-9_-]", "", str(shortcode or "").strip(":")) or "emoji"
            published = await publish_matrix_image(app, key, max_bytes=PROFILE_IMAGE_MAX_BYTES)
            if published is None:
                return None
            return 7, f":{name}:", [*refs, ["emoji", name, published.url]]
        return 7, ("+" if key == "👍" else key), refs
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
        from bridge.media import EncryptedMedia, MediaProblem

        try:
            url, file_tags = await _encrypted_file(app, content)
        except EncryptedMedia:
            from bridge.encrypted_media import hold

            hold(app, event.get("room_id", ""), event)
            return None
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
        authors = list(planes(community))
        if not authors:
            continue
        for relay in community.relays[:_MAX_RELAYS]:
            out[(normalize_relay_url(relay), f"concord:{community.id[:16]}")] = [
                {"kinds": [WRAP_KIND], "authors": sorted(set(authors))}]
    return out
