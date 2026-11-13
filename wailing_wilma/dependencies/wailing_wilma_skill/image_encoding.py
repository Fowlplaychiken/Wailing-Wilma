"""Optional higher-detail image encoding for Wailing Wilma."""

from __future__ import annotations

import base64
import io
import math
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

# Haiku 5.5's documented high-resolution tier: 4,784 patches of 28x28 pixels.
# These are an optional profile bound, not universal model limits.
HIGH_DETAIL_MAX_LONG_EDGE = 2576
HIGH_DETAIL_MAX_PIXELS = 4_784 * 28 * 28
HIGH_DETAIL_JPEG_QUALITY = 90


def _target_size(width: int, height: int) -> tuple[int, int]:
    scale = min(
        1.0,
        HIGH_DETAIL_MAX_LONG_EDGE / max(width, height),
        math.sqrt(HIGH_DETAIL_MAX_PIXELS / (width * height)),
    )
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def encode_capture(
    image: Image.Image,
    detail: str,
    stock_encoder: Callable[[Image.Image], str],
) -> str:
    """Encode one captured PIL image using stock or bounded high-detail settings.

    The stock profile delegates the original object and encoder unchanged. The
    high profile owns every copy and stream it creates and never mutates or closes
    the captured image.
    """
    if detail == "stock":
        return stock_encoder(image)
    if detail != "high":
        raise ValueError("unknown Wilma image detail profile")

    from PIL import Image

    owned: list[Image.Image] = []
    stream = io.BytesIO()
    try:
        working = image.copy()
        owned.append(working)
        if working.mode != "RGB":
            working = working.convert("RGB")
            owned.append(working)
        target = _target_size(*working.size)
        if target != working.size:
            working = working.resize(target, Image.Resampling.LANCZOS)
            owned.append(working)
        working.save(stream, format="JPEG", quality=HIGH_DETAIL_JPEG_QUALITY)
        return base64.b64encode(stream.getvalue()).decode("ascii")
    finally:
        stream.close()
        for owned_image in owned:
            owned_image.close()
