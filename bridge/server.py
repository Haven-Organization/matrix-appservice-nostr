"""FastAPI application factory.

Wires together config, the repository, the key vault and signer, the
homeserver client, the guarded fetcher and the outgoing relay pool, then
mounts the NIP-05 endpoint (``bridge.nip05_routes``) and the AppService
API (``bridge.appservice_routes``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from bridge.appservice_routes import router as appservice_router
from bridge.users import bot_mxid
from bridge.config import BridgeConfig
from bridge.homeserver_client import HomeserverClient, HomeserverError
from bridge.key_vault import KeyVault
from bridge.identity_events import sync_all_identities
from bridge.media_routes import BLOB_PATH_RE
from bridge.media_routes import router as media_router
from bridge.feeds import Feeds
from bridge.inbound import Seen
from bridge.nip05_routes import router as nip05_router
from bridge.relay_server import RelayHub
from bridge.relay_server import router as relay_router
from bridge.spam import SpamGuard
from bridge.widget import router as widget_router
from bridge.publishing import retry_loop
from bridge.nip05_routes import self_check_soon
from bridge.nostr.relay_pool import RelayPool
from bridge.repository import BridgeRepository
from bridge.safe_fetch import SafeFetcher
from bridge.signing import Signer
from bridge.storage import open_repository
from bridge.tasks import cancel_all, run_in_background

logger = logging.getLogger(__name__)

# Service managers only guarantee the homeserver's process has started,
# not that it accepts connections yet, so wait for it rather than crash.
_HOMESERVER_READY_TIMEOUT = 60.0
_HOMESERVER_READY_POLL_INTERVAL = 2.0
_PING_DELAY = 3.0


async def _wait_for_homeserver(homeserver: HomeserverClient, base_url: str) -> None:
    deadline = time.monotonic() + _HOMESERVER_READY_TIMEOUT
    last_exc: Exception | None = None
    logged = False
    while time.monotonic() < deadline:
        try:
            await homeserver.get_versions()
            return
        except (httpx.HTTPError, HomeserverError) as exc:
            last_exc = exc
            if not logged:
                logger.info("Waiting for the homeserver to become reachable at %s...", base_url)
                logged = True
            await asyncio.sleep(_HOMESERVER_READY_POLL_INTERVAL)
    raise RuntimeError(f"The homeserver never became reachable at {base_url}") from last_exc


async def _ensure_bot_user(homeserver: HomeserverClient, config: BridgeConfig) -> None:
    """Register the bot's Matrix account (a no-op once it exists) and apply
    its configured profile, only where that differs."""
    try:
        await homeserver.register_appservice_user(config.appservice.bot_localpart)
    except HomeserverError as exc:
        if exc.errcode != "M_USER_IN_USE":
            logger.error(
                "Could not register the bot user @%s: %s. Has the AppService registration been "
                "added to the homeserver, and the homeserver restarted since?",
                config.appservice.bot_localpart,
                exc,
            )
            return
    mxid = bot_mxid(config)
    try:
        profile = await homeserver.get_profile(mxid)
    except HomeserverError:
        profile = {}
    try:
        name, avatar = config.appservice.bot_display_name, config.appservice.bot_avatar_mxc
        if name and profile.get("displayname") != name:
            await homeserver.set_display_name(mxid, name)
        if avatar and profile.get("avatar_url") != avatar:
            await homeserver.set_avatar_url(mxid, avatar)
    except HomeserverError:
        logger.warning("Could not update the bot's profile", exc_info=True)


# Handled event IDs are kept this long: far longer than any homeserver
# keeps retrying a transaction.
_PROCESSED_EVENTS_KEPT = timedelta(days=7)
_HOUSEKEEPING_INTERVAL = 6 * 3600.0


async def _housekeeping_loop(app: FastAPI) -> None:
    while True:
        try:
            before = (datetime.now(timezone.utc) - _PROCESSED_EVENTS_KEPT).isoformat()
            await app.state.repository.prune_processed_events(before)
        except Exception:
            logger.exception("Housekeeping failed")
        await asyncio.sleep(_HOUSEKEEPING_INTERVAL)


async def _spam_tick_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await app.state.spam.tick()
        except Exception:
            logger.exception("Spam housekeeping failed")


_SHIELD_POLICY = {
    "pow": "strangers now need proof of work.",
    "web_of_trust": "strangers now need to be in the web of trust.",
    "open": "the relay's rules are unchanged (wave_policy is open).",
}


async def _shields_changed(app: FastAPI, up: bool, count: int) -> None:
    """Tell bridge admins when the relay tightens up against a wave of new
    accounts, and when it relaxes again."""
    from bridge.notifications import ensure_notifications_room
    from bridge.messages import notice

    config = app.state.config
    policy = config.spam.wave_policy
    message = (
        f"🛡️ Shields up: {count} new accounts in a few minutes; " + _SHIELD_POLICY.get(policy, "the relay is stricter with strangers.")
        if up
        else "🛡️ Shields down: the wave has passed, and the relay is back to its usual rules."
    )
    logger.warning(message)
    for admin in config.bridge.admins:
        room_id = await ensure_notifications_room(app, admin)
        if room_id:
            await notice(app, room_id, message)


async def _ping_soon(homeserver: HomeserverClient, appservice_id: str) -> None:
    """Once the server is up, ask the homeserver to reach us (spec v1.7),
    so a homeserver that can't push events is reported at startup."""
    await asyncio.sleep(_PING_DELAY)
    try:
        duration = await homeserver.ping_appservice(appservice_id)
    except HomeserverError as exc:
        logger.warning(
            "The homeserver could not reach this bridge (%s). Check the registration's url "
            "(bridge.internal_base_url) is reachable from the homeserver.",
            exc,
        )
        return
    except httpx.HTTPError:
        logger.warning("Could not ask the homeserver to ping the bridge", exc_info=True)
        return
    if duration is None:
        logger.info("The homeserver doesn't support AppService pings; skipping that check")
    else:
        logger.info("The homeserver reaches this bridge (%d ms)", duration)


class PublicCors:
    """Lets web pages on any site read the bridge's public endpoints.
    Browser-based Nostr clients check handles by fetching
    ``/.well-known/nostr.json`` from the visitor's browser, which refuses
    the answer without this permission. NIP-05 asks for the header on every
    response, not only ones to requests carrying an ``Origin``, so it is
    always added (and a preflight answered). Nothing here uses cookies, so
    allowing every origin gives nothing away. The AppService API is left
    alone: no browser has business there."""

    _PUBLIC_PREFIXES = ("/.well-known/",)

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    @classmethod
    def is_public(cls, path: str) -> bool:
        # Published media (/<sha256>.<ext>) and uploads too: Blossom
        # requires CORS. And the relay's own address (NIP-11, NIP-86).
        return (
            path.startswith(cls._PUBLIC_PREFIXES)
            or BLOB_PATH_RE.match(path) is not None
            or path in ("/upload", "/")
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.is_public(str(scope.get("path", ""))):
            await self._app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", b"*"),
                        (b"access-control-allow-methods", b"GET, HEAD, PUT, POST, OPTIONS"),
                        (b"access-control-allow-headers", b"Authorization, Content-Type, *"),
                        (b"access-control-max-age", b"86400"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"access-control-allow-origin"]
                headers.append((b"access-control-allow-origin", b"*"))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_cors)


async def _matrix_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """AppService API errors in the Matrix shape ({"errcode", "error"})."""
    if isinstance(exc.detail, dict) and "errcode" in exc.detail:
        return JSONResponse(exc.detail, status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def create_app(
    config: BridgeConfig,
    *,
    repository: BridgeRepository | None = None,
    homeserver: HomeserverClient | None = None,
    relay_pool: RelayPool | None = None,
    run_startup_checks: bool = True,
) -> FastAPI:
    """Build the FastAPI app.

    ``repository`` defaults to the one ``storage.backend`` selects; pass
    ``SqliteBridgeRepository(":memory:")`` for tests, and likewise a
    ``homeserver`` client pointed at a stand-in.
    """
    owns_repository = repository is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = config
        app.state.repository = repository or open_repository(config)
        app.state.vault = KeyVault(config.nostr.key_encryption_secret)
        app.state.signer = Signer(app.state.repository, app.state.vault)
        from bridge.widget import load_sessions

        await load_sessions(app)  # widgets stay signed in through a restart
        app.state.bot_identity = await app.state.signer.load_bot_identity()
        app.state.fetcher = SafeFetcher(allow_private=config.nostr.allow_private_addresses)
        app.state.relay_pool = relay_pool or RelayPool(
            allow_private=config.nostr.allow_private_addresses,
            auth_signer=app.state.signer.auth_event,
            fetcher=app.state.fetcher,
        )
        app.state.signer.pool = app.state.relay_pool
        app.state.nip05_self_check = None
        app.state.background_tasks = set()
        app.state.identity_locks = {}
        app.state.remote_locks = {}
        app.state.ingest_inflight = {}
        app.state.seen = Seen()
        app.state.web_of_trust = set()
        app.state.shared_blocks = set()
        app.state.emoji_cache = {}
        app.state.relay_hub = RelayHub(app)
        app.state.feeds = Feeds(app)
        app.state.spam = SpamGuard(
            config.spam, app.state.repository, on_shields_change=lambda up, count: _shields_changed(app, up, count)
        )
        app.state.homeserver = homeserver or HomeserverClient(
            config.homeserver.base_url, as_token=config.appservice.as_token
        )
        tasks: list[asyncio.Task[None]] = []
        try:
            if run_startup_checks:
                await _wait_for_homeserver(app.state.homeserver, config.homeserver.base_url)
                await _ensure_bot_user(app.state.homeserver, config)
                tasks.append(asyncio.create_task(_ping_soon(app.state.homeserver, config.appservice.id)))
                tasks.append(asyncio.create_task(self_check_soon(app)))
                # Catches a changed bridge.domain, relay or media URL, and
                # anything a previous run couldn't publish.
                run_in_background(app, sync_all_identities(app), name="startup identity sync")
                tasks.append(asyncio.create_task(retry_loop(app), name="publish retries"))
                tasks.append(asyncio.create_task(_housekeeping_loop(app), name="housekeeping"))
                tasks.append(asyncio.create_task(_spam_tick_loop(app), name="spam tick"))
                from bridge.concord import expiry_loop

                tasks.append(asyncio.create_task(expiry_loop(app), name="community message expiry"))
                app.state.feeds.start()
                from bridge.room_widget import add_missing_widgets

                run_in_background(app, add_missing_widgets(app), name="widgets")
                from bridge.remote import converge_all

                async def converge() -> None:
                    from bridge.inbound import repair_repost_cards

                    await converge_all(app)
                    await repair_repost_cards(app)

                run_in_background(app, converge(), name="remote accounts")
            logger.info(
                "Bridge identity: _@%s (%s)", config.bridge.domain, app.state.bot_identity.pubkey
            )
            yield
        finally:
            for task in tasks:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await app.state.feeds.stop()
            await cancel_all(app)
            await app.state.relay_pool.close()
            await app.state.fetcher.aclose()
            await app.state.homeserver.aclose()
            if owns_repository:
                await app.state.repository.close()

    app = FastAPI(title="matrix-appservice-nostr", description="Matrix <-> Nostr bridge and relay", lifespan=lifespan)
    app.add_middleware(PublicCors)
    # Outermost: the webxdc sandbox host never reaches the bridge's routes.
    from bridge.webxdc_routes import SandboxHost, router as webxdc_router

    app.add_middleware(SandboxHost)
    app.add_exception_handler(HTTPException, _matrix_error_handler)
    app.include_router(nip05_router)
    app.include_router(appservice_router)
    app.include_router(relay_router)
    app.include_router(widget_router)
    from bridge.games_hub import router as games_hub_router

    app.include_router(games_hub_router)
    app.include_router(webxdc_router)
    # Last: its "/{blob}" path would otherwise shadow single-segment routes.
    app.include_router(media_router)
    return app
