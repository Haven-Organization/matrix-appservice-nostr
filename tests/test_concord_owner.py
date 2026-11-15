"""Owning a Concord community made here (bridge.concord_owner): creating
it, attaching a room as a channel, invite links and direct invites."""

from __future__ import annotations

import base64
import json

from bridge import concord, concord_owner
from bridge.nostr import nip19, nip44, nip59
from bridge.remote import ghost_user_id
from tests.test_federation import Stranger, run
from tests.test_ids import ids_of, message, send


def _made(client, homeserver, relay_pool, name="Bunker"):
    send(client, message("[create profile"))
    [q] = ids_of(client)
    send(client, message(f"[concord create {name}"))
    [community] = run(client, concord.all_communities, client.app)
    return q, community


def _editions(relay_pool, community):
    """Every Control edition published for the community: (rumor, seal)."""
    read = community.control()
    out = []
    for event, _relays in relay_pool.published:
        if event.kind == concord.WRAP_KIND and event.pubkey == community.control_pk:
            seal = json.loads(nip44.decrypt(event.content, read.conv))
            out.append((json.loads(seal["content"]), seal))
    return out


def _tags(rumor):
    return {t[0]: t[1:] for t in rumor["tags"]}


def _to_network(relay_pool):
    for event, _relays in relay_pool.published:
        if event.to_dict() not in relay_pool.network:
            relay_pool.network.append(event.to_dict())


def test_create_publishes_a_genesis_others_can_read(client, homeserver, relay_pool) -> None:
    q, community = _made(client, homeserver, relay_pool)
    assert community.owner == q.pubkey and community.control_root and community.owner_salt
    assert concord.community_id(q.pubkey, community.owner_salt) == community.id
    # The Control Plane's address is the staff signer, not the read key.
    assert community.control_pk == concord_owner.control_signer(community).pk != community.control().pk
    editions = _editions(relay_pool, community)
    assert [_tags(r)["vsk"][0] for r, _ in editions] == ["0", "2", "13"]
    meta, general, everyone = (r for r, _ in editions)
    assert all(seal["kind"] == concord.SEAL_PLAINTEXT and seal["pubkey"] == q.pubkey for _, seal in editions)
    assert _tags(meta)["eid"] == [community.id] and json.loads(meta["content"])["name"] == "Bunker"
    assert "eh" not in _tags(meta) and "eh" not in _tags(general)  # genesis: before Roles v2
    assert json.loads(general["content"]) == {"name": "general", "private": False}
    # @everyone switches the community to Roles v2: marked, bound to it.
    tags = _tags(everyone)
    assert tags["eid"] == [concord_owner.locator(community.id, "concord/everyone")]
    assert tags["eh"] == ["2"] and tags["eb"] == [concord_owner.locator(community.id, "concord/edition-binding")]
    assert json.loads(everyone["content"]) == {"permissions": "541057024", "legacy": [_tags(general)["eid"][0]]}
    # A Space and #general, the owner invited; the owner's Join announced.
    room = community.channels[_tags(general)["eid"][0]]["room"]
    assert (community.space, "@q:example.org") in homeserver.invites and (room, "@q:example.org") in homeserver.invites
    joins = [e for e, _ in relay_pool.published if e.pubkey == community.guestbook().pk]
    assert concord.open_wrap(joins[0].to_dict(), community.guestbook())["content"] == "join"
    # A reader holding only what an invite carries folds the same community.
    fresh = concord.Community(id=community.id, owner=q.pubkey, root=community.root, epoch=0,
                              control_pk=community.control_pk, relays=community.relays, name="?", description="",
                              picture="", channels={}, space=None, members=[])
    concord.fold_control(fresh, [concord.open_wrap(e.to_dict(), community.control()) for e, _ in relay_pool.published
                                 if e.pubkey == community.control_pk])
    assert fresh.name == "Bunker" and [c["name"] for c in fresh.channels.values()] == ["general"]


def test_create_lists_it_for_the_owners_other_clients(client, homeserver, relay_pool) -> None:
    q, community = _made(client, homeserver, relay_pool)
    [listed] = [e for e, _ in relay_pool.published if e.kind == concord_owner.COMMUNITY_LIST_KIND]
    secret = run(client, client.app.state.signer.secret_key, q.pubkey)
    plain = json.loads(nip44.decrypt(listed.content, nip44.get_conversation_key(secret, q.pubkey)))
    assert ["d", "0"] in [list(t) for t in listed.tags] and plain["frags"] == 1
    [entry] = plain["entries"]

    def b64(hex_value):
        return base64.urlsafe_b64encode(bytes.fromhex(hex_value)).decode().rstrip("=")

    assert entry["community_id"] == b64(community.id)
    current = entry["current"]
    assert current["community_root"] == b64(community.root) and current["control_root"] == b64(community.control_root)
    assert current["control_pk"] == b64(community.control_pk) and current["name"] == "Bunker"


def test_an_existing_room_becomes_a_channel(client, homeserver, relay_pool) -> None:
    q, community = _made(client, homeserver, relay_pool)
    room = "!lounge:example.org"
    homeserver._store(room, "m.room.name", {"name": "Our lounge"}, "@q:example.org", "")
    homeserver._room(room)["members"].update({"@q:example.org", "@bot:example.org"})
    send(client, message("[concord channel memes", room=room))
    community = run(client, concord.load, client.app, community.id)
    [(channel_id, info)] = [(c, i) for c, i in community.channels.items() if i.get("room") == room]
    assert info["name"] == "memes" and info["attached"]
    rumor, _ = _editions(relay_pool, community)[-1]
    tags = _tags(rumor)
    assert tags["vsk"] == ["2"] and tags["eid"] == [channel_id] and tags["ev"] == ["1"] and tags["eh"] == ["2"]
    assert json.loads(rumor["content"]) == {"name": "memes", "private": False}
    # In the Space; the room keeps its name and join rules.
    assert homeserver.state_of(room, "m.room.name") == {"name": "Our lounge"}
    assert homeserver.state_of(room, "m.room.join_rules") is None
    assert homeserver._room(community.space)["state"][("m.space.child", room)]["content"]["via"]
    # What's said there goes to the channel.
    send(client, message("hello channel", room=room))
    key = community.channel_key(channel_id)
    [wrap] = [e for e, _ in relay_pool.published if e.pubkey == key.pk]
    said = concord.open_wrap(wrap.to_dict(), key)
    assert said["content"] == "hello channel" and ["channel", channel_id] in said["tags"]


def test_a_room_already_bridged_isnt_taken(client, homeserver, relay_pool) -> None:
    q, community = _made(client, homeserver, relay_pool)
    general_room = next(i["room"] for i in community.channels.values())
    send(client, message("[concord channel again", room=general_room))
    assert "already a Concord channel room" in homeserver.sent_to(general_room)[-1]["body"]


def test_a_link_opens_and_can_be_retired(client, homeserver, relay_pool) -> None:
    q, community = _made(client, homeserver, relay_pool)
    send(client, message("[concord link Reddit"))
    url = homeserver.sent_to("!dm:example.org")[-1]["body"].split("\n")[1]
    assert url.startswith("https://armada.buzz/invite/naddr1")
    _to_network(relay_pool)
    invite = concord.parse_invite(url)
    bundle = run(client, concord.fetch_bundle, client.app, invite)
    assert bundle["community_id"] == community.id and bundle["community_root"] == community.root
    assert bundle["control_pk"] == community.control_pk and bundle["label"] == "Reddit"
    assert [c["name"] for c in bundle["channels"]] == ["general"] and bundle["channels"][0]["key"] == community.root
    # Recorded in the owner's Invite List, and listed in the Registry.
    secret = run(client, client.app.state.signer.secret_key, q.pubkey)
    [listed] = [e for e, _ in relay_pool.published if e.kind == concord_owner.INVITE_LIST_KIND]
    plain = json.loads(nip44.decrypt(listed.content, nip44.get_conversation_key(secret, q.pubkey)))
    assert [e["url"] for e in plain["entries"]] == [url]
    community = run(client, concord.load, client.app, community.id)
    registry, _ = _editions(relay_pool, community)[-1]
    assert _tags(registry)["vsk"] == ["8"] and json.loads(registry["content"]) == [invite.signer]

    send(client, message("[concord revoke 1"))
    assert "Retired" in homeserver.sent_to("!dm:example.org")[-1]["body"]
    _to_network(relay_pool)
    try:
        run(client, concord.fetch_bundle, client.app, invite)
        raise AssertionError("a revoked link still opened")
    except concord.ConcordProblem as exc:
        assert "revoked" in str(exc)
    community = run(client, concord.load, client.app, community.id)
    registry, _ = _editions(relay_pool, community)[-1]
    assert json.loads(registry["content"]) == [] and _tags(registry)["ev"] == ["2"]
    assert _tags(registry)["ep"]  # chained on the first


def _invites_for(relay_pool, stranger):
    out = []
    for event, _relays in relay_pool.published:
        if event.kind == 1059 and ["k", "3313"] in [list(t) for t in event.tags]:
            if ["p", stranger.pubkey] in [list(t) for t in event.tags]:
                out.append(nip59.unwrap(event.to_dict(), stranger.secret))
    return out


def test_a_direct_invite_by_npub(client, homeserver, relay_pool) -> None:
    q, community = _made(client, homeserver, relay_pool)
    alice = Stranger()
    send(client, message(f"[concord invite {nip19.npub_encode(alice.pubkey)}"))
    assert "Invited" in homeserver.sent_to("!dm:example.org")[-1]["body"]
    [opened] = _invites_for(relay_pool, alice)
    assert opened.sender == q.pubkey and opened.rumor["kind"] == 3313
    bundle = json.loads(opened.rumor["content"])
    assert bundle["community_id"] == community.id and bundle["creator_npub"] == q.pubkey
    assert concord.community_id(bundle["owner"], bundle["owner_salt"]) == community.id


def test_inviting_a_ghost_in_matrix_sends_the_invite(client, homeserver, relay_pool) -> None:
    q, community = _made(client, homeserver, relay_pool)
    alice = Stranger()
    ghost = ghost_user_id(client.app, alice.pubkey)
    send(client, {"type": "m.room.member", "event_id": "$inv1", "room_id": community.space, "sender": "@q:example.org",
                  "state_key": ghost, "content": {"membership": "invite"}})
    [opened] = _invites_for(relay_pool, alice)
    assert json.loads(opened.rumor["content"])["community_id"] == community.id
    assert "Sent" in homeserver.sent_to(community.space)[-1]["body"]
    # By a pill to the ghost, too.
    pill = {"msgtype": "m.text", "body": "[concord invite Alice",
            "format": "org.matrix.custom.html",
            "formatted_body": f'[concord invite <a href="https://matrix.to/#/{ghost}">Alice</a>'}
    send(client, {**message(""), "content": pill})
    assert len(_invites_for(relay_pool, alice)) == 2


def test_only_the_owner_makes_links(client, homeserver, relay_pool) -> None:
    _made(client, homeserver, relay_pool)
    send(client, message("[create profile", sender="@mallory:example.org", room="!mdm:example.org"))
    send(client, message("[concord link", sender="@mallory:example.org", room="!mdm:example.org"))
    assert "don't own a community" in homeserver.sent_to("!mdm:example.org")[-1]["body"]


def test_an_admin_adds_a_channel_to_someone_elses_community(client, homeserver, relay_pool) -> None:
    """A legacy community (no staff key: members hold the plane's signer),
    where the owner granted q a role with MANAGE_CHANNELS: q's channel
    edition cites that Grant."""
    import secrets as _secrets

    from tests.test_concord import joined

    q, fake, alice, state, room, _ = joined(client, homeserver, relay_pool)
    role = _secrets.token_hex(32)
    fake.edition("1", role, {"role_id": role, "name": "Admin", "position": 1, "permissions": "2943",
                             "scope": {"kind": "server"}})
    grant_eid = concord.hkdf(bytes.fromhex(fake.id), "concord/grant", bytes.fromhex(q.pubkey)).hex()
    grant_content = {"member": q.pubkey, "role_ids": [role]}
    fake.edition("3", grant_eid, grant_content)
    run(client, concord.refresh_all, client.app)
    lounge = "!lounge:example.org"
    homeserver._room(lounge)["members"].add("@q:example.org")
    send(client, message("[concord channel lounge", room=lounge))
    community = run(client, concord.load, client.app, fake.id)
    [(channel_id, _info)] = [(c, i) for c, i in community.channels.items() if i.get("room") == lounge]
    mine = [e for e, _ in relay_pool.published if e.pubkey == fake.control().pk]
    rumor = concord.open_wrap(mine[-1].to_dict(), fake.control())
    tags = _tags(rumor)
    assert rumor["pubkey"] == q.pubkey and tags["eid"] == [channel_id] and "eh" not in tags  # legacy: unmarked
    expected = concord_owner.edition_hash(grant_eid, 1, None, json.dumps(grant_content), None)
    assert tags["vac"] == [grant_eid, "1", expected]
    # The bridge folds it back as a channel by an authorized member.
    concord.fold_control(community, [concord.open_wrap(w, fake.control()) for w in relay_pool.network
                                     if w.get("pubkey") == fake.control().pk] + [rumor])
    assert community.channels[channel_id]["name"] == "lounge"


def test_a_member_without_the_role_cant(client, homeserver, relay_pool) -> None:
    from tests.test_concord import joined

    joined(client, homeserver, relay_pool)
    send(client, message("[concord channel lounge", room="!lounge:example.org"))
    assert "don't manage the channels" in homeserver.sent_to("!lounge:example.org")[-1]["body"]


def test_the_channels_echo_doesnt_make_a_second_room(client, homeserver, relay_pool) -> None:
    """The relays echo a new channel's edition at once; reading it then
    must find the channel already given the room."""
    q, community = _made(client, homeserver, relay_pool)
    room = "!lounge:example.org"
    homeserver._room(room)["members"].add("@q:example.org")
    created = len(homeserver.created)

    def echo(event, relays):
        if event.pubkey == community.control_pk:
            relay_pool.network.append(event.to_dict())
            client.portal.call(concord.receive, client.app, event.to_dict())

    relay_pool.publish_hooks.append(echo)
    send(client, message("[concord channel memes", room=room))
    relay_pool.publish_hooks.remove(echo)
    assert len(homeserver.created) == created  # no new room
    community = run(client, concord.load, client.app, community.id)
    assert [i["room"] for i in community.channels.values() if i["name"] == "memes"] == [room]


def test_rename_a_channel(client, homeserver, relay_pool) -> None:
    q, community = _made(client, homeserver, relay_pool)
    room = "!lounge:example.org"
    homeserver._store(room, "m.room.name", {"name": "Our lounge"}, "@q:example.org", "")
    homeserver._room(room)["members"].add("@q:example.org")
    send(client, message("[concord channel grift", room=room))
    _to_network(relay_pool)
    send(client, message("[concord rename Grift", room=room))
    assert "Renamed #grift to #Grift" in homeserver.sent_to(room)[-1]["body"]
    community = run(client, concord.load, client.app, community.id)
    first, renamed = [r for r, _ in _editions(relay_pool, community) if _tags(r)["vsk"] == ["2"]][-2:]
    assert _tags(renamed)["ev"] == ["2"] and json.loads(renamed["content"]) == {"name": "Grift", "private": False}
    assert _tags(renamed)["ep"] == [concord_owner.edition_hash(_tags(first)["eid"][0], 1, None, first["content"],
                                                               q.pubkey)]
    assert homeserver.state_of(room, "m.room.name") == {"name": "Our lounge"}
    assert [i["name"] for i in community.channels.values() if i.get("room") == room] == ["Grift"]


def test_an_attached_rooms_topic_is_the_channels(client, homeserver, relay_pool) -> None:
    from tests.test_federation import transact

    q, community = _made(client, homeserver, relay_pool)
    room = "!lounge:example.org"
    homeserver._store(room, "m.room.topic", {"topic": "Lolcows and grifters"}, "@q:example.org", "")
    homeserver._room(room)["members"].add("@q:example.org")
    send(client, message("[concord channel grift", room=room))
    community = run(client, concord.load, client.app, community.id)
    first = _editions(relay_pool, community)[-1][0]
    assert json.loads(first["content"]) == {"name": "grift", "private": False, "topic": "Lolcows and grifters"}
    _to_network(relay_pool)
    # Changed in Matrix: the channel follows, keeping the rest.
    transact(client, homeserver._store(room, "m.room.topic", {"topic": "Grifters only"}, "@q:example.org", ""))
    changed = _editions(relay_pool, community)[-1][0]
    assert json.loads(changed["content"]) == {"name": "grift", "private": False, "topic": "Grifters only"}
    assert _tags(changed)["ev"] == ["2"]
    # By someone who can't manage channels: nothing.
    count = len(_editions(relay_pool, community))
    transact(client, homeserver._store(room, "m.room.topic", {"topic": "spam"}, "@mallory:example.org", ""))
    assert len(_editions(relay_pool, community)) == count
    # [concord topic sends the room's topic as it is now (it did change).
    _to_network(relay_pool)
    send(client, message("[concord topic", room=room))
    assert json.loads(_editions(relay_pool, community)[-1][0]["content"])["topic"] == "spam"
    assert "now has this room's topic" in homeserver.sent_to(room)[-1]["body"]
    # Again, unchanged: nothing new is written.
    _to_network(relay_pool)
    count = len(_editions(relay_pool, community))
    send(client, message("[concord topic", room=room))
    assert len(_editions(relay_pool, community)) == count
