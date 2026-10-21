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

from bridge.repository import Blob, EventRow, NostrId, PendingPublish, RelayRefusal, RetiredName, UserRooms

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
    renamed_at TEXT
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
    expires_at INTEGER
);

-- The single-letter tags queries can filter on (#e, #p, ...): references,
-- never text.
CREATE TABLE IF NOT EXISTS nostr_event_tags (
    event_id TEXT NOT NULL,
    letter TEXT NOT NULL,
    value TEXT NOT NULL
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
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = ()

_INDEXES = """
CREATE INDEX IF NOT EXISTS ids_matrix_user ON ids (matrix_user_id);
CREATE INDEX IF NOT EXISTS nostr_events_author_kind ON nostr_events (pubkey, kind, d_tag, created_at);
CREATE INDEX IF NOT EXISTS nostr_events_kind_time ON nostr_events (kind, created_at);
CREATE INDEX IF NOT EXISTS nostr_events_matrix_event ON nostr_events (matrix_event_id);
CREATE INDEX IF NOT EXISTS nostr_event_tags_lookup ON nostr_event_tags (letter, value);
CREATE INDEX IF NOT EXISTS nostr_event_tags_event ON nostr_event_tags (event_id);
CREATE INDEX IF NOT EXISTS processed_events_time ON processed_events (processed_at);
"""

# Run on every startup, after the schema is in place.
_STARTUP_FIXUPS: tuple[str, ...] = ()

_ID_COLUMNS = (
    "id, name, matrix_user_id, pubkey, secret_key, key_origin, bunker_uri, profile_room_id, created_at, renamed_at"
)
_RETIRED_COLUMNS = "name, matrix_user_id, id_ref, reason, retired_at, resolves_until"
_EVENT_COLUMNS = "id, pubkey, kind, created_at, sig, source, d_tag, room_id, matrix_event_id, expires_at"
_BLOB_COLUMNS = "sha256, mxc, mime_type, size, created_at"


def _nostr_id(row: tuple) -> NostrId:
    return NostrId(*row)


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
    ) -> NostrId | None:
        def insert(conn: Any) -> NostrId | None:
            row = conn.execute(
                "INSERT INTO ids (name, matrix_user_id, pubkey, secret_key, key_origin, bunker_uri, profile_room_id, created_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING {_ID_COLUMNS}",
                (name, matrix_user_id, pubkey, secret_key, key_origin, bunker_uri, profile_room_id, created_at),
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

    async def set_profile_room(self, id: int, room_id: str) -> None:
        await self._run(lambda conn: conn.execute("UPDATE ids SET profile_room_id = ? WHERE id = ?", (room_id, id)))

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
                f"INSERT INTO nostr_events ({_EVENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    row.id, row.pubkey, row.kind, row.created_at, row.sig, row.source, row.d_tag,
                    row.room_id, row.matrix_event_id, row.expires_at,
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

    async def get_event_by_matrix_event(self, matrix_event_id: str) -> EventRow | None:
        def query(conn: Any) -> EventRow | None:
            row = conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM nostr_events WHERE matrix_event_id = ? ORDER BY created_at, id LIMIT 1",
                (matrix_event_id,),
            ).fetchone()
            return _event_row(conn, row) if row else None

        return await self._run(query)

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

    async def set_state(self, key: str, value: str) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO bridge_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        )
