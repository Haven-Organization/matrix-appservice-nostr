"""The spam layers (bridge.spam), with a fake clock and an in-memory ban store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bridge.nostr import event as nostr_event
from bridge.nostr import secp256k1
from bridge.nostr.event import compute_id
from bridge.spam import (
    Ban,
    RateLimiter,
    RateSpec,
    SpamConfig,
    SpamGuard,
    Tier,
    TokenBucket,
    committed_pow,
    meets_pow,
    pow_difficulty,
)

T0 = 1_800_000_000.0
TARGET = "aa" * 32


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.wall = datetime.fromtimestamp(T0, tz=timezone.utc)

    def now(self) -> float:
        return self.t

    def wall_now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.wall += timedelta(seconds=seconds)


class MemoryBanStore:
    def __init__(self) -> None:
        self.bans: dict[tuple[str, str], Ban] = {}

    async def get_ban(self, kind: str, key: str) -> Ban | None:
        return self.bans.get((kind, key))

    async def put_ban(self, ban: Ban) -> None:
        self.bans[(ban.kind, ban.key)] = ban

    async def delete_ban(self, kind: str, key: str) -> None:
        self.bans.pop((kind, key), None)

    async def list_bans(self) -> list[Ban]:
        return list(self.bans.values())


def make_guard(**overrides):
    clock = FakeClock()
    store = MemoryBanStore()
    config = SpamConfig(**overrides)
    guard = SpamGuard(config, store, now=clock.now, wall_now=clock.wall_now)
    return guard, clock, store


_SECRETS: dict[str, bytes] = {}


def key(name: str) -> bytes:
    return _SECRETS.setdefault(name, secp256k1.generate_secret_key())


def note(secret: bytes, content: str = "hello there", *, created_at: int | None = None, kind: int = 1, tags=()) -> nostr_event.Event:
    return nostr_event.build(
        secret, kind=kind, content=content, created_at=int(created_at if created_at is not None else T0), tags=tags
    )


# -- building blocks ------------------------------------------------------------------


def test_token_bucket_refills() -> None:
    bucket = TokenBucket(RateSpec(2, 1, 10), now=0)
    assert bucket.take(0) and bucket.take(0) and not bucket.take(0)
    assert not bucket.take(5)
    assert bucket.take(10)
    assert bucket.full(100) and bucket.tokens == 2


def test_rate_limiter_bounded() -> None:
    limiter = RateLimiter(RateSpec(1, 1, 60), max_keys=3)
    for name in "abcd":
        assert limiter.take(name, 0)
    assert len(limiter) == 3  # "a" evicted
    assert limiter.take("a", 0)  # comes back full
    limiter.sweep(1000)
    assert len(limiter) == 0


def test_pow_difficulty() -> None:
    assert pow_difficulty("f" * 64) == 0
    assert pow_difficulty("0" * 64) == 256
    assert pow_difficulty("000006" + "f" * 58) == 21  # NIP-13's own example: 0x000006 -> 21 bits
    assert pow_difficulty("8" + "0" * 63) == 0
    assert pow_difficulty("1" + "0" * 63) == 3


def _mined(secret: bytes, bits: int, target: int | None) -> nostr_event.Event:
    """An event whose id has at least ``bits`` leading zero bits."""
    pubkey = secp256k1.public_key(secret).hex()
    for nonce in range(2_000_000):
        tags = [["nonce", str(nonce), str(target)]] if target is not None else [["nonce", str(nonce)]]
        if pow_difficulty(compute_id(pubkey, int(T0), 1, tags, "mined")) >= bits:
            return note(secret, "mined", tags=tags)
    raise AssertionError("couldn't mine")


def test_committed_pow() -> None:
    secret = key("miner")
    honest = _mined(secret, 8, 8)
    assert committed_pow(honest) >= 8 and meets_pow(honest, 8)
    # Lucky but committed to less than required: doesn't count.
    low_target = _mined(secret, 8, 4)
    assert committed_pow(low_target) == 4 and not meets_pow(low_target, 8)
    # No commitment at all: doesn't count.
    uncommitted = _mined(secret, 8, None)
    assert committed_pow(uncommitted) == 0 and not meets_pow(uncommitted, 8)
    assert meets_pow(note(secret), 0)


# -- content checks ------------------------------------------------------------------------


async def test_size_and_timestamps() -> None:
    guard, clock, _ = make_guard(max_event_bytes=600)
    secret = key("a")
    big = note(secret, "x" * 1000)
    verdict = await guard.check_event(big, tier=Tier.STRANGER, targets=[TARGET])
    assert not verdict.ok and verdict.prefix == "invalid" and verdict.reply.startswith("invalid: event is")

    future = note(secret, created_at=int(T0) + 3600)
    assert (await guard.check_event(future, tier=Tier.STRANGER)).message == "created_at is too far in the future"
    slightly_ahead = note(secret, "slightly ahead", created_at=int(T0) + 600)
    assert (await guard.check_event(slightly_ahead, tier=Tier.STRANGER)).ok

    old = note(secret, "an old reply", created_at=int(T0) - 5 * 86400)
    assert not (await guard.check_event(old, tier=Tier.STRANGER, interaction=True)).ok
    # Old is fine when it isn't a fresh interaction (backfill).
    assert (await guard.check_event(old, tier=Tier.STRANGER, interaction=False)).ok


async def test_hellthread_base64_and_patterns() -> None:
    guard, _, _ = make_guard(spam_patterns=(r"(?i)free\s+bitcoin",))
    secret = key("b")
    many = note(secret, "look", tags=[["p", f"{i:064x}"] for i in range(21)])
    verdict = await guard.check_event(many, tier=Tier.STRANGER)
    assert verdict.prefix == "blocked" and "21 accounts" in verdict.message
    ok_many = note(secret, "fine", tags=[["p", f"{i:064x}"] for i in range(20)])
    assert (await guard.check_event(ok_many, tier=Tier.STRANGER)).ok

    inline = note(secret, "pic: data:image/png;base64," + "A" * 300)
    assert (await guard.check_event(inline, tier=Tier.STRANGER)).message.startswith("base64-embedded media")

    spam = note(secret, "Get FREE   Bitcoin now")
    assert (await guard.check_event(spam, tier=Tier.STRANGER)).message == "content matches a spam pattern"


async def test_duplicate_wave() -> None:
    guard, clock, _ = make_guard(duplicate_min_pubkeys=3)
    text = "check out my amazing new token"
    assert (await guard.check_event(note(key("d1"), text), tier=Tier.STRANGER)).ok
    assert (await guard.check_event(note(key("d2"), text), tier=Tier.STRANGER)).ok
    third = await guard.check_event(note(key("d3"), text), tier=Tier.STRANGER)
    assert not third.ok and "several accounts" in third.message
    # The same account repeating itself isn't a wave.
    guard2, _, _ = make_guard(duplicate_min_pubkeys=3)
    for _ in range(3):
        assert (await guard2.check_event(note(key("d1"), text), tier=Tier.STRANGER)).ok
    # Short texts and reactions are never judged.
    for name in ("s1", "s2", "s3"):
        assert (await guard.check_event(note(key(name), "gm"), tier=Tier.STRANGER)).ok
        assert (await guard.check_event(note(key(name), "+" * 20, kind=7), tier=Tier.STRANGER)).ok
    # Past the window, the text is fresh again.
    clock.advance(601)
    assert (await guard.check_event(note(key("d4"), text), tier=Tier.STRANGER)).ok


# -- rate limits ------------------------------------------------------------------------------


async def test_ip_and_pubkey_limits() -> None:
    guard, clock, _ = make_guard(events_per_pubkey=RateSpec(3, 1, 60), events_per_ip=RateSpec(100, 1, 1))
    secret = key("r")
    for i in range(3):
        assert (await guard.check_event(note(secret, f"message {i}"), ip="1.2.3.4", tier=Tier.STRANGER)).ok
    limited = await guard.check_event(note(secret, "message 4"), ip="1.2.3.4", tier=Tier.STRANGER)
    assert limited.prefix == "rate-limited" and limited.message == "too many events from this key"
    clock.advance(60)
    assert (await guard.check_event(note(secret, "message 5"), ip="1.2.3.4", tier=Tier.STRANGER)).ok

    guard, _, _ = make_guard(events_per_ip=RateSpec(2, 1, 180))
    assert (await guard.check_event(note(key("i1")), ip="9.9.9.9", tier=Tier.STRANGER)).ok
    assert (await guard.check_event(note(key("i2"), "second"), ip="9.9.9.9", tier=Tier.STRANGER)).ok
    third = await guard.check_event(note(key("i3"), "third"), ip="9.9.9.9", tier=Tier.STRANGER)
    assert third.message == "too many events from this address"
    # No IP (from the bridge's own subscriptions): no IP limit.
    assert (await guard.check_event(note(key("i4"), "fourth"), tier=Tier.STRANGER)).ok


async def test_target_limit_and_trusted_multiplier() -> None:
    guard, _, _ = make_guard(events_per_pubkey_target=RateSpec(2, 1, 3600), trusted_multiplier=2.0)
    secret = key("t")
    for i in range(2):
        assert (await guard.check_event(note(secret, f"hey {i}"), tier=Tier.STRANGER, targets=[TARGET])).ok
    third = await guard.check_event(note(secret, "hey 3"), tier=Tier.STRANGER, targets=[TARGET])
    assert third.message == "too many events to this account"
    # Another target is a different bucket.
    assert (await guard.check_event(note(secret, "hey other"), tier=Tier.STRANGER, targets=["bb" * 32])).ok
    # Trusted pubkeys get bigger buckets (2 x 2 = 4).
    friend = key("friend")
    for i in range(4):
        assert (await guard.check_event(note(friend, f"hi {i}"), tier=Tier.TRUSTED, targets=[TARGET])).ok
    assert not (await guard.check_event(note(friend, "hi 5"), tier=Tier.TRUSTED, targets=[TARGET])).ok


async def test_connection_and_req_limits() -> None:
    guard, clock, _ = make_guard(connections=RateSpec(2, 1, 300), reqs=RateSpec(1, 1, 60))
    assert (await guard.check_connection("5.5.5.5")).ok
    assert (await guard.check_connection("5.5.5.5")).ok
    assert (await guard.check_connection("5.5.5.5")).prefix == "rate-limited"
    assert (await guard.check_connection(None)).ok
    assert (await guard.check_req("5.5.5.5")).ok
    assert not (await guard.check_req("5.5.5.5")).ok
    clock.advance(60)
    assert (await guard.check_req("5.5.5.5")).ok


async def test_local_skips_everything_but_size() -> None:
    guard, _, _ = make_guard(events_per_pubkey=RateSpec(1, 1, 3600), max_p_tags=1, inbound_policy="web_of_trust")
    secret = key("local")
    for i in range(5):
        tags = [["p", "aa" * 32], ["p", "bb" * 32]]
        assert (await guard.check_event(note(secret, f"mine {i}", tags=tags), ip="1.1.1.1", tier=Tier.LOCAL)).ok
    guard2, _, _ = make_guard(max_event_bytes=400)
    assert not (await guard2.check_event(note(secret, "x" * 500), tier=Tier.LOCAL)).ok


# -- strikes and bans -------------------------------------------------------------------------------


async def test_strikes_ban_and_escalate() -> None:
    guard, clock, store = make_guard(strikes_to_ban=3, max_p_tags=1)
    secret = key("spammer")
    pubkey = secp256k1.public_key(secret).hex()
    hellthread = lambda i: note(secret, f"spam {i}", tags=[["p", "aa" * 32], ["p", "bb" * 32]])  # noqa: E731
    for i in range(3):
        assert not (await guard.check_event(hellthread(i), ip="6.6.6.6", tier=Tier.STRANGER)).ok
    ban = store.bans[("pubkey", pubkey)]
    assert ban.level == 1 and ban.reason.startswith("automatic:")
    assert store.bans[("ip", "6.6.6.6")].level == 1
    assert ban.until == (clock.wall + timedelta(hours=1)).isoformat()

    # Banned: even a clean event is refused, and that doesn't add strikes.
    clean = await guard.check_event(note(secret, "innocent"), tier=Tier.STRANGER)
    assert clean.prefix == "blocked" and "banned" in clean.message
    assert (await guard.check_connection("6.6.6.6")).prefix == "blocked"

    # Expired after an hour; the next offence escalates to a day.
    clock.advance(3601)
    assert (await guard.check_event(note(secret, "back again"), tier=Tier.STRANGER)).ok
    for i in range(3):
        await guard.check_event(hellthread(10 + i), tier=Tier.STRANGER)
    ban = store.bans[("pubkey", pubkey)]
    assert ban.level == 2 and ban.until == (clock.wall + timedelta(days=1)).isoformat()

    # Then a week, and it stays at a week.
    for level in (3, 4):
        clock.advance(8 * 86400 if level == 4 else 86401)
        for i in range(3):
            await guard.check_event(hellthread(100 * level + i), tier=Tier.STRANGER)
        ban = store.bans[("pubkey", pubkey)]
        assert ban.level == level and ban.until == (clock.wall + timedelta(days=7)).isoformat()

    # After the escalation memory, it starts over.
    clock.advance(40 * 86400)
    for i in range(3):
        await guard.check_event(hellthread(900 + i), tier=Tier.STRANGER)
    assert store.bans[("pubkey", pubkey)].level == 1


async def test_strikes_expire_from_window() -> None:
    guard, clock, store = make_guard(strikes_to_ban=3, strike_window_seconds=600, max_p_tags=1)
    secret = key("slow")
    for i in range(5):
        await guard.check_event(note(secret, f"s{i}", tags=[["p", "aa" * 32], ["p", "bb" * 32]]), tier=Tier.STRANGER)
        clock.advance(400)
    assert store.bans == {}


async def test_policy_rejections_are_not_strikes() -> None:
    guard, _, store = make_guard(strikes_to_ban=2, inbound_policy="web_of_trust")
    secret = key("stranger")
    for i in range(5):
        verdict = await guard.check_event(note(secret, f"hello {i}"), tier=Tier.STRANGER)
        assert verdict.prefix == "restricted"
    assert store.bans == {}


async def test_manual_ban_unban_and_reports() -> None:
    guard, clock, store = make_guard(strikes_to_ban=4)
    secret = key("reported")
    pubkey = secp256k1.public_key(secret).hex()
    forever = await guard.ban("pubkey", pubkey, None, "spam account")
    assert forever.until is None and forever.level == 1
    clock.advance(10 * 365 * 86400)
    assert not (await guard.check_event(note(secret), tier=Tier.STRANGER)).ok
    await guard.unban("pubkey", pubkey)
    assert (await guard.check_event(note(secret, "freed"), tier=Tier.STRANGER)).ok
    assert await guard.list_bans() == []

    await guard.record_report(pubkey, strikes=3)
    assert store.bans == {}
    await guard.record_report(pubkey)  # a redaction: the fourth strike
    assert store.bans[("pubkey", pubkey)].reason == "automatic: reported in Matrix"

    with pytest.raises(ValueError):
        await guard.ban("email", "x", None, "nope")


# -- policy and tiers ----------------------------------------------------------------------------


async def test_policy_by_tier() -> None:
    secret = key("p")
    for tier in (Tier.TRUSTED, Tier.WEB_OF_TRUST):
        guard, _, _ = make_guard(inbound_policy="web_of_trust")
        assert (await guard.check_event(note(secret), tier=tier)).ok

    guard, _, _ = make_guard(inbound_policy="pow", stranger_pow_bits=8)
    verdict = await guard.check_event(note(secret, "no work done"), tier=Tier.STRANGER)
    assert verdict.prefix == "pow" and verdict.reply.startswith("pow: proof-of-work of at least 8 bits")
    assert (await guard.check_event(_mined(secret, 8, 8), tier=Tier.STRANGER)).ok
    assert (await guard.check_event(note(secret, "friend"), tier=Tier.WEB_OF_TRUST)).ok

    guard, _, _ = make_guard()  # open
    assert (await guard.check_event(note(secret, "anyone"), tier=Tier.STRANGER)).ok


# -- wave detection --------------------------------------------------------------------------------


async def test_shields_up_and_down() -> None:
    calls: list[tuple[bool, int]] = []
    guard, clock, _ = make_guard(wave_threshold=5, wave_window_seconds=300, wave_cooldown_seconds=3600,
                                 stranger_pow_bits=8)

    async def announce(up: bool, count: int) -> None:
        calls.append((up, count))

    guard.on_shields_change = announce
    for i in range(4):
        assert (await guard.check_event(note(key(f"w{i}"), f"wave {i}"), tier=Tier.STRANGER)).ok
    assert not guard.shields_up
    fifth = await guard.check_event(note(key("w4"), "wave 4"), tier=Tier.STRANGER)
    assert guard.shields_up and calls == [(True, 5)]
    assert fifth.prefix == "pow" and guard.effective_policy == "pow"
    # Trusted and web-of-trust accounts are never affected.
    assert (await guard.check_event(note(key("friend"), "hi"), tier=Tier.TRUSTED)).ok
    assert (await guard.check_event(note(key("fof"), "hi"), tier=Tier.WEB_OF_TRUST)).ok
    # Strangers with PoW still get through.
    assert (await guard.check_event(_mined(key("w9"), 8, 8), tier=Tier.STRANGER)).ok

    # Quiet for the cooldown: shields come down by themselves (via tick).
    clock.advance(3601)
    await guard.tick()
    assert not guard.shields_up and calls[-1] == (False, 0)
    assert (await guard.check_event(note(key("w10"), "calm again"), tier=Tier.STRANGER)).ok


async def test_wave_policy_never_loosens() -> None:
    guard, _, _ = make_guard(inbound_policy="web_of_trust", wave_policy="pow", wave_threshold=1)
    await guard.check_event(note(key("x"), "first"), tier=Tier.STRANGER)
    assert guard.effective_policy == "web_of_trust"


async def test_sync_callback_is_fine() -> None:
    seen: list[bool] = []
    guard, _, _ = make_guard(wave_threshold=1)
    guard.on_shields_change = lambda up, count: seen.append(up)
    await guard.check_event(note(key("y"), "one"), tier=Tier.STRANGER)
    assert seen == [True]


async def test_stale_strangers_drop_out_of_wave() -> None:
    guard, clock, _ = make_guard(wave_threshold=3, wave_window_seconds=300)
    await guard.check_event(note(key("o1"), "one"), tier=Tier.STRANGER)
    await guard.check_event(note(key("o2"), "two"), tier=Tier.STRANGER)
    clock.advance(301)
    await guard.check_event(note(key("o3"), "three"), tier=Tier.STRANGER)
    assert not guard.shields_up


# -- gift wraps -------------------------------------------------------------------------------------


async def test_gift_wraps_judged_by_ip_and_target() -> None:
    guard, _, store = make_guard(events_per_pubkey=RateSpec(1, 1, 3600),
                                 events_per_pubkey_target=RateSpec(2, 1, 3600), inbound_policy="web_of_trust",
                                 strikes_to_ban=2)
    # Each wrap has a fresh key; a two-day-old timestamp is normal (NIP-59).
    def wrap(i: int) -> nostr_event.Event:
        return note(secp256k1.generate_secret_key(), f"ciphertext {i}", kind=1059,
                    created_at=int(T0) - 2 * 86400, tags=[["p", TARGET]])

    assert (await guard.check_event(wrap(1), ip="7.7.7.7", tier=Tier.STRANGER, targets=[TARGET], interaction=True)).ok
    assert (await guard.check_event(wrap(2), ip="7.7.7.7", tier=Tier.STRANGER, targets=[TARGET], interaction=True)).ok
    third = await guard.check_event(wrap(3), ip="7.7.7.7", tier=Tier.STRANGER, targets=[TARGET], interaction=True)
    assert third.message == "too many events to this account"
    # Strikes land on the IP only; throwaway keys are never banned.
    await guard.check_event(wrap(4), ip="7.7.7.7", tier=Tier.STRANGER, targets=[TARGET])
    assert set(store.bans) == {("ip", "7.7.7.7")}


async def test_unwrapped_sender() -> None:
    guard, _, _ = make_guard(events_per_pubkey=RateSpec(1, 1, 3600))
    sender = secp256k1.public_key(key("dm")).hex()
    assert (await guard.check_unwrapped_sender(sender, Tier.STRANGER)).ok
    assert (await guard.check_unwrapped_sender(sender, Tier.STRANGER)).prefix == "rate-limited"
    assert (await guard.check_unwrapped_sender(sender, Tier.LOCAL)).ok

    guard, _, _ = make_guard(inbound_policy="web_of_trust")
    assert (await guard.check_unwrapped_sender(sender, Tier.STRANGER)).prefix == "restricted"
    assert (await guard.check_unwrapped_sender(sender, Tier.WEB_OF_TRUST)).ok
    guard, _, _ = make_guard(inbound_policy="pow")
    assert (await guard.check_unwrapped_sender(sender, Tier.STRANGER)).ok
    await guard.ban("pubkey", sender, timedelta(hours=1), "dm spam")
    assert (await guard.check_unwrapped_sender(sender, Tier.TRUSTED)).prefix == "blocked"


# -- memory -----------------------------------------------------------------------------------------


async def test_memory_is_swept() -> None:
    guard, clock, _ = make_guard(max_p_tags=1, strikes_to_ban=100)
    for i in range(50):
        secret = secp256k1.generate_secret_key()
        await guard.check_event(note(secret, f"unique text number {i}"), ip=f"10.0.0.{i}", tier=Tier.STRANGER)
        await guard.check_event(note(secret, "x", tags=[["p", "aa" * 32], ["p", "bb" * 32]]), tier=Tier.STRANGER)
    sizes = guard.memory_sizes()
    assert sizes["strikes"] > 0 and sizes["duplicates"] > 0 and sizes["buckets"] > 0
    clock.advance(4 * 3600)
    await guard.tick()
    assert guard.memory_sizes() == {"buckets": 0, "strikes": 0, "duplicates": 0, "wave": 0, "ban_cache": 0}


# -- config ------------------------------------------------------------------------------------------


def test_config_from_dict() -> None:
    config = SpamConfig.from_dict(
        {
            "inbound_policy": "pow",
            "stranger_pow_bits": 16,
            "connections": {"burst": 50, "tokens": 2, "per_seconds": 60},
            "spam_patterns": ["(?i)airdrop"],
            "ban_durations_seconds": [60, 120],
            "trusted_multiplier": 3,
        }
    )
    assert config.inbound_policy == "pow" and config.stranger_pow_bits == 16
    assert config.connections == RateSpec(50, 2.0, 60.0)
    assert config.spam_patterns == ("(?i)airdrop",) and config.ban_durations_seconds == (60, 120)
    assert SpamConfig.from_dict(None) == SpamConfig()
    assert SpamConfig.from_dict({}) == SpamConfig()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"inbound_policy": "closed"}, "inbound_policy must be one of"),
        ({"wave_policy": "nope"}, "wave_policy must be one of"),
        ({"spam_patterns": ["(unclosed"]}, "not a valid regular expression"),
        ({"spam_patterns": "airdrop"}, "must be a list"),
        ({"connections": {"burst": 0}}, "connections.burst must be at least 1"),
        ({"connections": [1, 2]}, "must be a mapping"),
        ({"strikes_to_ban": 0}, "strikes_to_ban must be at least 1"),
        ({"max_p_tags": "20"}, "must be a whole number"),
        ({"stranger_pow_bits": 300}, "between 0 and 256"),
        ({"ban_durations_seconds": []}, "non-empty list"),
        ({"strike_window": 5}, "unknown spam setting"),
        ({"stranger_pow_bits": True}, "whole number"),
    ],
)
def test_config_validation(raw, message) -> None:
    with pytest.raises(ValueError, match=message):
        SpamConfig.from_dict(raw)
