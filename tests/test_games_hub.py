"""The Nostr Games widget: open by link, browse Nostr, browse a room, and a
room's own games."""

from __future__ import annotations

import io
import json
import time
import zipfile

from PIL import Image

from bridge import games_room, webxdc
from bridge.safe_fetch import Fetched
from tests.test_federation import Stranger, run, transact
from tests.test_ids import message, send
from tests.test_webxdc import GAME, followed_game, games, play, session_for  # noqa: F401  (fixture)

HUB, GAME_W = games_room.HUB_WIDGET_ID, games_room.GAME_WIDGET_ID


def xdc_bytes() -> bytes:
    return _XDC


def _make_xdc() -> bytes:
    icon = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 120, 240)).save(icon, format="PNG")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("index.html", "<p>game</p>")
        archive.writestr("icon.png", icon.getvalue())
    return out.getvalue()


_XDC = _make_xdc()  # once: a zip carries the time it was made


class Fetcher:
    async def get(self, url, **kwargs):
        return Fetched(url, 200, xdc_bytes(), "application/zip")

    async def aclose(self):
        pass


def layout(homeserver, room):
    return homeserver.state_of(room, games_room.LAYOUT_STATE_TYPE)["widgets"]


def joined_games_room(client, homeserver, relay_pool):
    """q, with a Games room they've joined and a followed account's game post."""
    client.app.state.fetcher = Fetcher()
    q, alice, post, remote, shown = followed_game(client, homeserver, relay_pool)
    room, _ = run(client, games_room.ensure_games_room, client.app, "@q:example.org")
    homeserver._room(room)["members"].add("@q:example.org")
    return q, alice, post, remote, shown, room


def auth(client):
    return {"Authorization": "Bearer " + session_for(client)}


def test_games_room_starts_with_the_hub_and_play_brings_it_back(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown, room = joined_games_room(games, homeserver, relay_pool)
    hub = homeserver._room(room)["state"][("im.vector.modular.widgets", HUB)]["content"]
    assert hub["name"] == "🎮 Nostr Games" and "/xdc/hub?" in hub["url"] and "$matrix_room_id" in hub["url"]
    assert layout(homeserver, room) == {HUB: {"container": "center", "index": 0, "width": 100, "height": 100}}
    # A game plays in it: the widget opens straight into the game. An old
    # separate game widget goes.
    for state_type in games_room.WIDGET_STATE_TYPES:
        homeserver._store(room, state_type, {"url": "https://old.example/xdc/play"}, "@nostrbot:example.org", GAME_W)
    play(games, homeserver, remote.room_id, shown["event_id"])
    hub = homeserver._room(room)["state"][("m.widget", HUB)]["content"]
    assert hub["url"].endswith(f"&game={GAME}") and hub["data"] == {"title": "Chess", "game": GAME}
    assert homeserver._room(room)["state"][("m.widget", GAME_W)]["content"] == {}
    assert layout(homeserver, room) == {HUB: {"container": "center", "index": 0, "width": 100, "height": 100}}
    # Removed, it comes back with "[play" on its own.
    for state_type in games_room.WIDGET_STATE_TYPES:
        homeserver._room(room)["state"][(state_type, HUB)]["content"] = {}
    send(games, message("[play"))
    assert homeserver._room(room)["state"][("m.widget", HUB)]["content"]["url"].startswith("https://")
    assert "Nostr Games widget is in your Games room" in homeserver.sent_to("!dm:example.org")[-1]["body"]
    page = games.get("/xdc/hub")
    assert page.status_code == 200 and "/xdc/static/hub.js" in page.text
    assert games.get("/xdc/static/hub.js").status_code == 200


def test_browsing_nostr(games, homeserver, relay_pool) -> None:
    games.app.state.fetcher = Fetcher()
    ditto, poster = Stranger(), Stranger()
    first = ditto.event(1063, "A bird game", [["url", "https://b.example/bird.xdc"], ["m", "application/vnd.webxdc+zip"],
                                             ["alt", "Webxdc app: Bird"], ["webxdc", "bird-1"]], created_at=1000)
    copy = poster.event(1063, "mine now", [["url", "https://evil.example/bird.xdc"], ["m", webxdc.MIME],
                                          ["alt", "Webxdc app: Bird"], ["webxdc", "bird-1"],
                                          ["image", "https://b.example/bird.png"]], created_at=2000)
    note = poster.event(1, "chess! https://c.example/chess.xdc #webxdc", [
        ["t", "webxdc"], ["imeta", "url https://c.example/chess.xdc", f"m {webxdc.MIME}", "webxdc chess-1",
                          "alt Webxdc app: Chess"]], created_at=1500)
    forged = dict(copy, id="0" * 64, tags=copy["tags"][:-1] + [["webxdc", "fake-1"]])
    relay_pool.network += [first, copy, note, forged,
                           ditto.event(0, json.dumps({"name": "Ditto Dev"}), created_at=10)]
    headers = auth(games)
    found = games.get("/xdc/api/catalog", headers=headers).json()["games"]
    assert [g["id"] for g in found] == ["bird-1", "chess-1"]  # newest first; the forgery is gone
    bird = found[0]
    assert bird["author"] == {"pubkey": ditto.pubkey, "name": "Ditto Dev"} and bird["description"] == "A bird game"
    # The first announcement is credited (and its file used); the later picture is borrowed.
    assert webxdc.App("bird-1", "https://b.example/bird.xdc", None, "Bird", "https://b.example/bird.png") == \
        games.app.state.games_catalog["apps"]["bird-1"]
    picture = games.get("/xdc/api/cartridge?game=chess-1", headers=headers)
    assert picture.status_code == 200 and Image.open(io.BytesIO(picture.content)).size == (480, 560)
    assert games.get("/xdc/api/catalog").status_code == 401


def test_opening_from_the_hub_puts_the_game_and_its_card_in_the_room(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown, room = joined_games_room(games, homeserver, relay_pool)
    headers = auth(games)
    from bridge.nostr import nip19

    link = "https://ditto.pub/" + nip19.nevent_encode(nip19.EventPointer(post["id"], (), alice.pubkey, 1))
    relay_pool.network.append(post)
    opened = games.post("/xdc/api/open", headers=headers, json={"room_id": room, "link": link})
    assert opened.status_code == 200 and opened.json()["message"] == "Opened Chess."
    widget = homeserver._room(room)["state"][("m.widget", HUB)]["content"]
    assert widget["data"]["game"] == GAME
    card = homeserver.messages(room)[-1]["content"]
    assert card["msgtype"] == "m.image" and card[webxdc.GAME_FIELD]["id"] == GAME
    # Again by id: the same card, not a second one.
    count = len(homeserver.messages(room))
    assert games.post("/xdc/api/open", headers=headers, json={"room_id": room, "game": GAME}).status_code == 200
    assert len(homeserver.messages(room)) == count
    # Picked in the widget itself, it plays there: the card, but the widget isn't changed.
    for state_type in games_room.WIDGET_STATE_TYPES:
        homeserver._room(room)["state"].pop((state_type, HUB))
    in_hub = games.post("/xdc/api/open", headers=headers, json={"room_id": room, "game": GAME, "in_hub": True})
    assert in_hub.json() == {"message": "Opened Chess.", "game": GAME, "name": "Chess"}
    assert ("m.widget", HUB) not in homeserver._room(room)["state"] and len(homeserver.messages(room)) == count
    # Someone who can't change the room's widgets can't.
    other = {"Authorization": "Bearer " + session_for(games, "@admin:example.org")}
    assert games.post("/xdc/api/open", headers=other, json={"room_id": room, "game": GAME}).status_code == 403


def test_a_rooms_own_games(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown, room = joined_games_room(games, homeserver, relay_pool)
    headers = auth(games)
    homeserver.media["mxc://example.org/tetris"] = (xdc_bytes(), "application/octet-stream")
    upload = homeserver._store(room, "m.room.message", {"msgtype": "m.file", "body": "tetris.xdc", "filename": "tetris.xdc",
                                                        "url": "mxc://example.org/tetris"}, "@q:example.org")
    # A game post pinned in the room, too.
    homeserver._store(room, "m.room.pinned_events", {"pinned": [shown["event_id"]]}, "@q:example.org", "")
    homeserver._room(room)["events"][shown["event_id"]] = shown
    listed = games.get(f"/xdc/api/room?room_id={room}", headers=headers).json()
    by_name = {g["name"]: g for g in listed["games"]}
    assert listed["can_open"] and set(by_name) == {"Chess", "tetris"}
    assert by_name["Chess"]["sources"][0] == "pinned" and by_name["tetris"]["local"]
    tetris_id = by_name["tetris"]["id"]
    assert tetris_id == webxdc.local_game_id(room, upload["event_id"])
    # It opens like any game, with a card; its picture is its own icon.
    assert games.post("/xdc/api/open", headers=headers, json={"room_id": room, "game": tetris_id}).status_code == 200
    assert homeserver._room(room)["state"][("m.widget", HUB)]["content"]["data"]["game"] == tetris_id
    card = homeserver.messages(room)[-1]["content"]
    pixel = Image.open(io.BytesIO(homeserver.media[card["url"]][0])).convert("RGB").getpixel((240, 254))
    assert card[webxdc.GAME_FIELD]["id"] == tetris_id and pixel == (0, 120, 240)
    # Its file comes from the homeserver, for the room's members only.
    assert games.get(f"/xdc/app?game={tetris_id}", headers=headers).content == xdc_bytes()
    stranger = {"Authorization": "Bearer " + session_for(games, "@admin:example.org")}
    assert games.get(f"/xdc/app?game={tetris_id}", headers=stranger).status_code == 403
    # Played: moves stay in the room, in a thread under the file, and never reach Nostr.
    published = len(relay_pool.published)
    session = session_for(games)
    with games.websocket_connect(f"/xdc/ws?game={tetris_id}&session={session}") as ws:
        init = ws.receive_json()
        assert init["self"]["addr"] == "@q:example.org" and init["updates"] == []
        ws.send_json({"t": "send", "id": 1, "update": {"payload": {"line": 4}, "info": "4 lines"}})
        replies = [ws.receive_json(), ws.receive_json()]
        assert {"t": "ack", "id": 1, "ok": True} in replies
    from tests.conftest import settle

    settle(games)
    [kept] = homeserver.messages(room, webxdc.UPDATE_EVENT_TYPE)
    assert kept["content"]["m.relates_to"]["event_id"] == upload["event_id"]
    assert json.loads(kept["content"]["update"]["content"]) == {"line": 4}
    assert len(relay_pool.published) == published
    with games.websocket_connect(f"/xdc/ws?game={tetris_id}&session={session}") as ws:
        assert [json.loads(u["content"]) for u in ws.receive_json()["updates"]] == [{"line": 4}]
    with games.websocket_connect(f"/xdc/ws?game={tetris_id}&session={session_for(games, '@admin:example.org')}") as ws:
        assert ws.receive_json()["t"] == "error"


def xdc_named(name: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("index.html", "<p>game</p>")
        archive.writestr("manifest.toml", f'name = "{name}"\n')
    return out.getvalue()


def test_a_rooms_game_is_named_by_its_manifest_and_old_cards_follow(games, homeserver, relay_pool) -> None:
    from bridge import games_hub

    q, alice, post, remote, shown, room = joined_games_room(games, homeserver, relay_pool)
    homeserver.media["mxc://example.org/kc"] = (xdc_named("Katawa Crash"), "application/octet-stream")
    upload = homeserver._store(room, "m.room.message", {"msgtype": "m.file", "body": "katawa_crash.xdc",
                                                        "url": "mxc://example.org/kc"}, "@q:example.org")
    game_id = webxdc.local_game_id(room, upload["event_id"])
    # As registered before names came from the manifest, with a card from then.
    run(games, games.app.state.repository.set_state, f"webxdc:local:{game_id}", json.dumps(
        {"room": room, "event": upload["event_id"], "url": "mxc://example.org/kc", "sender": "@q:example.org"}))
    old = webxdc.App(game_id, "mxc://example.org/kc", None, "katawa_crash")
    card = run(games, games_room.ensure_card, games.app, room, old, None, {"room": room})
    assert homeserver._room(room)["events"][card]["content"]["body"].startswith("🎮 katawa_crash")
    # Now it's read from inside the game, and opening it again fixes the card with an edit.
    assert [g["name"] for g in run(games, games_hub.room_games, games.app, room) if g["local"]] == ["Katawa Crash"]
    assert games.post("/xdc/api/open", headers=auth(games), json={"room_id": room, "game": game_id}).status_code == 200
    edit = homeserver.messages(room)[-1]["content"]
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": card}
    assert edit["m.new_content"]["body"].startswith("🎮 Katawa Crash")
    assert edit["m.new_content"][webxdc.GAME_FIELD]["name"] == "Katawa Crash"
    count = len(homeserver.messages(room))
    games.post("/xdc/api/open", headers=auth(games), json={"room_id": room, "game": game_id})
    assert len(homeserver.messages(room)) == count  # once


def test_saves_follow_the_player(games, homeserver, relay_pool) -> None:
    from bridge.game_saves import SAVE_STATE_TYPE

    q, alice, post, remote, shown, room = joined_games_room(games, homeserver, relay_pool)
    session = session_for(games)
    with games.websocket_connect(f"/xdc/ws?game={GAME}&session={session}") as ws:
        assert ws.receive_json()["save"] is None
        ws.send_json({"t": "save", "id": 7, "data": {"localhost/game.swf/kcrashdata": "AL8AAAt0", "best": "42"}})
        assert ws.receive_json() == {"t": "ack", "id": 7, "ok": True}
        ws.send_json({"t": "save", "id": 8, "data": "not a save"})
        assert ws.receive_json()["ok"] is False
    kept = homeserver._room(room)["state"][(SAVE_STATE_TYPE, GAME)]["content"]
    assert kept["data"] == {"localhost/game.swf/kcrashdata": "AL8AAAt0", "best": "42"}
    with games.websocket_connect(f"/xdc/ws?game={GAME}&session={session}") as ws:
        assert ws.receive_json()["save"]["best"] == "42"
        # A big one goes up as a file.
        ws.send_json({"t": "save", "id": 9, "data": {"big": "x" * 100_000}})
        assert ws.receive_json()["ok"] is True
    kept = homeserver._room(room)["state"][(SAVE_STATE_TYPE, GAME)]["content"]
    assert kept["url"].startswith("mxc://") and "data" not in kept
    with games.websocket_connect(f"/xdc/ws?game={GAME}&session={session}") as ws:
        assert ws.receive_json()["save"] == {"big": "x" * 100_000}
    # Someone else's save is their own.
    with games.websocket_connect(f"/xdc/ws?game={GAME}&session={session_for(games, '@admin:example.org')}") as ws:
        assert ws.receive_json()["save"] is None


def test_widget_sessions_last_while_used_and_through_a_restart(repository, homeserver, relay_pool) -> None:
    from bridge import widget
    from bridge.config import parse_config
    from tests.conftest import make_client, raw_config

    config = parse_config(raw_config(webxdc={"enabled": True}))
    with make_client(config, repository, homeserver, relay_pool) as first:
        session = widget._new_session("@q:example.org")
        headers = {"Authorization": "Bearer " + session}
        assert first.get("/widget/api/ping", headers=headers).json() == {"ok": True}
        from tests.conftest import settle

        settle(first)
        # Kept under a hash of the token, never the token itself.
        kept = run(first, repository.list_state, "widget:session:")
        assert len(kept) >= 1 and all(session not in k + v for k, v in kept)
    widget._sessions.clear()  # a restart: nothing in memory
    with make_client(config, repository, homeserver, relay_pool) as second:
        assert second.get("/widget/api/ping", headers=headers).status_code == 200
        assert second.get("/widget/api/ping", headers={"Authorization": "Bearer nope"}).status_code == 401
    # Used, it lasts longer; unused for a day, it's gone.
    digest = widget._hash(session)
    user, expires = widget._sessions[digest]
    widget._sessions[digest] = (user, time.time() - 1)
    assert widget.user_of_session(session) is None
