"""NIP-29 relay-based groups."""

from __future__ import annotations

import time

from bridge import groups
from bridge.nostr import nip19
from tests.test_federation import Stranger, post, run
from tests.test_ids import ids_of, message, send

RELAY = "wss://groups.example"


class FakeGroup:
    """A group as a NIP-29 relay keeps one: metadata, admins and members
    signed by the relay's key, and members' messages."""

    def __init__(self, relay_pool, group_id="pizza", closed=False, private=False):
        self.relay = Stranger()
        self.admin = Stranger()
        self.alice = Stranger()
        self.id = group_id
        self.pool = relay_pool
        flags = [["closed"]] if closed else []
        flags += [["private"]] if private else []
        self.meta(["name", "Pizza Lovers"], ["about", "for pizza"], *flags)
        self.pool.network.append(self.relay.event(groups.ADMINS, "", [["d", group_id], ["p", self.admin.pubkey, "admin"]]))
        self.pool.network.append(self.relay.event(groups.MEMBERS, "", [["d", group_id], ["p", self.admin.pubkey],
                                                                      ["p", self.alice.pubkey]]))

    def meta(self, *tags, created_at=None):
        event = self.relay.event(groups.METADATA, "", [["d", self.id], *tags], created_at=created_at)
        self.pool.network.append(event)
        return event

    def say(self, who, kind, text, tags=()):
        return who.event(kind, text, [["h", self.id], *tags])

    def naddr(self) -> str:
        return nip19.naddr_encode(nip19.Address(self.id, self.relay.pubkey, groups.METADATA, (RELAY,)))


def joined(client, relay_pool, **kwargs):
    send(client, message("[create profile"))
    [q] = ids_of(client)
    group = FakeGroup(relay_pool, **kwargs)
    earlier = group.say(group.alice, groups.CHAT, "first slice")
    relay_pool.network.append(earlier)
    send(client, message(f"[join {group.naddr()}?invite=abc"))
    state = run(client, groups.load, client.app, RELAY, group.id)
    return q, group, state, earlier


def test_references() -> None:
    assert groups.parse_reference("groups.0xchat.com'abc123") == ("wss://groups.0xchat.com", "abc123", None)
    assert groups.parse_reference("wss://relay.example/path'x_y") == ("wss://relay.example/path", "x_y", None)
    address = nip19.naddr_encode(nip19.Address("g1", "ab" * 32, groups.METADATA, ("wss://r.example",)))
    assert groups.parse_reference(f"https://chachi.chat/{address}?invite=XYZ") == ("wss://r.example", "g1", "XYZ")
    assert groups.parse_reference("https://armada.buzz/invite/naddr1qqqq#abc") is None
    assert groups.parse_reference("hello") is None


def test_join_makes_a_room_in_the_relays_space(client, homeserver, relay_pool) -> None:
    q, group, state, earlier = joined(client, relay_pool)
    [request] = relay_pool.of_kind(groups.JOIN_REQUEST)
    assert request.pubkey == q.pubkey and ("h", "pizza") in request.tags and ("code", "abc") in request.tags
    assert state.name == "Pizza Lovers" and state.members == [q.id] and state.relay_pubkey == group.relay.pubkey
    room = next(r for r in homeserver.created if r["room_id"] == state.room)
    space = next(r for r in homeserver.created if r["room_type"] == "m.space")
    assert room["name"] == "Pizza Lovers" and (state.room, "@q:example.org") in homeserver.invites
    assert (space["room_id"], "@q:example.org") in homeserver.invites
    shown = homeserver.messages(state.room)[-1]
    assert shown["content"]["body"] == "first slice" and shown["sender"].startswith("@nostr_")
    levels = homeserver.state_of(state.room, "m.room.power_levels")["users"]
    from bridge.remote import ghost_user_id

    assert levels[ghost_user_id(client.app, group.admin.pubkey)] == 50
    bodies = [c["body"] for c in homeserver.sent_to("!dm:example.org")]
    assert any(b.startswith("Joined Pizza Lovers on groups.example as q@example.org") for b in bodies)
    run(client, client.app.state.feeds._desired)  # subscribes without error
    desired = run(client, groups.desired, client.app)
    [(relay, name)] = desired
    assert relay == RELAY and desired[(relay, name)][0]["_auth"] == q.pubkey


def test_messages_both_ways(client, homeserver, relay_pool) -> None:
    q, group, state, earlier = joined(client, relay_pool)
    room = state.room
    run(client, groups.receive, client.app, RELAY, group.say(group.alice, groups.CHAT, "second slice"))
    shown = homeserver.messages(room)[-1]
    sent = post(client, homeserver, room, {"msgtype": "m.text", "body": "> <@x> second slice\n\nyum",
                                           "m.relates_to": {"m.in_reply_to": {"event_id": shown["event_id"]}}})
    reply = relay_pool.of_kind(groups.CHAT)[-1]
    assert reply.pubkey == q.pubkey and reply.content == "yum" and ("h", "pizza") in reply.tags
    assert any(t[0] == "q" and t[2] == RELAY for t in reply.tags)
    assert any(t[0] == "previous" for t in reply.tags)
    # The echo isn't shown again; replies from Nostr are replies here.
    count = len(homeserver.messages(room))
    run(client, groups.receive, client.app, RELAY, reply.to_dict())
    assert len(homeserver.messages(room)) == count
    answer = group.say(group.alice, groups.CHAT, "glad you like it", [["q", reply.id, RELAY, q.pubkey]])
    run(client, groups.receive, client.app, RELAY, answer)
    assert homeserver.messages(room)[-1]["content"]["m.relates_to"] == {"m.in_reply_to": {"event_id": sent["event_id"]}}
    # Reactions and deletions, both ways.
    run(client, groups.receive, client.app, RELAY, group.say(group.alice, groups.REACTION, "🍕", [["e", reply.id], ["p", q.pubkey]]))
    assert homeserver.messages(room, "m.reaction")[-1]["content"]["m.relates_to"]["key"] == "🍕"
    from tests.test_federation import transact

    transact(client, homeserver._store(room, "m.reaction", {"m.relates_to": {
        "rel_type": "m.annotation", "event_id": shown["event_id"], "key": "👍"}}, "@q:example.org"))
    like = relay_pool.of_kind(groups.REACTION)[-1]
    assert like.content == "+" and ("h", "pizza") in like.tags
    transact(client, {"type": "m.room.redaction", "event_id": "$red", "room_id": room, "sender": "@q:example.org",
                      "redacts": sent["event_id"], "content": {}})
    assert ("e", reply.id) in relay_pool.of_kind(groups.DELETION)[-1].tags


def test_admins_moderate_and_the_relay_renames(client, homeserver, relay_pool) -> None:
    q, group, state, earlier = joined(client, relay_pool)
    room = state.room
    shown = homeserver.messages(room)[-1]
    # Only an admin's delete-event counts.
    run(client, groups.receive, client.app, RELAY, group.say(group.alice, groups.DELETE_EVENT, "", [["e", earlier["id"]]]))
    assert shown["event_id"] not in {e for _, e, _ in homeserver.redactions}
    run(client, groups.receive, client.app, RELAY, group.say(group.admin, groups.DELETE_EVENT, "", [["e", earlier["id"]]]))
    assert shown["event_id"] in {e for _, e, _ in homeserver.redactions}
    # The relay renames the group.
    run(client, groups.receive, client.app, RELAY, group.meta(["name", "Pasta Lovers"], created_at=int(time.time()) + 5))
    assert homeserver.state_of(room, "m.room.name") == {"name": "Pasta Lovers"}
    # A rename signed by anyone else isn't believed.
    forged = group.alice.event(groups.METADATA, "", [["d", "pizza"], ["name", "Hacked"]], created_at=int(time.time()) + 9)
    run(client, groups.receive, client.app, RELAY, forged)
    assert homeserver.state_of(room, "m.room.name") == {"name": "Pasta Lovers"}
    # An admin removing q takes q out of the room.
    run(client, groups.receive, client.app, RELAY, group.say(group.admin, groups.REMOVE_USER, "", [["p", q.pubkey]]))
    assert (room, "@q:example.org") in homeserver.kicked
    assert run(client, groups.load, client.app, RELAY, "pizza").members == []


def test_a_closed_group_needs_an_invite(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    group = FakeGroup(relay_pool, closed=True)
    relay_pool.answers[RELAY] = (False, "restricted: this group is closed")
    send(client, message(f"[join groups.example'{group.id}"))
    assert run(client, groups.load, client.app, RELAY, group.id) is None
    assert "is closed" in homeserver.sent_to("!dm:example.org")[-1]["body"]
