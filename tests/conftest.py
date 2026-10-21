"""Shared fixtures: a valid config, an in-memory repository, a fake
homeserver that keeps rooms, state, events and media in memory, and a
relay pool that records what would have been published."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bridge.config import BridgeConfig, parse_config
from bridge.homeserver_client import HomeserverClient, HomeserverError, MediaDownload
from bridge.nostr.event import Event
from bridge.nostr.relay_pool import PublishResult
from bridge.server import create_app
from bridge.sqlite_repository import SqliteBridgeRepository
from bridge.tasks import drain

HS_TOKEN = "hs_test_token"


def raw_config(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "bridge": {"domain": "example.org", "admins": ["@admin:example.org"]},
        "homeserver": {"base_url": "http://homeserver.invalid", "server_name": "example.org"},
        "appservice": {
            "registration_path": "./appservice-registration.yaml",
            "id": "nostr-test",
            "hs_token": HS_TOKEN,
            "as_token": "as_test_token",
        },
        "nostr": {
            "relay_url": "wss://nostr.example.org",
            "key_encryption_secret": "k" * 64,
            "publish_relays": ["wss://relay.one.example", "wss://relay.two.example"],
        },
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return raw


@pytest.fixture
def config() -> BridgeConfig:
    return parse_config(raw_config())


class FakeHomeserver(HomeserverClient):
    """Just enough of a Matrix homeserver, in memory."""

    def __init__(self) -> None:
        super().__init__("http://homeserver.invalid", as_token="as_test_token")
        self._ids = itertools.count(1)
        self.rooms: dict[str, dict[str, Any]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.media: dict[str, tuple[bytes, str]] = {}
        self.sent: list[tuple[str, dict[str, Any], str]] = []
        self.joined: list[tuple[str, str]] = []
        self.invites: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []
        # Overrides for get_room_state: (room, type) -> content.
        self.room_state: dict[tuple[str, str], dict[str, Any]] = {}

    def _room(self, room_id: str) -> dict[str, Any]:
        return self.rooms.setdefault(room_id, {"state": {}, "events": {}, "members": set()})

    def _store(self, room_id: str, event_type: str, content: dict, sender: str, state_key: str | None = None) -> dict:
        event = {
            "event_id": f"$ev{next(self._ids)}",
            "room_id": room_id,
            "type": event_type,
            "sender": sender,
            "content": content,
            "origin_server_ts": 1_700_000_000_000,
        }
        if state_key is not None:
            event["state_key"] = state_key
            self._room(room_id)["state"][(event_type, state_key)] = event
        self._room(room_id)["events"][event["event_id"]] = event
        return event

    async def create_room(self, *, as_user_id, name=None, topic=None, invite=None, avatar_mxc=None, room_type=None,
                          power_level_users=None, power_level_events=None, is_direct=False, preset="private_chat",
                          join_rule=None, history_visibility=None, initial_state=None):
        room_id = f"!room{next(self._ids)}:example.org"
        self.created.append({"room_id": room_id, "name": name, "invite": invite, "room_type": room_type,
                             "power_level_users": power_level_users, "power_level_events": power_level_events,
                             "join_rule": join_rule, "is_direct": is_direct, "initial_state": initial_state})
        room = self._room(room_id)
        room["members"].add(as_user_id)
        self._store(room_id, "m.room.create", {"type": room_type} if room_type else {}, as_user_id, "")
        users = {as_user_id: 100, **(power_level_users or {})}
        self._store(room_id, "m.room.power_levels", {"users": users, "events": power_level_events or {}}, as_user_id, "")
        if join_rule:
            self._store(room_id, "m.room.join_rules", {"join_rule": join_rule}, as_user_id, "")
        for state in initial_state or []:
            self._store(room_id, state["type"], state["content"], as_user_id, state.get("state_key", ""))
        if avatar_mxc:
            self._store(room_id, "m.room.avatar", {"url": avatar_mxc}, as_user_id, "")
        if name:
            self._store(room_id, "m.room.name", {"name": name}, as_user_id, "")
        if topic:
            self._store(room_id, "m.room.topic", {"topic": topic}, as_user_id, "")
        for user in invite or []:
            self.invites.append((room_id, user))
        return room_id

    async def invite_user(self, room_id, user_id, *, as_user_id, is_direct=False):
        self.invites.append((room_id, user_id))

    async def join_room(self, room_id, *, as_user_id):
        self.joined.append((room_id, as_user_id))
        self._room(room_id)["members"].add(as_user_id)

    async def get_joined_members(self, room_id, *, as_user_id):
        return sorted(self._room(room_id)["members"])

    async def send_message_event(self, room_id, content, *, as_user_id, ts=None, event_type="m.room.message"):
        self.sent.append((room_id, content, as_user_id))
        return self._store(room_id, event_type, content, as_user_id)["event_id"]

    async def send_state_event(self, room_id, event_type, content, *, as_user_id, state_key=""):
        return self._store(room_id, event_type, content, as_user_id, state_key)["event_id"]

    async def get_room_state(self, room_id, event_type, state_key="", *, as_user_id):
        if (room_id, event_type) in self.room_state:
            return self.room_state[(room_id, event_type)]
        event = self._room(room_id)["state"].get((event_type, state_key))
        return dict(event["content"]) if event else None

    async def get_full_state(self, room_id, *, as_user_id):
        return list(self._room(room_id)["state"].values())

    async def get_event(self, room_id, event_id, *, as_user_id):
        return self._room(room_id)["events"].get(event_id)

    async def get_profile(self, user_id):
        if user_id not in self.profiles:
            raise HomeserverError(404, "M_NOT_FOUND", "Profile was not found")
        return dict(self.profiles[user_id])

    async def upload_media(self, data, content_type, filename, *, as_user_id):
        mxc = f"mxc://example.org/upload{next(self._ids)}"
        self.media[mxc] = (data, content_type)
        return mxc

    async def download_media(self, mxc):
        if mxc not in self.media:
            return None
        data, content_type = self.media[mxc]
        return MediaDownload(content=data, content_type=content_type)

    def state_of(self, room_id: str, event_type: str) -> dict | None:
        event = self._room(room_id)["state"].get((event_type, ""))
        return event["content"] if event else None

    def sent_to(self, room_id: str) -> list[dict]:
        return [content for room, content, _ in self.sent if room == room_id]


class FakeRelayPool:
    """Records publishes instead of connecting anywhere."""

    def __init__(self) -> None:
        self.published: list[tuple[Event, list[str]]] = []

    async def publish(self, event: Event, relays: list[str], timeout: float | None = None) -> list[PublishResult]:
        self.published.append((event, list(relays)))
        return [PublishResult(relay, True, "") for relay in relays]

    def connections(self) -> list:
        return []

    async def relay_info(self, url: str) -> dict:
        return {}

    async def close(self) -> None:
        pass

    def kinds(self) -> list[int]:
        return [event.kind for event, _ in self.published]


@pytest.fixture
def homeserver() -> FakeHomeserver:
    hs = FakeHomeserver()
    hs.profiles["@q:example.org"] = {"displayname": "Q Person"}
    hs.profiles["@admin:example.org"] = {"displayname": "Admin"}
    return hs


@pytest.fixture
def relay_pool() -> FakeRelayPool:
    return FakeRelayPool()


@pytest.fixture
def repository() -> SqliteBridgeRepository:
    return SqliteBridgeRepository(":memory:")


def make_client(config, repository, homeserver, relay_pool):
    app = create_app(config, repository=repository, homeserver=homeserver, relay_pool=relay_pool, run_startup_checks=False)
    return TestClient(app)


@pytest.fixture
def client(config, repository, homeserver, relay_pool):
    with make_client(config, repository, homeserver, relay_pool) as test_client:
        yield test_client


def settle(client: TestClient) -> None:
    """Let background work (publishing, profile syncs) finish."""
    client.portal.call(drain, client.app)
