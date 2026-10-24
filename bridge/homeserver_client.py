"""Async wrapper around the Matrix Client-Server API.

Every interaction this bridge has with Matrix goes through this client,
authenticated with the AppService's ``as_token`` and acting as the bot or
a ghost via ``user_id=`` impersonation (the AppService API's identity
assertion). It uses only endpoints in the Matrix spec, so it works with
any spec-compliant homeserver. The one Synapse-only call the bridge can
make lives apart from it, in ``bridge.admins``.

It is a thin, typed wrapper: it knows nothing about Nostr or Profile Rooms.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class HomeserverError(Exception):
    """The homeserver returned an error response."""

    def __init__(self, status_code: int, errcode: str | None, error: str | None) -> None:
        self.status_code = status_code
        self.errcode = errcode
        self.error = error
        super().__init__(f"Homeserver error {status_code} {errcode}: {error}")


@dataclass(frozen=True)
class MediaDownload:
    content: bytes
    content_type: str
    content_disposition: str | None = None


def _error_from(response: httpx.Response) -> HomeserverError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return HomeserverError(response.status_code, body.get("errcode"), body.get("error"))


class HomeserverClient:
    def __init__(self, base_url: str, *, as_token: str, timeout: float = 15.0) -> None:
        self._as_token = as_token
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._default_room_version: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        """The underlying client, for the rare caller that needs a raw
        request against the homeserver (e.g. ``bridge.admins``)."""
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        as_user_id: str | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._as_token}"} if authenticated else {}
        request_params = dict(params or {})
        if as_user_id:
            request_params["user_id"] = as_user_id
        response = await self._client.request(method, path, headers=headers, params=request_params, json=json_body)
        if response.status_code >= 400:
            raise _error_from(response)
        return response.json() if response.content else {}

    # -- server --------------------------------------------------------------

    async def get_versions(self) -> dict[str, Any]:
        """Public and side-effect-free: used as a startup readiness probe."""
        return await self._request("GET", "/_matrix/client/versions", authenticated=False)

    async def ping_appservice(self, appservice_id: str) -> int | None:
        """Ask the homeserver to ping this AppService back (spec v1.7).
        Returns the round trip in milliseconds, or ``None`` if the
        homeserver doesn't support the endpoint. Raises if it does and the
        ping failed: the homeserver can't reach the bridge."""
        try:
            result = await self._request(
                "POST",
                f"/_matrix/client/v1/appservice/{quote(appservice_id, safe='')}/ping",
                json_body={"transaction_id": f"ping-{uuid.uuid4().hex}"},
            )
        except HomeserverError as exc:
            if exc.status_code in (404, 405) or exc.errcode == "M_UNRECOGNIZED":
                return None
            raise
        duration = result.get("duration_ms")
        return int(duration) if isinstance(duration, (int, float)) else 0

    async def _is_pre_v12_default(self) -> bool:
        """Whether rooms this homeserver creates by default predate room
        version 12, which changed how a room's creator relates to its power
        levels (see ``create_room``). Unknown counts as pre-v12."""
        if self._default_room_version is None:
            try:
                caps = await self._request("GET", "/_matrix/client/v3/capabilities")
                self._default_room_version = caps.get("capabilities", {}).get("m.room_versions", {}).get("default") or "1"
            except HomeserverError:
                self._default_room_version = "1"
        try:
            return int(self._default_room_version) < 12
        except ValueError:
            return True

    # -- users and profiles --------------------------------------------------

    async def register_appservice_user(self, localpart: str) -> str:
        """Provision a user in the AppService's namespace. Returns the full MXID."""
        body = await self._request(
            "POST",
            "/_matrix/client/v3/register",
            json_body={"type": "m.login.application_service", "username": localpart},
        )
        return body["user_id"]

    async def set_display_name(self, user_id: str, display_name: str) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{quote(user_id, safe='@:')}/displayname",
            json_body={"displayname": display_name},
            as_user_id=user_id,
        )

    async def set_avatar_url(self, user_id: str, mxc_uri: str) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{quote(user_id, safe='@:')}/avatar_url",
            json_body={"avatar_url": mxc_uri},
            as_user_id=user_id,
        )

    async def get_profile(self, user_id: str) -> dict[str, Any]:
        """``{"displayname": ..., "avatar_url": "mxc://..."}`` (keys may be absent)."""
        return await self._request("GET", f"/_matrix/client/v3/profile/{quote(user_id, safe='@:')}")

    async def set_profile_field(self, user_id: str, key: str, value: Any) -> None:
        """Set a custom profile field (MSC4133, extensible profiles). Raises
        ``HomeserverError`` on a homeserver without it."""
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{quote(user_id, safe='@:')}/{quote(key, safe='')}",
            json_body={key: value},
            as_user_id=user_id,
        )

    async def delete_profile_field(self, user_id: str, key: str) -> None:
        """Remove a custom profile field (MSC4133)."""
        await self._request(
            "DELETE", f"/_matrix/client/v3/profile/{quote(user_id, safe='@:')}/{quote(key, safe='')}", as_user_id=user_id
        )

    # -- rooms ---------------------------------------------------------------

    async def create_room(
        self,
        *,
        as_user_id: str,
        name: str | None = None,
        topic: str | None = None,
        invite: list[str] | None = None,
        avatar_mxc: str | None = None,
        room_type: str | None = None,
        power_level_users: dict[str, int] | None = None,
        power_level_events: dict[str, int] | None = None,
        is_direct: bool = False,
        preset: str = "private_chat",
        join_rule: str | None = None,
        history_visibility: str | None = None,
        initial_state: list[dict[str, Any]] | None = None,
    ) -> str:
        """Create a room as ``as_user_id``. Returns the room ID.

        ``power_level_users`` is applied on top of the defaults. The creator
        must not be given a level in a v12+ room (creators are implicitly
        above every level there) but must be listed in an older one, so that
        is handled here."""
        body: dict[str, Any] = {"preset": preset, "is_direct": is_direct}
        if name:
            body["name"] = name
        if topic:
            body["topic"] = topic
        if invite:
            body["invite"] = invite
        state: list[dict[str, Any]] = list(initial_state or [])
        if avatar_mxc:
            state.append({"type": "m.room.avatar", "state_key": "", "content": {"url": avatar_mxc}})
        if join_rule:
            state.append({"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": join_rule}})
        if history_visibility:
            state.append(
                {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": history_visibility}}
            )
        if state:
            body["initial_state"] = state
        if room_type:
            body["creation_content"] = {"type": room_type}
        override: dict[str, Any] = {}
        if power_level_users:
            users = dict(power_level_users)
            if await self._is_pre_v12_default():
                users.setdefault(as_user_id, 100)
            override["users"] = users
        if power_level_events:
            override["events"] = dict(power_level_events)
        if override:
            body["power_level_content_override"] = override
        result = await self._request("POST", "/_matrix/client/v3/createRoom", json_body=body, as_user_id=as_user_id)
        return result["room_id"]

    async def invite_user(self, room_id: str, user_id: str, *, as_user_id: str, is_direct: bool = False) -> None:
        body: dict[str, Any] = {"user_id": user_id}
        if is_direct:
            body["is_direct"] = True
        await self._request(
            "POST", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/invite", json_body=body, as_user_id=as_user_id
        )

    async def join_room(self, room_id: str, *, as_user_id: str) -> None:
        await self._request("POST", f"/_matrix/client/v3/join/{quote(room_id, safe='')}", as_user_id=as_user_id)

    async def leave_room(self, room_id: str, *, as_user_id: str) -> None:
        await self._request("POST", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/leave", as_user_id=as_user_id)

    async def kick_user(self, room_id: str, user_id: str, *, as_user_id: str, reason: str | None = None) -> None:
        body: dict[str, Any] = {"user_id": user_id}
        if reason:
            body["reason"] = reason
        await self._request("POST", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/kick", as_user_id=as_user_id, json_body=body)

    async def get_joined_members(self, room_id: str, *, as_user_id: str) -> list[str]:
        result = await self._request(
            "GET", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/joined_members", as_user_id=as_user_id
        )
        return list(result.get("joined", {}).keys())

    async def send_message_event(
        self,
        room_id: str,
        content: dict[str, Any],
        *,
        as_user_id: str,
        ts: int | None = None,
        event_type: str = "m.room.message",
    ) -> str:
        """Send a message event. ``ts`` (Unix milliseconds) uses the
        AppService timestamp-massaging parameter, to show the event at
        that time rather than when the bridge processed it."""
        result = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/{quote(event_type, safe='')}/{uuid.uuid4().hex}",
            json_body=content,
            params={"ts": ts} if ts is not None else None,
            as_user_id=as_user_id,
        )
        return result["event_id"]

    async def send_state_event(
        self, room_id: str, event_type: str, content: dict[str, Any], *, as_user_id: str, state_key: str = ""
    ) -> str:
        result = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/{quote(event_type, safe='')}/{quote(state_key, safe='')}",
            json_body=content,
            as_user_id=as_user_id,
        )
        return result.get("event_id", "")

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = "", *, as_user_id: str
    ) -> dict[str, Any] | None:
        """The content of one current state event, or ``None`` if the room
        has no such state. Any other failure raises."""
        try:
            return await self._request(
                "GET",
                f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/{quote(event_type, safe='')}/{quote(state_key, safe='')}",
                as_user_id=as_user_id,
            )
        except HomeserverError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_full_state(self, room_id: str, *, as_user_id: str) -> list[dict[str, Any]]:
        """Every current state event in the room, with event IDs."""
        result = await self._request(
            "GET", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state", as_user_id=as_user_id
        )
        return [event for event in result if isinstance(event, dict)] if isinstance(result, list) else []

    async def get_event(self, room_id: str, event_id: str, *, as_user_id: str) -> dict[str, Any] | None:
        """A single event, or ``None`` if it doesn't exist (or isn't
        visible). Any other failure raises: callers rebuilding Nostr events
        from Matrix rely on "missing" and "couldn't ask" staying distinct."""
        try:
            return await self._request(
                "GET",
                f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/event/{quote(event_id, safe='')}",
                as_user_id=as_user_id,
            )
        except HomeserverError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_messages(self, room_id: str, *, as_user_id: str, limit: int = 100,
                           from_token: str | None = None) -> dict[str, Any]:
        """One page of the room's timeline, newest first: ``chunk`` and,
        while there's more, ``end`` (the ``from_token`` for the next page)."""
        params: dict[str, Any] = {"dir": "b", "limit": str(limit)}
        if from_token:
            params["from"] = from_token
        return await self._request("GET", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages",
                                   params=params, as_user_id=as_user_id)

    async def get_relations(self, room_id: str, event_id: str, rel_type: str, *, as_user_id: str, limit: int = 100,
                            from_token: str | None = None) -> dict[str, Any]:
        """One page of the events relating to ``event_id`` with ``rel_type``:
        ``chunk`` and, while there's more, ``next_batch``."""
        params: dict[str, Any] = {"limit": str(limit)}
        if from_token:
            params["from"] = from_token
        return await self._request(
            "GET", f"/_matrix/client/v1/rooms/{quote(room_id, safe='')}/relations/{quote(event_id, safe='')}/{quote(rel_type, safe='')}",
            params=params, as_user_id=as_user_id)

    async def redact_event(self, room_id: str, event_id: str, *, as_user_id: str, reason: str | None = None) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/redact/{quote(event_id, safe='')}/{uuid.uuid4().hex}",
            json_body={"reason": reason} if reason else {},
            as_user_id=as_user_id,
        )

    # -- media ---------------------------------------------------------------

    async def upload_media(self, data: bytes, content_type: str, filename: str, *, as_user_id: str) -> str:
        """Upload to the homeserver's media repository. Returns the ``mxc://`` URI."""
        response = await self._client.post(
            "/_matrix/media/v3/upload",
            headers={"Authorization": f"Bearer {self._as_token}", "Content-Type": content_type},
            params={"filename": filename, "user_id": as_user_id},
            content=data,
        )
        if response.status_code >= 400:
            raise _error_from(response)
        return response.json()["content_uri"]

    async def download_media(self, mxc: str) -> MediaDownload | None:
        """A media item via the authenticated media API (spec v1.11), or
        ``None`` if it doesn't exist. Any other failure raises."""
        server_name, _, media_id = mxc.removeprefix("mxc://").partition("/")
        if not mxc.startswith("mxc://") or not server_name or not media_id or "/" in media_id:
            return None
        response = await self._client.get(
            f"/_matrix/client/v1/media/download/{quote(server_name, safe=':')}/{quote(media_id, safe='')}",
            headers={"Authorization": f"Bearer {self._as_token}"},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise _error_from(response)
        return MediaDownload(
            content=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            content_disposition=response.headers.get("content-disposition"),
        )

    # -- widgets -------------------------------------------------------------

    async def verify_openid_token(self, access_token: str) -> str | None:
        """The ``@user:server`` a Matrix OpenID token (what a widget gets
        from its client) belongs to, or ``None`` if it isn't valid. Uses the
        spec's federation ``openid/userinfo`` endpoint on this homeserver."""
        try:
            response = await self._client.get(
                "/_matrix/federation/v1/openid/userinfo", params={"access_token": access_token}
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            sub = response.json().get("sub")
        except ValueError:
            return None
        return sub if isinstance(sub, str) else None
