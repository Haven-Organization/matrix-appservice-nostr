"""Spam protection for everything strangers send the bridge.

The relay only accepts strangers' events that are addressed to a local ID
(replies, mentions, reactions, DMs), and the same events also arrive through
the bridge's own ``#p`` subscriptions on public relays. A bot wave can still
aim thousands of those at one person, so every such event goes through
``SpamGuard.check_event`` before the bridge does anything with it. See
DESIGN.md, "Spam". The layers, cheapest first:

1. Bans (pubkey and IP), kept by a ``BanStore`` so they survive a restart.
2. Cheap content checks: size, timestamps, mass mentions ("hellthreads"),
   base64-embedded media, operator regexes, and the same text arriving from
   many accounts at once.
3. Rate limits: token buckets per IP, per pubkey, and per (pubkey, local
   target).
4. Policy by trust tier: strangers may have to show NIP-13 proof-of-work,
   or be refused, depending on ``inbound_policy``, and on "shields up".

Every rejection from layers 2 and 3 is a strike against the pubkey and IP.
Enough strikes in a window ban them, for longer each time. Bot waves show
up as many never-seen pubkeys at once: past ``wave_threshold`` the guard
raises its shields (strangers need PoW, by default) until things have been
quiet for ``wave_cooldown_seconds``.

Everything here is in memory and bounded, except bans. The clocks are
injectable so tests don't sleep.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Protocol

from bridge.nostr.event import Event

GIFT_WRAP_KIND = 1059
REACTION_KIND = 7

_POLICIES = ("open", "pow", "web_of_trust")
_BASE64_MEDIA_RE = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]{200,}", re.IGNORECASE)
_BAN_CACHE_SECONDS = 30.0
_SWEEP_SECONDS = 60.0


# -- storage --------------------------------------------------------------------


@dataclass(frozen=True)
class Ban:
    kind: str  # "pubkey" | "ip"
    key: str
    until: str | None  # ISO time; None = permanent
    reason: str
    level: int  # escalation count: 1 for a first ban
    created_at: str


class BanStore(Protocol):
    async def get_ban(self, kind: str, key: str) -> Ban | None: ...
    async def put_ban(self, ban: Ban) -> None: ...
    async def delete_ban(self, kind: str, key: str) -> None: ...
    async def list_bans(self) -> list[Ban]: ...


# -- config -----------------------------------------------------------------------


@dataclass(frozen=True)
class RateSpec:
    """A token bucket: ``burst`` tokens at most, ``tokens`` added every
    ``per_seconds``."""

    burst: int
    tokens: float
    per_seconds: float

    @property
    def refill_per_second(self) -> float:
        return self.tokens / self.per_seconds

    def scaled(self, factor: float) -> "RateSpec":
        return RateSpec(max(1, int(self.burst * factor)), self.tokens * factor, self.per_seconds)


@dataclass(frozen=True)
class SpamConfig:
    # Per IP.
    connections: RateSpec = RateSpec(100, 1, 300)
    reqs: RateSpec = RateSpec(100, 20, 60)
    events_per_ip: RateSpec = RateSpec(10, 2, 180)
    # Per pubkey, for events aimed at local IDs, and per (pubkey, local ID).
    events_per_pubkey: RateSpec = RateSpec(10, 5, 3600)
    events_per_pubkey_target: RateSpec = RateSpec(5, 2, 3600)
    # Trusted pubkeys (followed here) get buckets this many times larger.
    trusted_multiplier: float = 5.0
    max_event_bytes: int = 65536
    future_skew_seconds: int = 900
    max_interaction_age_seconds: int = 3 * 24 * 3600
    max_p_tags: int = 20
    duplicate_window_seconds: int = 600
    # The same text from this many distinct pubkeys within the window is a wave.
    duplicate_min_pubkeys: int = 3
    # Shorter texts ("gm", "+") are too common to judge.
    duplicate_min_length: int = 12
    spam_patterns: tuple[str, ...] = ()
    inbound_policy: str = "open"
    stranger_pow_bits: int = 20
    strikes_to_ban: int = 10
    strike_window_seconds: int = 600
    ban_durations_seconds: tuple[int, ...] = (3600, 86400, 7 * 86400)
    escalation_memory_days: int = 30
    wave_threshold: int = 30
    wave_window_seconds: int = 300
    wave_policy: str = "pow"
    wave_cooldown_seconds: int = 3600

    @staticmethod
    def from_dict(raw: dict[str, Any] | None) -> "SpamConfig":
        """Build from the ``spam:`` config section. Raises ``ValueError``
        with a message naming the bad key."""
        raw = dict(raw or {})
        known = {f.name for f in fields(SpamConfig)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown spam setting(s): {', '.join(unknown)}")
        defaults = SpamConfig()
        values: dict[str, Any] = {}
        for f in fields(SpamConfig):
            if f.name not in raw:
                continue
            value = raw[f.name]
            default = getattr(defaults, f.name)
            if isinstance(default, RateSpec):
                values[f.name] = _rate_spec(f.name, value)
            elif f.name == "spam_patterns":
                values[f.name] = _patterns(value)
            elif f.name == "ban_durations_seconds":
                values[f.name] = _durations(value)
            elif f.name in ("inbound_policy", "wave_policy"):
                if value not in _POLICIES:
                    raise ValueError(f"spam.{f.name} must be one of {', '.join(_POLICIES)}, got {value!r}")
                values[f.name] = value
            elif isinstance(default, float):
                values[f.name] = _number(f.name, value, minimum=0.0)
            else:
                values[f.name] = int(_number(f.name, value, minimum=0, integer=True))
        config = SpamConfig(**values)
        for name in ("max_event_bytes", "duplicate_min_pubkeys", "strikes_to_ban", "wave_threshold", "max_p_tags"):
            if getattr(config, name) < 1:
                raise ValueError(f"spam.{name} must be at least 1")
        if not 0 <= config.stranger_pow_bits <= 256:
            raise ValueError("spam.stranger_pow_bits must be between 0 and 256")
        return config


def _number(name: str, value: Any, *, minimum: float, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not float(value).is_integer()):
        raise ValueError(f"spam.{name} must be a {'whole ' if integer else ''}number, got {value!r}")
    if value < minimum:
        raise ValueError(f"spam.{name} must be at least {minimum}, got {value!r}")
    return value


def _rate_spec(name: str, value: Any) -> RateSpec:
    if not isinstance(value, dict) or set(value) - {"burst", "tokens", "per_seconds"}:
        raise ValueError(f"spam.{name} must be a mapping of burst, tokens, per_seconds")
    burst = int(_number(f"{name}.burst", value.get("burst", 1), minimum=1, integer=True))
    tokens = float(_number(f"{name}.tokens", value.get("tokens", 1), minimum=0))
    per_seconds = float(_number(f"{name}.per_seconds", value.get("per_seconds", 1), minimum=0.001))
    return RateSpec(burst, tokens, per_seconds)


def _patterns(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("spam.spam_patterns must be a list of regular expressions")
    for pattern in value:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"spam.spam_patterns: {pattern!r} is not a valid regular expression ({exc})") from exc
    return tuple(value)


def _durations(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("spam.ban_durations_seconds must be a non-empty list of seconds")
    return tuple(int(_number("ban_durations_seconds", item, minimum=1, integer=True)) for item in value)


# -- rate limiting ------------------------------------------------------------------


class TokenBucket:
    __slots__ = ("capacity", "rate", "tokens", "updated")

    def __init__(self, spec: RateSpec, now: float) -> None:
        self.capacity = float(spec.burst)
        self.rate = spec.refill_per_second
        self.tokens = float(spec.burst)
        self.updated = now

    def _refill(self, now: float) -> None:
        if now > self.updated:
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now

    def take(self, now: float, cost: float = 1.0) -> bool:
        self._refill(now)
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def full(self, now: float) -> bool:
        self._refill(now)
        return self.tokens >= self.capacity


class RateLimiter:
    """Token buckets by key, with bounded memory: past ``max_keys`` the
    least recently used buckets are dropped (a dropped bucket comes back
    full, which only ever errs towards letting someone through)."""

    def __init__(self, spec: RateSpec, *, max_keys: int = 20000) -> None:
        self.spec = spec
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def take(self, key: str, now: float, cost: float = 1.0) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self.spec, now)
            self._buckets[key] = bucket
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        return bucket.take(now, cost)

    def sweep(self, now: float) -> None:
        """Forget buckets that have refilled completely."""
        for key in [k for k, b in self._buckets.items() if b.full(now)]:
            del self._buckets[key]

    def __len__(self) -> int:
        return len(self._buckets)


# -- proof of work (NIP-13) ------------------------------------------------------------


def pow_difficulty(event_id_hex: str) -> int:
    """Leading zero bits of an event id."""
    bits = 0
    for char in event_id_hex:
        nibble = int(char, 16)
        if nibble == 0:
            bits += 4
            continue
        return bits + (4 - nibble.bit_length())
    return bits


def committed_pow(event: Event) -> int:
    """The difficulty an event can claim: its actual leading zero bits,
    but no more than the target committed to in its ``nonce`` tag (NIP-13).
    Without a commitment, it claims nothing: otherwise a lucky id from a
    flood of cheap attempts would count."""
    for tag in event.tags:
        if len(tag) >= 3 and tag[0] == "nonce":
            try:
                target = int(tag[2])
            except ValueError:
                return 0
            return min(pow_difficulty(event.id), target)
    return 0


def meets_pow(event: Event, required: int) -> bool:
    return required <= 0 or committed_pow(event) >= required


# -- verdicts and tiers -------------------------------------------------------------------


# Kinds whose p tags are mentions of people (where tagging dozens at once is
# the "hellthread" spam pattern), as opposed to lists of them.
_CONVERSATION_KINDS = frozenset({1, 6, 7, 16, 20, 21, 22, 1111, 1068, 30023})


class Tier(Enum):
    LOCAL = "local"  # signed by one of the bridge's own IDs
    TRUSTED = "trusted"  # followed by a local ID
    WEB_OF_TRUST = "web_of_trust"  # followed by someone a local ID follows
    STRANGER = "stranger"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    prefix: str = ""
    message: str = ""

    @property
    def reply(self) -> str:
        """The ``OK``/``CLOSED`` message: ``"prefix: message"``."""
        return f"{self.prefix}: {self.message}" if self.prefix else self.message


ACCEPT = Verdict(True)

_STRICTNESS = {policy: rank for rank, policy in enumerate(_POLICIES)}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


ShieldsCallback = Callable[[bool, int], "Awaitable[None] | None"]


# -- the guard --------------------------------------------------------------------------


class SpamGuard:
    def __init__(
        self,
        config: SpamConfig,
        store: BanStore,
        *,
        now: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_shields_change: ShieldsCallback | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._now = now
        self._wall_now = wall_now
        self.on_shields_change = on_shields_change
        self._patterns = [re.compile(pattern) for pattern in config.spam_patterns]

        self._connections = RateLimiter(config.connections)
        self._reqs = RateLimiter(config.reqs)
        self._ip_events = RateLimiter(config.events_per_ip)
        self._pubkey_events = RateLimiter(config.events_per_pubkey)
        self._target_events = RateLimiter(config.events_per_pubkey_target)
        multiplier = config.trusted_multiplier
        self._trusted_pubkey_events = RateLimiter(config.events_per_pubkey.scaled(multiplier))
        self._trusted_target_events = RateLimiter(config.events_per_pubkey_target.scaled(multiplier))
        self._limiters = (
            self._connections, self._reqs, self._ip_events, self._pubkey_events, self._target_events,
            self._trusted_pubkey_events, self._trusted_target_events,
        )

        self._strikes: dict[tuple[str, str], deque[float]] = {}
        # content hash -> {pubkey: last seen}
        self._duplicates: OrderedDict[str, dict[str, float]] = OrderedDict()
        self._max_duplicate_hashes = 50000
        # Strangers that passed the cheap checks: pubkey -> last seen.
        self._wave: OrderedDict[str, float] = OrderedDict()
        self._shields_up = False
        self._last_wave_crossing = 0.0
        self._ban_cache: dict[tuple[str, str], tuple[float, Ban | None]] = {}
        self._last_sweep = now()

    # -- public state -------------------------------------------------------

    @property
    def shields_up(self) -> bool:
        return self._shields_up

    @property
    def effective_policy(self) -> str:
        policy = self.config.inbound_policy
        if self._shields_up and _STRICTNESS[self.config.wave_policy] > _STRICTNESS[policy]:
            return self.config.wave_policy
        return policy

    # -- bans ---------------------------------------------------------------

    def _ban_active(self, ban: Ban | None) -> bool:
        return ban is not None and (ban.until is None or _parse_iso(ban.until) > self._wall_now())

    async def active_ban(self, kind: str, key: str) -> Ban | None:
        cache_key = (kind, key)
        now = self._now()
        cached = self._ban_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            ban = cached[1]
        else:
            ban = await self.store.get_ban(kind, key)
            self._ban_cache[cache_key] = (now + _BAN_CACHE_SECONDS, ban)
        return ban if self._ban_active(ban) else None

    async def ban(self, kind: str, key: str, duration: timedelta | None, reason: str) -> Ban:
        """Ban ``key`` (``kind`` is "pubkey", "ip" or "event") for
        ``duration``, or for good with ``None``."""
        if kind not in ("pubkey", "ip", "event"):
            raise ValueError("kind must be 'pubkey', 'ip' or 'event'")
        wall = self._wall_now()
        previous = await self.store.get_ban(kind, key)
        level = self._next_level(previous, wall)
        ban = Ban(kind, key, _iso(wall + duration) if duration is not None else None, reason, level, _iso(wall))
        await self._put(ban)
        return ban

    async def unban(self, kind: str, key: str) -> None:
        await self.store.delete_ban(kind, key)
        self._ban_cache.pop((kind, key), None)
        self._strikes.pop((kind, key), None)

    async def list_bans(self) -> list[Ban]:
        return await self.store.list_bans()

    async def _put(self, ban: Ban) -> None:
        await self.store.put_ban(ban)
        self._ban_cache[(ban.kind, ban.key)] = (self._now() + _BAN_CACHE_SECONDS, ban)

    def _next_level(self, previous: Ban | None, wall: datetime) -> int:
        memory = timedelta(days=self.config.escalation_memory_days)
        if previous is not None and wall - _parse_iso(previous.created_at) <= memory:
            return previous.level + 1
        return 1

    # -- strikes ------------------------------------------------------------

    async def _strike(self, kind: str, key: str | None, reason: str, count: int = 1) -> None:
        if not key:
            return
        now = self._now()
        window = self.config.strike_window_seconds
        strikes = self._strikes.setdefault((kind, key), deque())
        while strikes and now - strikes[0] > window:
            strikes.popleft()
        strikes.extend([now] * count)
        if len(strikes) < self.config.strikes_to_ban:
            return
        self._strikes.pop((kind, key), None)
        if await self.active_ban(kind, key) is not None:
            return
        wall = self._wall_now()
        previous = await self.store.get_ban(kind, key)
        level = self._next_level(previous, wall)
        durations = self.config.ban_durations_seconds
        duration = durations[min(level, len(durations)) - 1]
        await self._put(
            Ban(kind, key, _iso(wall + timedelta(seconds=duration)), f"automatic: {reason}", level, _iso(wall))
        )

    async def _reject(self, prefix: str, message: str, *, pubkey: str | None, ip: str | None) -> Verdict:
        await self._strike("pubkey", pubkey, message)
        await self._strike("ip", ip, message)
        return Verdict(False, prefix, message)

    async def record_report(self, pubkey: str, strikes: int = 1) -> None:
        """Strikes from Matrix (a stranger's reply redacted in a Profile Room)."""
        await self._strike("pubkey", pubkey, "reported in Matrix", strikes)

    # -- connections and subscriptions ----------------------------------------

    async def check_connection(self, ip: str | None) -> Verdict:
        self._maybe_sweep()
        if ip is None:
            return ACCEPT
        if await self.active_ban("ip", ip) is not None:
            return Verdict(False, "blocked", "this address is banned")
        if not self._connections.take(ip, self._now()):
            return Verdict(False, "rate-limited", "too many connections from this address")
        return ACCEPT

    async def check_req(self, ip: str | None) -> Verdict:
        if ip is None:
            return ACCEPT
        if not self._reqs.take(ip, self._now()):
            return Verdict(False, "rate-limited", "too many subscriptions from this address")
        return ACCEPT

    # -- events -------------------------------------------------------------

    async def check_event(
        self,
        event: Event,
        *,
        ip: str | None = None,
        tier: Tier,
        targets: Iterable[str] = (),
        interaction: bool = False,
    ) -> Verdict:
        """Whether to take ``event``. ``targets`` are the local pubkeys it is
        aimed at; ``interaction`` marks replies, reactions and the like, which
        are refused when stale (backfill isn't)."""
        self._maybe_sweep()
        await self._lower_shields_if_quiet()
        targets = list(dict.fromkeys(targets))
        wrap = event.kind == GIFT_WRAP_KIND
        # A gift wrap's key is thrown away after one use: per-pubkey
        # anything means nothing, so it's judged by IP and target only.
        pubkey = None if wrap else event.pubkey

        if tier is Tier.LOCAL:
            return self._size_and_clock(event, interaction=False) or ACCEPT

        if pubkey and await self.active_ban("pubkey", pubkey) is not None:
            return Verdict(False, "blocked", "this key is banned here")
        if ip and await self.active_ban("ip", ip) is not None:
            return Verdict(False, "blocked", "this address is banned here")

        problem = self._size_and_clock(event, interaction=interaction and not wrap)
        if problem is not None:
            return await self._reject(problem.prefix, problem.message, pubkey=pubkey, ip=ip)

        if not wrap:
            problem = self._content_problem(event)
            if problem is not None:
                return await self._reject(problem.prefix, problem.message, pubkey=pubkey, ip=ip)

        now = self._now()
        trusted = tier is Tier.TRUSTED
        if ip and not self._ip_events.take(ip, now):
            return await self._reject("rate-limited", "too many events from this address", pubkey=pubkey, ip=ip)
        if pubkey:
            limiter = self._trusted_pubkey_events if trusted else self._pubkey_events
            if not limiter.take(pubkey, now):
                return await self._reject("rate-limited", "too many events from this key", pubkey=pubkey, ip=ip)
        target_limiter = self._trusted_target_events if trusted else self._target_events
        sender = pubkey or f"wrap:{ip or '-'}"
        for target in targets:
            if not target_limiter.take(f"{sender}|{target}", now):
                return await self._reject(
                    "rate-limited", "too many events to this account", pubkey=pubkey, ip=ip
                )

        if tier is Tier.STRANGER and pubkey:
            await self._note_stranger(pubkey)
            return self._policy(event)
        return ACCEPT

    async def check_unwrapped_sender(self, pubkey: str, tier: Tier) -> Verdict:
        """Judge a DM's real sender, known only once its gift wrap is
        opened. Proof-of-work can't apply (the rumor the sender wrote is
        unsigned), so under a "pow" policy a stranger's DM still arrives,
        as an invite the user is free to decline."""
        if tier is Tier.LOCAL:
            return ACCEPT
        if await self.active_ban("pubkey", pubkey) is not None:
            return Verdict(False, "blocked", "this key is banned here")
        limiter = self._trusted_pubkey_events if tier is Tier.TRUSTED else self._pubkey_events
        if not limiter.take(pubkey, self._now()):
            return await self._reject("rate-limited", "too many messages from this key", pubkey=pubkey, ip=None)
        if tier is Tier.STRANGER and self.effective_policy == "web_of_trust":
            return Verdict(False, "restricted", "this relay only takes messages from accounts people here know")
        return ACCEPT

    # -- the individual checks ------------------------------------------------

    def _size_and_clock(self, event: Event, *, interaction: bool) -> Verdict | None:
        size = len(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > self.config.max_event_bytes:
            return Verdict(False, "invalid", f"event is {size} bytes, more than {self.config.max_event_bytes}")
        wall = self._wall_now().timestamp()
        if event.created_at > wall + self.config.future_skew_seconds:
            return Verdict(False, "invalid", "created_at is too far in the future")
        if interaction and event.created_at < wall - self.config.max_interaction_age_seconds:
            return Verdict(False, "invalid", "created_at is too far in the past")
        return None

    def _content_problem(self, event: Event) -> Verdict | None:
        p_tags = sum(1 for tag in event.tags if tag and tag[0] == "p")
        # Mass mentions are a spam pattern in what people post; lists (a
        # follow list tags everyone followed) are supposed to be long.
        if event.kind in _CONVERSATION_KINDS and p_tags > self.config.max_p_tags:
            return Verdict(False, "blocked", f"mentions {p_tags} accounts, more than {self.config.max_p_tags}")
        if _BASE64_MEDIA_RE.search(event.content):
            return Verdict(False, "invalid", "base64-embedded media isn't accepted; link to the file instead")
        for pattern in self._patterns:
            if pattern.search(event.content):
                return Verdict(False, "blocked", "content matches a spam pattern")
        if self._is_duplicate_wave(event):
            return Verdict(False, "blocked", "the same text was just sent by several accounts")
        return None

    def _is_duplicate_wave(self, event: Event) -> bool:
        text = event.content.strip()
        if event.kind == REACTION_KIND or len(text) < self.config.duplicate_min_length:
            return False
        now = self._now()
        window = self.config.duplicate_window_seconds
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        seen = self._duplicates.get(digest)
        if seen is None:
            seen = {}
            self._duplicates[digest] = seen
            while len(self._duplicates) > self._max_duplicate_hashes:
                self._duplicates.popitem(last=False)
        else:
            self._duplicates.move_to_end(digest)
            for pubkey in [p for p, t in seen.items() if now - t > window]:
                del seen[pubkey]
        seen[event.pubkey] = now
        return len(seen) >= self.config.duplicate_min_pubkeys

    def _policy(self, event: Event) -> Verdict:
        policy = self.effective_policy
        if policy == "web_of_trust":
            return Verdict(False, "restricted", "this relay only takes events from accounts people here know")
        if policy == "pow" and not meets_pow(event, self.config.stranger_pow_bits):
            return Verdict(
                False,
                "pow",
                f"proof-of-work of at least {self.config.stranger_pow_bits} bits (NIP-13, with a committed target) "
                "is required from accounts nobody here knows",
            )
        return ACCEPT

    # -- wave detection ---------------------------------------------------------

    async def _note_stranger(self, pubkey: str) -> None:
        now = self._now()
        window = self.config.wave_window_seconds
        self._wave[pubkey] = now
        self._wave.move_to_end(pubkey)
        while self._wave and now - next(iter(self._wave.values())) > window:
            self._wave.popitem(last=False)
        count = len(self._wave)
        if count >= self.config.wave_threshold:
            self._last_wave_crossing = now
            if not self._shields_up:
                self._shields_up = True
                await self._announce(True, count)

    async def _lower_shields_if_quiet(self) -> None:
        if self._shields_up and self._now() - self._last_wave_crossing >= self.config.wave_cooldown_seconds:
            self._shields_up = False
            self._wave.clear()
            await self._announce(False, 0)

    async def tick(self) -> None:
        """Let shields come down on time even when no events arrive (call
        from a periodic loop)."""
        await self._lower_shields_if_quiet()
        self._maybe_sweep(force=True)

    async def _announce(self, up: bool, count: int) -> None:
        if self.on_shields_change is None:
            return
        result = self.on_shields_change(up, count)
        if inspect.isawaitable(result):
            await result

    # -- memory ------------------------------------------------------------------

    def _maybe_sweep(self, *, force: bool = False) -> None:
        now = self._now()
        if not force and now - self._last_sweep < _SWEEP_SECONDS:
            return
        self._last_sweep = now
        for limiter in self._limiters:
            limiter.sweep(now)
        strike_window = self.config.strike_window_seconds
        for key in [k for k, d in self._strikes.items() if not d or now - d[-1] > strike_window]:
            del self._strikes[key]
        duplicate_window = self.config.duplicate_window_seconds
        for digest in [h for h, seen in self._duplicates.items() if all(now - t > duplicate_window for t in seen.values())]:
            del self._duplicates[digest]
        wave_window = self.config.wave_window_seconds
        for pubkey in [p for p, t in self._wave.items() if now - t > wave_window]:
            del self._wave[pubkey]
        for key in [k for k, (expires, _) in self._ban_cache.items() if expires <= now]:
            del self._ban_cache[key]

    def memory_sizes(self) -> dict[str, int]:
        """How much each in-memory table holds (for tests and admin status)."""
        return {
            "buckets": sum(len(limiter) for limiter in self._limiters),
            "strikes": len(self._strikes),
            "duplicates": len(self._duplicates),
            "wave": len(self._wave),
            "ban_cache": len(self._ban_cache),
        }
