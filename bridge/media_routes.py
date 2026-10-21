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
