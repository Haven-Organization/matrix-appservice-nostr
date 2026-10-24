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
    assert json.loads(sent["content"]) == {"move": "d4"}
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


def play(client, homeserver, room, target):
    transact(client, {"type": "m.room.message", "event_id": f"$play{target}", "room_id": room, "sender": "@q:example.org",
                      "content": {"msgtype": "m.text", "body": "[play", "m.relates_to": {"m.in_reply_to": {"event_id": target}}}})


def test_play_opens_the_game_in_the_games_room(games, homeserver, relay_pool) -> None:
    from bridge.games_room import GAME_WIDGET_ID, LAYOUT_STATE_TYPE

    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    play(games, homeserver, remote.room_id, shown["event_id"])
    reply = homeserver.sent_to(remote.room_id)[-1]["body"]
    assert reply.startswith("Opened Chess in your Games room: ") and "widgets" in reply
    games_room = run(games, games.app.state.repository.get_state, "webxdc:room:@q:example.org")
    widget = homeserver._room(games_room)["state"][("im.vector.modular.widgets", GAME_WIDGET_ID)]["content"]
    assert widget["name"] == "🎮 Chess" and f"/xdc/play?game={GAME}" in widget["url"]
    assert "$matrix_user_id" in widget["url"]
    assert homeserver.state_of(games_room, LAYOUT_STATE_TYPE)["widgets"][GAME_WIDGET_ID]["container"] == "center"
    assert (games_room, "@q:example.org") in homeserver.invites
    assert "need a Matrix client that supports widgets" in homeserver.sent_to(games_room)[0]["body"]
    assert GAME in run(games, webxdc.active_games, games.app)

    # Another game replaces the widget, in the same room; no second room.
    other = game_post(alice, game_id="22222222-2222-2222-2222-222222222222", url="https://files.example/tetris.xdc")
    other["tags"][0] = [t if not t.startswith("alt ") else "alt Webxdc app: Tetris" for t in other["tags"][0]]
    other = alice.event(1, other["content"], other["tags"])
    assert run(games, ingest, games.app, other, origin="subscription").stored
    second = homeserver.messages(remote.room_id)[-1]
    play(games, homeserver, remote.room_id, second["event_id"])
    widget = homeserver._room(games_room)["state"][("m.widget", GAME_WIDGET_ID)]["content"]
    assert widget["name"] == "🎮 Tetris"
    assert run(games, games.app.state.repository.get_state, "webxdc:room:@q:example.org") == games_room


def test_play_needs_a_game_post(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    plain = alice.event(1, "not a game")
    run(games, ingest, games.app, plain, origin="subscription")
    play(games, homeserver, remote.room_id, homeserver.messages(remote.room_id)[-1]["event_id"])
    assert "Reply \"[play\" to a game post" in homeserver.sent_to(remote.room_id)[-1]["body"]


def test_play_is_off_when_games_are(client, homeserver, relay_pool) -> None:
    send(client, message("[create profile"))
    send(client, message("[play"))
    assert homeserver.sent_to("!dm:example.org")[-1]["body"] == "Games aren't turned on on this bridge."


# -- player, websocket and sandbox origin ------------------------------------------------


def session_for(client, user="@q:example.org"):
    from bridge.widget import _new_session

    return _new_session(user)


def test_sandbox_host_serves_only_the_loader_and_worker(games) -> None:
    host = {"host": "xdc.example.org"}
    loader = games.get("/__sandbox__/", headers=host)
    assert loader.status_code == 200 and "webxdc sandbox" in loader.text
    csp = loader.headers["content-security-policy"]
    assert "connect-src 'self' data: blob:" in csp and "http" not in csp
    worker = games.get("/sw.js", headers=host)
    assert worker.headers["service-worker-allowed"] == "/" and "serviceWorker" not in worker.text[:0]
    # Nothing of the bridge is reachable there: not the relay, not the widget, not handles.
    for path in ("/", "/widget", "/.well-known/nostr.json?name=q", "/xdc/play"):
        response = games.get(path, headers=host)
        assert "xdc-retry" in response.text, path
    # And the loader isn't served on the bridge's own host.
    assert games.get("/__sandbox__/").status_code == 404


def test_player_page(games) -> None:
    page = games.get("/xdc/play?game=" + GAME)
    assert page.status_code == 200 and '"sandboxOrigin": "https://xdc.example.org"' in page.text
    assert games.get("/xdc/static/player.js").status_code == 200
    assert games.get("/xdc/static/../server.py").status_code == 404


def test_games_off_means_no_player(client) -> None:
    assert client.get("/xdc/play?game=" + GAME).status_code == 404
    assert "xdc-retry" not in client.get("/", headers={"host": "xdc.example.org"}).text


def test_websocket_play_session(games, homeserver, relay_pool) -> None:
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    earlier = Stranger().event(webxdc.UPDATE_KIND, json.dumps({"move": "e4"}), [["i", GAME], ["alt", "Webxdc update"]])
    run(games, ingest, games.app, earlier, origin="relay")
    session = session_for(games)
    with games.websocket_connect(f"/xdc/ws?game={GAME}&session={session}") as ws:
        init = ws.receive_json()
        assert init["t"] == "init" and init["game"]["name"] == "Chess" and init["self"]["name"] == "q@example.org"
        assert [e["id"] for e in init["updates"]] == [earlier["id"]]
        ws.send_json({"t": "send", "id": 1, "update": {"payload": {"move": "e5"}}, "descr": "e5"})
        first, second = ws.receive_json(), ws.receive_json()
        update = first if first["t"] == "update" else second
        ack = second if first["t"] == "update" else first
        assert ack == {"t": "ack", "id": 1, "ok": True}
        assert json.loads(update["event"]["content"]) == {"move": "e5"} and update["event"]["pubkey"] == q.pubkey
        ws.send_json({"t": "rt_join", "id": 2})
        assert ws.receive_json() == {"t": "ack", "id": 2, "ok": True}
        assert webxdc.hub(games.app).playing(GAME)
    assert not webxdc.hub(games.app).playing(GAME)


def test_websocket_needs_a_session(games, homeserver, relay_pool) -> None:
    followed_game(games, homeserver, relay_pool)
    with games.websocket_connect(f"/xdc/ws?game={GAME}&session=nope") as ws:
        assert ws.receive_json()["t"] == "error"


def ditto_game(author, game_id=GAME):
    """A game as Ditto posts it: a NIP-94 file (kind 1063)."""
    return author.event(1063, "Dino", [["url", "https://blossom.example/dino.xdc"], ["m", webxdc.MIME], ["x", "cd" * 32],
                                       ["size", "70966"], ["alt", "Webxdc app: Dino"], ["webxdc", game_id],
                                       ["image", "https://blossom.example/dino.png"]])


def test_ditto_style_game_is_recognised_and_shown(games, homeserver, relay_pool) -> None:
    import io

    from PIL import Image

    from bridge.safe_fetch import Fetched

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (0, 200, 0)).save(buffer, format="PNG")

    class Fetcher:
        async def get(self, url, **kwargs):
            return Fetched(url, 200, buffer.getvalue(), "image/png")

        async def aclose(self):
            pass

    games.app.state.fetcher = Fetcher()
    [found] = webxdc.apps_of(ditto_game(Stranger()))
    assert found.name == "Dino" and found.url.endswith("dino.xdc") and found.sha256 == "cd" * 32
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    dino = ditto_game(alice, game_id="33333333-3333-3333-3333-333333333333")
    assert run(games, ingest, games.app, dino, origin="subscription").stored
    content = homeserver.messages(remote.room_id)[-1]["content"]
    assert content["msgtype"] == "m.image"  # the game's icon
    assert "🎮 Dino: reply [play to open" in content["body"] and "dino.xdc" not in content["body"]
    assert content[webxdc.GAME_FIELD]["name"] == "Dino"


def test_play_and_import_take_a_link(games, homeserver, relay_pool) -> None:
    from bridge.nostr import nip19

    send(games, message("[create profile"))
    alice = Stranger()
    dino = ditto_game(alice, game_id="44444444-4444-4444-4444-444444444444")
    relay_pool.network.append(dino)
    link = "https://ditto.pub/" + nip19.nevent_encode(nip19.EventPointer(dino["id"], (), alice.pubkey))
    send(games, message(f"[play {link}"))
    reply = homeserver.sent_to("!dm:example.org")[-1]["body"]
    assert reply.startswith("Opened Dino in your Games room")
    assert run(games, webxdc.game_post, games.app, "44444444-4444-4444-4444-444444444444") is not None
    other = ditto_game(alice, game_id="55555555-5555-5555-5555-555555555555")
    relay_pool.network.append(other)
    send(games, message(f"[import nostr:{nip19.note_encode(other['id'])}"))
    assert homeserver.sent_to("!dm:example.org")[-1]["body"].startswith("Imported: ")


def test_play_posts_a_cartridge_card_that_reloads_the_game(games, homeserver, relay_pool) -> None:
    import io
    import zipfile

    from PIL import Image

    from bridge.games_room import GAME_WIDGET_ID
    from bridge.safe_fetch import Fetched

    icon = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 0, 0)).save(icon, format="PNG")
    xdc = io.BytesIO()
    with zipfile.ZipFile(xdc, "w") as archive:
        archive.writestr("index.html", "<p>chess</p>")
        archive.writestr("icon.png", icon.getvalue())
    fetched: list[str] = []

    class Fetcher:
        async def get(self, url, **kwargs):
            fetched.append(url)
            return Fetched(url, 200, xdc.getvalue(), "application/zip")

        async def aclose(self):
            pass

    games.app.state.fetcher = Fetcher()
    q, alice, post, remote, shown = followed_game(games, homeserver, relay_pool)
    play(games, homeserver, remote.room_id, shown["event_id"])
    games_room = run(games, games.app.state.repository.get_state, "webxdc:room:@q:example.org")
    card = homeserver.messages(games_room)[-1]
    content = card["content"]
    assert content["msgtype"] == "m.image" and content["filename"] == "Chess.png" and fetched == [XDC]
    data, mime = homeserver.media[content["url"]]
    picture = Image.open(io.BytesIO(data))
    assert mime == "image/png" and picture.size == (480, 560)
    assert picture.convert("RGB").getpixel((240, 254)) == (200, 0, 0)  # the icon is the label
    assert "🎮 Chess" in content["body"] and "React 🎮 to this to load it again." in content["body"]
    assert "[play" not in content["body"]
    assert content["external_url"].startswith("https://") and "nevent1" in content["external_url"]
    assert content["com.beeper.per_message_profile"]["id"] == remote.ghost_user_id
    [bot_play] = homeserver.messages(games_room, "m.reaction")
    assert bot_play["content"]["m.relates_to"] == {"rel_type": "m.annotation", "event_id": card["event_id"], "key": "🎮"}
    assert bot_play["sender"] == "@nostrbot:example.org"
    cards = len(homeserver.messages(games_room))

    def widget_gone():
        homeserver._room(games_room)["state"].pop(("m.widget", GAME_WIDGET_ID))

    # Toggling a 🎮 on the card loads the game again: adding it, and taking it away.
    homeserver._room(games_room)["members"].add("@q:example.org")  # q joined it
    widget_gone()
    mine = homeserver._store(games_room, "m.reaction", {"m.relates_to": {
        "rel_type": "m.annotation", "event_id": card["event_id"], "key": "🎮\ufe0f"}}, "@q:example.org")
    transact(games, mine)
    assert ("m.widget", GAME_WIDGET_ID) in homeserver._room(games_room)["state"]
    widget_gone()
    transact(games, {"type": "m.room.redaction", "event_id": "$unreact", "room_id": games_room,
                     "sender": "@q:example.org", "redacts": mine["event_id"], "content": {}})
    assert ("m.widget", GAME_WIDGET_ID) in homeserver._room(games_room)["state"]
    # Neither went to Nostr as a reaction or a deletion; no new card, no notices.
    assert relay_pool.of_kind(7) == [] and relay_pool.of_kind(5) == []
    assert len(homeserver.messages(games_room)) == cards
    # [play again, from anywhere, reuses the card; the picture is made once.
    play(games, homeserver, remote.room_id, shown["event_id"])
    assert len(homeserver.messages(games_room)) == cards and fetched == [XDC]
    # Any other reaction on the card is a reaction to the game post.
    transact(games, homeserver._store(games_room, "m.reaction", {"m.relates_to": {
        "rel_type": "m.annotation", "event_id": card["event_id"], "key": "🔥"}}, "@q:example.org"))
    [fire] = relay_pool.of_kind(7)
    assert fire.content == "🔥" and ("e", post["id"]) in [tuple(t[:2]) for t in fire.tags]
