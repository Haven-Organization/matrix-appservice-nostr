"""Images made ready for publishing (bridge.media.prepare_image)."""

from __future__ import annotations

import asyncio
import io
import os

from PIL import Image

from bridge.media import prepare_image


def _noisy_gif(frames: int, side: int) -> bytes:
    images = [Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)) for _ in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:], duration=80, loop=0)
    return buffer.getvalue()


def test_a_small_animation_passes_through_untouched() -> None:
    data = _noisy_gif(3, 64)
    image = asyncio.run(prepare_image(data, max_bytes=len(data) + 1))
    assert image is not None and image.data == data and not image.reencoded


def test_an_animation_too_big_is_shrunk_not_flattened() -> None:
    data = _noisy_gif(8, 400)
    limit = len(data) // 3
    image = asyncio.run(prepare_image(data, max_bytes=limit))
    assert image is not None and image.mime_type == "image/gif" and len(image.data) <= limit
    shrunk = Image.open(io.BytesIO(image.data))
    assert shrunk.n_frames == 8 and max(shrunk.size) < 400
