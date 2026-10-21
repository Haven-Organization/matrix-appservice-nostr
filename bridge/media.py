"""Making Matrix media public for Nostr, without storing it anywhere else.

Nostr events carry plain URLs, so a local user's avatar or post image has
to be fetchable by anyone. The bridge serves such files at
``<relay http base>/<sha256>.<ext>``, the Blossom (BUD-01) URL shape, so
Blossom-aware clients can also find them by hash. The bytes stay in the
Matrix media repository: the ``blobs`` table only maps a hash to its
``mxc://`` URI (see ``bridge.media_routes``).

Images are cleaned before they're published. A photo's EXIF block often
holds GPS coordinates, and a Matrix room may be private where a Nostr
profile or post never is. So an image with metadata, in a format clients
can't be counted on to show, or too large, is re-encoded (rotation
applied, metadata dropped). The re-encoded file is uploaded back into
Matrix, so its bytes and hash are fixed from then on: running an encoder
again later couldn't promise the same bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import FastAPI
from PIL import Image, ImageOps

from bridge.homeserver_client import HomeserverError
from bridge.repository import Blob
from bridge.users import bot_mxid

logger = logging.getLogger(__name__)

# Profile pictures and banners: small enough for every client to load
# quickly, large enough to look sharp.
PROFILE_IMAGE_MAX_BYTES = 1_000_000

_PASSTHROUGH_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "application/pdf": "pdf",
}

# (longest side in pixels, JPEG quality), tried in order until the result
# fits. The first step keeps full resolution.
_REENCODE_STEPS = ((None, 90), (2000, 85), (2000, 75), (1600, 75), (1280, 70), (1024, 65), (800, 60), (600, 50))


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    # False: `data` is the original file, untouched.
    reencoded: bool


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)


def _prepare_image(data: bytes, max_bytes: int) -> PreparedImage | None:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:  # Pillow raises a wide assortment for bad input
        logger.info("Could not decode image", exc_info=True)
        return None
    mime_type = _PASSTHROUGH_FORMATS.get(image.format or "")
    has_metadata = bool(image.getexif()) or "exif" in image.info or "xmp" in image.info
    animated = getattr(image, "is_animated", False)
    if mime_type and len(data) <= max_bytes and (not has_metadata or animated):
        # Animated images keep their frames; Pillow can't re-encode every
        # animation faithfully, and GIF/WebP animations rarely carry GPS.
        return PreparedImage(data, mime_type, image.width, image.height, reencoded=False)

    image = ImageOps.exif_transpose(image)
    if _has_alpha(image):
        # Keep transparency (an avatar's round cut-out, a logo): PNG, if
        # that fits.
        rgba = image.convert("RGBA")
        buffer = io.BytesIO()
        rgba.save(buffer, format="PNG", optimize=True)
        if buffer.tell() <= max_bytes:
            return PreparedImage(buffer.getvalue(), "image/png", rgba.width, rgba.height, reencoded=True)
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    for longest_side, quality in _REENCODE_STEPS:
        candidate = image
        if longest_side is not None and max(image.size) > longest_side:
            candidate = image.copy()
            candidate.thumbnail((longest_side, longest_side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= max_bytes:
            return PreparedImage(buffer.getvalue(), "image/jpeg", candidate.width, candidate.height, reencoded=True)
    return None


async def prepare_image(data: bytes, *, max_bytes: int) -> PreparedImage | None:
    """``data`` cleaned and sized for publishing, or ``None`` if it isn't
    a decodable image or can't be made small enough."""
    return await asyncio.to_thread(_prepare_image, data, max_bytes)


def blob_url(base: str, sha256: str, mime_type: str) -> str:
    extension = EXTENSIONS.get(mime_type)
    return f"{base}/{sha256}.{extension}" if extension else f"{base}/{sha256}"


@dataclass(frozen=True)
class PublishedImage:
    """An image now served publicly: its URL, and what an ``imeta`` tag or
    profile needs to know about it."""

    url: str
    sha256: str
    mime_type: str
    size: int
    width: int
    height: int
    mxc: str


async def publish_matrix_image(app: FastAPI, mxc: str, *, max_bytes: int) -> PublishedImage | None:
    """Make the Matrix image at ``mxc`` publicly fetchable. Returns ``None``
    if it can't be downloaded or isn't a usable image."""
    homeserver = app.state.homeserver
    try:
        download = await homeserver.download_media(mxc)
    except HomeserverError:
        logger.warning("Could not download %s", mxc, exc_info=True)
        return None
    if download is None:
        return None
    image = await prepare_image(download.content, max_bytes=max_bytes)
    if image is None:
        return None
    stored_mxc = mxc
    if image.reencoded:
        extension = EXTENSIONS.get(image.mime_type, "bin")
        try:
            stored_mxc = await homeserver.upload_media(
                image.data, image.mime_type, f"image.{extension}", as_user_id=bot_mxid(app.state.config)
            )
        except HomeserverError:
            logger.warning("Could not store the cleaned copy of %s", mxc, exc_info=True)
            return None
    sha256 = hashlib.sha256(image.data).hexdigest()
    await app.state.repository.put_blob(
        Blob(sha256, stored_mxc, image.mime_type, len(image.data), datetime.now(timezone.utc).isoformat())
    )
    return PublishedImage(
        url=blob_url(app.state.config.nostr.relay_http_base, sha256, image.mime_type),
        sha256=sha256,
        mime_type=image.mime_type,
        size=len(image.data),
        width=image.width,
        height=image.height,
        mxc=stored_mxc,
    )
