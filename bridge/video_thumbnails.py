"""Poster-frame thumbnails for bridged videos.

A Nostr video is just a URL (with an ``imeta`` tag, at best), so a copy of
it in Matrix has nothing for a client to show before playback unless the
bridge adds a thumbnail, and a client left to grab a frame itself can't
always decode the video (confirmed live 2026-11-26: an H.264 High 4:4:4 MKV
that no Android decoder supports showed as a blank box in Haven Android).
The ``imeta`` ``image`` preview is used when the post has one
(``bridge.render``); otherwise the bridge decodes a frame with ffmpeg --
which handles any codec -- and uploads it as the event's ``thumbnail_url``,
with ``thumbnail_info``, the video's ``size`` and its ``duration``.

Results are cached per video ``mxc://`` URI, so a video already in Matrix
(this bridge's own upload, a copy made before) gets the same thumbnail
without being decoded again. Every failure (no ffmpeg in the image, an undecodable file, a
timeout) just leaves the event without a thumbnail, exactly as before.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
from collections import OrderedDict

from PIL import Image, UnidentifiedImageError

from bridge.homeserver_client import HomeserverClient, HomeserverError

logger = logging.getLogger(__name__)

# Above this the video isn't decoded just for a thumbnail (it is already in
# memory for the upload, but writing a huge file out again for ffmpeg isn't
# worth it).
MAX_SOURCE_BYTES = 200 * 1024 * 1024
# Longest side of the poster frame, like Element's own thumbnails.
THUMBNAIL_MAX_SIDE = 800
_FFMPEG_TIMEOUT_SECONDS = 60
_CACHE_SIZE = 512

_cache: OrderedDict[str, dict] = OrderedDict()
_ffmpeg_missing = False


def cached_video_info(mxc_uri: str) -> dict | None:
    """The thumbnail/size/duration ``info`` fields already worked out for
    ``mxc_uri``, or None if it hasn't been processed (in this process)."""
    fields = _cache.get(mxc_uri)
    if fields is not None:
        _cache.move_to_end(mxc_uri)
    return fields


def remember_video_info(mxc_uri: str, fields: dict) -> None:
    _cache[mxc_uri] = fields
    _cache.move_to_end(mxc_uri)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)


async def video_info_fields(
    homeserver: HomeserverClient, mxc_uri: str, data: bytes | None = None, *, as_user_id: str
) -> dict:
    """``info`` fields to add to an ``m.video`` event for ``mxc_uri``:
    ``thumbnail_url``, ``thumbnail_info`` (w/h/mimetype/size), ``size`` and
    ``duration`` (ms) -- whichever could be worked out, possibly none.

    ``data`` is the video's bytes if the caller already has them (a fresh
    upload); otherwise they're downloaded back from the homeserver, unless
    this video was already processed. The thumbnail is uploaded as
    ``as_user_id``."""
    cached = cached_video_info(mxc_uri)
    if cached is not None:
        return dict(cached)
    if data is None:
        try:
            download = await homeserver.download_media(mxc_uri)
        except HomeserverError:
            # Not cached: a later attempt may well succeed.
            logger.warning("Could not fetch %s to make a video thumbnail", mxc_uri, exc_info=True)
            return {}
        if download is None:
            return {}
        data = download.content
    fields: dict = {"size": len(data)}
    if len(data) <= MAX_SOURCE_BYTES:
        fields.update(await _thumbnail_fields(homeserver, mxc_uri, data, as_user_id))
    remember_video_info(mxc_uri, fields)
    return dict(fields)


async def _thumbnail_fields(homeserver: HomeserverClient, mxc_uri: str, data: bytes, as_user_id: str) -> dict:
    global _ffmpeg_missing
    if _ffmpeg_missing:
        return {}
    fields: dict = {}
    with tempfile.TemporaryDirectory(prefix="video-thumb-") as tmp:
        path = os.path.join(tmp, "video")
        try:
            await asyncio.to_thread(_write, path, data)
            duration = await _probe_duration(path)
            # A little way in rather than the very first frame, which is often
            # black (a fade-in), but never past the middle of a short clip.
            offset = min(1.0, duration / 2) if duration else 0.0
            jpeg = await _grab_frame(path, offset)
            if not jpeg and offset:
                jpeg = await _grab_frame(path, 0.0)
        except FileNotFoundError:
            _ffmpeg_missing = True
            logger.warning("ffmpeg isn't installed: bridged videos won't get thumbnails")
            return {}
        except (TimeoutError, OSError):
            logger.warning("Making a thumbnail for %s failed", mxc_uri, exc_info=True)
            return {}
    if duration:
        fields["duration"] = int(duration * 1000)
    if not jpeg:
        logger.info("No frame could be decoded from %s for a thumbnail", mxc_uri)
        return fields
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError):
        return fields
    try:
        thumbnail_mxc = await homeserver.upload_media(jpeg, "image/jpeg", "thumbnail.jpg", as_user_id=as_user_id)
    except HomeserverError:
        logger.warning("Uploading a thumbnail for %s failed", mxc_uri, exc_info=True)
        return fields
    fields["thumbnail_url"] = thumbnail_mxc
    fields["thumbnail_info"] = {"w": width, "h": height, "mimetype": "image/jpeg", "size": len(jpeg)}
    return fields


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def _run(*args: str) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), _FFMPEG_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, stdout


async def _probe_duration(path: str) -> float | None:
    code, out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path)
    if code != 0:
        return None
    try:
        duration = float(json.loads(out)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return None
    return duration if duration > 0 else None


async def _grab_frame(path: str, offset: float) -> bytes:
    side = THUMBNAIL_MAX_SIDE
    code, out = await _run(
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{offset:.3f}", "-i", path,
        "-frames:v", "1", "-an",
        "-vf", f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease",
        "-pix_fmt", "yuvj420p", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
    )
    return out if code == 0 else b""
