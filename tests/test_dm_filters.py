"""Screening DM requests: [dmfilter (bridge.dm_filters)."""

from __future__ import annotations

import json
import time

import pytest

from bridge.dm_filters import DmFilter, _wrong_language, load, update
from bridge.nostr import nip59
from tests.test_federation import Stranger, command, create_id, relay_send, run

ENGLISH = "Hello there, I wanted to ask you about the post you made yesterday."
RUSSIAN = "Привет! Хочешь легко заработать деньги? Напиши мне прямо сейчас, это быстро."


def _dm(client, q, stranger, text):
    rumor = nip59.make_rumor(stranger.pubkey, 14, text, [["p", q.pubkey]], int(time.time()))
    return relay_send(client, nip59.wrap_message(rumor, stranger.secret, q.pubkey).to_dict())


def _room(client, q, stranger):
    return run(client, client.app.state.repository.get_dm_room, q.id, stranger.pubkey)


def _with_profile(relay_pool, stranger, **fields):
    relay_pool.network.append(stranger.event(0, json.dumps(fields)))


def _filters(client, user="@q:example.org"):
    return run(client, load, client.app, user)


def test_keyword(client, homeserver) -> None:
    q = create_id(client)
    command(client, "[dmfilter keyword add free money")
    spammer, friend = Stranger(), Stranger()
    reply = _dm(client, q, spammer, "Get FREE MONEY now!!")
    assert reply[2] is True and _room(client, q, spammer) is None  # answered as delivered
    assert run(client, client.app.state.repository.get_remote_account, spammer.pubkey) is None  # no ghost
    _dm(client, q, friend, "is the money free? asking for a friend")
    assert _room(client, q, friend)
    assert _filters(client).filtered == 1


def test_an_open_conversation_and_followed_accounts_are_never_filtered(client) -> None:
    q = create_id(client)
    friend = Stranger()
    _dm(client, q, friend, "hello")
    command(client, "[dmfilter keyword add hello")
    _dm(client, q, friend, "hello again")
    room = _room(client, q, friend)
    assert room
    followed = Stranger()
    run(client, client.app.state.repository.add_follow, q.id, followed.pubkey, "2026-01-01T00:00:00+00:00")
    _dm(client, q, followed, "hello from someone you follow")
    assert _room(client, q, followed)
    assert _filters(client).filtered == 0


def test_language(client) -> None:
    pytest.importorskip("langdetect")
    q = create_id(client)
    command(client, "[dmfilter language en")
    russian, english, short = Stranger(), Stranger(), Stranger()
    _dm(client, q, russian, RUSSIAN)
    _dm(client, q, english, ENGLISH)
    _dm(client, q, short, "привет")  # too short to judge
    assert _room(client, q, russian) is None
    assert _room(client, q, english) and _room(client, q, short)


def test_no_display_name(client, relay_pool) -> None:
    q = create_id(client)
    command(client, "[dmfilter noname on")
    nameless, no_profile, named = Stranger(), Stranger(), Stranger()
    _with_profile(relay_pool, nameless, about="buy now")
    _with_profile(relay_pool, named, name="Alice")
    for who in (nameless, no_profile, named):
        _dm(client, q, who, "hi")
    assert _room(client, q, nameless) is None and _room(client, q, no_profile) is None
    assert _room(client, q, named)


def test_nip05_domain(client, relay_pool) -> None:
    q = create_id(client)
    command(client, "[dmfilter domain add Spam.example")
    spammer, sub, other = Stranger(), Stranger(), Stranger()
    _with_profile(relay_pool, spammer, name="Bot", nip05="bot1@spam.example")
    _with_profile(relay_pool, sub, name="Bot", nip05="x@relay.spam.example")
    _with_profile(relay_pool, other, name="Ok", nip05="me@notspam.example")
    for who in (spammer, sub, other):
        _dm(client, q, who, "hi")
    assert _room(client, q, spammer) is None and _room(client, q, sub) is None
    assert _room(client, q, other)


def test_settings_are_per_user(client) -> None:
    create_id(client)
    command(client, "[dmfilter keyword add crypto")
    assert _filters(client).keywords == ("crypto",)
    assert _filters(client, "@someone:example.org") == DmFilter()


def test_update() -> None:
    settings, error = update(DmFilter(), "keyword add \"Airdrop\"")
    assert error is None and settings.keywords == ("Airdrop",)
    assert update(settings, "keyword add airdrop")[0].keywords == ("Airdrop",)
    assert update(settings, "keyword remove AIRDROP")[0].keywords == ()
    assert update(DmFilter(), "language en, de")[0].languages == ("en", "de")
    assert update(DmFilter(languages=("en",)), "language any")[0].languages == ()
    assert update(DmFilter(), "language english")[1]
    assert update(DmFilter(), "domain add https://Spam.Example/x")[0].domains == ("spam.example",)
    assert update(DmFilter(), "domain add not a domain")[1]
    assert update(DmFilter(), "noname on")[0].require_name is True
    assert update(DmFilter(keywords=("a",), filtered=3), "clear")[0] == DmFilter(filtered=3)
    assert update(DmFilter(), "nonsense")[1] == "unknown"


def test_language_guess_is_cautious() -> None:
    pytest.importorskip("langdetect")
    assert _wrong_language(("en",), RUSSIAN) == "ru"
    assert _wrong_language(("en",), ENGLISH) is None
    assert _wrong_language(("en",), "hi") is None
