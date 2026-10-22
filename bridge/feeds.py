"""The bridge as a Nostr client: the subscriptions that bring events in.

Nostr has no inbox or firehose; the bridge goes and asks (DESIGN.md,
"Getting posts from followed accounts"):

- **follows**: for every relay a followed account writes to (its NIP-65
  list, or ``nostr.fallback_relays`` if it has none), one subscription to
  the accounts followed on that relay.
- **inbox**: on ``nostr.publish_relays`` and ``nostr.fallback_relays``,
  everything tagging a local ID (``#p``, and ``#P`` for NIP-22 comments),
  and gift-wrapped DMs. Most clients send replies to the big relays rather
  than the recipient's inbox, so these catch them.

Each subscription remembers how far it has read (``relay_cursors``), so a
restart resumes from there (minus a safety margin; repeats are harmless,
events are deduplicated by id). Events go into a bounded queue and through
``bridge.inbound.ingest`` a few at a time.

Every few hours the follow lists of followed accounts are read to build the
web of trust (accounts followed by someone a local ID follows), which the
spam rules treat more kindly than strangers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import FastAPI

from bridge.nostr.relay_pool import normalize_relay_url

logger = logging.getLogger(__name__)

FOLLOW_KINDS = [0, 1, 5, 6, 7, 16, 20, 21, 22, 1068, 1111, 10002, 30023, 34235, 34236]
INBOX_KINDS = [1, 3, 6, 7, 16, 1111, 9735]
DM_KINDS = [4, 1059]
_SAFETY_SECONDS = 300
# Gift wraps carry a randomized created_at up to two days in the past.
_GIFT_WRAP_LOOKBACK = 3 * 86400
_AUTHORS_PER_FILTER = 250
_FIRST_INBOX_LOOKBACK = 7 * 86400
_REFRESH_SECONDS = 1800
_WOT_REFRESH_SECONDS = 6 * 3600
_QUEUE_SIZE = 20000
_WORKERS = 4


class Feeds:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self._dirty = asyncio.Event()
        self._queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue(_QUEUE_SIZE)
        # (relay, name) -> (sub id, filters)
        self._active: dict[tuple[str, str], tuple[str, list[dict[str, Any]]]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._last_refresh = 0.0

    def changed(self) -> None:
        """Follows or relay lists changed: work out the subscriptions again."""
        self._dirty.set()

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._manage(), name="feeds"))
        self._tasks.append(asyncio.create_task(self._web_of_trust_loop(), name="web of trust"))
        for n in range(_WORKERS):
            self._tasks.append(asyncio.create_task(self._work(), name=f"ingest worker {n}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # -- desired subscriptions --------------------------------------------------

    async def _desired(self) -> dict[tuple[str, str], list[dict[str, Any]]]:
        app = self.app
        repository = app.state.repository
        config = app.state.config
        desired: dict[tuple[str, str], list[dict[str, Any]]] = {}

        by_relay: dict[str, list[str]] = {}
        for pubkey in await repository.list_followed_pubkeys():
            remote = await repository.get_remote_account(pubkey)
            relays = list(remote.write_relays[:4]) if remote and remote.write_relays else list(config.nostr.fallback_relays)
            for relay in relays:
                by_relay.setdefault(normalize_relay_url(relay), []).append(pubkey)
        for relay, authors in by_relay.items():
            since = await self._since(relay, "follows")
            filters = [
                {"authors": sorted(authors[i:i + _AUTHORS_PER_FILTER]), "kinds": FOLLOW_KINDS, "since": since}
                for i in range(0, len(authors), _AUTHORS_PER_FILTER)
            ]
            desired[(relay, "follows")] = filters

        local_keys = sorted(i.pubkey for i in await repository.list_all_ids())
        if local_keys:
            inbox_relays = dict.fromkeys(
                normalize_relay_url(r) for r in [*config.nostr.publish_relays, *config.nostr.fallback_relays]
            )
            for relay in inbox_relays:
                since = await self._since(relay, "inbox")
                dm_since = await self._since(relay, "dms", lookback=_GIFT_WRAP_LOOKBACK)
                desired[(relay, "inbox")] = [
                    {"#p": local_keys, "kinds": INBOX_KINDS, "since": since},
                    {"#P": local_keys, "kinds": [1111], "since": since},
                ]
                desired[(relay, "dms")] = [{"#p": local_keys, "kinds": DM_KINDS, "since": dm_since}]
        return desired

    async def _since(self, relay: str, name: str, *, lookback: int = _SAFETY_SECONDS) -> int:
        cursor = await self.app.state.repository.get_cursor(relay, name)
        now = int(time.time())
        if cursor is None:
            # First time: what's addressed to local IDs over the last week
            # (replies and follows that came before the bridge listened);
            # for follows only what's new (following backfills instead).
            first = _FIRST_INBOX_LOOKBACK if name in ("inbox", "dms") else lookback
            await self.app.state.repository.advance_cursor(relay, name, now - first)
            return now - first
        return max(0, cursor - lookback)

    @staticmethod
    def _shape(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in f.items() if k != "since"} for f in filters]

    async def _apply(self, refresh: bool) -> None:
        pool = self.app.state.relay_pool
        desired = await self._desired()
        for key in list(self._active):
            if key not in desired:
                relay, _ = key
                sub_id, _ = self._active.pop(key)
                await pool.unsubscribe(relay, sub_id)
        for key, filters in desired.items():
            current = self._active.get(key)
            if current is not None and not refresh and self._shape(current[1]) == self._shape(filters):
                continue
            relay, name = key
            if current is not None:
                await pool.unsubscribe(relay, current[0])
            sub_id = pool.subscribe(relay, filters, self._make_handler(name))
            self._active[key] = (sub_id, filters)
        logger.info("Subscriptions: %d on %d relays", len(self._active), len({k[0] for k in self._active}))

    def _make_handler(self, name: str):
        def on_event(relay: str, sub_id: str, raw: dict[str, Any]) -> None:
            try:
                self._queue.put_nowait((relay, name, raw))
            except asyncio.QueueFull:
                logger.warning("Incoming event queue is full; dropping an event from %s", relay)

        return on_event

    async def _manage(self) -> None:
        while True:
            refresh = time.monotonic() - self._last_refresh > _REFRESH_SECONDS
            try:
                await self._apply(refresh)
                if refresh:
                    self._last_refresh = time.monotonic()
            except Exception:
                logger.exception("Updating subscriptions failed")
            self._dirty.clear()
            try:
                await asyncio.wait_for(self._dirty.wait(), 60)
                await asyncio.sleep(2)  # gather a burst of changes
            except asyncio.TimeoutError:
                pass

    async def _work(self) -> None:
        from bridge.inbound import ingest

        repository = self.app.state.repository
        while True:
            relay, name, raw = await self._queue.get()
            try:
                result = await ingest(self.app, raw, origin="subscription")
                created = raw.get("created_at")
                if isinstance(created, int) and name != "dms" and created <= time.time() + 600:
                    await repository.advance_cursor(relay, name, created)
                if not result.ok and not result.message.startswith(("restricted", "duplicate", "blocked")):
                    logger.debug("Ignored %s from %s: %s", raw.get("id"), relay, result.message)
            except Exception:
                logger.exception("Handling an event from %s failed", relay)
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        await self._queue.join()

    # -- web of trust -------------------------------------------------------------

    async def _web_of_trust_loop(self) -> None:
        while True:
            for refresh in (self.refresh_web_of_trust, self.refresh_shared_blocks):
                try:
                    await refresh()
                except Exception:
                    logger.exception("%s failed", refresh.__name__)
            await asyncio.sleep(_WOT_REFRESH_SECONDS)

    async def refresh_shared_blocks(self) -> None:
        """Who the accounts in ``nostr.shared_block_lists`` mute (kind 10000,
        public entries) or report (kind 1984)."""
        app = self.app
        owners = app.state.config.nostr.shared_block_lists
        if not owners:
            app.state.shared_blocks = set()
            return
        relays = [*app.state.config.nostr.discovery_relays, *app.state.config.nostr.fallback_relays]
        found = await app.state.relay_pool.query(relays, [{"authors": owners, "kinds": [10000, 1984]}], timeout=15.0)
        from bridge.nostr import event as nostr_event

        blocked: set[str] = set()
        newest_mute: dict[str, dict] = {}
        for raw in found:
            if raw.get("pubkey") not in owners:
                continue
            try:
                nostr_event.verify(raw)
            except nostr_event.InvalidEvent:
                continue
            if raw["kind"] == 10000:
                if raw["pubkey"] not in newest_mute or raw["created_at"] > newest_mute[raw["pubkey"]]["created_at"]:
                    newest_mute[raw["pubkey"]] = raw
            else:
                blocked.update(t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "p" and len(t[1]) == 64)
        for raw in newest_mute.values():
            blocked.update(t[1] for t in raw.get("tags") or [] if len(t) >= 2 and t[0] == "p" and len(t[1]) == 64)
        app.state.shared_blocks = blocked
        logger.info("Shared block lists: %d accounts", len(blocked))

    async def refresh_web_of_trust(self) -> None:
        app = self.app
        followed = await app.state.repository.list_followed_pubkeys()
        if not followed:
            return
        relays = [*app.state.config.nostr.discovery_relays, *app.state.config.nostr.fallback_relays]
        trusted: set[str] = set()
        for i in range(0, len(followed), _AUTHORS_PER_FILTER):
            chunk = followed[i:i + _AUTHORS_PER_FILTER]
            for raw in await app.state.relay_pool.query(relays, [{"authors": chunk, "kinds": [3]}], timeout=15.0):
                if raw.get("pubkey") in chunk:
                    trusted.update(
                        t[1] for t in raw.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == "p" and len(t[1]) == 64
                    )
                if len(trusted) > 200_000:
                    break
        app.state.web_of_trust = trusted
        logger.info("Web of trust: %d accounts", len(trusted))
