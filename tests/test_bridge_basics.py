"""Config, storage, the NIP-05 endpoint, the AppService API and commands."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from bridge.appservice import build_registration
from bridge.config import ConfigError, parse_config
from bridge.nostr import nip05
from bridge.repository import RetiredName
from bridge.sqlite_repository import SqliteBridgeRepository
from tests.conftest import HS_TOKEN, raw_config

AUTH = {"Authorization": f"Bearer {HS_TOKEN}"}
_EVENT_IDS = itertools.count(1)

# -- config ------------------------------------------------------------------


def test_config_defaults(config) -> None:
    assert config.appservice.command_prefix == "["
    assert config.appservice.user_prefix == "nostr_"
    assert config.bridge.use_synapse_admin_api is False
    assert config.nostr.relay_http_base == "https://nostr.example.org"
    raw = raw_config()
    del raw["nostr"]["publish_relays"]
    defaults = parse_config(raw).nostr
    # poster.place only takes posts from its members, and requires auth even
    # to read, so it's neither.
    assert not any("poster.place" in url for url in defaults.publish_relays)
    assert not any("poster.place" in url for url in defaults.fallback_relays)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"bridge": {"use_synapse_admin_api": True}}, "synapse_admin_token"),
        ({"nostr": {"key_encryption_secret": "short"}}, "at least"),
        ({"nostr": {"relay_url": "https://nostr.example.org"}}, "ws://"),
        ({"bridge": {"domain": "not a domain"}}, "domain"),
        ({"nostr": {"publish_relays": ["http://x.org"]}}, "ws://"),
    ],
)
def test_config_errors(overrides, message) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(raw_config(**overrides))


def test_config_synapse_admin_api_with_token() -> None:
    config = parse_config(
        raw_config(bridge={"use_synapse_admin_api": True}, homeserver={"synapse_admin_token": "syt_x"})
    )
    assert config.bridge.use_synapse_admin_api


def test_registration_namespaces(config) -> None:
    registration = build_registration(config)
    regexes = [entry["regex"] for entry in registration["namespaces"]["users"]]
    assert regexes == ["@nostr_.*", "@nostrbot:.*"]
    assert registration["url"] == "http://127.0.0.1:8092"
    assert registration["sender_localpart"] == "nostrbot"


# -- storage -----------------------------------------------------------------


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


async def test_repository_ids_and_nip05_resolution() -> None:
    repo = SqliteBridgeRepository(":memory:")
    first = await repo.get_or_create_server_key("bot", "one")
    assert await repo.get_or_create_server_key("bot", "two") == first == "one"

    q = await repo.insert_id(
        name="q", matrix_user_id="@q:example.org", pubkey="aa" * 32, secret_key="v1:x", key_origin="generated",
        created_at=_iso(),
    )
    assert (await repo.get_id_by_name("q")).id == q.id
    assert (await repo.get_id_by_pubkey("aa" * 32)).name == "q"
    assert [i.name for i in await repo.list_ids_for_user("@q:example.org")] == ["q"]
    assert await repo.resolve_nip05("q", _iso()) == "aa" * 32
    assert await repo.resolve_nip05("nobody", _iso()) is None

    # A renamed ID's old name resolves during its grace period only.
    await repo.insert_retired_name(
        RetiredName("q-old", "@q:example.org", q.id, "renamed", _iso(), resolves_until=_iso(timedelta(days=30)))
    )
    await repo.insert_retired_name(RetiredName("q-gone", "@q:example.org", q.id, "deleted", _iso()))
    assert await repo.resolve_nip05("q-old", _iso()) == "aa" * 32
    assert await repo.resolve_nip05("q-old", _iso(timedelta(days=31))) is None
    assert await repo.resolve_nip05("q-gone", _iso()) is None

    assert await repo.claim_event("$a", "2026-01-01")
    assert not await repo.claim_event("$a", "2026-01-02")
    await repo.prune_processed_events("2026-01-05")
    assert await repo.claim_event("$a", "2026-01-06")

    await repo.set_state("k", "1")
    await repo.set_state("k", "2")
    assert await repo.get_state("k") == "2"
    await repo.close()


async def test_repository_persists(tmp_path) -> None:
    path = tmp_path / "data" / "bridge.db"
    repo = SqliteBridgeRepository(path)
    await repo.get_or_create_server_key("bot", "secret")
    await repo.close()
    reopened = SqliteBridgeRepository(path)
    assert await reopened.get_server_key("bot") == "secret"
    await reopened.close()


# -- NIP-05 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("q@example.org", "q@example.org"),
        ("Q@Example.Org", "q@example.org"),
        ("example.org", "example.org"),
        ("_@example.org", "example.org"),
        ("q+test@example.org", None),
        ("q@localhost", None),
        ("", None),
    ],
)
def test_parse_identifier(text, expected) -> None:
    parsed = nip05.parse_identifier(text)
    assert (str(parsed) if parsed else None) == expected


async def test_nip05_endpoint(client, repository) -> None:
    bot_pubkey = client.app.state.bot_identity.pubkey
    await repository.insert_id(
        name="q", matrix_user_id="@q:example.org", pubkey="aa" * 32, secret_key=None, key_origin="bunker",
        created_at=_iso(),
    )

    response = client.get("/.well-known/nostr.json", params={"name": "q"})
    assert response.status_code == 200
    assert response.json() == {"names": {"q": "aa" * 32}, "relays": {"aa" * 32: ["wss://nostr.example.org"]}}
    assert response.headers["access-control-allow-origin"] == "*"

    assert client.get("/.well-known/nostr.json", params={"name": "Q"}).json()["names"] == {"q": "aa" * 32}
    assert client.get("/.well-known/nostr.json", params={"name": "_"}).json()["names"] == {"_": bot_pubkey}
    # Unknown, invalid, or no name: an empty answer, never the full list.
    for params in ({"name": "nobody"}, {"name": "q+x"}, {}):
        assert client.get("/.well-known/nostr.json", params=params).json() == {"names": {}}


def test_bot_identity_is_stable(config, repository, homeserver) -> None:
    from fastapi.testclient import TestClient

    from bridge.server import create_app

    pubkeys = []
    for _ in range(2):
        app = create_app(config, repository=repository, homeserver=homeserver, run_startup_checks=False)
        with TestClient(app) as test_client:
            pubkeys.append(test_client.app.state.bot_identity.pubkey)
    assert pubkeys[0] == pubkeys[1]


# -- AppService API ----------------------------------------------------------


def _transaction(client, txn_id: str, *events: dict, headers=AUTH):
    return client.put(f"/_matrix/app/v1/transactions/{txn_id}", json={"events": list(events)}, headers=headers)


def _message(body: str, sender: str = "@q:example.org", room: str = "!room:example.org") -> dict:
    return {
        "type": "m.room.message",
        "event_id": f"$e{next(_EVENT_IDS)}",
        "room_id": room,
        "sender": sender,
        "content": {"msgtype": "m.text", "body": body},
    }


def test_hs_token_required(client) -> None:
    missing = _transaction(client, "t", headers={})
    assert missing.status_code == 401 and missing.json()["errcode"] == "M_UNAUTHORIZED"
    wrong = _transaction(client, "t", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 403 and wrong.json()["errcode"] == "M_FORBIDDEN"
    legacy = client.put("/_matrix/app/v1/transactions/t", json={"events": []}, params={"access_token": HS_TOKEN})
    assert legacy.status_code == 200


def test_ping_and_queries(client) -> None:
    assert client.post("/_matrix/app/v1/ping", json={"transaction_id": "x"}, headers=AUTH).json() == {}
    assert client.post("/_matrix/app/v1/ping", json={}).status_code == 401
    user = client.get("/_matrix/app/v1/users/@nostr_ab:example.org", headers=AUTH)
    assert user.status_code == 404 and user.json()["errcode"] == "M_NOT_FOUND"
    assert client.get("/_matrix/app/v1/rooms/%23a:example.org", headers=AUTH).status_code == 404


def test_help_command_and_dedupe(client, homeserver) -> None:
    help_message = _message("[help")
    assert _transaction(client, "t1", help_message).status_code == 200
    assert len(homeserver.sent) == 1
    room, content, sender = homeserver.sent[0]
    # Like matrix-appservice-activitypub's: a message, not a notice, with a
    # table of sections.
    assert sender == "@nostrbot:example.org" and content["msgtype"] == "m.text"
    assert "[help advanced" in content["body"] and "<table>" in content["formatted_body"]
    # The same transaction again is not processed twice...
    _transaction(client, "t1", help_message)
    assert len(homeserver.sent) == 1
    # ...but a reused transaction ID with new events is (Synapse on SQLite
    # restarts its numbering after a restart).
    _transaction(client, "t1", _message("[help status"))
    assert len(homeserver.sent) == 2


def test_prefix_needs_a_command(client, homeserver) -> None:
    """A "[" starting an ordinary message isn't a command."""
    _transaction(client, "t2", _message("[citation needed]"), _message("[helpful tip]"), _message("[statuses"))
    assert homeserver.sent == []


def test_tagged_bot(client, homeserver) -> None:
    _transaction(client, "t3", _message("@nostrbot:example.org status please"))
    assert "You don't have a Nostr ID yet." in homeserver.sent[0][1]["body"]


def test_status_shows_relays_to_admins_only(client, homeserver) -> None:
    _transaction(client, "t4", _message("[status", sender="@admin:example.org"), _message("[status"))
    admin_reply, user_reply = homeserver.sent[0][1]["body"], homeserver.sent[1][1]["body"]
    assert "Outgoing relay connections" in admin_reply
    assert "Outgoing relay connections" not in user_reply


def test_remote_users_cannot_use_commands(client, homeserver) -> None:
    _transaction(client, "t5", _message("[status", sender="@someone:other.org"))
    assert "Commands are only available to users on example.org." in homeserver.sent[0][1]["body"]


def test_bot_and_ghost_messages_ignored(client, homeserver) -> None:
    _transaction(
        client, "t6", _message("[help", sender="@nostrbot:example.org"), _message("[help", sender="@nostr_ab:example.org")
    )
    assert homeserver.sent == []


def test_invite_is_accepted(client, homeserver) -> None:
    invite = {
        "type": "m.room.member",
        "event_id": "$inv",
        "room_id": "!dm:example.org",
        "sender": "@q:example.org",
        "state_key": "@nostrbot:example.org",
        "content": {"membership": "invite", "is_direct": True},
    }
    _transaction(client, "t7", invite)
    assert homeserver.joined == [("!dm:example.org", "@nostrbot:example.org")]
    assert "[help" in homeserver.sent[0][1]["body"]


def test_encrypted_room_warning(client, homeserver) -> None:
    homeserver.room_state[("!enc:example.org", "m.room.encryption")] = {"algorithm": "m.megolm.v1.aes-sha2"}
    invite = {
        "type": "m.room.member",
        "event_id": "$inv2",
        "room_id": "!enc:example.org",
        "sender": "@q:example.org",
        "state_key": "@nostrbot:example.org",
        "content": {"membership": "invite", "is_direct": True},
    }
    _transaction(client, "t8", invite)
    assert "encrypted" in homeserver.sent[0][1]["body"]


def test_cors_preflight(client) -> None:
    response = client.options("/.well-known/nostr.json", headers={"Origin": "https://app.example", "Access-Control-Request-Method": "GET"})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    # The AppService API gets no CORS headers.
    assert "access-control-allow-origin" not in client.post("/_matrix/app/v1/ping", json={}, headers=AUTH).headers


def test_msc4503_value() -> None:
    from bridge.msc4501 import external_handle_value

    config = parse_config(raw_config(appservice={"bot_avatar_mxc": "mxc://example.org/logo"}))
    pubkey = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
    npub = "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg"
    assert external_handle_value(config, pubkey=pubkey, nip05="alice@example.com") == {
        "handle": "alice@example.com",
        "protocol": {"id": "nostr", "displayname": "Nostr", "avatar_url": "mxc://example.org/logo"},
        "url": f"https://ditto.pub/{npub}",
    }
    assert external_handle_value(config, pubkey=pubkey)["handle"] == npub
    with pytest.raises(ConfigError):
        parse_config(raw_config(bridge={"msc4503_external_handle": "sometimes"}))


def test_bridge_info_uses_bot_avatar(repository, homeserver, relay_pool) -> None:
    from tests.conftest import make_client

    config = parse_config(raw_config(appservice={"bot_avatar_mxc": "mxc://example.org/logo"}))
    with make_client(config, repository, homeserver, relay_pool) as client:
        client.put("/_matrix/app/v1/transactions/x1", headers=AUTH, json={"events": [_message("[create profile")]})
        from tests.conftest import settle

        settle(client)
        [q] = client.portal.call(repository.list_ids_for_user, "@q:example.org")
        info = homeserver.rooms[q.profile_room_id]["state"][("m.bridge", f"nostr://{q.pubkey}")]["content"]
        assert info["protocol"] == {"id": "nostr", "displayname": "Nostr", "avatar_url": "mxc://example.org/logo"}
        assert info["bridgebot"] == "@nostrbot:example.org"
