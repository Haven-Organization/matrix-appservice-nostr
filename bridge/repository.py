"""Repository abstraction over the bridge's own bookkeeping.

``BridgeRepository`` is the seam between the bridge and its storage, as in
the sibling bridges. ``bridge.sqlite_repository.SqliteBridgeRepository``
is the default (and, pointed at ``:memory:``, what tests use);
``bridge.postgres_repository`` runs the same queries against Postgres.

What is deliberately NOT here: event content and media. Those live in
Matrix and nowhere else (DESIGN.md, "The storage rule"). This is IDs and
their keys, cursors, and maps between Nostr and Matrix. A Nostr event the
bridge made is kept as its id, signature and a ``source``: a recipe for
rebuilding it from Matrix (``bridge.event_sources``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class NostrId:
    """One of a Matrix user's Nostr IDs: a NIP-05 name, its key and its
    Profile Room. ``id`` is a stable number that survives renames and key
    changes; everything else refers to the ID by it.

    ``secret_key`` is the vault-encrypted secret key (``bridge.key_vault``),
    or ``None`` when the key is held by a remote signer (NIP-46)."""

    id: int
    name: str
    matrix_user_id: str
    pubkey: str
    secret_key: str | None
    key_origin: str
    bunker_uri: str | None
    profile_room_id: str | None
    created_at: str
    renamed_at: str | None = None
    # A remote signer's (NIP-46) client key, vault-encrypted.
    client_key: str | None = None
    # No Profile Room on purpose ([unlink): don't create one for it.
    room_unlinked: bool = False


@dataclass(frozen=True)
class RetiredName:
    """A name no longer in use by an ID, kept out of anyone else's hands.
    While ``resolves_until`` is in the future (a rename's grace period),
    NIP-05 still answers for it with the key of ``id_ref``."""

    name: str
    matrix_user_id: str
    id_ref: int | None
    reason: str
    retired_at: str
    resolves_until: str | None = None


@dataclass(frozen=True)
class UserRooms:
    """The rooms the bridge keeps per Matrix user (not per ID)."""

    matrix_user_id: str
    space_room_id: str | None = None
    notifications_room_id: str | None = None


@dataclass(frozen=True)
class EventRow:
    """A Nostr event in the relay index: everything but its content.
    ``source`` (JSON) says how to rebuild it; ``tags`` are the ones kept for
    answering queries."""

    id: str
    pubkey: str
    kind: int
    created_at: int
    sig: str
    source: str
    d_tag: str = ""
    room_id: str | None = None
    matrix_event_id: str | None = None
    expires_at: int | None = None
    # Who may read it from the relay: "public", "followed" or "dm".
    scope: str = "public"
    tags: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RemoteAccount:
    """A Nostr account someone here deals with: its ghost user and (once
    needed) its Remote User Room, plus where it publishes and reads."""

    pubkey: str
    ghost_user_id: str
    room_id: str | None = None
    nip05: str | None = None
    write_relays: tuple[str, ...] = ()
    read_relays: tuple[str, ...] = ()
    dm_relays: tuple[str, ...] = ()
    relays_at: int = 0
    profile_at: int = 0
    avatar_url: str | None = None
    avatar_mxc: str | None = None
    checked_at: str | None = None


@dataclass(frozen=True)
class Blob:
    """A file served at ``/<sha256>`` (Blossom style). The bytes are in the
    Matrix media repository at ``mxc``."""

    sha256: str
    mxc: str
    mime_type: str
    size: int
    created_at: str


@dataclass(frozen=True)
class PendingPublish:
    """An event some relays haven't accepted yet, to try again at
    ``next_attempt_at`` (ISO time)."""

    event_id: str
    relays: tuple[str, ...]
    attempts: int
    next_attempt_at: str


@dataclass(frozen=True)
class RelayRefusal:
    """A relay that refused an ID by policy, not to be tried again for that
    ID until ``until`` (ISO time)."""

    relay: str
    pubkey: str
    until: str
    reason: str


class BridgeRepository(Protocol):
    async def close(self) -> None: ...

    # -- the bridge's own keys -------------------------------------------
    async def get_server_key(self, name: str) -> str | None: ...

    async def get_or_create_server_key(self, name: str, candidate: str) -> str:
        """The stored (encrypted) key called ``name``, storing ``candidate``
        first if there is none. Atomic: two racing callers get the same key."""
        ...

    # -- IDs -------------------------------------------------------------
    async def insert_id(
        self,
        *,
        name: str,
        matrix_user_id: str,
        pubkey: str,
        secret_key: str | None,
        key_origin: str,
        created_at: str,
        profile_room_id: str | None = None,
        bunker_uri: str | None = None,
    ) -> NostrId | None:
        """The new ID, or ``None`` if its name or key is already taken."""
        ...

    async def get_id_by_name(self, name: str) -> NostrId | None: ...
    async def get_id_by_pubkey(self, pubkey: str) -> NostrId | None: ...
    async def get_id(self, id: int) -> NostrId | None: ...
    async def get_id_for_profile_room(self, room_id: str) -> NostrId | None: ...
    async def list_ids_for_user(self, matrix_user_id: str) -> list[NostrId]: ...
    async def list_all_ids(self) -> list[NostrId]: ...
    async def set_profile_room(self, id: int, room_id: str) -> None: ...

    async def unlink_profile_room(self, id: int) -> None: ...

    async def rename_id(self, id: int, *, old_name: str, new_name: str, retired: RetiredName, now: str) -> bool:
        """Rename atomically: retire ``old_name`` and take ``new_name``.
        False if ``new_name`` was taken meanwhile."""
        ...

    async def get_retired_name(self, name: str) -> RetiredName | None: ...
    async def insert_retired_name(self, retired: RetiredName) -> None: ...

    async def resolve_nip05(self, name: str, now: str) -> str | None:
        """The pubkey NIP-05 should give for ``name`` at time ``now``: an
        ID's, or a renamed ID's during its grace period."""
        ...

    async def get_active_id(self, matrix_user_id: str) -> int | None: ...
    async def set_active_id(self, matrix_user_id: str, id: int | None) -> None: ...

    # -- per-user rooms --------------------------------------------------
    async def get_user_rooms(self, matrix_user_id: str) -> UserRooms: ...
    async def set_user_space(self, matrix_user_id: str, room_id: str) -> None: ...
    async def set_user_notifications_room(self, matrix_user_id: str, room_id: str) -> None: ...
    async def get_notifications_room_owner(self, room_id: str) -> str | None: ...
    async def get_space_owner(self, room_id: str) -> str | None: ...

    # -- the relay index -------------------------------------------------
    async def put_event(self, row: EventRow, *, replace: bool = False) -> None:
        """Store ``row``. With ``replace`` (replaceable and addressable
        kinds), every older event with the same pubkey, kind and d tag is
        removed first."""
        ...

    async def get_event(self, event_id: str) -> EventRow | None: ...
    async def get_latest_event(self, pubkey: str, kind: int, d_tag: str = "") -> EventRow | None: ...

    async def get_event_by_matrix_event(self, matrix_event_id: str) -> EventRow | None: ...

    async def list_events_of_kinds(self, kinds: tuple[int, ...]) -> list[EventRow]: ...

    async def delete_events_by(self, pubkey: str, *, until: int, keep: tuple[str, ...] = ()) -> list[EventRow]: ...

    async def delete_gift_wraps_to(self, pubkey: str) -> int: ...

    async def delete_id(self, id: int, retired: RetiredName) -> None: ...

    async def delete_event(self, event_id: str) -> None:
        """Remove an event from the index (and any pending publish of it)."""
        ...

    # -- publishing ------------------------------------------------------
    async def put_pending_publish(self, pending: PendingPublish) -> None: ...
    async def list_due_publishes(self, now: str, limit: int = 100) -> list[PendingPublish]: ...
    async def delete_pending_publish(self, event_id: str) -> None: ...
    async def get_relay_refusal(self, relay: str, pubkey: str) -> RelayRefusal | None: ...
    async def put_relay_refusal(self, refusal: RelayRefusal) -> None: ...

    # -- blobs -----------------------------------------------------------
    async def put_blob(self, blob: Blob) -> None: ...
    async def get_blob(self, sha256: str) -> Blob | None: ...

    # -- AppService transactions -----------------------------------------
    async def claim_event(self, event_id: str, now: str) -> bool:
        """Record a Matrix event as handled. False if it already was."""
        ...

    async def prune_processed_events(self, before: str) -> None: ...

    # -- small persistent values -----------------------------------------
    async def get_state(self, key: str) -> str | None: ...
    async def set_state(self, key: str, value: str) -> None: ...

    async def list_state(self, prefix: str) -> list[tuple[str, str]]: ...

    async def delete_state(self, key: str) -> None: ...
