"""Concord communities (CORD-01/02/03/05), phase 1."""

from __future__ import annotations

import base64
import json
import secrets
import time

from bridge import concord
from bridge.nostr import event as nostr_event
from bridge.nostr import nip19, nip44, secp256k1
from tests.test_federation import Stranger, run, transact
from tests.test_ids import ids_of, message, send

RELAY = "wss://relay.ditto.pub"


class FakeCommunity:
    """A community as Armada makes one: an invite on a relay, a legacy
    (pre-split) Control Plane, and a #general channel."""

    def __init__(self, relay_pool, name="PosterChan"):
        self.owner = Stranger()
        self.salt = secrets.token_hex(32)
        self.id = concord.community_id(self.owner.pubkey, self.salt)
        self.root = secrets.token_hex(32)
        self.general = secrets.token_hex(32)
        self.epoch = 0
        self.pool = relay_pool
        self.link_secret = secp256k1.generate_secret_key()
        self.token = secrets.token_bytes(16)
        bundle = {"community_id": self.id, "owner": self.owner.pubkey, "owner_salt": self.salt,
                  "community_root": self.root, "root_epoch": 0, "channels": [], "relays": [RELAY], "name": name}
        content = nip44.encrypt(json.dumps(bundle), concord.hkdf(self.token, "concord/invite-key"))
        invite = nostr_event.build(self.link_secret, kind=concord.INVITE_KIND, content=content, created_at=int(time.time()),
                                   tags=[["d", ""], ["vsk", "6"]])
        relay_pool.network.append(invite.to_dict())
        self.edition("0", self.id, {"name": name, "description": "A test", "relays": [RELAY]})
        self.edition("2", self.general, {"name": "general", "private": False})

    def link(self) -> str:
        naddr = nip19.naddr_encode(nip19.Address("", secp256k1.public_key(self.link_secret).hex(), concord.INVITE_KIND))
        fragment = base64.urlsafe_b64encode(bytes([4, 0, 1, 3]) + self.token).decode().rstrip("=")
        return f"https://armada.buzz/invite/{naddr}#{fragment}"

    def control(self) -> concord.GroupKey:
        return concord.group_key("concord/control", bytes.fromhex(self.root), bytes.fromhex(self.id), self.epoch)

    def edition(self, vsk, eid, content, version=1, actor=None):
        actor = actor or self.owner
        rumor = {"kind": concord.EDITION_KIND, "pubkey": actor.pubkey, "created_at": int(time.time()),
                 "content": json.dumps(content), "tags": [["vsk", vsk], ["eid", eid], ["ev", str(version)]]}
        seal = nostr_event.build(actor.secret, kind=concord.SEAL_PLAINTEXT, content=json.dumps(rumor), created_at=rumor["created_at"])
        key = self.control()
        wrap = nostr_event.build(key.sk, kind=concord.WRAP_KIND, created_at=rumor["created_at"],
                                 content=nip44.encrypt(json.dumps(seal.to_dict()), key.conv), tags=[["p", Stranger().pubkey]])
        self.pool.network.append(wrap.to_dict())
        return wrap.to_dict()

    def channel(self, channel_id=None) -> concord.GroupKey:
        return concord.group_key("concord/channel", bytes.fromhex(self.root), bytes.fromhex(channel_id or self.general), self.epoch)

    def say(self, who, kind, text, extra_tags=(), channel_id=None):
        wrap, rumor_id = concord.make_wrap(self.channel(channel_id), who.secret, kind, text,
                                           [["channel", channel_id or self.general], ["epoch", str(self.epoch)], *extra_tags])
        return wrap.to_dict(), rumor_id

    def guestbook(self) -> concord.GroupKey:
        return concord.group_key("concord/guestbook", bytes.fromhex(self.root), bytes.fromhex(self.id), self.epoch)

    def sign_in(self, who, kind=concord.JOIN_KIND, text="join", tags=()):
        wrap, _ = concord.make_wrap(self.guestbook(), who.secret, kind, text, list(tags))
        return wrap.to_dict()

    def refound(self, recipients, rotator=None, chunks=1):
        """A base rotation (CORD-06) to a new root, keyed to ``recipients``;
        returns the rekey wraps (one per chunk) and moves this fake along."""
        rotator = rotator or self.owner
        new_root = secrets.token_hex(32)
        cid = bytes.fromhex(self.id)
        address = concord.group_key("concord/base-rekey-pseudonym", bytes.fromhex(self.root), cid, self.epoch + 1)
        new_control = concord.group_key("concord/control", bytes.fromhex(new_root), cid, self.epoch + 1)
        scope = bytes(32)
        plain = scope + (self.epoch + 1).to_bytes(8, "big") + bytes.fromhex(new_root) + bytes.fromhex(new_control.pk)
        blobs = []
        for pubkey in recipients:
            locator = concord.hkdf(bytes.fromhex(rotator.pubkey) + bytes.fromhex(pubkey), "concord/recipient-pseudonym",
                                   scope, self.epoch + 1).hex()
            blobs.append({"locator": locator,
                          "wrapped": nip44.encrypt(plain, nip44.get_conversation_key(rotator.secret, pubkey))})
        commit = concord._commitment(self.epoch, bytes.fromhex(self.root))
        wraps = []
        for i in range(chunks):
            tags = [["scope", scope.hex()], ["newepoch", str(self.epoch + 1)], ["prevepoch", str(self.epoch)],
                    ["prevcommit", commit], ["chunk", str(i), str(chunks)]]
            wrap, _ = concord.make_wrap(address, rotator.secret, concord.REKEY_KIND, json.dumps(blobs if i == 0 else []), tags)
            wraps.append(wrap.to_dict())
        self.root, self.epoch = new_root, self.epoch + 1
        return wraps


def joined(client, homeserver, relay_pool):
    send(client, message("[create profile"))
    [q] = ids_of(client)
    community = FakeCommunity(relay_pool)
    alice = Stranger()
    earlier, earlier_id = community.say(alice, 9, "hello from Armada")
    relay_pool.network.append(earlier)
    send(client, message(f"[join {community.link()}"))
    state = run(client, concord.load, client.app, community.id)
    room = state.channels[community.general]["room"]
    return q, community, alice, state, room, earlier_id


def test_join_makes_a_space_with_a_room_per_channel(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    bodies = [c["body"] for c in homeserver.sent_to("!dm:example.org")]
    assert any("stored readable on this homeserver" in b for b in bodies)  # warned, not asked
    assert any(b.startswith("Joined PosterChan as q@example.org: #general") for b in bodies)
    assert state.members == [q.id] and state.name == "PosterChan" and state.description == "A test"
    space = next(r for r in homeserver.created if r["room_id"] == state.space)
    assert space["room_type"] == "m.space" and (state.space, "@q:example.org") in homeserver.invites
    assert homeserver.state_of(room, "m.room.name") == {"name": "#general"}
    assert (room, "@q:example.org") in homeserver.invites
    # The recent messages came in, from the author's ghost.
    shown = homeserver.messages(room)[-1]
    assert shown["content"]["body"] == "hello from Armada" and shown["sender"].startswith("@nostr_")
    # The Join was announced on the Guestbook, as q.
    guestbook = concord.group_key("concord/guestbook", bytes.fromhex(community.root), bytes.fromhex(community.id), 0)
    [join] = [e for e, _ in relay_pool.published if e.pubkey == guestbook.pk]
    rumor = concord.open_wrap(join.to_dict(), guestbook)
    assert rumor["kind"] == concord.JOIN_KIND and rumor["content"] == "join" and rumor["pubkey"] == q.pubkey


def test_messages_go_out_sealed_and_come_back_once(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    from tests.test_federation import post

    sent = post(client, homeserver, room, {"msgtype": "m.text", "body": "hi from Matrix"})
    [wrap] = [e for e, _ in relay_pool.published if e.pubkey == community.channel().pk]
    rumor = concord.open_wrap(wrap.to_dict(), community.channel())
    assert rumor["kind"] == 9 and rumor["content"] == "hi from Matrix" and rumor["pubkey"] == q.pubkey
    assert ["channel", community.general] in rumor["tags"] and ["epoch", "0"] in rumor["tags"]
    # Its echo from the relays isn't shown twice.
    count = len(homeserver.messages(room))
    run(client, concord.receive, client.app, wrap.to_dict())
    assert len(homeserver.messages(room)) == count
    # A Matrix reply becomes a threaded reply (kind 1111) to the message.
    shown = homeserver.messages(room)[0]
    post(client, homeserver, room, {"msgtype": "m.text", "body": "> quote\n\nreplying",
                                    "m.relates_to": {"m.in_reply_to": {"event_id": shown["event_id"]}}})
    reply = concord.open_wrap([e for e, _ in relay_pool.published if e.pubkey == community.channel().pk][-1].to_dict(),
                              community.channel())
    assert reply["kind"] == 1111 and ["E", earlier_id, "", alice.pubkey] in reply["tags"]
    assert sent


def test_live_reaction_reply_and_delete(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    original = homeserver.messages(room)[-1]
    reaction, _ = community.say(alice, 7, "🔥", [["e", earlier_id], ["p", alice.pubkey], ["k", "9"]])
    run(client, concord.receive, client.app, reaction)
    [shown] = homeserver.messages(room, "m.reaction")
    assert shown["content"]["m.relates_to"] == {"rel_type": "m.annotation", "event_id": original["event_id"], "key": "🔥"}
    bob = Stranger()
    reply, _ = community.say(bob, 1111, "in a thread", [["K", "9"], ["E", earlier_id, "", alice.pubkey], ["P", alice.pubkey],
                                                       ["k", "9"], ["e", earlier_id, "", alice.pubkey], ["p", alice.pubkey]])
    run(client, concord.receive, client.app, reply)
    threaded = homeserver.messages(room)[-1]
    assert threaded["content"]["m.relates_to"]["rel_type"] == "m.thread"
    assert threaded["content"]["m.relates_to"]["event_id"] == original["event_id"]
    # Bob can't delete Alice's message; Alice can.
    forged, _ = community.say(bob, 5, "", [["e", earlier_id], ["k", "9"]])
    run(client, concord.receive, client.app, forged)
    assert original["event_id"] not in {e for _, e, _ in homeserver.redactions}
    delete, _ = community.say(alice, 5, "", [["e", earlier_id], ["k", "9"]])
    run(client, concord.receive, client.app, delete)
    assert original["event_id"] in {e for _, e, _ in homeserver.redactions}


def test_rejected_and_untrusted(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    send(client, message("[join https://example.org/not-an-invite"))
    assert "doesn't look like a community invite" in homeserver.sent_to("!dm:example.org")[-1]["body"]
    community = FakeCommunity(relay_pool)
    tampered = community.link()[:-2] + ("AA" if not community.link().endswith("AA") else "BB")
    send(client, message(f"[join {tampered}"))
    assert "couldn't open that invite" in homeserver.sent_to("!dm:example.org")[-1]["body"]
    # A wrap for another channel's address, or with a wrong channel tag, isn't shown.
    send(client, message(f"[join {community.link()}"))
    state = run(client, concord.load, client.app, community.id)
    room = state.channels[community.general]["room"]
    wrong, _ = concord.make_wrap(community.channel(), Stranger().secret, 9, "misfiled", [["channel", "ab" * 32], ["epoch", "0"]])
    run(client, concord.receive, client.app, wrong.to_dict())
    assert all(m["content"].get("body") != "misfiled" for m in homeserver.messages(room))


def test_control_changes_add_channels_and_ignore_strangers(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    git = secrets.token_hex(32)
    wrap = community.edition("2", git, {"name": "Git", "private": False})
    # A member without a grant can't rename the community, even in a legacy community where they can write.
    community.edition("0", community.id, {"name": "Hijacked"}, version=50, actor=alice)
    run(client, concord.receive, client.app, wrap)
    state = run(client, concord.load, client.app, community.id)
    assert state.name == "PosterChan"
    assert homeserver.state_of(state.channels[git]["room"], "m.room.name") == {"name": "#Git"}


def test_bunker_identities_are_refused(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    run(client, client.app.state.repository.replace_key, q.id, pubkey=q.pubkey, secret_key=None, key_origin="bunker",
        bunker_uri="bunker://x", client_key=None)
    send(client, message(f"[join {FakeCommunity(relay_pool).link()}"))
    assert "remote signer" in homeserver.sent_to("!dm:example.org")[-1]["body"]


def test_feeds_subscribe_to_the_communitys_streams(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    desired = run(client, client.app.state.feeds._desired)
    filters = [f for (relay, name), fs in desired.items() if name.startswith("concord:") for f in fs]
    root, cid = bytes.fromhex(community.root), bytes.fromhex(community.id)
    assert filters and set(filters[0]["authors"]) == {
        community.control().pk, community.channel().pk,
        concord.group_key("concord/guestbook", root, cid, 0).pk,
        concord.group_key("concord/base-rekey-pseudonym", root, cid, 1).pk,
        concord.group_key("concord/rekey-pseudonym", root, bytes.fromhex(community.general), 1).pk}


def test_attachments_are_encrypted_both_ways(client, homeserver, relay_pool) -> None:
    import io

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from PIL import Image

    from bridge.safe_fetch import Fetched
    from tests.test_federation import post

    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (9, 9, 9)).save(buffer, format="PNG")
    key, nonce = secrets.token_bytes(32), secrets.token_bytes(16)
    sealed = AESGCM(key).encrypt(nonce, buffer.getvalue(), None)

    class Fetcher:
        async def get(self, url, **kwargs):
            return Fetched(url, 200, sealed, "application/octet-stream")

        async def aclose(self):
            pass

    client.app.state.fetcher = Fetcher()
    picture, _ = community.say(alice, 9, "", [["imeta", "url https://media.example/blob", "m image/png",
                                                "encryption-algorithm aes-gcm", f"decryption-key {key.hex()}",
                                                f"decryption-nonce {nonce.hex()}"]])
    run(client, concord.receive, client.app, picture)
    shown = homeserver.messages(room)[-1]["content"]
    assert shown["msgtype"] == "m.image" and homeserver.media[shown["url"]][0] == buffer.getvalue()
    # And what's sent from Matrix is encrypted for the community, never a plain link.
    homeserver.media["mxc://example.org/mine"] = (buffer.getvalue(), "image/png")
    post(client, homeserver, room, {"msgtype": "m.image", "body": "pic.png", "filename": "pic.png", "url": "mxc://example.org/mine",
                                    "info": {"mimetype": "image/png"}})
    wrap = [e for e, _ in relay_pool.published if e.pubkey == community.channel().pk][-1]
    rumor = concord.open_wrap(wrap.to_dict(), community.channel())
    imeta = concord._imeta(next(t for t in rumor["tags"] if t[0] == "imeta"))
    blob = homeserver.media[run(client, client.app.state.repository.get_blob, imeta["x"]).mxc][0]
    assert AESGCM(bytes.fromhex(imeta["decryption-key"])).decrypt(bytes.fromhex(imeta["decryption-nonce"]), blob, None) == buffer.getvalue()
    assert imeta["m"] == "image/png" and rumor["content"] == ""
    # A sticker goes the same way: the image, its description not a caption.
    sticker = homeserver._store(room, "m.sticker", {"body": "made-it-political", "url": "mxc://example.org/mine",
                                                    "info": {"mimetype": "image/png", "w": 8, "h": 8}}, "@q:example.org")
    transact(client, sticker)
    wrap = [e for e, _ in relay_pool.published if e.pubkey == community.channel().pk][-1]
    rumor = concord.open_wrap(wrap.to_dict(), community.channel())
    assert rumor["content"] == "" and any(t[0] == "imeta" for t in rumor["tags"])


def test_an_echo_arriving_while_publishing_isnt_shown(client, homeserver, relay_pool) -> None:
    """The relays echo a sent message back before the publish returns."""
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    from tests.test_federation import post

    echoed = []

    def echo(event, relays):
        if event.pubkey == community.channel().pk:
            client.app.state.seen = type(client.app.state.seen)()  # as if from another relay connection
            echoed.append(event)

    relay_pool.publish_hooks.append(echo)
    original_publish = relay_pool.publish

    async def publish_then_echo(event, relays, timeout=None):
        result = await original_publish(event, relays, timeout)
        if event.pubkey == community.channel().pk:
            await concord.receive(client.app, event.to_dict())
        return result

    relay_pool.publish = publish_then_echo
    before = len(homeserver.messages(room))
    post(client, homeserver, room, {"msgtype": "m.text", "body": "from Matrix"})
    assert echoed and len(homeserver.messages(room)) == before + 1  # only the user's own message


def test_custom_emoji_reactions_both_ways(client, homeserver, relay_pool) -> None:
    """NIP-30 emoji in a community: an MSC4027 image reaction in Matrix,
    and a Matrix image reaction goes out as ":name:" with its emoji tag."""
    import io

    from PIL import Image

    from bridge.safe_fetch import Fetched
    object.__setattr__(client.app.state.config.bridge, "msc4027_custom_reactions", True)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (250, 200, 0)).save(buffer, format="PNG")

    class Fetcher:
        async def get(self, url, **kwargs):
            return Fetched(url, 200, buffer.getvalue(), "image/png")

        async def aclose(self):
            pass

    client.app.state.fetcher = Fetcher()
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    original = homeserver.messages(room)[-1]
    reaction, _ = community.say(alice, 7, ":frierenspin:", [["e", earlier_id], ["p", alice.pubkey], ["k", "9"],
                                                            ["emoji", "frierenspin", "https://img.example/spin.png"]])
    run(client, concord.receive, client.app, reaction)
    [shown] = homeserver.messages(room, "m.reaction")
    relates = shown["content"]["m.relates_to"]
    assert relates["key"].startswith("mxc://") and relates["event_id"] == original["event_id"]
    assert shown["content"]["shortcode"] == ":frierenspin:"
    homeserver.media["mxc://example.org/blob"] = (buffer.getvalue(), "image/png")
    transact(client, homeserver._store(room, "m.reaction", {"m.relates_to": {
        "rel_type": "m.annotation", "event_id": original["event_id"], "key": "mxc://example.org/blob"},
        "shortcode": ":blobcat:"}, "@q:example.org"))
    wrap = [e for e, _ in relay_pool.published if e.pubkey == community.channel().pk][-1]
    rumor = concord.open_wrap(wrap.to_dict(), community.channel())
    assert rumor["kind"] == 7 and rumor["content"] == ":blobcat:"
    [emoji] = [t for t in rumor["tags"] if t[0] == "emoji"]
    assert emoji[1] == "blobcat" and emoji[2].startswith("http")


def test_the_guestbook_brings_members_and_honours_only_staff_kicks(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    bob = Stranger()
    run(client, concord.receive, client.app, community.sign_in(bob))
    ghost = next(m for m in homeserver._room(room)["members"] if m.endswith(bob.pubkey + ":example.org"))
    state = run(client, concord.load, client.app, community.id)
    assert bob.pubkey in state.present
    # Alice isn't staff: her kick of Bob is ignored. The owner's isn't.
    run(client, concord.receive, client.app, community.sign_in(alice, concord.KICK_KIND, "", [["p", bob.pubkey]]))
    assert ghost in homeserver._room(room)["members"]
    run(client, concord.receive, client.app, community.sign_in(community.owner, concord.KICK_KIND, "", [["p", bob.pubkey]]))
    assert ghost not in homeserver._room(room)["members"]
    # A live kick of q takes q out of the community's rooms.
    run(client, concord.receive, client.app, community.sign_in(community.owner, concord.KICK_KIND, "", [["p", q.pubkey]]))
    state = run(client, concord.load, client.app, community.id)
    assert state.removed and (room, "@q:example.org") in homeserver.kicked


def test_a_refounding_moves_to_the_new_root_or_removes_us(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    [rekey] = community.refound([alice.pubkey, q.pubkey])
    # The new epoch's Control Plane, rewrapped as a refounding does.
    community.edition("0", community.id, {"name": "PosterChan", "description": "A test", "relays": [RELAY]})
    community.edition("2", community.general, {"name": "general", "private": False})
    run(client, concord.receive, client.app, rekey)
    state = run(client, concord.load, client.app, community.id)
    assert state.epoch == 1 and state.root == community.root and not state.removed
    new_message, _ = community.say(alice, 9, "after the refounding")
    run(client, concord.receive, client.app, new_message)
    assert homeserver.messages(room)[-1]["content"]["body"] == "after the refounding"
    # A rotation by someone without BAN is ignored; one that leaves q out, in two chunks, removes q once both are in.
    forged = community.refound([q.pubkey], rotator=alice)
    community.root, community.epoch = state.root, 1
    run(client, concord.receive, client.app, forged[0])
    assert run(client, concord.load, client.app, community.id).epoch == 1
    first, second = community.refound([alice.pubkey], chunks=2)
    run(client, concord.receive, client.app, first)
    assert not run(client, concord.load, client.app, community.id).removed  # a missing chunk is never a removal
    run(client, concord.receive, client.app, second)
    state = run(client, concord.load, client.app, community.id)
    assert state.removed and (room, "@q:example.org") in homeserver.kicked


def test_roles_become_power_levels_and_bans_silence(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    mod = secrets.token_hex(32)
    community.edition("1", mod, {"name": "Mods", "position": 5, "permissions": concord.KICK | concord.MANAGE_MESSAGES})
    community.edition("3", secrets.token_hex(32), {"member": alice.pubkey, "role_ids": [mod]})
    bob = Stranger()
    community.edition("4", concord.hkdf(bytes.fromhex(community.id), "concord/banlist").hex(), [bob.pubkey])
    run(client, concord.receive, client.app, community.edition("0", community.id, {"name": "PosterChan", "relays": [RELAY]}, version=2))
    users = homeserver.state_of(room, "m.room.power_levels")["users"]
    from bridge.remote import ghost_user_id

    assert users[ghost_user_id(client.app, community.owner.pubkey)] == 90
    assert users[ghost_user_id(client.app, alice.pubkey)] == 50
    said, _ = community.say(bob, 9, "spam")
    count = len(homeserver.messages(room))
    run(client, concord.receive, client.app, said)
    assert len(homeserver.messages(room)) == count


def test_disappearing_messages_and_timer_notices(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    gone, _ = community.say(alice, 9, "already gone", [["expiration", str(int(time.time()) - 5)]])
    count = len(homeserver.messages(room))
    run(client, concord.receive, client.app, gone)
    assert len(homeserver.messages(room)) == count
    soon, _ = community.say(alice, 9, "soon gone", [["expiration", str(int(time.time()) + 2)]])
    run(client, concord.receive, client.app, soon)
    shown = homeserver.messages(room)[-1]
    assert shown["content"]["body"] == "soon gone"
    assert run(client, concord.expire_due, client.app) == 0
    time.sleep(2.1)
    assert run(client, concord.expire_due, client.app) == 1
    assert shown["event_id"] in {e for _, e, _ in homeserver.redactions}
    # Only staff are believed about the timer.
    forged, _ = community.say(alice, concord.TIMER_KIND, "", [["timer", "86400"]])
    run(client, concord.receive, client.app, forged)
    assert "disappearing" not in homeserver.messages(room)[-1]["content"].get("body", "")
    real, _ = community.say(community.owner, concord.TIMER_KIND, "", [["timer", "2592000"]])
    run(client, concord.receive, client.app, real)
    assert homeserver.messages(room)[-1]["content"]["body"].endswith("set disappearing messages to 30 days.")


def test_a_declined_room_isnt_invited_to_again(client, homeserver, relay_pool) -> None:
    """Rooms someone left or declined stay that way when the community
    changes; "[join" again invites them back."""
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)

    def invites():
        return homeserver.invites.count((room, "@q:example.org"))

    assert invites() == 1  # q never accepts: as if declined
    git = secrets.token_hex(32)
    run(client, concord.receive, client.app, community.edition("2", git, {"name": "Git", "private": False}))
    state = run(client, concord.load, client.app, community.id)
    assert invites() == 1  # not again for #general
    assert (state.channels[git]["room"], "@q:example.org") in homeserver.invites  # but the new #Git, once
    run(client, concord.receive, client.app, community.edition("0", community.id, {"name": "PosterChan 2"}, version=2))
    assert homeserver.invites.count((state.channels[git]["room"], "@q:example.org")) == 1
    send(client, message(f"[join {community.link()}"))
    assert invites() == 2


def test_the_icon_and_banner_go_on_the_space(client, homeserver, relay_pool) -> None:
    import hashlib
    import io

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from PIL import Image

    from bridge.msc4501 import PROFILE_BANNER_STATE_TYPE
    from bridge.safe_fetch import Fetched

    def png(colour):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), colour).save(buffer, format="PNG")
        return buffer.getvalue()

    files, fetched = {}, []

    def sealed(url, image):
        key, nonce = secrets.token_bytes(32), secrets.token_bytes(16)
        files[url] = AESGCM(key).encrypt(nonce, image, None)
        return {"url": url, "key": key.hex(), "nonce": nonce.hex(), "hash": hashlib.sha256(image).hexdigest(), "ext": "png"}

    class Fetcher:
        async def get(self, url, **kwargs):
            fetched.append(url)
            return Fetched(url, 200, files[url], "application/octet-stream")

        async def aclose(self):
            pass

    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    client.app.state.fetcher = Fetcher()
    icon, banner = png((1, 2, 3)), png((4, 5, 6))
    meta = {"name": "PosterChan", "relays": [RELAY], "icon": sealed("https://media.example/i1", icon),
            "banner": sealed("https://media.example/b1", banner)}
    run(client, concord.receive, client.app, community.edition("0", community.id, meta, version=2))
    state = run(client, concord.load, client.app, community.id)
    avatar = homeserver.state_of(state.space, "m.room.avatar")
    assert homeserver.media[avatar["url"]] == (icon, "image/png")
    assert homeserver.state_of(room, "m.room.avatar") == avatar  # the channels too
    assert homeserver.media[homeserver.state_of(state.space, PROFILE_BANNER_STATE_TYPE)["url"]][0] == banner
    # Nothing's fetched again until they change; then the new icon replaces it.
    run(client, concord.receive, client.app, community.edition("0", community.id, dict(meta, name="PosterChan 2"), version=3))
    assert fetched == ["https://media.example/i1", "https://media.example/b1"]
    files["https://media.example/plain.png"] = icon2 = png((7, 8, 9))
    meta = {"name": "PosterChan", "relays": [RELAY], "picture": "https://media.example/plain.png"}
    run(client, concord.receive, client.app, community.edition("0", community.id, meta, version=4))
    assert homeserver.media[homeserver.state_of(state.space, "m.room.avatar")["url"]] == (icon2, "image/png")
    assert homeserver.state_of(state.space, PROFILE_BANNER_STATE_TYPE) == {}  # the banner was taken away
    # A sealed file that doesn't match its hash isn't shown.
    bad = sealed("https://media.example/i2", png((0, 0, 0)))
    bad["hash"] = "00" * 32
    run(client, concord.receive, client.app, community.edition("0", community.id, dict(meta, icon=bad), version=5))
    assert homeserver.media[homeserver.state_of(state.space, "m.room.avatar")["url"]] == (icon2, "image/png")


def test_a_room_grant_joins_a_guest_quietly(client, homeserver, relay_pool) -> None:
    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    from tests.test_federation import command, post

    bronze = "@bronze:kitsunemimi.example"
    # No grant: nothing goes out, and nobody is told to run a command they can't.
    before = len(homeserver.sent_to(room))
    post(client, homeserver, room, {"msgtype": "m.text", "body": "hi?"}, sender=bronze)
    assert len(homeserver.sent_to(room)) == before
    assert run(client, client.app.state.repository.list_ids_for_user, bronze) == []
    command(client, "[allow room", sender="@admin:example.org", room=room)
    post(client, homeserver, room, {"msgtype": "m.text", "body": "hello all"}, sender=bronze)
    [shadow] = run(client, client.app.state.repository.list_ids_for_user, bronze)
    assert shadow.name == "bronze_kitsunemimi.example" and shadow.profile_room_id is None
    state = run(client, concord.load, client.app, community.id)
    assert shadow.id in state.members
    wraps = [e for e, _ in relay_pool.published if e.pubkey == community.channel().pk]
    rumor = concord.open_wrap(wraps[-1].to_dict(), community.channel())
    assert rumor["content"] == "hello all" and rumor["pubkey"] == shadow.pubkey
    # The join was announced in the guestbook, as the shadow ID.
    guestbook = concord.group_key("concord/guestbook", bytes.fromhex(community.root), bytes.fromhex(community.id), 0)
    joins = [concord.open_wrap(e.to_dict(), guestbook) for e, _ in relay_pool.published if e.pubkey == guestbook.pk]
    assert any(j["pubkey"] == shadow.pubkey for j in joins)
    bodies = [c.get("body", "") for c in homeserver.sent_to(room)]
    assert not any("Join the community first" in b for b in bodies)
    [told] = [b for b in bodies if "shadow Nostr identity for you" in b]
    assert "create profile" not in told  # a room grant doesn't let him make one
    assert told.startswith(f"{bronze}: I've created a shadow Nostr identity for you: bronze_kitsunemimi.example@")
    [content] = [c for c in homeserver.sent_to(room) if c.get("body") == told]
    assert content["m.mentions"] == {"user_ids": [bronze]} and f"matrix.to/#/{bronze}" in content["formatted_body"]


def test_a_slow_relay_doesnt_hold_up_the_homeservers_events(client, homeserver, relay_pool) -> None:
    import asyncio

    from tests.conftest import HS_TOKEN, settle

    q, community, alice, state, room, earlier_id = joined(client, homeserver, relay_pool)
    real = relay_pool.publish

    async def slow(event, relays, timeout=None):
        await asyncio.sleep(3)
        return await real(event, relays, timeout)

    relay_pool.publish = slow
    event = homeserver._store(room, "m.room.message", {"msgtype": "m.text", "body": "quick"}, "@q:example.org")
    started = time.monotonic()
    response = client.put("/_matrix/app/v1/transactions/slow-1", json={"events": [event]},
                          headers={"Authorization": f"Bearer {HS_TOKEN}"})
    assert response.status_code == 200 and time.monotonic() - started < 1.5
    settle(client)
    wrap = [e for e, _ in relay_pool.published if e.pubkey == community.channel().pk][-1]
    assert concord.open_wrap(wrap.to_dict(), community.channel())["content"] == "quick"
