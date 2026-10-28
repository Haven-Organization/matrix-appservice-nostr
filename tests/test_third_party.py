"""Shadow IDs and third-party access ([allow / [disallow / [allowed), as
matrix-appservice-activitypub has them."""

from __future__ import annotations

import json

from bridge.shadow_ids import sync_shadow_profiles, third_party_name
from tests.test_federation import _followed_post, command, run, transact

ADMIN = "@admin:example.org"
ALICE = "@alice:other.org"


def _bodies(homeserver, room="!dm:example.org"):
    return [c.get("body", "") for c in homeserver.sent_to(room)]


def _ids(client, user):
    return run(client, client.app.state.repository.list_ids_for_user, user)


def _react(client, homeserver, room, target, sender, key="👍"):
    transact(client, homeserver._store(room, "m.reaction", {"m.relates_to": {
        "rel_type": "m.annotation", "event_id": target, "key": key}}, sender))


def _profile(relay_pool, pubkey):
    return json.loads([e for e, _ in relay_pool.published if e.kind == 0 and e.pubkey == pubkey][-1].content)


def test_third_party_names_never_collide() -> None:
    assert third_party_name("@alice:matrix.org") == "alice_matrix.org"
    assert third_party_name("@a_b:c.org") != third_party_name("@a:b_c.org")
    assert third_party_name("@A=b:x.org") == "a-b_x.org"


def test_a_local_users_first_reaction_makes_a_shadow_id(client, homeserver, relay_pool) -> None:
    _q, _alice, _raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    homeserver.profiles["@bob:example.org"] = {"displayname": "Bob B"}
    _react(client, homeserver, remote.room_id, mirrored["event_id"], "@bob:example.org")
    [bob] = _ids(client, "@bob:example.org")
    assert bob.name == "bob" and bob.profile_room_id is None and bob.room_unlinked
    assert any("shadow Nostr identity" in b and '"[create profile"' in b for b in _bodies(homeserver, remote.room_id))
    assert [e for e, _ in relay_pool.published if e.kind == 7 and e.pubkey == bob.pubkey]
    # Its profile is Bob's Matrix profile, and follows it.
    assert _profile(relay_pool, bob.pubkey) == {"name": "Bob B", "display_name": "Bob B",
                                                "about": "@bob:example.org is a Matrix user.", "nip05": "bob@example.org"}
    homeserver.profiles["@bob:example.org"] = {"displayname": "Robert"}
    run(client, sync_shadow_profiles, client.app)
    assert _profile(relay_pool, bob.pubkey)["name"] == "Robert"
    # The relay can rebuild it.
    kind0 = [e for e, _ in relay_pool.published if e.kind == 0 and e.pubkey == bob.pubkey][-1]
    from bridge.event_sources import rebuild

    row = run(client, client.app.state.repository.get_event, kind0.id)
    assert run(client, rebuild, client.app, row).event["content"] == kind0.content
    # [create profile upgrades the same ID in place.
    command(client, "[create profile", sender="@bob:example.org")
    [upgraded] = _ids(client, "@bob:example.org")
    assert upgraded.id == bob.id and upgraded.pubkey == bob.pubkey and upgraded.profile_room_id
    assert _profile(relay_pool, bob.pubkey) == {"name": "Robert", "display_name": "Robert", "nip05": "bob@example.org"}


def test_strangers_from_other_homeservers_get_nothing(client, homeserver, relay_pool) -> None:
    _q, _alice, _raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    command(client, "[follow npub1xyz", sender=ALICE)
    assert "Commands are only available to users on example.org." in _bodies(homeserver)[-1]
    before = len(relay_pool.published)
    _react(client, homeserver, remote.room_id, mirrored["event_id"], ALICE)
    assert len(relay_pool.published) == before and _ids(client, ALICE) == []


def test_only_admins_manage_grants(client, homeserver) -> None:
    command(client, f"[allow mxid {ALICE}", sender="@q:example.org")
    assert "Only a bridge admin" in _bodies(homeserver)[-1]
    command(client, "[allowed", sender="@q:example.org")
    assert "Only a bridge admin" in _bodies(homeserver)[-1]


def test_a_shadow_grant(client, homeserver, relay_pool) -> None:
    _q, alice_npub, _raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    homeserver.profiles[ALICE] = {"displayname": "Alice Elsewhere"}
    command(client, f"[allow mxid {ALICE}", sender=ADMIN)
    assert f"Allowed {ALICE} to use this bridge at shadow tier." in _bodies(homeserver)[-1]
    # Following makes their shadow ID.
    command(client, f"[follow {alice_npub.pubkey}", sender=ALICE)
    [shadow] = _ids(client, ALICE)
    assert shadow.name == "alice_other.org" and shadow.profile_room_id is None
    [told] = [b for b in _bodies(homeserver) if "shadow Nostr identity for you: alice_other.org@example.org" in b]
    assert "create profile" not in told  # shadow tier: not hers to run
    assert _profile(relay_pool, shadow.pubkey)["name"] == "Alice Elsewhere"
    # Reactions go out; making a profile or DMing doesn't.
    _react(client, homeserver, remote.room_id, mirrored["event_id"], ALICE)
    assert [e for e, _ in relay_pool.published if e.kind == 7 and e.pubkey == shadow.pubkey]
    command(client, "[create profile", sender=ALICE)
    assert _bodies(homeserver)[-1] == '"[create profile" is disabled for third-party accounts at shadow tier.'
    command(client, "[dm", sender=ALICE)
    assert _bodies(homeserver)[-1] == '"[dm" is disabled for third-party accounts at shadow tier.'
    assert _ids(client, ALICE)[0].profile_room_id is None


def test_a_full_grant_and_parking(client, homeserver, relay_pool) -> None:
    command(client, f"[allow mxid {ALICE} full", sender=ADMIN)
    command(client, "[create profile", sender=ALICE)
    [alice] = _ids(client, ALICE)
    assert alice.name == "alice_other.org" and alice.profile_room_id
    transact(client, homeserver._store(alice.profile_room_id, "m.room.message", {"msgtype": "m.text", "body": "one"}, ALICE))
    assert [e for e, _ in relay_pool.published if e.kind == 1 and e.pubkey == alice.pubkey]
    # Down to shadow: the room is parked, its posts stay in Matrix, and the
    # profile mirrors Matrix again.
    homeserver.profiles[ALICE] = {"displayname": "Alice M"}
    command(client, f"[allow mxid {ALICE} shadow", sender=ADMIN)
    before = len([e for e, _ in relay_pool.published if e.kind == 1])
    transact(client, homeserver._store(alice.profile_room_id, "m.room.message", {"msgtype": "m.text", "body": "two"}, ALICE))
    assert len([e for e, _ in relay_pool.published if e.kind == 1]) == before
    run(client, sync_shadow_profiles, client.app)
    assert _profile(relay_pool, alice.pubkey)["name"] == "Alice M"
    assert _ids(client, ALICE)[0].profile_room_id == alice.profile_room_id  # nothing deleted


def test_an_mxid_ban_beats_its_homeserver(client, homeserver) -> None:
    command(client, "[allow homeserver other.org full", sender=ADMIN)
    warning = homeserver.messages("!dm:example.org")[-1]
    assert "EVERY user on other.org" in warning["content"]["body"]
    # Only an admin's "confirm" counts.
    transact(client, homeserver._store("!dm:example.org", "m.room.message", {
        "msgtype": "m.text", "body": "confirm", "m.relates_to": {"m.in_reply_to": {"event_id": warning["event_id"]}}}, ADMIN))
    assert "Allowed every user on other.org to use this bridge at full tier." in _bodies(homeserver)[-1]
    command(client, f"[disallow mxid {ALICE}", sender=ADMIN)
    command(client, "[status", sender=ALICE)
    assert "Commands are only available" in _bodies(homeserver)[-1]
    command(client, "[status", sender="@carol:other.org")
    assert "Your Nostr IDs" in _bodies(homeserver)[-1] or "don't have a Nostr ID" in _bodies(homeserver)[-1]
    command(client, "[allowed", sender=ADMIN)
    listing = _bodies(homeserver)[-1]
    assert f"- {ALICE}: banned" in listing and "- other.org: full" in listing


def test_a_room_grant_covers_that_rooms_reactions_only(client, homeserver, relay_pool) -> None:
    _q, alice_npub, _raw, remote, mirrored = _followed_post(client, homeserver, relay_pool)
    command(client, "[allow room", sender=ADMIN, room=remote.room_id)
    assert f"Allowed any third party to post/react/reply from {remote.room_id}" in _bodies(homeserver, remote.room_id)[-1]
    _react(client, homeserver, remote.room_id, mirrored["event_id"], ALICE)
    [shadow] = _ids(client, ALICE)
    assert [e for e, _ in relay_pool.published if e.kind == 7 and e.pubkey == shadow.pubkey]
    # Not commands, even there.
    command(client, f"[follow {alice_npub.pubkey}", sender=ALICE, room=remote.room_id)
    assert "Commands are only available" in _bodies(homeserver, remote.room_id)[-1]
    # Taken away: nothing more goes out, and nothing is deleted.
    command(client, f"[disallow room {remote.room_id}", sender=ADMIN)
    before = len(relay_pool.published)
    _react(client, homeserver, remote.room_id, mirrored["event_id"], ALICE, key="🔥")
    assert len(relay_pool.published) == before and _ids(client, ALICE)


def test_a_bare_room_grant_needs_a_bridged_room(client, homeserver) -> None:
    command(client, "[allow room", sender=ADMIN)
    assert "This room isn't bridged as anything" in _bodies(homeserver)[-1]
    homeserver.aliases["#lobby:example.org"] = "!lobby:example.org"
    command(client, "[allow room #lobby:example.org", sender=ADMIN)
    assert "from !lobby:example.org" in _bodies(homeserver)[-1]
    assert "the bot isn't currently in this room" in _bodies(homeserver)[-1]


def test_access_switched_off(config, repository, homeserver, relay_pool) -> None:
    from tests.conftest import make_client

    object.__setattr__(config.bridge, "third_party_access", False)
    with make_client(config, repository, homeserver, relay_pool) as client:
        command(client, f"[allow mxid {ALICE} full", sender=ADMIN)
        command(client, "[create profile", sender=ALICE)
        assert _bodies(homeserver)[-1] == "Third-party access is currently disabled."
        assert _ids(client, ALICE) == []


def test_the_widgets_access_card(client, homeserver) -> None:
    from tests.test_keys import widget

    call = widget(client, homeserver, user=ADMIN)
    assert call("/widget/api/context").json()["admin"] is True
    assert call("/widget/api/admin/allow", {"rule_type": "mxid", "value": ALICE, "tier": "full"}).status_code == 200
    grants = call("/widget/api/admin/grants").json()
    assert grants["third_party_access"] is True
    assert [(g["rule_type"], g["value"], g["tier"]) for g in grants["grants"]] == [("mxid", ALICE, "full")]
    assert call("/widget/api/admin/disallow", {"rule_type": "mxid", "value": ALICE}).status_code == 200
    assert call("/widget/api/admin/grants").json()["grants"][0]["tier"] == "banned"
    assert call("/widget/api/admin/allow", {"rule_type": "nonsense", "value": "x"}).status_code == 400
    q = widget(client, homeserver)
    assert q("/widget/api/admin/grants").status_code == 403
    assert q("/widget/api/admin/allow", {"rule_type": "mxid", "value": ALICE}).status_code == 403
