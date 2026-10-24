"""SQLite-backed ``BridgeRepository``: durable storage for bridge bookkeeping.

Persists to a single SQLite file under ``storage.data_dir``. This is the
default repository used by ``bridge.server.create_app``.

Every query here is written in the SQL that SQLite and PostgreSQL both
accept ("?" placeholders, ``ON CONFLICT`` rather than ``INSERT OR ...``,
``RETURNING`` rather than ``lastrowid``): ``bridge.postgres_repository``
runs these same methods against Postgres, so keep to that subset.

``sqlite3`` connections aren't safe to use from several threads at once,
so every call runs in ``asyncio.to_thread`` behind one ``asyncio.Lock``:
the event loop never blocks on the database, and access stays serialized.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from bridge.repository import (
    Blob,
    EventRow,
    NostrId,
    PendingPublish,
    RelayRefusal,
    RemoteAccount,
    RetiredName,
    UserRooms,
)

_SCHEMA = """
-- Keys belonging to the bridge itself rather than to one ID, e.g. 'bot'
-- (the bot's own Nostr identity, which answers for _@<domain>).
-- `secret` is vault-encrypted (bridge.key_vault).
CREATE TABLE IF NOT EXISTS server_keys (
    name TEXT PRIMARY KEY,
    secret TEXT NOT NULL
);

-- One row per Nostr ID. `id` never changes; `name` can (a rename) and so
-- can `pubkey` (a key replacement). `secret_key` is vault-encrypted, and
-- NULL when a remote signer (NIP-46) holds the key.
CREATE TABLE IF NOT EXISTS ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    matrix_user_id TEXT NOT NULL,
    pubkey TEXT NOT NULL UNIQUE,
    secret_key TEXT,
    key_origin TEXT NOT NULL,
    bunker_uri TEXT,
    profile_room_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    renamed_at TEXT,
    -- For a remote signer (NIP-46): the bridge's own client key, encrypted.
    client_key TEXT
);

-- Which ID each Matrix user is acting as.
CREATE TABLE IF NOT EXISTS user_settings (
    matrix_user_id TEXT PRIMARY KEY,
    active_id INTEGER
);

-- Names no longer used by an ID: never given to anyone else. During a
-- rename's grace period (`resolves_until`), NIP-05 still answers for one.
CREATE TABLE IF NOT EXISTS retired_ids (
    name TEXT PRIMARY KEY,
    matrix_user_id TEXT NOT NULL,
    id_ref INTEGER,
    reason TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    resolves_until TEXT
);

-- The space and notifications room the bridge keeps for each Matrix user.
CREATE TABLE IF NOT EXISTS user_rooms (
    matrix_user_id TEXT PRIMARY KEY,
    space_room_id TEXT UNIQUE,
    notifications_room_id TEXT UNIQUE
);

-- The relay index: every Nostr event the bridge holds, minus its content.
-- `source` is the recipe for rebuilding it (bridge.event_sources).
CREATE TABLE IF NOT EXISTS nostr_events (
    id TEXT PRIMARY KEY,
    pubkey TEXT NOT NULL,
    kind INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    d_tag TEXT NOT NULL DEFAULT '',
    room_id TEXT,
    matrix_event_id TEXT,
    source TEXT NOT NULL,
    sig TEXT NOT NULL,
    expires_at INTEGER,
    -- Who may read it from the relay: 'public', 'followed' (mirrored from a
    -- followed account) or 'dm' (a gift wrap; only its recipient).
    scope TEXT NOT NULL DEFAULT 'public'
);

-- The single-letter tags queries can filter on (#e, #p, ...): references,
-- never text.
CREATE TABLE IF NOT EXISTS nostr_event_tags (
    event_id TEXT NOT NULL,
    letter TEXT NOT NULL,
    value TEXT NOT NULL
);

-- Remote Nostr accounts the bridge deals with: their ghost user, their
-- Remote User Room (created when someone follows them, or a post of theirs
-- needs a home), and what's cached about where they publish. `nip05` is
-- only set once verified.
CREATE TABLE IF NOT EXISTS remote_accounts (
    pubkey TEXT PRIMARY KEY,
    ghost_user_id TEXT NOT NULL UNIQUE,
    room_id TEXT UNIQUE,
    nip05 TEXT,
    write_relays TEXT NOT NULL DEFAULT '[]',
    read_relays TEXT NOT NULL DEFAULT '[]',
    dm_relays TEXT NOT NULL DEFAULT '[]',
    relays_at INTEGER NOT NULL DEFAULT 0,
    profile_at INTEGER NOT NULL DEFAULT 0,
    avatar_url TEXT,
    avatar_mxc TEXT,
    checked_at TEXT
);

-- Which local ID follows which remote pubkey (its kind 3).
CREATE TABLE IF NOT EXISTS follows (
    id_ref INTEGER NOT NULL,
    pubkey TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id_ref, pubkey)
);

-- Remote pubkeys known to follow a local ID, so a follower is announced
-- once, not every time they change their follow list.
CREATE TABLE IF NOT EXISTS followers (
    id_ref INTEGER NOT NULL,
    pubkey TEXT NOT NULL,
    since INTEGER NOT NULL,
    PRIMARY KEY (id_ref, pubkey)
);

-- Pubkeys a local ID blocks (its public mute list, kind 10000).
CREATE TABLE IF NOT EXISTS blocks (
    id_ref INTEGER NOT NULL,
    pubkey TEXT NOT NULL,
    PRIMARY KEY (id_ref, pubkey)
);

-- Matrix events that show a Nostr event but aren't its home in the index:
-- cross-posted copies, repost cards. What a reaction or reply to them acts on.
CREATE TABLE IF NOT EXISTS mirrored_events (
    matrix_event_id TEXT PRIMARY KEY,
    nostr_id TEXT NOT NULL,
    room_id TEXT NOT NULL
);

-- How far each subscription on each relay has read (created_at).
CREATE TABLE IF NOT EXISTS relay_cursors (
    relay TEXT NOT NULL,
    name TEXT NOT NULL,
    since INTEGER NOT NULL,
    PRIMARY KEY (relay, name)
);

-- One DM room per (local ID, remote pubkey).
CREATE TABLE IF NOT EXISTS dm_rooms (
    room_id TEXT PRIMARY KEY,
    id_ref INTEGER NOT NULL,
    pubkey TEXT NOT NULL,
    UNIQUE (id_ref, pubkey)
);

-- Relay bans (bridge.spam): a pubkey or IP, until when (NULL: for good).
CREATE TABLE IF NOT EXISTS bans (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    until TEXT,
    reason TEXT NOT NULL,
    level INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

-- Files served at /<sha256>; the bytes are in the Matrix media repository.
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    mxc TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Events not yet accepted by some relays they should reach: ids and
-- relay URLs only. The event itself is rebuilt from the index to retry.
CREATE TABLE IF NOT EXISTS pending_publishes (
    event_id TEXT PRIMARY KEY,
    relays TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TEXT NOT NULL
);

-- Relays that refused an ID by policy (a web-of-trust relay, say): not
-- tried again for that ID until `until`.
CREATE TABLE IF NOT EXISTS relay_refusals (
    relay TEXT NOT NULL,
    pubkey TEXT NOT NULL,
    until TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (relay, pubkey)
);

-- Matrix events already handled, so a retried AppService transaction
-- isn't acted on twice. Keyed by event ID, not transaction ID: some
-- homeservers (Synapse on SQLite) restart transaction numbering after a
-- restart, so a transaction ID can come back carrying new events.
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bridge_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after a table first shipped: (table, column, definition).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("nostr_events", "scope", "TEXT NOT NULL DEFAULT 'public'"),
    ("ids", "client_key", "TEXT"),
    # Set by [unlink: the ID has no Profile Room on purpose, so none is
    # created for it until [link or [create room.
    ("ids", "room_unlinked", "INTEGER NOT NULL DEFAULT 0"),
)

_INDEXES = """
CREATE INDEX IF NOT EXISTS ids_matrix_user ON ids (matrix_user_id);
CREATE INDEX IF NOT EXISTS nostr_events_author_kind ON nostr_events (pubkey, kind, d_tag, created_at);
CREATE INDEX IF NOT EXISTS nostr_events_kind_time ON nostr_events (kind, created_at);
CREATE INDEX IF NOT EXISTS nostr_events_matrix_event ON nostr_events (matrix_event_id);
CREATE INDEX IF NOT EXISTS nostr_event_tags_lookup ON nostr_event_tags (letter, value);
CREATE INDEX IF NOT EXISTS nostr_event_tags_event ON nostr_event_tags (event_id);
CREATE INDEX IF NOT EXISTS processed_events_time ON processed_events (processed_at);
CREATE INDEX IF NOT EXISTS follows_pubkey ON follows (pubkey);
CREATE INDEX IF NOT EXISTS mirrored_events_nostr ON mirrored_events (nostr_id);
"""

# Run on every startup, after the schema is in place.
_STARTUP_FIXUPS: tuple[str, ...] = ()

_ID_COLUMNS = (
    "id, name, matrix_user_id, pubkey, secret_key, key_origin, bunker_uri, profile_room_id, created_at, renamed_at, "
    "client_key, room_unlinked"
)
_RETIRED_COLUMNS = "name, matrix_user_id, id_ref, reason, retired_at, resolves_until"
_EVENT_COLUMNS = "id, pubkey, kind, created_at, sig, source, d_tag, room_id, matrix_event_id, expires_at, scope"
_REMOTE_COLUMNS = (
    "pubkey, ghost_user_id, room_id, nip05, write_relays, read_relays, dm_relays, relays_at, profile_at, "
    "avatar_url, avatar_mxc, checked_at"
)
_BLOB_COLUMNS = "sha256, mxc, mime_type, size, created_at"


def _nostr_id(row: tuple) -> NostrId:
    return NostrId(*row[:-1], room_unlinked=bool(row[-1]))


def _remote(row: tuple) -> RemoteAccount:
    return RemoteAccount(
        pubkey=row[0], ghost_user_id=row[1], room_id=row[2], nip05=row[3],
        write_relays=tuple(json.loads(row[4])), read_relays=tuple(json.loads(row[5])), dm_relays=tuple(json.loads(row[6])),
        relays_at=row[7], profile_at=row[8], avatar_url=row[9], avatar_mxc=row[10], checked_at=row[11],
    )


def _event_row(conn: Any, row: tuple) -> EventRow:
    tags = conn.execute(
        "SELECT letter, value FROM nostr_event_tags WHERE event_id = ? ORDER BY letter, value", (row[0],)
    ).fetchall()
    return EventRow(*row, tags=tuple((letter, value) for letter, value in tags))


class SqliteBridgeRepository:
    def __init__(self, path: str | Path) -> None:
        """``path`` may be ``":memory:"`` for a throwaway database."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        for table, column, definition in _ADDED_COLUMNS:
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self._conn.executescript(_INDEXES)
        for statement in _STARTUP_FIXUPS:
            self._conn.execute(statement)
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def _run(self, fn: Callable[[Any], Any]) -> Any:
        """Run ``fn`` in a worker thread inside one transaction: committed
        if it returns, rolled back if it raises."""

        def transaction(conn: sqlite3.Connection) -> Any:
            with conn:
                return fn(conn)

        async with self._lock:
            return await asyncio.to_thread(transaction, self._conn)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._conn.close)

    # -- the bridge's own keys -------------------------------------------

    async def get_server_key(self, name: str) -> str | None:
        def query(conn: Any) -> str | None:
            row = conn.execute("SELECT secret FROM server_keys WHERE name = ?", (name,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def get_or_create_server_key(self, name: str, candidate: str) -> str:
        def upsert(conn: Any) -> str:
            conn.execute(
                "INSERT INTO server_keys (name, secret) VALUES (?, ?) ON CONFLICT DO NOTHING", (name, candidate)
            )
            return conn.execute("SELECT secret FROM server_keys WHERE name = ?", (name,)).fetchone()[0]

        return await self._run(upsert)

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
        client_key: str | None = None,
    ) -> NostrId | None:
        def insert(conn: Any) -> NostrId | None:
            row = conn.execute(
                "INSERT INTO ids (name, matrix_user_id, pubkey, secret_key, key_origin, bunker_uri, profile_room_id, "
                f"created_at, client_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING {_ID_COLUMNS}",
                (name, matrix_user_id, pubkey, secret_key, key_origin, bunker_uri, profile_room_id, created_at, client_key),
            ).fetchone()
            if row is None:
                return None
            # A name can be reclaimed by the user it was retired from.
            conn.execute("DELETE FROM retired_ids WHERE name = ?", (name,))
            return _nostr_id(row)

        return await self._run(insert)

    async def _one_id(self, where: str, value: Any) -> NostrId | None:
        def query(conn: Any) -> NostrId | None:
            row = conn.execute(f"SELECT {_ID_COLUMNS} FROM ids WHERE {where} = ?", (value,)).fetchone()
            return _nostr_id(row) if row else None

        return await self._run(query)

    async def get_id_by_name(self, name: str) -> NostrId | None:
        return await self._one_id("name", name)

    async def get_id_by_pubkey(self, pubkey: str) -> NostrId | None:
        return await self._one_id("pubkey", pubkey)

    async def get_id(self, id: int) -> NostrId | None:
        return await self._one_id("id", id)

    async def get_id_for_profile_room(self, room_id: str) -> NostrId | None:
        return await self._one_id("profile_room_id", room_id)

    async def list_ids_for_user(self, matrix_user_id: str) -> list[NostrId]:
        def query(conn: Any) -> list[NostrId]:
            rows = conn.execute(
                f"SELECT {_ID_COLUMNS} FROM ids WHERE matrix_user_id = ? ORDER BY created_at, id", (matrix_user_id,)
            ).fetchall()
            return [_nostr_id(row) for row in rows]

        return await self._run(query)

    async def list_all_ids(self) -> list[NostrId]:
        def query(conn: Any) -> list[NostrId]:
            return [_nostr_id(row) for row in conn.execute(f"SELECT {_ID_COLUMNS} FROM ids ORDER BY id").fetchall()]

        return await self._run(query)

    async def replace_key(
        self, id: int, *, pubkey: str, secret_key: str | None, key_origin: str,
        bunker_uri: str | None = None, client_key: str | None = None,
    ) -> bool:
        """Give an ID a different key. False if another ID already has it."""
        def update(conn: Any) -> bool:
            if conn.execute("SELECT 1 FROM ids WHERE pubkey = ? AND id <> ?", (pubkey, id)).fetchone():
                return False
            conn.execute(
                "UPDATE ids SET pubkey = ?, secret_key = ?, key_origin = ?, bunker_uri = ?, client_key = ? WHERE id = ?",
                (pubkey, secret_key, key_origin, bunker_uri, client_key, id),
            )
            return True

        return await self._run(update)

    async def set_profile_room(self, id: int, room_id: str) -> None:
        await self._run(
            lambda conn: conn.execute("UPDATE ids SET profile_room_id = ?, room_unlinked = 0 WHERE id = ?", (room_id, id))
        )

    async def unlink_profile_room(self, id: int) -> None:
        await self._run(
            lambda conn: conn.execute("UPDATE ids SET profile_room_id = NULL, room_unlinked = 1 WHERE id = ?", (id,))
        )

    async def rename_id(self, id: int, *, old_name: str, new_name: str, retired: RetiredName, now: str) -> bool:
        def rename(conn: Any) -> bool:
            if conn.execute("SELECT 1 FROM ids WHERE name = ?", (new_name,)).fetchone() is not None:
                return False
            taken = conn.execute("SELECT matrix_user_id FROM retired_ids WHERE name = ?", (new_name,)).fetchone()
            if taken is not None and taken[0] != retired.matrix_user_id:
                return False
            conn.execute("DELETE FROM retired_ids WHERE name = ?", (new_name,))
            conn.execute(
                "UPDATE ids SET name = ?, renamed_at = ? WHERE id = ? AND name = ?", (new_name, now, id, old_name)
            )
            conn.execute(
                f"INSERT INTO retired_ids ({_RETIRED_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET matrix_user_id = excluded.matrix_user_id, id_ref = excluded.id_ref, "
                "reason = excluded.reason, retired_at = excluded.retired_at, resolves_until = excluded.resolves_until",
                (retired.name, retired.matrix_user_id, retired.id_ref, retired.reason, retired.retired_at, retired.resolves_until),
            )
            return True

        return await self._run(rename)

    async def get_retired_name(self, name: str) -> RetiredName | None:
        def query(conn: Any) -> RetiredName | None:
            row = conn.execute(f"SELECT {_RETIRED_COLUMNS} FROM retired_ids WHERE name = ?", (name,)).fetchone()
            return RetiredName(*row) if row else None

        return await self._run(query)

    async def insert_retired_name(self, retired: RetiredName) -> None:
        await self._run(
            lambda conn: conn.execute(
                f"INSERT INTO retired_ids ({_RETIRED_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                (retired.name, retired.matrix_user_id, retired.id_ref, retired.reason, retired.retired_at, retired.resolves_until),
            )
        )

    async def resolve_nip05(self, name: str, now: str) -> str | None:
        def query(conn: Any) -> str | None:
            row = conn.execute("SELECT pubkey FROM ids WHERE name = ?", (name,)).fetchone()
            if row:
                return row[0]
            row = conn.execute(
                "SELECT ids.pubkey FROM retired_ids JOIN ids ON ids.id = retired_ids.id_ref "
                "WHERE retired_ids.name = ? AND retired_ids.resolves_until IS NOT NULL AND retired_ids.resolves_until > ?",
                (name, now),
            ).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def get_active_id(self, matrix_user_id: str) -> int | None:
        def query(conn: Any) -> int | None:
            row = conn.execute("SELECT active_id FROM user_settings WHERE matrix_user_id = ?", (matrix_user_id,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def set_active_id(self, matrix_user_id: str, id: int | None) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO user_settings (matrix_user_id, active_id) VALUES (?, ?) "
                "ON CONFLICT (matrix_user_id) DO UPDATE SET active_id = excluded.active_id",
                (matrix_user_id, id),
            )
        )

    # -- per-user rooms --------------------------------------------------

    async def get_user_rooms(self, matrix_user_id: str) -> UserRooms:
        def query(conn: Any) -> UserRooms:
            row = conn.execute(
                "SELECT matrix_user_id, space_room_id, notifications_room_id FROM user_rooms WHERE matrix_user_id = ?",
                (matrix_user_id,),
            ).fetchone()
            return UserRooms(*row) if row else UserRooms(matrix_user_id)

        return await self._run(query)

    async def _set_user_room(self, column: str, matrix_user_id: str, room_id: str) -> None:
        await self._run(
            lambda conn: conn.execute(
                f"INSERT INTO user_rooms (matrix_user_id, {column}) VALUES (?, ?) "
                f"ON CONFLICT (matrix_user_id) DO UPDATE SET {column} = excluded.{column}",
                (matrix_user_id, room_id),
            )
        )

    async def set_user_space(self, matrix_user_id: str, room_id: str) -> None:
        await self._set_user_room("space_room_id", matrix_user_id, room_id)

    async def set_user_notifications_room(self, matrix_user_id: str, room_id: str) -> None:
        await self._set_user_room("notifications_room_id", matrix_user_id, room_id)

    async def _user_room_owner(self, column: str, room_id: str) -> str | None:
        def query(conn: Any) -> str | None:
            row = conn.execute(f"SELECT matrix_user_id FROM user_rooms WHERE {column} = ?", (room_id,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def get_notifications_room_owner(self, room_id: str) -> str | None:
        return await self._user_room_owner("notifications_room_id", room_id)

    async def get_space_owner(self, room_id: str) -> str | None:
        return await self._user_room_owner("space_room_id", room_id)

    # -- the relay index -------------------------------------------------

    async def put_event(self, row: EventRow, *, replace: bool = False) -> None:
        def insert(conn: Any) -> None:
            if replace:
                older = conn.execute(
                    "SELECT id FROM nostr_events WHERE pubkey = ? AND kind = ? AND d_tag = ? AND id <> ?",
                    (row.pubkey, row.kind, row.d_tag, row.id),
                ).fetchall()
                for (old_id,) in older:
                    conn.execute("DELETE FROM nostr_event_tags WHERE event_id = ?", (old_id,))
                    conn.execute("DELETE FROM nostr_events WHERE id = ?", (old_id,))
            cursor = conn.execute(
                f"INSERT INTO nostr_events ({_EVENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    row.id, row.pubkey, row.kind, row.created_at, row.sig, row.source, row.d_tag,
                    row.room_id, row.matrix_event_id, row.expires_at, row.scope,
                ),
            )
            if cursor.rowcount == 1:
                for letter, value in row.tags:
                    conn.execute(
                        "INSERT INTO nostr_event_tags (event_id, letter, value) VALUES (?, ?, ?)", (row.id, letter, value)
                    )

        await self._run(insert)

    async def get_event(self, event_id: str) -> EventRow | None:
        def query(conn: Any) -> EventRow | None:
            row = conn.execute(f"SELECT {_EVENT_COLUMNS} FROM nostr_events WHERE id = ?", (event_id,)).fetchone()
            return _event_row(conn, row) if row else None

        return await self._run(query)

    async def get_latest_event(self, pubkey: str, kind: int, d_tag: str = "") -> EventRow | None:
        def query(conn: Any) -> EventRow | None:
            row = conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM nostr_events WHERE pubkey = ? AND kind = ? AND d_tag = ? "
                "ORDER BY created_at DESC, id LIMIT 1",
                (pubkey, kind, d_tag),
            ).fetchone()
            return _event_row(conn, row) if row else None

        return await self._run(query)

    async def get_event_by_matrix_event(self, matrix_event_id: str, *, include_copies: bool = True) -> EventRow | None:
        """The Nostr event a Matrix event shows: the one it is the home of,
        or (``include_copies``) the one it's a cross-posted copy of."""
        def query(conn: Any) -> EventRow | None:
            row = conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM nostr_events WHERE matrix_event_id = ? ORDER BY created_at, id LIMIT 1",
                (matrix_event_id,),
            ).fetchone()
            if row is None and include_copies:
                row = conn.execute(
                    f"SELECT {', '.join('e.' + c.strip() for c in _EVENT_COLUMNS.split(','))} FROM mirrored_events m "
                    "JOIN nostr_events e ON e.id = m.nostr_id WHERE m.matrix_event_id = ?",
                    (matrix_event_id,),
                ).fetchone()
            return _event_row(conn, row) if row else None

        return await self._run(query)

    async def put_mirrored_event(self, matrix_event_id: str, nostr_id: str, room_id: str) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO mirrored_events (matrix_event_id, nostr_id, room_id) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (matrix_event_id, nostr_id, room_id),
            )
        )

    async def get_mirrored_nostr_id(self, matrix_event_id: str) -> str | None:
        def query(conn: Any) -> str | None:
            row = conn.execute("SELECT nostr_id FROM mirrored_events WHERE matrix_event_id = ?", (matrix_event_id,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def get_mirrored_matrix_event(self, nostr_id: str, room_id: str) -> str | None:
        def query(conn: Any) -> str | None:
            row = conn.execute(
                "SELECT matrix_event_id FROM mirrored_events WHERE nostr_id = ? AND room_id = ?", (nostr_id, room_id)
            ).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def list_mirrored_copies(self, nostr_id: str) -> list[tuple[str, str]]:
        """(room, Matrix event) of every extra copy of a Nostr event."""
        def query(conn: Any) -> list[tuple[str, str]]:
            return [
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT room_id, matrix_event_id FROM mirrored_events WHERE nostr_id = ?", (nostr_id,)
                ).fetchall()
            ]

        return await self._run(query)

    async def query_events(
        self,
        flt: dict[str, Any],
        *,
        limit: int,
        now: int,
        dm_readers: frozenset[str] = frozenset(),
        followed_readers: frozenset[str] = frozenset(),
        count_only: bool = False,
    ) -> list[EventRow] | int:
        """Index rows matching a NIP-01 filter (``ids``, ``authors``,
        ``kinds``, ``since``, ``until``, ``#x``), newest first. Only what the
        reader may see: gift wraps addressed to one of ``dm_readers``, and
        followed accounts' posts only if ``followed_readers`` (the local
        pubkeys the reader authenticated as) includes a follower of the
        author. Tag filters for letters that aren't indexed match nothing."""
        where = ["(e.expires_at IS NULL OR e.expires_at > ?)"]
        params: list[Any] = [now]
        for key, column in (("ids", "e.id"), ("authors", "e.pubkey"), ("kinds", "e.kind")):
            values = flt.get(key)
            if values is not None:
                if not values:
                    return 0 if count_only else []
                where.append(f"{column} IN ({', '.join('?' * len(values))})")
                params += list(values)
        if isinstance(flt.get("since"), int):
            where.append("e.created_at >= ?")
            params.append(flt["since"])
        if isinstance(flt.get("until"), int):
            where.append("e.created_at <= ?")
            params.append(flt["until"])
        for key, values in flt.items():
            if key.startswith("#") and len(key) == 2:
                if not values:
                    return 0 if count_only else []
                where.append(
                    "EXISTS (SELECT 1 FROM nostr_event_tags t WHERE t.event_id = e.id AND t.letter = ? "
                    f"AND t.value IN ({', '.join('?' * len(values))}))"
                )
                params += [key[1], *values]
        dm = sorted(dm_readers)
        if dm:
            where.append(
                "(e.scope <> 'dm' OR EXISTS (SELECT 1 FROM nostr_event_tags t WHERE t.event_id = e.id AND t.letter = 'p' "
                f"AND t.value IN ({', '.join('?' * len(dm))})))"
            )
            params += dm
        else:
            where.append("e.scope <> 'dm'")
        readers = sorted(followed_readers)
        if readers:
            where.append(
                "(e.scope <> 'followed' OR EXISTS (SELECT 1 FROM follows f JOIN ids i ON i.id = f.id_ref "
                f"WHERE f.pubkey = e.pubkey AND i.pubkey IN ({', '.join('?' * len(readers))})))"
            )
            params += readers
        else:
            where.append("e.scope <> 'followed'")
        clause = " AND ".join(where)

        def query(conn: Any) -> list[EventRow] | int:
            if count_only:
                return conn.execute(f"SELECT COUNT(*) FROM nostr_events e WHERE {clause}", params).fetchone()[0]
            columns = ", ".join("e." + c.strip() for c in _EVENT_COLUMNS.split(","))
            rows = conn.execute(
                f"SELECT {columns} FROM nostr_events e WHERE {clause} ORDER BY e.created_at DESC, e.id LIMIT ?",
                [*params, limit],
            ).fetchall()
            return [_event_row(conn, row) for row in rows]

        return await self._run(query)

    # -- remote accounts ---------------------------------------------------

    async def get_remote_account(self, pubkey: str) -> RemoteAccount | None:
        def query(conn: Any) -> RemoteAccount | None:
            row = conn.execute(f"SELECT {_REMOTE_COLUMNS} FROM remote_accounts WHERE pubkey = ?", (pubkey,)).fetchone()
            return _remote(row) if row else None

        return await self._run(query)

    async def get_remote_account_for_room(self, room_id: str) -> RemoteAccount | None:
        def query(conn: Any) -> RemoteAccount | None:
            row = conn.execute(f"SELECT {_REMOTE_COLUMNS} FROM remote_accounts WHERE room_id = ?", (room_id,)).fetchone()
            return _remote(row) if row else None

        return await self._run(query)

    async def get_remote_account_for_ghost(self, ghost_user_id: str) -> RemoteAccount | None:
        def query(conn: Any) -> RemoteAccount | None:
            row = conn.execute(
                f"SELECT {_REMOTE_COLUMNS} FROM remote_accounts WHERE ghost_user_id = ?", (ghost_user_id,)
            ).fetchone()
            return _remote(row) if row else None

        return await self._run(query)

    async def list_remote_accounts(self) -> list[RemoteAccount]:
        def query(conn: Any) -> list[RemoteAccount]:
            return [_remote(r) for r in conn.execute(f"SELECT {_REMOTE_COLUMNS} FROM remote_accounts").fetchall()]

        return await self._run(query)

    async def save_remote_account(self, remote: RemoteAccount) -> None:
        values = (
            remote.pubkey, remote.ghost_user_id, remote.room_id, remote.nip05, json.dumps(list(remote.write_relays)),
            json.dumps(list(remote.read_relays)), json.dumps(list(remote.dm_relays)), remote.relays_at,
            remote.profile_at, remote.avatar_url, remote.avatar_mxc, remote.checked_at,
        )
        await self._run(
            lambda conn: conn.execute(
                f"INSERT INTO remote_accounts ({_REMOTE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (pubkey) DO UPDATE SET ghost_user_id = excluded.ghost_user_id, room_id = excluded.room_id, "
                "nip05 = excluded.nip05, write_relays = excluded.write_relays, read_relays = excluded.read_relays, "
                "dm_relays = excluded.dm_relays, relays_at = excluded.relays_at, profile_at = excluded.profile_at, "
                "avatar_url = excluded.avatar_url, avatar_mxc = excluded.avatar_mxc, checked_at = excluded.checked_at",
                values,
            )
        )

    # -- follows, followers, blocks -----------------------------------------

    async def add_follow(self, id_ref: int, pubkey: str, now: str) -> bool:
        def insert(conn: Any) -> bool:
            return conn.execute(
                "INSERT INTO follows (id_ref, pubkey, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (id_ref, pubkey, now),
            ).rowcount == 1

        return await self._run(insert)

    async def remove_follow(self, id_ref: int, pubkey: str) -> bool:
        def delete(conn: Any) -> bool:
            return conn.execute("DELETE FROM follows WHERE id_ref = ? AND pubkey = ?", (id_ref, pubkey)).rowcount == 1

        return await self._run(delete)

    async def list_follows(self, id_ref: int) -> list[str]:
        def query(conn: Any) -> list[str]:
            return [r[0] for r in conn.execute(
                "SELECT pubkey FROM follows WHERE id_ref = ? ORDER BY created_at, pubkey", (id_ref,)
            ).fetchall()]

        return await self._run(query)

    async def list_followers_of_remote(self, pubkey: str) -> list[int]:
        """The local IDs (by number) following a remote pubkey."""
        def query(conn: Any) -> list[int]:
            return [r[0] for r in conn.execute("SELECT id_ref FROM follows WHERE pubkey = ?", (pubkey,)).fetchall()]

        return await self._run(query)

    async def list_followed_pubkeys(self) -> list[str]:
        def query(conn: Any) -> list[str]:
            return [r[0] for r in conn.execute("SELECT DISTINCT pubkey FROM follows").fetchall()]

        return await self._run(query)

    async def set_follower(self, id_ref: int, pubkey: str, since: int, following: bool) -> bool:
        """Record whether ``pubkey`` follows the ID. True if that is news
        (a new follower, or one who stopped)."""
        def update(conn: Any) -> bool:
            if following:
                return conn.execute(
                    "INSERT INTO followers (id_ref, pubkey, since) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                    (id_ref, pubkey, since),
                ).rowcount == 1
            return conn.execute("DELETE FROM followers WHERE id_ref = ? AND pubkey = ?", (id_ref, pubkey)).rowcount == 1

        return await self._run(update)

    async def count_followers(self, id_ref: int) -> int:
        def query(conn: Any) -> int:
            return conn.execute("SELECT COUNT(*) FROM followers WHERE id_ref = ?", (id_ref,)).fetchone()[0]

        return await self._run(query)

    async def set_block(self, id_ref: int, pubkey: str, blocked: bool) -> bool:
        def update(conn: Any) -> bool:
            if blocked:
                return conn.execute(
                    "INSERT INTO blocks (id_ref, pubkey) VALUES (?, ?) ON CONFLICT DO NOTHING", (id_ref, pubkey)
                ).rowcount == 1
            return conn.execute("DELETE FROM blocks WHERE id_ref = ? AND pubkey = ?", (id_ref, pubkey)).rowcount == 1

        return await self._run(update)

    async def list_blocks(self, id_ref: int) -> list[str]:
        def query(conn: Any) -> list[str]:
            return [r[0] for r in conn.execute("SELECT pubkey FROM blocks WHERE id_ref = ? ORDER BY pubkey", (id_ref,)).fetchall()]

        return await self._run(query)

    async def is_blocked_by_any(self, pubkey: str, id_refs: list[int]) -> bool:
        if not id_refs:
            return False

        def query(conn: Any) -> bool:
            return conn.execute(
                f"SELECT 1 FROM blocks WHERE pubkey = ? AND id_ref IN ({', '.join('?' * len(id_refs))})",
                (pubkey, *id_refs),
            ).fetchone() is not None

        return await self._run(query)

    # -- relay cursors -------------------------------------------------------

    async def get_cursor(self, relay: str, name: str) -> int | None:
        def query(conn: Any) -> int | None:
            row = conn.execute("SELECT since FROM relay_cursors WHERE relay = ? AND name = ?", (relay, name)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def advance_cursor(self, relay: str, name: str, since: int) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO relay_cursors (relay, name, since) VALUES (?, ?, ?) ON CONFLICT (relay, name) "
                "DO UPDATE SET since = CASE WHEN excluded.since > relay_cursors.since THEN excluded.since ELSE relay_cursors.since END",
                (relay, name, since),
            )
        )

    # -- DM rooms ------------------------------------------------------------

    async def get_dm_room(self, id_ref: int, pubkey: str) -> str | None:
        def query(conn: Any) -> str | None:
            row = conn.execute("SELECT room_id FROM dm_rooms WHERE id_ref = ? AND pubkey = ?", (id_ref, pubkey)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def get_dm_room_info(self, room_id: str) -> tuple[int, str] | None:
        def query(conn: Any) -> tuple[int, str] | None:
            row = conn.execute("SELECT id_ref, pubkey FROM dm_rooms WHERE room_id = ?", (room_id,)).fetchone()
            return (row[0], row[1]) if row else None

        return await self._run(query)

    async def set_dm_room(self, room_id: str, id_ref: int, pubkey: str) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO dm_rooms (room_id, id_ref, pubkey) VALUES (?, ?, ?) ON CONFLICT (id_ref, pubkey) "
                "DO UPDATE SET room_id = excluded.room_id",
                (room_id, id_ref, pubkey),
            )
        )

    # -- bans (bridge.spam.BanStore) -----------------------------------------

    async def get_ban(self, kind: str, key: str):
        from bridge.spam import Ban

        def query(conn: Any):
            row = conn.execute(
                "SELECT kind, key, until, reason, level, created_at FROM bans WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
            return Ban(*row) if row else None

        return await self._run(query)

    async def put_ban(self, ban) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO bans (kind, key, until, reason, level, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (kind, key) DO UPDATE SET until = excluded.until, reason = excluded.reason, "
                "level = excluded.level, created_at = excluded.created_at",
                (ban.kind, ban.key, ban.until, ban.reason, ban.level, ban.created_at),
            )
        )

    async def delete_ban(self, kind: str, key: str) -> None:
        await self._run(lambda conn: conn.execute("DELETE FROM bans WHERE kind = ? AND key = ?", (kind, key)))

    async def list_bans(self) -> list:
        from bridge.spam import Ban

        def query(conn: Any) -> list:
            return [Ban(*r) for r in conn.execute(
                "SELECT kind, key, until, reason, level, created_at FROM bans ORDER BY created_at"
            ).fetchall()]

        return await self._run(query)

    async def list_events_of_kinds(self, kinds: tuple[int, ...]) -> list[EventRow]:
        def query(conn: Any) -> list[EventRow]:
            marks = ", ".join("?" for _ in kinds)
            rows = conn.execute(f"SELECT {_EVENT_COLUMNS} FROM nostr_events WHERE kind IN ({marks})", kinds).fetchall()
            return [_event_row(conn, row) for row in rows]

        return await self._run(query)

    async def delete_events_by(self, pubkey: str, *, until: int, keep: tuple[str, ...] = ()) -> list[EventRow]:
        """Take every event ``pubkey`` signed up to ``until`` (created_at)
        out of the index, except ``keep``. Returns what was removed."""
        def delete(conn: Any) -> list[EventRow]:
            rows = [
                _event_row(conn, row) for row in conn.execute(
                    f"SELECT {_EVENT_COLUMNS} FROM nostr_events WHERE pubkey = ? AND created_at <= ?", (pubkey, until)
                ).fetchall()
            ]
            rows = [row for row in rows if row.id not in keep]
            for row in rows:
                conn.execute("DELETE FROM nostr_event_tags WHERE event_id = ?", (row.id,))
                conn.execute("DELETE FROM nostr_events WHERE id = ?", (row.id,))
                conn.execute("DELETE FROM pending_publishes WHERE event_id = ?", (row.id,))
                conn.execute("DELETE FROM mirrored_events WHERE nostr_id = ?", (row.id,))
            return rows

        return await self._run(delete)

    async def delete_gift_wraps_to(self, pubkey: str) -> int:
        """Take the DMs (NIP-59 gift wraps, kind 1059) addressed to
        ``pubkey`` out of the index."""
        def delete(conn: Any) -> int:
            ids = [row[0] for row in conn.execute(
                "SELECT e.id FROM nostr_events e JOIN nostr_event_tags t ON t.event_id = e.id "
                "WHERE e.kind = 1059 AND t.letter = 'p' AND t.value = ?", (pubkey,)
            ).fetchall()]
            for event_id in ids:
                conn.execute("DELETE FROM nostr_event_tags WHERE event_id = ?", (event_id,))
                conn.execute("DELETE FROM nostr_events WHERE id = ?", (event_id,))
                conn.execute("DELETE FROM pending_publishes WHERE event_id = ?", (event_id,))
            return len(ids)

        return await self._run(delete)

    async def delete_id(self, id: int, retired: RetiredName) -> None:
        """Forget an ID: its key, follows, blocks and DM rooms. Its name is
        retired for good (and so are names it was renamed from)."""
        def delete(conn: Any) -> None:
            for table in ("follows", "followers", "blocks", "dm_rooms"):
                conn.execute(f"DELETE FROM {table} WHERE id_ref = ?", (id,))
            conn.execute("UPDATE user_settings SET active_id = NULL WHERE active_id = ?", (id,))
            conn.execute("UPDATE retired_ids SET resolves_until = NULL WHERE id_ref = ?", (id,))
            conn.execute("DELETE FROM ids WHERE id = ?", (id,))
            conn.execute(
                f"INSERT INTO retired_ids ({_RETIRED_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET matrix_user_id = excluded.matrix_user_id, id_ref = excluded.id_ref, "
                "reason = excluded.reason, retired_at = excluded.retired_at, resolves_until = excluded.resolves_until",
                (retired.name, retired.matrix_user_id, retired.id_ref, retired.reason, retired.retired_at, retired.resolves_until),
            )

        await self._run(delete)

    async def delete_event(self, event_id: str) -> None:
        def delete(conn: Any) -> None:
            conn.execute("DELETE FROM nostr_event_tags WHERE event_id = ?", (event_id,))
            conn.execute("DELETE FROM nostr_events WHERE id = ?", (event_id,))
            conn.execute("DELETE FROM pending_publishes WHERE event_id = ?", (event_id,))

        await self._run(delete)

    # -- publishing ------------------------------------------------------

    async def put_pending_publish(self, pending: PendingPublish) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO pending_publishes (event_id, relays, attempts, next_attempt_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (event_id) DO UPDATE SET relays = excluded.relays, attempts = excluded.attempts, "
                "next_attempt_at = excluded.next_attempt_at",
                (pending.event_id, json.dumps(list(pending.relays)), pending.attempts, pending.next_attempt_at),
            )
        )

    async def list_due_publishes(self, now: str, limit: int = 100) -> list[PendingPublish]:
        def query(conn: Any) -> list[PendingPublish]:
            rows = conn.execute(
                "SELECT event_id, relays, attempts, next_attempt_at FROM pending_publishes "
                "WHERE next_attempt_at <= ? ORDER BY next_attempt_at LIMIT ?",
                (now, limit),
            ).fetchall()
            return [PendingPublish(r[0], tuple(json.loads(r[1])), r[2], r[3]) for r in rows]

        return await self._run(query)

    async def delete_pending_publish(self, event_id: str) -> None:
        await self._run(lambda conn: conn.execute("DELETE FROM pending_publishes WHERE event_id = ?", (event_id,)))

    async def get_relay_refusal(self, relay: str, pubkey: str) -> RelayRefusal | None:
        def query(conn: Any) -> RelayRefusal | None:
            row = conn.execute(
                "SELECT relay, pubkey, until, reason FROM relay_refusals WHERE relay = ? AND pubkey = ?", (relay, pubkey)
            ).fetchone()
            return RelayRefusal(*row) if row else None

        return await self._run(query)

    async def put_relay_refusal(self, refusal: RelayRefusal) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO relay_refusals (relay, pubkey, until, reason) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (relay, pubkey) DO UPDATE SET until = excluded.until, reason = excluded.reason",
                (refusal.relay, refusal.pubkey, refusal.until, refusal.reason),
            )
        )

    # -- blobs -----------------------------------------------------------

    async def put_blob(self, blob: Blob) -> None:
        await self._run(
            lambda conn: conn.execute(
                f"INSERT INTO blobs ({_BLOB_COLUMNS}) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (blob.sha256, blob.mxc, blob.mime_type, blob.size, blob.created_at),
            )
        )

    async def get_blob(self, sha256: str) -> Blob | None:
        def query(conn: Any) -> Blob | None:
            row = conn.execute(f"SELECT {_BLOB_COLUMNS} FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
            return Blob(*row) if row else None

        return await self._run(query)

    # -- AppService transactions -----------------------------------------

    async def claim_event(self, event_id: str, now: str) -> bool:
        def claim(conn: Any) -> bool:
            cursor = conn.execute(
                "INSERT INTO processed_events (event_id, processed_at) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (event_id, now),
            )
            return cursor.rowcount == 1

        return await self._run(claim)

    async def prune_processed_events(self, before: str) -> None:
        await self._run(lambda conn: conn.execute("DELETE FROM processed_events WHERE processed_at < ?", (before,)))

    # -- small persistent values -----------------------------------------

    async def get_state(self, key: str) -> str | None:
        def query(conn: Any) -> str | None:
            row = conn.execute("SELECT value FROM bridge_state WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

        return await self._run(query)

    async def delete_state(self, key: str) -> None:
        await self._run(lambda conn: conn.execute("DELETE FROM bridge_state WHERE key = ?", (key,)))

    async def list_state(self, prefix: str) -> list[tuple[str, str]]:
        """Every (key, value) whose key starts with ``prefix``."""
        def query(conn: Any) -> list[tuple[str, str]]:
            escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            return [(r[0], r[1]) for r in conn.execute(
                "SELECT key, value FROM bridge_state WHERE key LIKE ? ESCAPE '!'", (escaped + "%",)
            ).fetchall()]

        return await self._run(query)

    async def set_state(self, key: str, value: str) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO bridge_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        )
