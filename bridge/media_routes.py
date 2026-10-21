"""Serving published media: ``GET``/``HEAD /<sha256>[.ext]`` (Blossom BUD-01).

Only files listed in the ``blobs`` table are reachable: Matrix media needs
an access token to download, and the bridge must not become an open proxy
to everything in the media repository. The bytes come from the homeserver
on each request; nothing is stored here.

The hash is the file's identity, so responses can be cached forever.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request, Response

from bridge.homeserver_client import HomeserverError

logger = logging.getLogger(__name__)

router = APIRouter()

BLOB_PATH_RE = re.compile(r"^/([0-9a-f]{64})(\.[a-z0-9]{1,10})?$")

_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    # A file opened directly must not be able to run script on this origin.
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


def _not_found() -> Response:
    return Response(status_code=404, content="Not found", media_type="text/plain")


async def _serve(request: Request, blob_name: str, *, head: bool) -> Response:
    match = BLOB_PATH_RE.match("/" + blob_name)
    if match is None:
        return _not_found()
    blob = await request.app.state.repository.get_blob(match.group(1))
    if blob is None:
        return _not_found()
    headers = {**_HEADERS, "Content-Length": str(blob.size)}
    if head:
        return Response(status_code=200, headers=headers, media_type=blob.mime_type)
    try:
        download = await request.app.state.homeserver.download_media(blob.mxc)
    except HomeserverError:
        logger.warning("Could not fetch %s for %s", blob.mxc, blob.sha256, exc_info=True)
        return Response(status_code=502, content="Upstream error", media_type="text/plain")
    if download is None:
        return _not_found()
    headers["Content-Length"] = str(len(download.content))
    return Response(content=download.content, headers=headers, media_type=blob.mime_type)


@router.get("/{blob_name}")
async def get_blob(request: Request, blob_name: str) -> Response:
    return await _serve(request, blob_name, head=False)


@router.head("/{blob_name}")
async def head_blob(request: Request, blob_name: str) -> Response:
    return await _serve(request, blob_name, head=True)


# -- Blossom uploads (BUD-02) --------------------------------------------------------
#
# A local ID's own Nostr apps may upload media through the bridge (its kind
# 10063 names the bridge as its media server). The upload is authorized by a
# kind 24242 event signed by a local ID's key, with "t" = "upload", a future
# "expiration", and (if given) "x" tags naming the file's hash. The bytes go
# into the Matrix media repository, like everything else the bridge serves.

UPLOAD_AUTH_KIND = 24242


@router.put("/upload")
async def upload(request: Request) -> Response:
    import hashlib
    import json as _json
    import time as _time
    from datetime import datetime, timezone

    from bridge.media import blob_url
    from bridge.nostr.http_auth import AuthError, parse
    from bridge.repository import Blob
    from bridge.users import bot_mxid

    app = request.app

    def refuse(status: int, reason: str) -> Response:
        return Response(status_code=status, headers={"X-Reason": reason}, content=reason, media_type="text/plain")

    try:
        auth = parse(request.headers.get("authorization"), kind=UPLOAD_AUTH_KIND, max_age=0)
    except AuthError as exc:
        return refuse(401, str(exc))
    if auth.first_tag_value("t") != "upload":
        return refuse(401, "authorization is not for an upload")
    expiration = auth.first_tag_value("expiration")
    if not expiration or not expiration.isdigit() or int(expiration) < _time.time():
        return refuse(401, "authorization has expired")
    if await app.state.repository.get_id_by_pubkey(auth.pubkey) is None:
        return refuse(403, "only this server's users can upload here")
    limit = app.state.config.nostr.max_media_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return refuse(413, "file too large")
    data = await request.body()
    if len(data) > limit:
        return refuse(413, "file too large")
    sha256 = hashlib.sha256(data).hexdigest()
    allowed = auth.tag_values("x")
    if allowed and sha256 not in allowed:
        return refuse(403, "the file's hash isn't one the authorization names")
    mime = (request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip().lower()
    existing = await app.state.repository.get_blob(sha256)
    if existing is None:
        try:
            mxc = await app.state.homeserver.upload_media(data, mime, "upload", as_user_id=bot_mxid(app.state.config))
        except HomeserverError:
            return refuse(502, "could not store the file")
        uploaded = int(_time.time())
        await app.state.repository.put_blob(Blob(sha256, mxc, mime, len(data), datetime.now(timezone.utc).isoformat()))
    else:
        mime = existing.mime_type
        uploaded = int(datetime.fromisoformat(existing.created_at).timestamp())
    descriptor = {
        "url": blob_url(app.state.config.nostr.relay_http_base, sha256, mime),
        "sha256": sha256,
        "size": len(data),
        "type": mime,
        "uploaded": uploaded,
    }
    return Response(content=_json.dumps(descriptor), media_type="application/json")
