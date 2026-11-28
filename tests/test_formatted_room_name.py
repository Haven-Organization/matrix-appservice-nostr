"""MSC4591: a Remote User Room's name carries its ghost's MSC4589 formatted
display name, under the same setting, and in the same m.room.name event.

Run with: python -m pytest tests
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from bridge import remote as remote_module
from bridge.render import FORMATTED_DISPLAYNAME_FIELD, FORMATTED_ROOM_NAME_FIELD, room_name_content
from bridge.repository import RemoteAccount

PUBKEY = "a" * 64
GHOST = f"@nostr_{PUBKEY}:example.org"
FORMATTED = 'Alice <img data-mx-emoticon src="mxc://example.org/cat" alt=":cat:">'


def _formatted(body: str) -> dict:
    return {"format": "org.matrix.custom.html", "formatted_body": body}


def _app(config, homeserver, repository, *, enabled: bool) -> SimpleNamespace:
    bridge = dataclasses.replace(config.bridge, use_msc4589_formatted_displayname=enabled)
    return SimpleNamespace(state=SimpleNamespace(
        config=dataclasses.replace(config, bridge=bridge), homeserver=homeserver, repository=repository,
    ))


def _profile_event(name: str, created_at: int) -> dict:
    return {"id": f"{created_at:064x}", "sig": "0" * 128, "kind": 0, "pubkey": PUBKEY, "created_at": created_at,
            "tags": [], "content": f'{{"name": "{name}"}}'}


@pytest.fixture
def setup(config, homeserver, repository, monkeypatch):
    async def fake_build(app, raw, ghost, name):
        return FORMATTED if ":cat:" in name else None

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(remote_module, "build_formatted_displayname", fake_build)
    monkeypatch.setattr(remote_module, "apply_profile_fields", nothing)
    room = asyncio.run(homeserver.create_room(as_user_id=GHOST, name="placeholder"))
    remote = RemoteAccount(pubkey=PUBKEY, ghost_user_id=GHOST, room_id=room)
    return homeserver, repository, remote, room


def _apply(app, remote, name, created_at):
    return asyncio.run(remote_module.apply_profile(app, remote, _profile_event(name, created_at)))


def _room_name(homeserver, room):
    return asyncio.run(homeserver.get_room_state(room, "m.room.name", as_user_id=GHOST))


def test_room_name_content() -> None:
    assert room_name_content("A", None) == {"name": "A"}
    assert room_name_content("A", "<b>A</b>") == {"name": "A", FORMATTED_ROOM_NAME_FIELD: _formatted("<b>A</b>")}


def test_a_formatted_name_reaches_the_room_name_too(config, setup) -> None:
    homeserver, repository, remote, room = setup
    app = _app(config, homeserver, repository, enabled=True)
    remote = _apply(app, remote, "Alice :cat:", 10)
    assert homeserver.profiles[GHOST][FORMATTED_DISPLAYNAME_FIELD] == _formatted(FORMATTED)
    assert _room_name(homeserver, room) == {"name": "Alice :cat:", FORMATTED_ROOM_NAME_FIELD: _formatted(FORMATTED)}
    # Renamed to a plain name: both formatted versions go.
    _apply(app, remote, "Alice", 20)
    assert _room_name(homeserver, room) == {"name": "Alice"}


def test_off_leaves_the_room_name_plain(config, setup) -> None:
    homeserver, repository, remote, room = setup
    app = _app(config, homeserver, repository, enabled=False)
    _apply(app, remote, "Alice :cat:", 10)
    assert _room_name(homeserver, room) == {"name": "Alice :cat:"}


def test_off_keeps_a_formatted_room_name_already_there(config, setup) -> None:
    homeserver, repository, remote, room = setup
    remote = _apply(_app(config, homeserver, repository, enabled=True), remote, "Alice :cat:", 10)
    # Turned off later, and the same profile re-published: nothing cleared.
    _apply(_app(config, homeserver, repository, enabled=False), remote, "Alice :cat:", 20)
    assert _room_name(homeserver, room)[FORMATTED_ROOM_NAME_FIELD] == _formatted(FORMATTED)
