"""Media already in the Matrix media repository is reused, not downloaded
and uploaded again (as matrix-appservice-activitypub does)."""

from __future__ import annotations

import hashlib

from bridge.render import Attachment, copy_attachment
from bridge.repository import Blob
from bridge.safe_fetch import Fetched
from tests.test_federation import run

GHOST = "@nostr_ghost:example.org"


class Fetcher:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    async def get(self, url, **kwargs):
        self.asked.append(url)
        return Fetched(url, 200, self.files.get(url, b"some bytes"), "image/png")

    async def aclose(self):
        pass


def _setup(client, files=None):
    fetcher = Fetcher(files or {})
    client.app.state.fetcher = fetcher
    return fetcher


def _copy(client, url, **fields):
    return run(client, copy_attachment, client.app, Attachment(url, "image/png", **fields), as_user_id=GHOST)


def test_our_own_published_files(client) -> None:
    fetcher = _setup(client)
    digest = "ab" * 32
    run(client, client.app.state.repository.put_blob, Blob(digest, "mxc://example.org/mine", "image/png", 5, "2026-01-01"))
    base = client.app.state.config.nostr.relay_http_base
    assert _copy(client, f"{base}/{digest}.png")["url"] == "mxc://example.org/mine"
    # Mirrored on another Blossom server, named by its hash.
    assert _copy(client, f"https://blossom.example/{digest}.png", sha256=digest)["url"] == "mxc://example.org/mine"
    assert fetcher.asked == []


def test_the_activitypub_bridges_media_links(client) -> None:
    fetcher = _setup(client)
    assert _copy(client, "https://example.org/media/example.org/AbCdEf")["url"] == "mxc://example.org/AbCdEf"
    assert fetcher.asked == []
    # Another domain's, or another server's media: downloaded.
    _copy(client, "https://elsewhere.example/media/example.org/AbCdEf")
    _copy(client, "https://example.org/media/matrix.org/AbCdEf")
    assert len(fetcher.asked) == 2


def test_copied_once(client) -> None:
    same = b"the same picture"
    fetcher = _setup(client, {"https://a.example/1.png": same, "https://b.example/2.png": same})
    first = _copy(client, "https://a.example/1.png")
    assert _copy(client, "https://a.example/1.png")["url"] == first["url"]
    assert fetcher.asked == ["https://a.example/1.png"]
    # The same bytes elsewhere: fetched (to know), but not uploaded again.
    assert _copy(client, "https://b.example/2.png")["url"] == first["url"]
    # And by hash, from anywhere, without fetching.
    digest = hashlib.sha256(same).hexdigest()
    assert _copy(client, "https://c.example/3.png", sha256=digest)["url"] == first["url"]
    assert fetcher.asked == ["https://a.example/1.png", "https://b.example/2.png"]
