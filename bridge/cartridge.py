"""A game's card picture: a handheld game cartridge in one of several
colours, with the game's image as its label, as Ditto shows webxdc games.
Drawn at twice the size and scaled down, for smooth edges."""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 480, 560
_SCALE = 2

# Body colours, picked by the game's id so a game always looks the same.
PALETTE = ("#1ba5c4", "#de6a0e", "#8e2fe0", "#d93b3b", "#2fa84f", "#2f6fdb", "#e0489b", "#e3b21f", "#7d8592")


def colour_for(game_id: str) -> str:
    return PALETTE[int(hashlib.sha256(game_id.encode()).hexdigest(), 16) % len(PALETTE)]


def _shade(hex_colour: str, factor: float) -> tuple[int, int, int, int]:
    """Darker below 1, lighter above."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    if factor < 1:
        r, g, b = (int(c * factor) for c in (r, g, b))
    else:
        r, g, b = (int(c + (255 - c) * (factor - 1)) for c in (r, g, b))
    return r, g, b, 255


def render(game_id: str, label: bytes | None = None, title: str = "") -> bytes:
    """The cartridge as a PNG. ``label``: the game's image (any format
    Pillow reads); without one, the title is written on the label."""
    s = _SCALE
    colour = colour_for(game_id)
    body, dark, darker, light = _shade(colour, 1.0), _shade(colour, 0.72), _shade(colour, 0.5), _shade(colour, 1.28)
    image = Image.new("RGBA", (WIDTH * s, HEIGHT * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(x0, y0, x1, y1):
        return [x0 * s, y0 * s, x1 * s, y1 * s]

    # Shadow, body, and its lit and shaded edges.
    draw.rounded_rectangle(box(26, 18, 466, 552), 16 * s, fill=(0, 0, 0, 70))
    draw.rounded_rectangle(box(20, 10, 460, 544), 16 * s, fill=darker)
    draw.rounded_rectangle(box(20, 10, 456, 538), 16 * s, fill=body)
    draw.rounded_rectangle(box(20, 10, 452, 532), 16 * s, outline=light, width=3 * s)
    # Grips down both sides, near the top.
    for y in range(34, 128, 10):
        draw.line(box(22, y, 46, y), fill=dark, width=4 * s)
        draw.line(box(432, y, 456, y), fill=dark, width=4 * s)
    # The slot-shaped recess at the top.
    draw.rounded_rectangle(box(112, 22, 368, 78), 28 * s, fill=dark, outline=darker, width=3 * s)
    draw.rounded_rectangle(box(118, 28, 362, 72), 22 * s, fill=body)
    # The label's frame.
    draw.rounded_rectangle(box(62, 100, 418, 438), 10 * s, fill=dark, outline=darker, width=5 * s)
    draw.rounded_rectangle(box(74, 112, 406, 426), 6 * s, outline=darker, width=2 * s)
    # The label.
    lx0, ly0, lx1, ly1 = 112, 126, 368, 382
    draw.rounded_rectangle(box(lx0, ly0, lx1, ly1), 20 * s, fill=(203, 212, 222, 255))
    picture = _label(label, (lx1 - lx0 - 24) * s, (ly1 - ly0 - 24) * s)
    if picture is not None:
        x = (lx0 + lx1) * s // 2 - picture.width // 2
        y = (ly0 + ly1) * s // 2 - picture.height // 2
        mask = Image.new("L", picture.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, picture.width - 1, picture.height - 1], 12 * s, fill=255)
        image.paste(picture, (x, y), mask)
    else:
        _write(draw, title or "🎮", box(lx0 + 16, ly0 + 16, lx1 - 16, ly1 - 16))
    # The arrow, and the edge that goes into the console.
    draw.polygon([(206 * s, 452 * s), (274 * s, 452 * s), (240 * s, 488 * s)], fill=dark, outline=darker)
    draw.rounded_rectangle(box(44, 512, 436, 534), 4 * s, fill=darker)
    for x in range(56, 428, 12):
        draw.line(box(x, 518, x, 530), fill=dark, width=3 * s)
    out = io.BytesIO()
    image.resize((WIDTH, HEIGHT), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


def _label(data: bytes | None, width: int, height: int) -> Image.Image | None:
    if not data:
        return None
    try:
        picture = Image.open(io.BytesIO(data))
        picture.load()
    except Exception:
        return None
    picture = picture.convert("RGBA")
    ratio = min(width / picture.width, height / picture.height)
    resample = Image.NEAREST if ratio >= 3 else Image.LANCZOS  # pixel art stays crisp
    return picture.resize((max(1, int(picture.width * ratio)), max(1, int(picture.height * ratio))), resample)


def _write(draw: ImageDraw.ImageDraw, text: str, area: list[int]) -> None:
    x0, y0, x1, y1 = area
    size = 64
    while size > 16:
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            font = ImageFont.load_default()
            break
        if draw.textbbox((0, 0), text, font=font)[2] <= x1 - x0:
            break
        size -= 6
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((x0 + x1 - (right - left)) / 2, (y0 + y1 - (bottom - top)) / 2), text, font=font, fill=(60, 66, 76, 255))
