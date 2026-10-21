"""Step 2: Nostr IDs, Profile Rooms, and the identity events they publish."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from bridge import ids
from bridge.config import parse_config
from bridge.confirm import CONFIRM_FIELD
from bridge.event_sources import rebuild
from bridge.msc4501 import SOCIAL_PROFILE_ROOM_TYPE, SOCIAL_PROFILE_USER_ID_STATE_TYPE
from bridge.nostr import event as nostr_event
from tests.conftest import HS_TOKEN, make_client, raw_config, settle

AUTH = {"Authorization": f"Bearer {HS_TOKEN}"}
_txn = itertools.count(1)
_ev = itertools.count(1)


def send(client, *events: dict) -> None:
    response = client.put(f"/_matrix/app/v1/transactions/t{next(_txn)}", json={"events": list(events)}, headers=AUTH)
    assert response.status_code == 200
    settle(client)


def message(body: str, sender: str = "@q:example.org", room: str = "!dm:example.org", reply_to: str | None = None) -> dict:
    content: dict = {"msgtype": "m.text", "body": body}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return {"type": "m.room.message", "event_id": f"$in{next(_ev)}", "room_id": room, "sender": sender, "content": content}


def last_body(homeserver, room: str = "!dm:example.org") -> str:
    return homeserver.sent_to(room)[-1]["body"]


def repo_call(client, fn, *args, **kwargs):
    async def run():
        return await fn(*args, **kwargs)

    return client.portal.call(run)


def ids_of(client, user: str = "@q:example.org"):
    return repo_call(client, client.app.state.repository.list_ids_for_user, user)


def latest(client, pubkey: str, kind: int):
    return repo_call(client, client.app.state.repository.get_latest_event, pubkey, kind)


def rebuilt(client, row):
    return repo_call(client, rebuild, client.app, row)


# -- name rules -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "ok"),
    [("q", True), ("q.art", True), ("q-2_x", True), ("Q", False), ("q+test", False), ("-q", False), ("q.", False),
     ("", False), ("a" * 33, False), ("q@x", False)],
)
def test_syntax(config, name, ok) -> None:
    assert (ids.syntax_problem(config, name) is None) is ok


@pytest.mark.parametrize(
    ("localpart", "expected"),
    [("q+test", "q-test"), ("a==b", "a-b"), ("+x+", "x"), ("=+/", "user"), ("Q/Art", "q-art"), ("a" * 40, "a" * 32)],
)
def test_suggestion_base(config, localpart, expected) -> None:
    assert ids.suggestion_base(config, localpart) == expected


def test_normalize_requested() -> None:
    assert ids.normalize_requested(" Q ", "example.org") == "q"
    assert ids.normalize_requested("q@example.org", "example.org") == "q"
    assert ids.normalize_requested("@q@Example.org", "example.org") == "q"
    assert ids.normalize_requested("q@other.org", "example.org") is None


# -- creating ---------------------------------------------------------------------


def test_create_profile_default(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    assert q.name == "q" and q.profile_room_id and q.secret_key.startswith("v1:")

    # The Profile Room: an MSC4501 profile room owned by @q, named after them.
    created = next(c for c in homeserver.created if c["room_id"] == q.profile_room_id)
    assert created["room_type"] == SOCIAL_PROFILE_ROOM_TYPE
    assert created["power_level_users"] == {"@q:example.org": 99}
    assert created["join_rule"] == "knock"
    assert homeserver.state_of(q.profile_room_id, SOCIAL_PROFILE_USER_ID_STATE_TYPE) == {"user_id": "@q:example.org"}
    assert homeserver.state_of(q.profile_room_id, "m.room.name") == {"name": "Q Person"}
    bridge_info = homeserver.rooms[q.profile_room_id]["state"][("m.bridge", f"nostr://{q.pubkey}")]["content"]
    assert bridge_info["channel"] == {"id": q.pubkey, "displayname": "Q Person"}
    assert bridge_info["protocol"]["id"] == "nostr"

    # Their space and notifications room exist, and they're invited to all three.
    rooms = repo_call(client, client.app.state.repository.get_user_rooms, "@q:example.org")
    assert rooms.space_room_id and rooms.notifications_room_id
    invited = {room for room, user in homeserver.invites if user == "@q:example.org"}
    assert {q.profile_room_id, rooms.space_room_id, rooms.notifications_room_id} <= invited
    assert ("m.space.child", q.profile_room_id) in homeserver.rooms[rooms.space_room_id]["state"]

    # Identity events: signed, stored, published to the publish relays.
    assert sorted(relay_pool.kinds()) == [0, 10002, 10050, 10063]
    for event, relays in relay_pool.published:
        assert event.pubkey == q.pubkey
        assert nostr_event.verify(event.to_dict()) == event
        assert relays == ["wss://relay.one.example", "wss://relay.two.example"]
    profile = json.loads(next(e for e, _ in relay_pool.published if e.kind == 0).content)
    assert profile == {"name": "Q Person", "display_name": "Q Person", "nip05": "q@example.org"}
    relay_list = next(e for e, _ in relay_pool.published if e.kind == 10002)
    assert [list(t) for t in relay_list.tags] == [
        ["r", "wss://nostr.example.org"],
        ["r", "wss://relay.one.example", "write"],
        ["r", "wss://relay.two.example", "write"],
    ]
    blossom = next(e for e, _ in relay_pool.published if e.kind == 10063)
    assert [list(t) for t in blossom.tags] == [["server", "https://nostr.example.org"]]

    # NIP-05 answers for the new ID.
    assert client.get("/.well-known/nostr.json", params={"name": "q"}).json()["names"] == {"q": q.pubkey}

    # The reply names the handle, and it's the active ID.
    body = last_body(homeserver)
    assert "q@example.org" in body and "active ID" in body


def test_profile_events_rebuild_from_matrix(client, relay_pool) -> None:
    """The index holds no content, yet every identity event can be rebuilt
    byte for byte."""
    send(client, message("[create profile"))
    [q] = ids_of(client)
    for kind in (0, 10002, 10050, 10063):
        row = latest(client, q.pubkey, kind)
        result = rebuilt(client, row)
        assert result.status == "ok"
        published = next(e for e, _ in relay_pool.published if e.id == row.id)
        assert result.event == published.to_dict()
    # Content isn't in the index: the profile row's source names Matrix events.
    source = json.loads(latest(client, q.pubkey, 0).source)
    assert source["kind"] == "profile" and source["name_event"].startswith("$")
    assert "Q Person" not in latest(client, q.pubkey, 0).source


def test_unchanged_profile_not_republished(client, relay_pool) -> None:
    from bridge.identity_events import sync_identity

    send(client, message("[create profile"))
    [q] = ids_of(client)
    before = len(relay_pool.published)
    assert repo_call(client, sync_identity, client.app, q) == []
    assert len(relay_pool.published) == before


def test_room_changes_republish_profile(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    room = q.profile_room_id
    first = latest(client, q.pubkey, 0)
    name_event = {"type": "m.room.name", "state_key": "", "room_id": room, "sender": "@q:example.org",
                  "event_id": "$x", "content": {"name": "Q on Nostr"}}
    homeserver._store(room, "m.room.name", {"name": "Q on Nostr"}, "@q:example.org", "")
    homeserver._store(room, "m.room.topic", {"topic": "hello\nworld"}, "@q:example.org", "")
    send(client, name_event)
    newest = latest(client, q.pubkey, 0)
    assert newest.id != first.id and newest.created_at > first.created_at
    assert latest(client, q.pubkey, 0).id == newest.id  # the old one is replaced in the index
    profile = json.loads(next(e for e, _ in relay_pool.published if e.id == newest.id).content)
    assert profile["name"] == "Q on Nostr" and profile["about"] == "hello\nworld"
    assert rebuilt(client, newest).status == "ok"
    # The recipe names the state events it was built from, so a later
    # change to the room doesn't alter the published event.
    homeserver._store(room, "m.room.name", {"name": "Something else"}, "@q:example.org", "")
    assert rebuilt(client, newest).status == "ok"


def _jpeg_with_gps() -> bytes:
    image = Image.new("RGB", (64, 48), (200, 30, 30))
    exif = Image.Exif()
    exif[0x8825] = {2: (1.0, 2.0, 3.0)}  # GPSInfo
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def test_avatar_is_cleaned_and_served(client, homeserver, relay_pool) -> None:
    original = _jpeg_with_gps()
    assert Image.open(io.BytesIO(original)).getexif()
    homeserver.media["mxc://example.org/avatar"] = (original, "image/jpeg")
    homeserver.profiles["@q:example.org"]["avatar_url"] = "mxc://example.org/avatar"

    send(client, message("[create profile"))
    profile = json.loads(next(e for e, _ in relay_pool.published if e.kind == 0).content)
    picture = profile["picture"]
    assert picture.startswith("https://nostr.example.org/") and picture.endswith(".jpg")

    path = "/" + picture.rsplit("/", 1)[1]
    served = client.get(path)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["access-control-allow-origin"] == "*"
    assert "immutable" in served.headers["cache-control"]
    assert hashlib.sha256(served.content).hexdigest() == path[1:].split(".")[0]
    assert not Image.open(io.BytesIO(served.content)).getexif()  # GPS gone
    assert served.content != original
    assert client.head(path).headers["content-length"] == str(len(served.content))
    # The cleaned copy lives in Matrix too.
    assert any(data == served.content for data, _ in homeserver.media.values())


def test_unknown_blob_not_served(client) -> None:
    assert client.get("/" + "ab" * 32 + ".jpg").status_code == 404
    assert client.get("/not-a-hash").status_code == 404


def test_rerun_create_reinvites(client, homeserver) -> None:
    send(client, message("[create profile"))
    homeserver.invites.clear()
    send(client, message("[create profile"))
    assert "already yours" in last_body(homeserver)
    [q] = ids_of(client)
    assert (q.profile_room_id, "@q:example.org") in homeserver.invites
    assert len(ids_of(client)) == 1


# -- the invalid / taken default --------------------------------------------------


def test_invalid_localpart_offers_suggestion(client, homeserver) -> None:
    user = "@q+test:example.org"
    send(client, message("[create profile", sender=user))
    offer = homeserver.sent_to("!dm:example.org")[-1]
    assert "can't be used as a Nostr ID" in offer["body"]
    assert "q-test@example.org" in offer["body"] and "[create profile <id>" in offer["body"]
    assert offer[CONFIRM_FIELD] == {"action": "create_id", "user": user, "args": {"name": "q-test"}}
    assert ids_of(client, user) == []

    offer_id = next(e for e in homeserver.rooms["!dm:example.org"]["events"].values() if e["content"] is offer)["event_id"]
    # Someone else's "confirm" does nothing.
    send(client, message("confirm", sender="@q:example.org", reply_to=offer_id))
    assert ids_of(client, user) == []
    # Theirs creates it.
    send(client, message("> <@nostrbot:example.org> ...\n\nconfirm", sender=user, reply_to=offer_id))
    [created] = ids_of(client, user)
    assert created.name == "q-test" and created.profile_room_id


def test_custom_id_and_rules(client, homeserver) -> None:
    homeserver.profiles["@bob:example.org"] = {"displayname": "Bob"}
    send(client, message("[create profile bob"))
    assert "reserved for @bob:example.org" in last_body(homeserver)
    send(client, message("[create profile admin"))
    assert "reserved" in last_body(homeserver)
    send(client, message("[create profile Q+X"))
    assert "can only contain" in last_body(homeserver)
    send(client, message("[create profile q@elsewhere.org"))
    assert "always @example.org" in last_body(homeserver)
    send(client, message("[create profile QBot@example.org"))
    assert [i.name for i in ids_of(client)] == ["qbot"]


def test_taken_default_suggests_alternative(client, homeserver) -> None:
    # Someone took "bob" before @bob arrived (as could happen if @bob
    # registered later): @bob is offered bob-2.
    send(client, message("[create profile bob", sender="@q:example.org"))
    send(client, message("[create profile", sender="@bob:example.org"))
    offer = homeserver.sent_to("!dm:example.org")[-1]
    assert "already taken" in offer["body"]
    assert offer[CONFIRM_FIELD]["args"] == {"name": "bob-2"}


def test_retired_names_stay_with_their_owner(client, homeserver) -> None:
    send(client, message("[create profile qa"))
    [qa] = ids_of(client)
    client.portal.call(_force_rename, client.app, qa, "qb")
    send(client, message("[create profile qa", sender="@admin:example.org"))
    assert "already taken" in last_body(homeserver)


async def _force_rename(app, nostr_id, new_name):
    await ids.rename_id(app, nostr_id, new_name)


# -- several IDs ------------------------------------------------------------------


def test_profiles_and_switch(client, homeserver) -> None:
    send(client, message("[create profile"))
    send(client, message("[create profile qbot"))
    assert "isn't your active ID" in last_body(homeserver) and "[switch 2" in last_body(homeserver)
    q, qbot = ids_of(client)
    # The second ID's room is named after the ID, not the Matrix user.
    assert homeserver.state_of(qbot.profile_room_id, "m.room.name") == {"name": "qbot"}

    send(client, message("[profiles"))
    listing = last_body(homeserver)
    assert "1. q@example.org  ← active" in listing and "2. qbot@example.org\n" in listing

    send(client, message("[switch 2"))
    assert "Now acting as qbot@example.org" in last_body(homeserver)
    assert repo_call(client, ids.active_id, client.app, "@q:example.org").id == qbot.id
    send(client, message("[switch q@example.org"))
    assert repo_call(client, ids.active_id, client.app, "@q:example.org").id == q.id
    send(client, message("[switch 7"))
    assert "don't have an ID" in last_body(homeserver)


def test_id_limit(config, repository, homeserver, relay_pool) -> None:
    limited = parse_config(raw_config(bridge={"max_ids_per_user": 2}))
    with make_client(limited, repository, homeserver, relay_pool) as client:
        send(client, message("[create profile"))
        send(client, message("[create profile qtwo"))
        send(client, message("[create profile qthree"))
        assert "the most this server allows" in last_body(homeserver)
        assert len(ids_of(client)) == 2
        # Admins are exempt.
        for name in ("a1", "a2", "a3"):
            send(client, message(f"[create profile {name}", sender="@admin:example.org"))
        assert len(ids_of(client, "@admin:example.org")) == 3


def test_allowed_users(repository, homeserver, relay_pool) -> None:
    restricted = parse_config(raw_config(bridge={"id_allowed_users": ["@admin:*"]}))
    with make_client(restricted, repository, homeserver, relay_pool) as client:
        send(client, message("[create profile"))
        assert "hasn't enabled Nostr IDs" in last_body(homeserver)


# -- renaming ---------------------------------------------------------------------


def test_rename(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    send(client, message("[rename 1 qart"))
    ask = homeserver.sent_to("!dm:example.org")[-1]
    assert "Rename q@example.org to qart@example.org?" in ask["body"] and "30 days" in ask["body"]
    assert ids_of(client)[0].name == "q"  # nothing yet

    ask_id = next(e for e in homeserver.rooms["!dm:example.org"]["events"].values() if e["content"] is ask)["event_id"]
    send(client, message("confirm", reply_to=ask_id))
    [renamed] = ids_of(client)
    assert renamed.name == "qart" and renamed.pubkey == q.pubkey and renamed.id == q.id
    assert "Renamed q@example.org to qart@example.org" in last_body(homeserver)

    # The new profile carries the new handle.
    newest = latest(client, q.pubkey, 0)
    content = next(e for e, _ in relay_pool.published if e.id == newest.id).content
    assert json.loads(content)["nip05"] == "qart@example.org"

    # Both names resolve during the grace period.
    for name in ("q", "qart"):
        assert client.get("/.well-known/nostr.json", params={"name": name}).json()["names"] == {name: q.pubkey}

    # Too soon to rename again; and the same "confirm" can't be replayed.
    send(client, message("[rename qart qnew"))
    assert "renamed recently" in last_body(homeserver)
    send(client, message("confirm", reply_to=ask_id))
    assert "has changed since" in last_body(homeserver)


def test_rename_grace_period_ends(client) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    client.portal.call(_force_rename, client.app, q, "qart")
    later = (datetime.now(timezone.utc) + timedelta(days=31)).isoformat()
    repository = client.app.state.repository
    assert repo_call(client, repository.resolve_nip05, "q", later) is None
    assert repo_call(client, repository.resolve_nip05, "qart", later) == q.pubkey


def test_rename_back_to_own_retired_name(client) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    client.portal.call(_force_rename, client.app, q, "qart")
    renamed = ids_of(client)[0]
    # Skip the cooldown for the test.
    stale = renamed.__class__(**{**renamed.__dict__, "renamed_at": None})
    client.portal.call(_force_rename, client.app, stale, "q")
    assert ids_of(client)[0].name == "q"


# -- membership -------------------------------------------------------------------


def test_owner_knock_and_join(client, homeserver) -> None:
    send(client, message("[create profile"))
    [q] = ids_of(client)
    room = q.profile_room_id
    homeserver.invites.clear()
    send(client, {"type": "m.room.member", "state_key": "@q:example.org", "sender": "@q:example.org",
                  "room_id": room, "event_id": "$k", "content": {"membership": "knock"}})
    assert (room, "@q:example.org") in homeserver.invites
    # A stranger's knock is left alone.
    homeserver.invites.clear()
    send(client, {"type": "m.room.member", "state_key": "@x:example.org", "sender": "@x:example.org",
                  "room_id": room, "event_id": "$k2", "content": {"membership": "knock"}})
    assert homeserver.invites == []

    send(client, {"type": "m.room.member", "state_key": "@q:example.org", "sender": "@q:example.org",
                  "room_id": room, "event_id": "$j", "content": {"membership": "join"}})
    welcome = homeserver.sent_to(room)[-1]["body"]
    assert "This is the Profile Room of q@example.org" in welcome and "public on Nostr" in welcome


def test_help_lists_profile_commands(client, homeserver) -> None:
    send(client, message("[help profile"))
    body = last_body(homeserver)
    for command in ("[create profile", "[profiles", "[switch", "[rename"):
        assert command in body
