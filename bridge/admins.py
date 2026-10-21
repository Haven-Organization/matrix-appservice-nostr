"""Who is a bridge admin.

Anyone listed in ``bridge.admins``, on any homeserver. With
``bridge.use_synapse_admin_api`` on, Synapse server admins count too: the
one place this bridge uses a Synapse-only API. It is off by default, so
the bridge needs nothing beyond the Matrix spec.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from bridge.homeserver_client import HomeserverClient

logger = logging.getLogger(__name__)


async def _is_synapse_server_admin(homeserver: HomeserverClient, token: str, user_id: str) -> bool:
    try:
        response = await homeserver.http.get(
            f"/_synapse/admin/v2/users/{quote(user_id, safe='@:')}",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError:
        logger.warning("Could not reach the Synapse Admin API to check %s", user_id, exc_info=True)
        return False
    if response.status_code != 200:
        return False
    try:
        return bool(response.json().get("admin"))
    except ValueError:
        return False


async def is_admin(config: Any, homeserver: HomeserverClient, user_id: str) -> bool:
    if user_id in config.bridge.admins:
        return True
    token = config.homeserver.synapse_admin_token
    if config.bridge.use_synapse_admin_api and token:
        return await _is_synapse_server_admin(homeserver, token, user_id)
    return False
