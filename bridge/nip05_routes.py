"""The NIP-05 endpoint, ``/.well-known/nostr.json``, and its self-check.

Every Nostr ID's handle is ``name@<bridge.domain>``. NIP-05 clients check
one by fetching ``https://<bridge.domain>/.well-known/nostr.json?name=<name>``,
so that domain's web server must proxy that path here. It may not redirect:
NIP-05 forbids redirects, and clients ignore them.

The answer contains only the name asked for, never the full list (the
spec allows either; listing everyone would hand out the user directory),
plus where that key's events can be found: this bridge's relay.

``_`` is the bot, so ``_@<domain>`` (shown as just the domain) is the
bridge's own identity. That is also what the startup self-check asks for:
if the bot's key comes back, the proxy is in place.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from bridge.nostr.nip05 import ROOT_NAME, is_valid_name

logger = logging.getLogger(__name__)

router = APIRouter()

WELL_KNOWN_PATH = "/.well-known/nostr.json"
_SELF_CHECK_DELAY = 3.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def lookup(app: FastAPI, name: str) -> str | None:
    """The pubkey ``name`` resolves to here, if any."""
    if name == ROOT_NAME:
        return app.state.bot_identity.pubkey
    return await app.state.repository.resolve_nip05(name, _now())


@router.get(WELL_KNOWN_PATH)
async def nostr_json(request: Request) -> JSONResponse:
    # Clients and caches vary in how they treat case; names are lowercase.
    name = (request.query_params.get("name") or "").strip().lower()
    body: dict[str, Any] = {"names": {}}
    if name and is_valid_name(name):
        pubkey = await lookup(request.app, name)
        if pubkey is not None:
            body["names"][name] = pubkey
            body["relays"] = {pubkey: [request.app.state.config.nostr.relay_url]}
    # Short cache: a rename or a new ID should show up within minutes.
    return JSONResponse(body, headers={"Cache-Control": "public, max-age=300"})


async def run_self_check(app: FastAPI, client: httpx.AsyncClient) -> tuple[bool, str]:
    """Fetch our own ``_@<domain>`` over the public internet and say whether
    it answers with the bot's key. Returns (ok, explanation)."""
    config = app.state.config
    url = f"https://{config.bridge.domain}{WELL_KNOWN_PATH}?name={ROOT_NAME}"
    try:
        response = await client.get(url, follow_redirects=False, timeout=10.0)
    except httpx.HTTPError as exc:
        return False, f"{url} could not be reached ({exc!r})"
    if response.is_redirect:
        return False, f"{url} redirects, which NIP-05 forbids: proxy the path to the bridge instead"
    if response.status_code != 200:
        return False, f"{url} answered HTTP {response.status_code}"
    try:
        names = response.json().get("names") or {}
    except (ValueError, AttributeError):
        return False, f"{url} did not return JSON"
    found = names.get(ROOT_NAME) if isinstance(names, dict) else None
    expected = app.state.bot_identity.pubkey
    if found != expected:
        return False, f"{url} answered, but not with this bridge's key: is another service serving that path?"
    if response.headers.get("access-control-allow-origin") != "*":
        return False, f"{url} works, but without Access-Control-Allow-Origin: *, so web clients can't check handles"
    return True, f"{url} reaches this bridge"


async def self_check_soon(app: FastAPI) -> None:
    """At startup, once the server is accepting connections: check the
    NIP-05 proxy and tell the operator, rather than leaving them to find
    out from a user."""
    await asyncio.sleep(_SELF_CHECK_DELAY)
    async with httpx.AsyncClient(headers={"User-Agent": "matrix-appservice-nostr self-check"}) as client:
        ok, explanation = await run_self_check(app, client)
    app.state.nip05_self_check = (ok, explanation, _now())
    if ok:
        logger.info("NIP-05 self-check passed: %s", explanation)
    else:
        logger.warning(
            "NIP-05 self-check failed: %s. Handles under %s won't verify until it passes; "
            "see deploy/nginx.conf.example.",
            explanation,
            app.state.config.bridge.domain,
        )
