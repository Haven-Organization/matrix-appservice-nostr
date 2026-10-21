"""Nostr IDs: who may have which name, and creating, finding, switching and
renaming them. See DESIGN.md, "Nostr IDs".

An ID is a NIP-05 name (``q``, shown as ``q@<bridge.domain>``) with its own
keypair and Profile Room. A Matrix user may hold several, and one of them
is *active*: the one used for actions outside a Profile Room or DM room.

Name rules, cheapest check first:

1. Syntax: ``a-z 0-9 - _ .`` (what NIP-05 allows), starting and ending
   with a letter or digit, ``bridge.id_min_length``-``id_max_length`` long.
2. Not reserved: the built-in list, ``bridge.reserved_ids``, the bot.
3. Not taken: by another ID, or retired from someone else's (a deleted
   or renamed ID's name is never handed to a stranger).
4. Not someone else's Matrix username: ``bob`` belongs to ``@bob`` whether
   or not they have made an ID yet.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from fastapi import FastAPI

from bridge.homeserver_client import HomeserverError
from bridge.nostr import secp256k1
from bridge.repository import NostrId, RetiredName
from bridge.signing import id_key_purpose
from bridge.users import localpart

logger = logging.getLogger(__name__)

# Names any site is likely to want kept for itself or that would mislead.
BUILTIN_RESERVED = frozenset(
    {
        "_", "abuse", "admin", "administrator", "api", "bot", "bridge", "help", "hostmaster", "info", "mod",
        "moderator", "mods", "noreply", "no-reply", "nostr", "official", "operator", "postmaster", "relay", "root",
        "security", "staff", "support", "system", "webmaster", "www",
    }
)

_ALLOWED_CHARS_RE = re.compile(r"^[a-z0-9._-]+$")
_INVALID_CHAR_RE = re.compile(r"[^a-z0-9._-]")
_EDGE_RE = re.compile(r"^[a-z0-9].*[a-z0-9]$|^[a-z0-9]$")
_SEPARATOR_RUN_RE = re.compile(r"[-._]{2,}")
_MAX_SUGGESTION_ATTEMPTS = 50

KEY_ORIGIN_GENERATED = "generated"


class RejectionKind(Enum):
    INVALID = "invalid"
    RESERVED = "reserved"
    TAKEN = "taken"
    OWN = "own"  # the user already has an ID with this name
    LIMIT = "limit"
    NOT_ALLOWED = "not_allowed"
    COOLDOWN = "cooldown"


@dataclass(frozen=True)
class Rejection:
    kind: RejectionKind
    message: str  # safe to show the user


class IdRejected(Exception):
    def __init__(self, rejection: Rejection) -> None:
        super().__init__(rejection.message)
        self.rejection = rejection


def _now() -> datetime:
    return datetime.now(timezone.utc)


ALLOWED_CHARS_TEXT = "lowercase letters, digits, dashes, underscores and dots"


# -- syntax ---------------------------------------------------------------------


def normalize_requested(text: str, domain: str) -> str | None:
    """What someone typed as an ID name, as a bare lowercase name: accepts
    ``q``, ``Q``, ``q@<domain>``. ``None`` if it names another domain."""
    name = text.strip().lower().removeprefix("@")
    if "@" in name:
        name, _, at_domain = name.partition("@")
        if at_domain.rstrip(".") != domain:
            return None
    return name


def syntax_problem(config, name: str) -> Rejection | None:
    low, high = config.bridge.id_min_length, config.bridge.id_max_length
    if not name or not _ALLOWED_CHARS_RE.match(name):
        return Rejection(RejectionKind.INVALID, f"Nostr IDs can only contain {ALLOWED_CHARS_TEXT}.")
    if not _EDGE_RE.match(name):
        return Rejection(RejectionKind.INVALID, "Nostr IDs must start and end with a letter or digit.")
    if not low <= len(name) <= high:
        return Rejection(RejectionKind.INVALID, f"Nostr IDs must be {low} to {high} characters long.")
    return None


def suggestion_base(config, text: str) -> str:
    """``text`` (a Matrix localpart) turned into a valid ID name: invalid
    characters become ``-``, runs of separators collapse, separators come
    off the ends, and it is cut to the maximum length."""
    name = _INVALID_CHAR_RE.sub("-", text.lower())
    name = _SEPARATOR_RUN_RE.sub(lambda m: m.group(0)[0], name)
    name = name.strip("-._")[: config.bridge.id_max_length].strip("-._")
    if len(name) < config.bridge.id_min_length:
        name = (name + "-user" if name else "user")[: config.bridge.id_max_length]
    return name


# -- availability -----------------------------------------------------------------


def _reserved(config, name: str) -> bool:
    if name in BUILTIN_RESERVED or name == config.appservice.bot_localpart:
        return True
    return any(fnmatch.fnmatchcase(name, pattern.lower()) for pattern in config.bridge.reserved_ids)


async def availability_problem(app: FastAPI, name: str, sender: str) -> Rejection | None:
    """Whether ``sender`` may take ``name`` (syntax already checked)."""
    config = app.state.config
    handle = f"{name}@{config.bridge.domain}"
    if _reserved(config, name):
        return Rejection(RejectionKind.RESERVED, f"{handle} is reserved.")
    repository = app.state.repository
    existing = await repository.get_id_by_name(name)
    if existing is not None:
        if existing.matrix_user_id == sender:
            return Rejection(RejectionKind.OWN, f"{handle} is already yours.")
        return Rejection(RejectionKind.TAKEN, f"{handle} is already taken.")
    retired = await repository.get_retired_name(name)
    if retired is not None and retired.matrix_user_id != sender:
        return Rejection(RejectionKind.TAKEN, f"{handle} is already taken.")
    if name != localpart(sender):
        # Someone else's Matrix username is theirs on Nostr too.
        mxid = f"@{name}:{config.homeserver.server_name}"
        if await _user_exists(app, mxid):
            return Rejection(RejectionKind.TAKEN, f"{handle} is reserved for {mxid}.")
    return None


async def _user_exists(app: FastAPI, mxid: str) -> bool:
    """Whether a Matrix user exists, judged by their profile: a clear "not
    found" means no. A homeserver that hides profiles from the bot
    (answering 403, say) can't tell us, and then the name is allowed: a
    lookup that can't answer shouldn't block every custom ID on that
    server."""
    try:
        await app.state.homeserver.get_profile(mxid)
    except HomeserverError as exc:
        if exc.status_code != 404:
            logger.info("Couldn't tell whether %s exists (%s); allowing the name", mxid, exc)
        return False
    return True


async def check_new_name(app: FastAPI, name: str, sender: str) -> Rejection | None:
    return syntax_problem(app.state.config, name) or await availability_problem(app, name, sender)


async def suggest_available(app: FastAPI, text: str, sender: str) -> str | None:
    """A free, valid name close to ``text``: its cleaned-up form, or that
    with ``-2``, ``-3``, ... added."""
    config = app.state.config
    base = suggestion_base(config, text)
    for attempt in range(1, _MAX_SUGGESTION_ATTEMPTS + 1):
        if attempt == 1:
            candidate = base
        else:
            suffix = f"-{attempt}"
            candidate = base[: config.bridge.id_max_length - len(suffix)].rstrip("-._") + suffix
        if await check_new_name(app, candidate, sender) is None:
            return candidate
    return None


def user_allowed(config, sender: str) -> bool:
    patterns = config.bridge.id_allowed_users
    return not patterns or any(fnmatch.fnmatchcase(sender, pattern) for pattern in patterns)


async def limit_problem(app: FastAPI, sender: str, *, is_admin: bool) -> Rejection | None:
    if is_admin:
        return None
    limit = app.state.config.bridge.max_ids_per_user
    if len(await app.state.repository.list_ids_for_user(sender)) >= limit:
        return Rejection(
            RejectionKind.LIMIT, f"You already have {limit} Nostr IDs, the most this server allows."
        )
    return None


# -- creating ---------------------------------------------------------------------


async def create_id(app: FastAPI, sender: str, name: str) -> NostrId:
    """Create the ID ``name`` for ``sender``, with a new key. The caller
    has checked the name, but it is checked again here and taken atomically,
    so a race loses cleanly. Raises ``IdRejected``. Doesn't create rooms."""
    rejection = await check_new_name(app, name, sender)
    if rejection is not None:
        raise IdRejected(rejection)
    repository = app.state.repository
    secret = secp256k1.generate_secret_key()
    pubkey = secp256k1.public_key(secret).hex()
    created = await repository.insert_id(
        name=name,
        matrix_user_id=sender,
        pubkey=pubkey,
        secret_key=app.state.vault.encrypt(secret, id_key_purpose(pubkey)),
        key_origin=KEY_ORIGIN_GENERATED,
        created_at=_now().isoformat(),
    )
    if created is None:
        raise IdRejected(Rejection(RejectionKind.TAKEN, f"{name}@{app.state.config.bridge.domain} is already taken."))
    # A user's first ID becomes active; so does any ID when the stored
    # choice no longer exists.
    chosen = await repository.get_active_id(sender)
    if chosen is None or all(i.id != chosen for i in await repository.list_ids_for_user(sender)):
        await repository.set_active_id(sender, created.id)
    logger.info("Created Nostr ID %s@%s (%s) for %s", name, app.state.config.bridge.domain, pubkey, sender)
    return created


# -- finding ----------------------------------------------------------------------


async def active_id(app: FastAPI, matrix_user_id: str) -> NostrId | None:
    """The user's active ID: the one they chose, or their first."""
    repository = app.state.repository
    ids = await repository.list_ids_for_user(matrix_user_id)
    if not ids:
        return None
    chosen = await repository.get_active_id(matrix_user_id)
    return next((i for i in ids if i.id == chosen), ids[0])


async def find_own_id(app: FastAPI, matrix_user_id: str, reference: str) -> NostrId | None:
    """One of the user's IDs, by its number in ``[profiles`` (``2``) or its
    name (``qbot``, ``qbot@<domain>``)."""
    ids = await app.state.repository.list_ids_for_user(matrix_user_id)
    reference = reference.strip()
    if reference.isdigit():
        number = int(reference)
        return ids[number - 1] if 1 <= number <= len(ids) else None
    name = normalize_requested(reference, app.state.config.bridge.domain)
    return next((i for i in ids if i.name == name), None)


def id_number(ids: list[NostrId], nostr_id: NostrId) -> int:
    return next(n for n, i in enumerate(ids, start=1) if i.id == nostr_id.id)


# -- renaming ---------------------------------------------------------------------


def rename_cooldown_problem(config, nostr_id: NostrId) -> Rejection | None:
    if not nostr_id.renamed_at:
        return None
    available = datetime.fromisoformat(nostr_id.renamed_at) + timedelta(days=config.bridge.rename_cooldown_days)
    if _now() >= available:
        return None
    return Rejection(
        RejectionKind.COOLDOWN,
        f"{nostr_id.name}@{config.bridge.domain} was renamed recently. It can be renamed again "
        f"after {available.strftime('%Y-%m-%d %H:%M')} UTC.",
    )


async def rename_problem(app: FastAPI, nostr_id: NostrId, new_name: str) -> Rejection | None:
    config = app.state.config
    if new_name == nostr_id.name:
        return Rejection(RejectionKind.OWN, f"It's already called {new_name}@{config.bridge.domain}.")
    return rename_cooldown_problem(config, nostr_id) or await check_new_name(app, new_name, nostr_id.matrix_user_id)


async def rename_id(app: FastAPI, nostr_id: NostrId, new_name: str) -> NostrId:
    """Rename, keeping the key. The old name keeps resolving to this ID for
    ``bridge.renamed_id_grace_days``, then stays retired for its owner.
    Raises ``IdRejected``."""
    rejection = await rename_problem(app, nostr_id, new_name)
    if rejection is not None:
        raise IdRejected(rejection)
    config = app.state.config
    now = _now()
    retired = RetiredName(
        name=nostr_id.name,
        matrix_user_id=nostr_id.matrix_user_id,
        id_ref=nostr_id.id,
        reason="renamed",
        retired_at=now.isoformat(),
        resolves_until=(now + timedelta(days=config.bridge.renamed_id_grace_days)).isoformat(),
    )
    repository = app.state.repository
    if not await repository.rename_id(
        nostr_id.id, old_name=nostr_id.name, new_name=new_name, retired=retired, now=now.isoformat()
    ):
        raise IdRejected(Rejection(RejectionKind.TAKEN, f"{new_name}@{config.bridge.domain} is already taken."))
    renamed = await repository.get_id(nostr_id.id)
    assert renamed is not None
    logger.info("Renamed Nostr ID %s to %s (%s)", nostr_id.name, new_name, nostr_id.pubkey)
    return renamed
