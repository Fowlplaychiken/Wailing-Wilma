"""Read the player's English incapacitation heading and its adjacent HUD timer."""

from __future__ import annotations

import hashlib
import re

from adapters.perception.native_regions import NativeTextRegion
from adapters.perception.tesseract import TesseractRecognizer
from PIL import Image, ImageChops, ImageFilter, ImageOps

_HEADING = "you are incapacitated"
_TIMER = re.compile(r"time\s+until\s+death\s*:\s*\d{1,2}:\d{2}(?::\d{2})?", re.I)
_TIMER_LABEL = re.compile(r"time\s+until\s+death\s*:?", re.I)
_BEACON = re.compile(r"\brescue beacon\b", re.I)


def _has_personal_banner(lines: tuple[NativeTextRegion, ...]) -> bool:
    for line in lines:
        if " ".join(line.text.split()).casefold() != _HEADING:
            continue
        left, top, _, bottom = line.bounds
        height = max(1, bottom - top)
        below = [r for r in lines if abs(r.bounds[0] - left) <= 2 * height
                 and bottom <= r.bounds[1] <= bottom + 6 * height]
        for row in below:
            if row.bounds[1] > bottom + 3 * height:
                continue
            if _TIMER.fullmatch(row.text.strip()):
                return True
            # Sparse OCR may split the countdown into another text region. In
            # that case require the game's second aligned HUD instruction too.
            if _TIMER_LABEL.fullmatch(row.text.strip()) and any(
                r.bounds[1] >= row.bounds[3] and _BEACON.search(r.text) for r in below
            ):
                return True
    return False


def _bright_text(gray: Image.Image, frame_height: int) -> Image.Image:
    """Keep bright HUD strokes that stand out from their local background."""
    with (
        gray.filter(ImageFilter.GaussianBlur(max(2.0, frame_height / 240))) as background,
        ImageChops.subtract(gray, background) as contrast,
        gray.point(lambda value: 255 if value >= 140 else 0) as bright,
        contrast.point(lambda value: 255 if value >= 12 else 0) as distinct,
        ImageChops.multiply(bright, distinct) as foreground,
    ):
        return ImageOps.invert(foreground)


async def read_personal_banner(
    image: Image.Image, recognizer: TesseractRecognizer, *, bright_text: bool = False,
) -> bool:
    width, height = image.size
    box = (width // 20, 3 * height // 5, 3 * width // 5, 49 * height // 50)
    if box[0] >= box[2] or box[1] >= box[3]:
        return False
    with image.crop(box) as crop, crop.convert("L") as gray:
        with (_bright_text(gray, height) if bright_text else ImageOps.invert(gray)) as text:
            pgm = f"P5\n{text.width} {text.height}\n255\n".encode("ascii") + text.tobytes()
    regions = await recognizer.propose_regions(
        pgm, source_sha256=hashlib.sha256(pgm).hexdigest(), max_rows=2048, max_regions=64,
    )
    return _has_personal_banner(regions)
