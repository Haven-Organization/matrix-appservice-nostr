"""A video copied into Matrix gets a thumbnail: the post's own preview
image when it has one, else a frame decoded with ffmpeg."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from bridge import video_thumbnails
from bridge.render import Attachment, attachments_of, copy_attachment
from bridge.safe_fetch import Fetched
from tests.test_federation import run

GHOST = "@nostr_ghost:example.org"
VIDEO_URL = "https://media.example/clip.mp4"
PREVIEW_URL = "https://media.example/clip.jpg"

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")


@pytest.fixture(autouse=True)
def _fresh_cache():
    # The cache is per process, keyed by mxc://, and the fake homeserver
    # numbers its uploads from 1 again in every test.
    video_thumbnails._cache.clear()
    yield
    video_thumbnails._cache.clear()


def _video(tmp_path) -> bytes:
    path = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-pix_fmt", "yuv444p", str(path)],
        check=True,
    )
    return path.read_bytes()


class Fetcher:
    def __init__(self, files: dict[str, tuple[bytes, str]]) -> None:
        self.files = files
        self.asked: list[str] = []

    async def get(self, url, **kwargs):
        self.asked.append(url)
        data, content_type = self.files.get(url, (b"", "application/octet-stream"))
        return Fetched(url, 200 if data else 404, data, content_type)

    async def aclose(self):
        pass


@needs_ffmpeg
def test_a_frame_of_the_video(client, tmp_path) -> None:
    client.app.state.fetcher = Fetcher({VIDEO_URL: (_video(tmp_path), "video/mp4")})
    copied = run(client, copy_attachment, client.app, Attachment(VIDEO_URL, "video/mp4"), as_user_id=GHOST)
    info = copied["info"]
    assert info["duration"] == 2000
    assert info["size"] > 0
    assert info["thumbnail_info"]["mimetype"] == "image/jpeg"
    assert (info["thumbnail_info"]["w"], info["thumbnail_info"]["h"]) == (320, 240)
    thumbnail, content_type = client.app.state.homeserver.media[info["thumbnail_url"]]
    assert content_type == "image/jpeg" and thumbnail.startswith(b"\xff\xd8")

    # Already in Matrix the second time: the same thumbnail, not decoded again.
    uploads = len(client.app.state.homeserver.media)
    again = run(client, copy_attachment, client.app, Attachment(VIDEO_URL, "video/mp4"), as_user_id=GHOST)
    assert again["info"]["thumbnail_url"] == info["thumbnail_url"]
    assert len(client.app.state.homeserver.media) == uploads


def test_the_posts_own_preview_image(client) -> None:
    client.app.state.fetcher = Fetcher({
        VIDEO_URL: (b"not decoded", "video/mp4"),
        PREVIEW_URL: (b"\xff\xd8 a preview", "image/jpeg"),
    })
    raw = {"kind": 1, "content": VIDEO_URL, "tags": [["imeta", f"url {VIDEO_URL}", "m video/mp4", f"image {PREVIEW_URL}"]]}
    (attachment,) = attachments_of(raw)
    assert attachment.thumb == PREVIEW_URL
    copied = run(client, copy_attachment, client.app, attachment, as_user_id=GHOST)
    info = copied["info"]
    assert info["thumbnail_info"]["mimetype"] == "image/jpeg"
    assert client.app.state.homeserver.media[info["thumbnail_url"]][0] == b"\xff\xd8 a preview"


def test_an_undecodable_video_has_no_thumbnail(client) -> None:
    client.app.state.fetcher = Fetcher({VIDEO_URL: (b"not a video at all", "video/mp4")})
    copied = run(client, copy_attachment, client.app, Attachment(VIDEO_URL, "video/mp4"), as_user_id=GHOST)
    assert "thumbnail_url" not in copied["info"]
    assert copied["info"]["size"] == len(b"not a video at all")


def test_images_are_left_alone(client) -> None:
    client.app.state.fetcher = Fetcher({PREVIEW_URL: (b"\xff\xd8 a picture", "image/jpeg")})
    copied = run(client, copy_attachment, client.app, Attachment(PREVIEW_URL, "image/jpeg"), as_user_id=GHOST)
    assert set(copied["info"]) == {"mimetype", "size"}
