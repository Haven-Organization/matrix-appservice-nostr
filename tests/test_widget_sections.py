"""The widget's sections: Home (browsing and importing), Profile,
Communities (including leaving), Settings and Admin."""

from __future__ import annotations

import json
import time

from bridge import concord, groups
from bridge.nostr import nip19
from tests.test_concord import FakeCommunity
from tests.test_federation import Stranger
from tests.test_groups import FakeGroup, RELAY as GROUP_RELAY
from tests.test_ids import ids_of, message, send
from tests.test_keys import run
from tests.test_keys import widget as _widget

ADMIN = "@admin:example.org"


def widget(client, homeserver, user="@q:example.org", room="!dm:example.org"):
    """tests.test_keys.widget, keeping a GET path's own query string."""
    from urllib.parse import parse_qsl, urlsplit

    call = _widget(client, homeserver, user=user, room=room)

    def wrapped(path, body=None):
        if body is not None or "?" not in path:
            return call(path, body)
        parts = urlsplit(path)
        session = client.post("/widget/api/session", json={"access_token": "tok", "matrix_server_name": "example.org"}).json()
        return client.get(parts.path, params={**dict(parse_qsl(parts.query)), "room_id": room},
                          headers={"Authorization": f"Bearer {session['session_id']}"})

    return wrapped


def _profile(who: Stranger, name: str, **extra) -> dict:
    return who.event(0, json.dumps({"name": name, "picture": "https://img.example/a.png", **extra}))


def test_the_page_and_its_shell(client) -> None:
    response = client.get("/widget")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, must-revalidate"
    for part in ('id="shell"', 'id="sidebar"', 'id="content"', 'id="toast"', 'id="scroll-top-btn"',
                 'id="mobile-menu-btn"', "--accent: #662482", "nostr-bridge-sidebar-collapsed"):
        assert part in response.text
    assert "—" not in response.text  # house style: no em dashes


def test_context_works_from_any_room_the_user_is_in(client, homeserver) -> None:
    call = widget(client, homeserver, room="!anywhere:example.org")
    context = call("/widget/api/context")
    assert context.headers["cache-control"] == "no-store"
    data = context.json()
    assert data["has_profile"] is False and data["room"] == {"kind": "other"} and data["is_admin"] is False
    assert "wss://relay.one.example" in data["relays"]
    send(client, message("[create profile"))
    [q] = ids_of(client)
    data = call("/widget/api/context").json()
    assert data["ids"][0]["handle"] == "q@example.org" and data["ids"][0]["active"]
    own = widget(client, homeserver, room=q.profile_room_id)("/widget/api/context").json()
    assert own["room"]["kind"] == "profile" and own["room"]["mine"] and own["room"]["number"] == 1


# -- Home ---------------------------------------------------------------------------------


def test_global_feed_shows_verified_posts_and_marks_imports(client, homeserver, relay_pool) -> None:
    alice = Stranger()
    relay_pool.network.append(_profile(alice, "Alice"))
    note = alice.event(1, "hello #nostr https://img.example/cat.jpg :wave:", [["emoji", "wave", "https://img.example/w.png"]])
    forged = dict(alice.event(1, "forged"), content="changed after signing")
    reply = Stranger().event(1, "nice", [["e", note["id"], "", "reply"], ["p", alice.pubkey]])
    relay_pool.network += [note, forged, reply]
    call = widget(client, homeserver)
    data = call("/widget/api/browse/feed?source=global").json()
    ids = [p["id"] for p in data["posts"]]
    assert note["id"] in ids and forged["id"] not in ids
    card = next(p for p in data["posts"] if p["id"] == note["id"])
    assert card["author"]["display_name"] == "Alice" and card["author"]["avatar_url"] == "https://img.example/a.png"
    assert card["media"] == [{"type": "image", "url": "https://img.example/cat.jpg", "full_url": "https://img.example/cat.jpg",
                              "description": "", "thumb": None}]
    assert "cat.jpg" not in card["content_html"] and 'class="hashtag"' in card["content_html"]
    assert '<img class="emoji"' in card["content_html"]
    assert card["counts"]["replies"] == 1 and card["already_imported"] is False
    assert next(p for p in data["posts"] if p["id"] == reply["id"])["in_reply_to"] == note["id"]

    imported = call("/widget/api/import", {"reference": card["nevent"]}).json()
    assert imported["ok"] and imported["room_id"] and imported["event_id"] and imported["already"] is False
    assert (imported["room_id"], "@q:example.org") in homeserver.invites
    client.app.state.__dict__.pop("widget_browse", None)
    again = call("/widget/api/browse/feed?source=global").json()
    card = next(p for p in again["posts"] if p["id"] == note["id"])
    assert card["already_imported"] and card["matrix_event_id"] == imported["event_id"]
    assert call("/widget/api/import", {"reference": "nonsense"}).status_code == 400


def test_following_feed_relay_feed_and_reposts(client, homeserver, relay_pool) -> None:
    call = widget(client, homeserver)
    assert call("/widget/api/browse/feed?source=following").json()["needs_id"] is True
    send(client, message("[create profile"))
    assert call("/widget/api/browse/feed?source=following").json()["no_follows"] is True
    alice, bob = Stranger(), Stranger()
    note = alice.event(1, "from alice")
    relay_pool.network += [note, bob.event(1, "from bob"), bob.event(6, json.dumps(note), [["e", note["id"]], ["p", alice.pubkey]])]
    send(client, message(f"[follow {nip19.npub_encode(bob.pubkey)}"))
    client.app.state.__dict__.pop("widget_browse", None)
    posts = call("/widget/api/browse/feed?source=following").json()["posts"]
    assert {p["content_html"] for p in posts} >= {"<p>from bob</p>", "<p>from alice</p>"}
    repost = next(p for p in posts if p["id"] == note["id"])
    assert repost["boosted_by"]["pubkey"] == bob.pubkey and repost["author"]["pubkey"] == alice.pubkey
    assert call("/widget/api/browse/feed?source=relay&relay=wss://relay.picked.example").status_code == 200
    assert call("/widget/api/browse/feed?source=relay&relay=https://nope.example").status_code == 400
    assert call("/widget/api/browse/feed?source=sideways").status_code == 400


def test_thread_account_and_search(client, homeserver, relay_pool) -> None:
    alice, bob = Stranger(), Stranger()
    root = alice.event(1, "root post", created_at=int(time.time()) - 60)
    reply = bob.event(1, f"a reply to nostr:{nip19.npub_encode(alice.pubkey)}", [["e", root["id"], "", "root"], ["p", alice.pubkey]])
    relay_pool.network += [root, reply, _profile(alice, "Alice", about="I post")]
    call = widget(client, homeserver)
    thread = call("/widget/api/browse/thread?ref=" + nip19.note_encode(reply["id"])).json()
    assert [a["id"] for a in thread["ancestors"]] == [root["id"]] and thread["post"]["id"] == reply["id"]
    assert f'data-pubkey="{alice.pubkey}">@Alice</a>' in thread["post"]["content_html"]
    assert [r["id"] for r in call("/widget/api/browse/thread?ref=" + root["id"]).json()["replies"]] == [reply["id"]]
    assert call("/widget/api/browse/thread?ref=" + "0" * 64).status_code == 404

    account = call("/widget/api/browse/account?pubkey=" + nip19.npub_encode(alice.pubkey)).json()
    assert account["display_name"] == "Alice" and "I post" in account["bio_html"] and account["is_following"] is False
    posts = call("/widget/api/browse/account/posts?pubkey=" + alice.pubkey).json()["posts"]
    assert [p["id"] for p in posts] == [root["id"]]
    assert call("/widget/api/browse/account?pubkey=bogus").status_code == 400

    found = call("/widget/api/browse/search?q=" + nip19.npub_encode(alice.pubkey)).json()
    assert [a["pubkey"] for a in found["accounts"]] == [alice.pubkey]
    assert call("/widget/api/browse/search?q=" + nip19.note_encode(root["id"])).json()["open"] == nip19.note_encode(root["id"])


# -- Profile ----------------------------------------------------------------------------------


def test_profile_edit_image_and_following(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    call = widget(client, homeserver)
    profile = call("/widget/api/profile").json()
    assert profile["handle"] == "q@example.org" and profile["editable"] and profile["number"] == 1
    assert call("/widget/api/profile/update", {"name": "Q!", "about": "hi", "website": "https://q.example", "lud16": "q@ln.example"}).json()["ok"]
    assert homeserver.state_of(q.profile_room_id, "m.room.name") == {"name": "Q!"}
    assert homeserver.state_of(q.profile_room_id, "m.room.topic") == {"topic": "hi"}
    extra = homeserver.state_of(q.profile_room_id, "software.haven.nostr.profile_extra")
    assert extra == {"website": "https://q.example", "lud16": "q@ln.example"}
    published = [e for e, _ in relay_pool.published if e.kind == 0 and e.pubkey == q.pubkey][-1]
    assert json.loads(published.content)["name"] == "Q!"
    assert call("/widget/api/profile").json()["fields"]["about"] == "hi"
    assert call("/widget/api/profile/update", {"website": "ftp://x"}).status_code == 400

    session = client.post("/widget/api/session", json={"access_token": "tok", "matrix_server_name": "example.org"}).json()
    image = client.post("/widget/api/profile/image", params={"room_id": "!dm:example.org", "which": "avatar"},
                        content=b"\x89PNG fake", headers={"Authorization": f"Bearer {session['session_id']}", "Content-Type": "image/png"})
    assert image.status_code == 200
    assert homeserver.state_of(q.profile_room_id, "m.room.avatar")["url"].startswith("mxc://")
    refused = client.post("/widget/api/profile/image", params={"room_id": "!dm:example.org", "which": "avatar"},
                          content=b"text", headers={"Authorization": f"Bearer {session['session_id']}", "Content-Type": "text/plain"})
    assert refused.status_code == 400

    alice = Stranger()
    send(client, message(f"[follow {nip19.npub_encode(alice.pubkey)}"))
    following = call("/widget/api/following").json()["following"]
    assert [f["pubkey"] for f in following] == [alice.pubkey]
    assert call("/widget/api/import_follows", {}).json()["ok"]


# -- Settings ---------------------------------------------------------------------------------


def test_room_recovery_and_dm_filters(client, homeserver) -> None:
    call = widget(client, homeserver)
    assert call("/widget/api/recovery_rooms").json()["profile"] == {"room_id": None, "status": "not_created"}
    send(client, message("[create profile"))
    [q] = ids_of(client)
    rooms = call("/widget/api/recovery_rooms").json()
    assert rooms["profile"] == {"room_id": q.profile_room_id, "status": "rejoin"}
    homeserver.invites.clear()
    assert call("/widget/api/rejoin_room", {"kind": "profile"}).json()["ok"]
    assert homeserver.invites == [(q.profile_room_id, "@q:example.org")]
    assert call("/widget/api/rejoin_room", {"kind": "elsewhere"}).status_code == 400

    assert call("/widget/api/dm_filters").json() == {"keywords": [], "languages": [], "require_name": False, "domains": [], "filtered": 0}
    assert call("/widget/api/dm_filters", {"keywords": ["free money"], "languages": ["en", "de"], "require_name": True,
                                           "domains": ["spam.example"]}).json()["ok"]
    assert call("/widget/api/dm_filters").json() == {"keywords": ["free money"], "languages": ["en", "de"],
                                                     "require_name": True, "domains": ["spam.example"], "filtered": 0}
    assert call("/widget/api/dm_filters", {"languages": ["english"]}).status_code == 400


# -- Communities ------------------------------------------------------------------------------


def test_leaving_a_nip29_group(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    group = FakeGroup(relay_pool)
    send(client, message(f"[join {group.naddr()}"))
    state = run(client, groups.load, client.app, GROUP_RELAY, group.id)
    call = widget(client, homeserver)
    listed = call("/widget/api/communities").json()["groups"]
    assert [(g["name"], g["as"], g["room_id"]) for g in listed] == [("Pizza Lovers", ["q@example.org"], state.room)]
    assert call("/widget/api/communities/leave", {"type": "group", "id": "nope"}).status_code == 404
    left = call("/widget/api/communities/leave", {"type": "group", "id": state.key}).json()
    assert left["message"] == "Left Pizza Lovers as q@example.org."
    [request] = relay_pool.of_kind(groups.LEAVE_REQUEST)
    assert request.pubkey == q.pubkey and ("h", group.id) in request.tags
    assert (state.room, "@q:example.org") in homeserver.kicked
    assert run(client, groups.load, client.app, GROUP_RELAY, group.id).members == []
    assert call("/widget/api/communities").json()["groups"] == []


def test_leaving_a_concord_community_with_the_command(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    community = FakeCommunity(relay_pool)
    send(client, message(f"[join {community.link()}"))
    state = run(client, concord.load, client.app, community.id)
    call = widget(client, homeserver)
    [listed] = call("/widget/api/communities").json()["concord"]
    assert listed["name"] == "PosterChan" and listed["role"] == "member" and listed["can_leave"]
    assert not listed["can_manage_links"] and [c["name"] for c in listed["channels"]] == ["general"]
    published = len(relay_pool.published)
    send(client, message("[leave PosterChan"))
    assert "Left PosterChan as q@example.org." in homeserver.sent_to("!dm:example.org")[-1]["body"]
    leaves = [e for e, _ in relay_pool.published[published:] if e.kind == concord.WRAP_KIND]
    rumors = [concord.open_wrap(e.to_dict(), state.guestbook()) for e in leaves]
    assert any(r and r["kind"] == concord.JOIN_KIND and r["content"] == "leave" and r["pubkey"] == q.pubkey for r in rumors)
    after = run(client, concord.load, client.app, community.id)
    assert q.id not in after.members and q.pubkey not in after.present
    assert (state.space, "@q:example.org") in homeserver.kicked
    assert call("/widget/api/communities").json()["concord"] == []
    send(client, message("[leave PosterChan"))
    assert "not in any" in homeserver.sent_to("!dm:example.org")[-1]["body"]


def test_owner_actions_and_the_owner_cant_leave(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    call = widget(client, homeserver)
    assert call("/widget/api/communities/create", {"name": "Bunker"}).json()["ok"]
    [listed] = call("/widget/api/communities").json()["concord"]
    assert listed["role"] == "owner" and listed["can_manage_links"] and not listed["can_leave"]
    assert call("/widget/api/communities/concord/link", {"community": listed["id"], "label": "web"}).json()["ok"]
    [link] = call("/widget/api/communities").json()["concord"][0]["links"]
    assert link["label"] == "web" and link["url"].startswith("https://armada.buzz/invite/")
    assert call("/widget/api/communities/concord/revoke", {"community": listed["id"], "number": "1"}).json()["ok"]
    assert call("/widget/api/communities").json()["concord"][0]["links"] == []
    refused = call("/widget/api/communities/leave", {"type": "concord", "id": listed["id"]})
    assert refused.status_code == 400 and "own it" in refused.json()["detail"]
    assert call("/widget/api/communities/join", {"link": ""}).status_code == 400


# -- Admin ------------------------------------------------------------------------------------


def test_admin_sections(client, homeserver) -> None:
    send(client, message("[create profile"))
    user = widget(client, homeserver)
    for path in ("/widget/api/admin/identities", "/widget/api/admin/rooms", "/widget/api/admin/backstage_space",
                 "/widget/api/admin/bans"):
        assert user(path).status_code == 403
    assert user("/widget/api/admin/backstage_space/invite", {}).status_code == 403
    assert user("/widget/api/admin/ban", {"kind": "ip", "key": "203.0.113.9"}).status_code == 403

    admin = widget(client, homeserver, user=ADMIN, room="!admin:example.org")
    [identity] = admin("/widget/api/admin/identities").json()["identities"]
    assert identity["handle"] == "q@example.org" and identity["owner_matrix_id"] == "@q:example.org"
    assert "secret" not in json.dumps(identity)
    kinds = {r["kind"] for r in admin("/widget/api/admin/rooms").json()["rooms"]}
    assert {"Profile", "Space", "Notifications"} <= kinds

    assert admin("/widget/api/admin/backstage_space").json() == {"enabled": False, "room_id": None}
    assert admin("/widget/api/admin/backstage_space/invite", {}).status_code == 409

    stranger = Stranger()
    assert admin("/widget/api/admin/ban", {"kind": "pubkey", "key": nip19.npub_encode(stranger.pubkey), "duration": "1d"}).json()["ok"]
    assert admin("/widget/api/admin/ban", {"kind": "ip", "key": "not an ip"}).status_code == 400
    [ban] = admin("/widget/api/admin/bans").json()["bans"]
    assert ban["key"] == stranger.pubkey and ban["until"]
    assert admin("/widget/api/admin/unban", {"kind": "pubkey", "key": stranger.pubkey}).json()["ok"]
    assert admin("/widget/api/admin/bans").json()["bans"] == []


def test_backstage_invite_when_enabled(repository, homeserver, relay_pool) -> None:
    from bridge.config import parse_config
    from tests.conftest import make_client, raw_config

    config = parse_config(raw_config(bridge={"backstage_space_enabled": True}))
    with make_client(config, repository, homeserver, relay_pool) as client:
        admin = widget(client, homeserver, user=ADMIN)
        space = admin("/widget/api/admin/backstage_space").json()
        assert space["enabled"] and space["room_id"]
        assert admin("/widget/api/admin/backstage_space/invite", {}).json() == {"ok": True, "room_id": space["room_id"]}
        assert (space["room_id"], ADMIN) in homeserver.invites


def test_room_and_community_actions_run_their_commands(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    call = widget(client, homeserver)
    # Joining a NIP-29 group from the widget, as "[join" does.
    group = FakeGroup(relay_pool)
    assert call("/widget/api/communities/join", {"link": group.naddr()}).json()["ok"]
    assert [g["name"] for g in call("/widget/api/communities").json()["groups"]] == ["Pizza Lovers"]
    # A community's owner makes the widget's room a channel, and invites someone.
    call("/widget/api/communities/create", {"name": "Bunker"})
    [community] = call("/widget/api/communities").json()["concord"]
    channel_room = "!channel:example.org"
    in_channel = widget(client, homeserver, room=channel_room)
    assert in_channel("/widget/api/communities/concord/channel", {"community": community["id"], "name": "lounge"}).json()["ok"]
    [listed] = call("/widget/api/communities").json()["concord"]
    assert {c["name"]: c["room_id"] for c in listed["channels"]}["lounge"] == channel_room
    stranger = Stranger()
    assert call("/widget/api/communities/concord/invite", {"community": community["id"], "who": nip19.npub_encode(stranger.pubkey)}).json()["ok"]
    assert "Invited" in homeserver.sent_to("!dm:example.org")[-1]["body"]
    # The Profile Room's actions answer in the room, as their commands do.
    in_profile = widget(client, homeserver, room=q.profile_room_id)
    assert in_profile("/widget/api/rename", {"id": "1", "new_name": "qq"}).json()["ok"]
    assert "confirm" in homeserver.sent_to(q.profile_room_id)[-1]["body"].lower()
    assert in_profile("/widget/api/rename", {"id": "1", "new_name": ""}).status_code == 400
    assert in_profile("/widget/api/delete_profile", {"id": "1"}).json()["ok"]
    assert "confirm" in homeserver.sent_to(q.profile_room_id)[-1]["body"].lower()
    before = len(homeserver.sent_to(q.profile_room_id))
    assert in_profile("/widget/api/unlink", {"id": "1"}).json()["ok"]
    assert len(homeserver.sent_to(q.profile_room_id)) > before  # it answers (asks) in the room
