"""The bridge's NIP-89 app announcement (kind 31990).

Public events the bridge signs carry a "client" tag (``bridge.signing``)
that points here: ``["client", <name>, "31990:<bot pubkey>:<name>",
<relay>]``. Apps that list clients (Radar Nostr, and those showing "via
...") read this event for the bridge's name, icon and website.

Signed by the bridge's own identity (``_@<bridge.domain>``), published at
startup when it says something new, and indexed (inline: its content is
config, not anyone's message) so the bridge's relay serves it too.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI

from bridge.event_sources import inline_source
from bridge.nostr.event import compute_id
from bridge.publishing import publish_in_background
from bridge.repository import EventRow

logger = logging.getLogger(__name__)

HANDLER_KIND = 31990
ABOUT = "A Matrix bridge for Nostr: Matrix users post, follow, reply, react and DM on Nostr from their own rooms."


def handler_address(app: FastAPI) -> str:
    name = app.state.config.bridge.client_name
    return f"{HANDLER_KIND}:{app.state.bot_identity.pubkey}:{name}"


def client_tag(app: FastAPI) -> list[str]:
    """The "client" tag for public events, or [] with no client name."""
    config = app.state.config
    name = config.bridge.client_name
    if not name:
        return []
    return ["client", name, handler_address(app), config.nostr.relay_url]


def _content(app: FastAPI) -> str:
    bridge = app.state.config.bridge
    metadata = {"name": bridge.client_name, "display_name": bridge.client_name, "about": ABOUT}
    if bridge.client_picture:
        metadata["picture"] = bridge.client_picture
    if bridge.client_website:
        metadata["website"] = bridge.client_website
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))


async def publish_handler(app: FastAPI) -> None:
    """Sign and publish the announcement, unless the last one says the same."""
    name = app.state.config.bridge.client_name
    if not name:
        return
    pubkey = app.state.bot_identity.pubkey
    repository = app.state.repository
    tags = [["d", name]]
    content = _content(app)
    previous = await repository.get_latest_event(pubkey, HANDLER_KIND)
    if previous is not None and compute_id(pubkey, previous.created_at, HANDLER_KIND, tags, content) == previous.id:
        return
    created_at = int(time.time())
    if previous is not None and created_at <= previous.created_at:
        created_at = previous.created_at + 1
    event = await app.state.signer.sign(pubkey, kind=HANDLER_KIND, content=content, created_at=created_at, tags=tags)
    if event is None:
        return
    await repository.put_event(
        EventRow(id=event.id, pubkey=pubkey, kind=HANDLER_KIND, created_at=created_at, sig=event.sig,
                 source=json.dumps(inline_source(HANDLER_KIND, tags, content), ensure_ascii=False, separators=(",", ":")),
                 tags=(("d", name),)),
        replace=True,
    )
    logger.info("Published the app announcement %s", handler_address(app))
    publish_in_background(app, event)
