"""Filters for DM requests: ``[dmfilter``.

A DM request is a private message from an account the identity doesn't
follow and has no DM room with yet: the one that would open a new room
and invite its owner. Each Matrix user can have those screened, for all
their identities, by:

- keywords or phrases in the message (whole words, any case),
- language: only the listed ones (judged only when the message is long
  enough to tell, and only on a confident guess),
- no display name: the sender's Nostr profile has no name,
- NIP-05 domain: the sender's profile claims a handle at a listed domain
  (or a subdomain of one). The claim is enough: a spammer's own domain
  needn't verify.

A filtered request is dropped quietly: no room, no ghost, nothing stored,
and the sender's relay is answered as for any delivered DM so the filter
can't be probed. Conversations already open, and accounts followed, are
never filtered. Settings are bookkeeping in the state table.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from fastapi import FastAPI

from bridge.nostr import event as nostr_event

logger = logging.getLogger(__name__)

_KEY = "dm_filter:"
_MAX_ITEMS = 200
# Language is judged only with this many letters, and only on a guess at
# least this sure: a short "hi" reads as Swahili.
_MIN_LETTERS = 25
_SURE = 0.9
_PROFILE_TIMEOUT = 6.0


@dataclass(frozen=True)
class DmFilter:
    keywords: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()  # allowed; empty: any
    require_name: bool = False
    domains: tuple[str, ...] = ()  # blocked NIP-05 domains
    filtered: int = 0  # how many requests were dropped

    @property
    def active(self) -> bool:
        return bool(self.keywords or self.languages or self.require_name or self.domains)


async def load(app: FastAPI, matrix_user_id: str) -> DmFilter:
    raw = await app.state.repository.get_state(_KEY + matrix_user_id)
    if not raw:
        return DmFilter()
    try:
        data = json.loads(raw)
        return DmFilter(
            keywords=tuple(str(k) for k in data.get("keywords") or ()),
            languages=tuple(str(k) for k in data.get("languages") or ()),
            require_name=bool(data.get("require_name")),
            domains=tuple(str(k) for k in data.get("domains") or ()),
            filtered=int(data.get("filtered") or 0),
        )
    except (ValueError, TypeError, AttributeError):
        logger.warning("Unreadable DM filter for %s; ignoring it", matrix_user_id)
        return DmFilter()


async def save(app: FastAPI, matrix_user_id: str, settings: DmFilter) -> None:
    if not settings.active and not settings.filtered:
        await app.state.repository.delete_state(_KEY + matrix_user_id)
        return
    await app.state.repository.set_state(_KEY + matrix_user_id, json.dumps(asdict(settings), ensure_ascii=False))


# -- checks ------------------------------------------------------------------


def _keyword_hit(keywords: tuple[str, ...], text: str) -> str | None:
    folded = text.casefold()
    for keyword in keywords:
        if re.search(rf"(?<!\w){re.escape(keyword.casefold())}(?!\w)", folded):
            return keyword
    return None


def _languages(text: str) -> list[tuple[str, float]]:
    """Confident-enough guesses, most likely first; empty if the text is too
    short to tell or no detector is installed."""
    if sum(ch.isalpha() for ch in text) < _MIN_LETTERS:
        return []
    try:
        from langdetect import DetectorFactory, detect_langs
        from langdetect.lang_detect_exception import LangDetectException
    except ImportError:
        logger.warning("langdetect isn't installed; DM language filters are skipped")
        return []
    DetectorFactory.seed = 0  # the same answer every time
    try:
        return [(guess.lang.split("-")[0].lower(), guess.prob) for guess in detect_langs(text)]
    except LangDetectException:
        return []


def _wrong_language(allowed: tuple[str, ...], text: str) -> str | None:
    guesses = _languages(text)
    if not guesses:
        return None
    if any(lang in allowed and prob >= 0.1 for lang, prob in guesses):
        return None
    lang, prob = guesses[0]
    return lang if prob >= _SURE else None


def _domain_of(nip05: str) -> str:
    return nip05.rsplit("@", 1)[-1].strip().lower().rstrip(".")


def _domain_hit(domains: tuple[str, ...], nip05: str | None) -> str | None:
    if not nip05:
        return None
    domain = _domain_of(nip05)
    for blocked in domains:
        if domain == blocked or domain.endswith("." + blocked):
            return blocked
    return None


async def _profile(app: FastAPI, pubkey: str) -> dict[str, str]:
    """The sender's newest kind 0, from where it's likely to be."""
    from bridge.remote import parse_profile

    config = app.state.config
    remote = await app.state.repository.get_remote_account(pubkey)
    relays = list(dict.fromkeys([*(remote.write_relays if remote else ()), *config.nostr.discovery_relays,
                                 *config.nostr.fallback_relays[:2]]))
    try:
        events = await app.state.relay_pool.query(relays, [{"authors": [pubkey], "kinds": [0]}], timeout=_PROFILE_TIMEOUT)
    except Exception:
        logger.info("Could not fetch the profile of %s to screen a DM", pubkey, exc_info=True)
        return {"unknown": "1"}
    newest: dict[str, Any] | None = None
    for raw in events:
        try:
            event = nostr_event.verify(raw)
        except nostr_event.InvalidEvent:
            continue
        if event.pubkey == pubkey and event.kind == 0 and (newest is None or raw["created_at"] > newest["created_at"]):
            newest = raw
    return parse_profile(newest)


async def screen(app: FastAPI, matrix_user_id: str, sender: str, text: str) -> str | None:
    """Why a DM request from ``sender`` saying ``text`` is filtered for this
    user, or ``None`` to let it through."""
    settings = await load(app, matrix_user_id)
    if not settings.active:
        return None
    reason = None
    keyword = _keyword_hit(settings.keywords, text)
    if keyword is not None:
        reason = f"keyword {keyword!r}"
    elif settings.languages and (lang := _wrong_language(settings.languages, text)):
        reason = f"language {lang}"
    elif settings.require_name or settings.domains:
        profile = await _profile(app, sender)
        if "unknown" not in profile:
            if settings.require_name and not profile.get("name"):
                reason = "no display name"
            elif blocked := _domain_hit(settings.domains, profile.get("nip05")):
                reason = f"NIP-05 domain {blocked}"
    if reason is not None:
        await save(app, matrix_user_id, replace(await load(app, matrix_user_id), filtered=settings.filtered + 1))
    return reason


# -- [dmfilter ----------------------------------------------------------------


def _describe(settings: DmFilter, prefix: str) -> str:
    lines = ["DM request filters (for all your identities; open conversations and accounts you follow are never "
             "filtered):"]
    lines.append(f"- keywords: {', '.join(repr(k) for k in settings.keywords) if settings.keywords else 'none'}")
    lines.append(f"- languages allowed: {', '.join(settings.languages) if settings.languages else 'any'}")
    lines.append(f"- no display name: {'filtered' if settings.require_name else 'allowed'}")
    lines.append(f"- NIP-05 domains: {', '.join(settings.domains) if settings.domains else 'none'}")
    lines.append(f"Requests filtered so far: {settings.filtered}")
    lines.append(f'Change them with "{prefix}dmfilter keyword add|remove <word or phrase>", '
                 f'"{prefix}dmfilter language <codes, e.g. en de>|any", "{prefix}dmfilter noname on|off", '
                 f'"{prefix}dmfilter domain add|remove <domain>", or "{prefix}dmfilter clear".')
    return "\n".join(lines)


_LANG_RE = re.compile(r"^[a-z]{2,3}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z0-9-]{1,63}$")


def update(settings: DmFilter, argument: str) -> tuple[DmFilter, str | None]:
    """``settings`` changed as ``argument`` says, and an error if it says
    nothing that makes sense."""
    words = argument.split()
    what = words[0].lower() if words else ""
    rest = words[1:]
    if what == "clear":
        return DmFilter(filtered=settings.filtered), None
    if what in ("keyword", "keywords", "domain", "domains") and rest and rest[0].lower() in ("add", "remove"):
        adding = rest[0].lower() == "add"
        value = " ".join(rest[1:]).strip()
        if what.startswith("domain"):
            value = _domain_of(value.lower().removeprefix("https://").removeprefix("http://").split("/")[0])
            if not _DOMAIN_RE.match(value):
                return settings, "That doesn't look like a domain."
            current = settings.domains
        else:
            value = value.strip("\"'“”")[:100]
            if not value:
                return settings, "Which word or phrase?"
            current = settings.keywords
        folded = [c.casefold() for c in current]
        if adding:
            if value.casefold() in folded:
                return settings, None
            if len(current) >= _MAX_ITEMS:
                return settings, f"That's the most there can be ({_MAX_ITEMS})."
            new = (*current, value)
        else:
            new = tuple(c for c in current if c.casefold() != value.casefold())
        return (replace(settings, domains=new) if what.startswith("domain") else replace(settings, keywords=new)), None
    if what in ("language", "languages") and rest:
        codes = [c.lower().strip(",") for c in " ".join(rest).replace(",", " ").split()]
        if codes in (["any"], ["off"], ["all"]):
            return replace(settings, languages=()), None
        if not all(_LANG_RE.match(c) for c in codes):
            return settings, "Give language codes like en, de or es, or \"any\"."
        return replace(settings, languages=tuple(dict.fromkeys(codes))), None
    if what in ("noname", "no-name") and rest and rest[0].lower() in ("on", "off"):
        return replace(settings, require_name=rest[0].lower() == "on"), None
    return settings, "unknown"


async def handle_command(app: FastAPI, room_id: str, sender: str, argument: str) -> None:
    from bridge.messages import notice

    prefix = app.state.config.appservice.command_prefix
    settings = await load(app, sender)
    if argument.strip():
        changed, error = update(settings, argument)
        if error == "unknown":
            await notice(app, room_id, _describe(settings, prefix))
            return
        if error:
            await notice(app, room_id, error)
            return
        settings = changed
        await save(app, sender, settings)
    await notice(app, room_id, _describe(settings, prefix))
