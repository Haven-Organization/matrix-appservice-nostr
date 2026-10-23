"""Games: webxdc mini apps over Nostr (NIP-DC), server side."""

from __future__ import annotations

import asyncio
import json

import pytest

from bridge import webxdc
from bridge.config import parse_config
from bridge.event_sources import rebuild
from bridge.inbound import ingest
from tests.conftest import make_client, raw_config
from tests.test_federation import Stranger, command, run, transact
from tests.test_ids import ids_of, message, send

GAME = "1f0e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
XDC = "https://files.example/chess.xdc"


@pytest.fixture
def games(repository, homeserver, relay_pool):
    config = parse_config(raw_config(webxdc={"enabled": True}))
    with make_client(config, repository, homeserver, relay_pool) as client:
        yield client


def game_post(author, game_id=GAME, url=XDC):
    return author.event(1, f"let's play {url}", [["imeta", f"url {url}", f"m {webxdc.MIME}", "x " + "ab" * 32,
                                                   f"webxdc {game_id}", "alt Webxdc app: Chess"]])


def followed_game(client, homeserver, relay_pool):
    send(client, message("[create profile"))
    [q] = ids_of(client)
    alice = Stranger()
    relay_pool.network.append(alice.event(0, json.dumps({"name": "Alice"}), created_at=1000))
    command(client, f"[follow {alice.pubkey}")
    post = game_post(alice)
    assert run(client, ingest, client.app, post, origin="subscription").stored
    remote = run(client, client.app.state.repository.get_remote_account, alice.pubkey)
    shown = homeserver.messages(remote.room_id)[-1]
    return q, alice, post, remote, shown


def test_config() -> None:
    config = parse_config(raw_config(webxdc={"enabled": True}))
    assert config.webxdc.sandbox_domain == "xdc.example.org" and config.webxdc.active_days == 14
    assert parse_config(raw_config()).webxdc.enabled is False


def test_apps_of() -> None:
    alice = Stranger()
    [found] = webxdc.apps_of(game_post(alice))
    assert found == webxdc.App(GAME, XDC, "ab" * 32, "Chess")
    no_id = alice.event(1, "x", [["imeta", f"url {XDC}", f"m {webxdc.MIME}"]])
    assert webxdc.apps_of(no_id) == []
    label = alice.event(1, "x", [["imeta", f"url {XDC}", f"m {webxdc.MIME}", "x hl", f"webxdc {GAME}"]])
    assert webxdc.apps_of(label)[0].sha256 is None and webxdc.apps_of(label)[0].name == "chess"


def test_game_post_is_shown_with_play_line_and_not_copied(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    content = shown["content"]
    assert "🎮 Chess: reply [play to open" in content["body"]
    assert "Other Attachments" not in content["body"]
    assert content[webxdc.GAME_FIELD] == {"id": GAME, "url": XDC, "x": "ab" * 32, "name": "Chess"}
    assert not any(url for url in homeserver.media if "chess" in url)
    assert run(games, webxdc.game_post, games.app, GAME).id == post["id"]


def test_moves_are_stored_hidden_in_a_thread_and_served(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    bob = Stranger()
    move = bob.event(webxdc.UPDATE_KIND, json.dumps({"payload": {"move": "e4"}}), [["i", GAME], ["alt", "Webxdc update"]])
    assert run(games, ingest, games.app, move, origin="relay").stored
    [hidden] = homeserver.messages(remote.room_id, webxdc.UPDATE_EVENT_TYPE)
    assert hidden["content"]["m.relates_to"]["rel_type"] == "m.thread"
    assert hidden["content"]["m.relates_to"]["event_id"] == shown["event_id"]
    assert [e["id"] for e in run(games, webxdc.updates, games.app, GAME)] == [move["id"]]
    row = run(games, games.app.state.repository.get_event, move["id"])
    assert run(games, rebuild, games.app, row).event == move
    # Moves for games nobody posted here are refused.
    stray = bob.event(webxdc.UPDATE_KIND, "{}", [["i", "unknown-game"], ["alt", "x"]])
    assert not run(games, ingest, games.app, stray, origin="relay").ok


def test_off_by_default(client, homeserver, relay_pool) -> None:
    move = Stranger().event(webxdc.UPDATE_KIND, "{}", [["i", GAME], ["alt", "x"]])
    assert not run(client, ingest, client.app, move, origin="relay").ok


def test_send_update_goes_out_before_it_is_stored(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    got = []
    session = webxdc.Session(GAME, got.append)
    webxdc.hub(games.app).add(session)
    sent = run(games, webxdc.send_update, games.app, q, GAME, {"payload": {"move": "d4"}, "info": "q played d4"})
    assert sent["pubkey"] == q.pubkey and ["i", GAME] in sent["tags"] and ["info", "q played d4"] in sent["tags"]
    assert got[0] == {"t": "update", "event": sent}  # delivered at once, here
    from tests.conftest import settle

    settle(games)
    assert [e for e, _ in relay_pool.published if e.id == sent["id"]]
    assert homeserver.messages(remote.room_id, webxdc.UPDATE_EVENT_TYPE)[-1]["content"]["game"] == GAME


def test_realtime_only_while_someone_plays(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    packet = Stranger().event(webxdc.REALTIME_KIND, "aGVsbG8=", [["i", GAME]])
    assert not run(games, ingest, games.app, packet, origin="relay").ok
    got = []
    session = webxdc.Session(GAME, got.append, realtime=True)
    webxdc.hub(games.app).add(session)
    fresh = Stranger().event(webxdc.REALTIME_KIND, "aGVsbG8=", [["i", GAME]])
    result = run(games, ingest, games.app, fresh, origin="relay")
    assert result.ok and not result.stored
    assert got == [{"t": "realtime", "b64": "aGVsbG8="}]
    assert run(games, games.app.state.repository.get_event, fresh["id"]) is None


def test_xdc_upload_becomes_a_game_post(games, homeserver, relay_pool) -> None:
    send(games, message("[create profile"))
    [q] = ids_of(games)
    homeserver.media["mxc://example.org/app"] = (b"PK\x03\x04 fake zip", "application/zip")
    from tests.test_federation import post as post_message

    post_message(games, homeserver, q.profile_room_id, {"msgtype": "m.file", "body": "tetris.xdc", "filename": "tetris.xdc",
                                                         "url": "mxc://example.org/app", "info": {"mimetype": "application/zip", "size": 16}})
    note = relay_pool.of_kind(1)[-1]
    [game] = webxdc.apps_of(note.to_dict())
    assert game.name == "tetris" and len(game.id) == 36
    imeta = next(t for t in note.tags if t[0] == "imeta")
    assert f"m {webxdc.MIME}" in imeta
    assert run(games, webxdc.game_post, games.app, game.id).id == note.id
    row = run(games, games.app.state.repository.get_event, note.id)
    assert run(games, rebuild, games.app, row).event == note.to_dict()


def test_active_games_are_followed(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    run(games, webxdc.touch, games.app, GAME)
    desired = run(games, games.app.state.feeds._desired)
    filters = [f for (relay, name), fs in desired.items() if name == "games" for f in fs]
    assert {"#i": [GAME], "kinds": [webxdc.UPDATE_KIND]} in [{k: v for k, v in f.items() if k != "since"} for f in filters]
