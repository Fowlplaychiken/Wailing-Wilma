"""Read visible reactor and component failure banners on Star Citizen engineering terminals.

The detector scans the whole supplied RGB frame for a wide, saturated red
text row, then derives a crop from those pixels. It never accepts crop
coordinates from a caller. OCR is delegated to the existing admitted finite
Tesseract recognizer; this module does not enable or authorize any action.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from adapters.perception.image_bounds import (
    MAX_CAPTURE_PIXELS,
    MAX_ROI_PIXELS,
    MAX_SUPPORTED_CAPTURE_PIXELS,
)
from adapters.perception.native_regions import NativeTextRegion
from adapters.perception.tesseract import (
    TesseractResult,
    TesseractUnavailable,
    TsvWord,
    Whitelist,
)
from integrations.wingman_wilma.policy import _HARDWARE_GAUGE, _failure_strip_subject
from PIL import Image, ImageChops, ImageOps

_REACTOR_LINE = re.compile(
    r"\bREACTOR\s+CRITICAL\s*:?\s+EXPLOSION\s+IMMINENT\s+([0-9]{1,3})\s+SECONDS\b",
    re.IGNORECASE,
)
_REACTOR_BANNER = re.compile(
    r"\bREACTOR\s+CRITICAL\s*:?\s+EXPLOSION\s+IMMINENT\b", re.IGNORECASE,
)
_COMPONENT_MENTION = re.compile(
    rf"(?<![A-Z0-9]){_HARDWARE_GAUGE.pattern}\s+FAILURE(?![A-Z0-9])",
    _HARDWARE_GAUGE.flags,
)
_STATUS_LABELS = ("ARMOR", "HULL", "COOLING SYSTEM", "LIFE SUPPORT", "HYDROGEN FUEL")
_FILTER_LABELS = ("SYSTEMS", "ROOMS", "CONNECTIONS")


@dataclass(frozen=True, slots=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True, slots=True)
class ReactorReading:
    text: str | None
    seconds: int | None
    box: Box | None
    elapsed_ms: float
    reason: str | None = None
    console_identified: bool = False
    console_cues: tuple[str, ...] = ()


class Recognizer(Protocol):
    async def recognize(self, pgm: bytes, whitelist: Whitelist) -> TesseractResult: ...

    async def propose_regions(
        self, pgm: bytes, *, source_sha256: str, max_rows: int, max_regions: int,
    ) -> tuple[NativeTextRegion, ...]: ...


def locate_reactor_text(rgb: bytes, width: int, height: int) -> Box | None:
    """Find a wide saturated-red text band from the entire RGB frame."""
    if (type(rgb) is not bytes or not (1 <= width <= 8192 and 1 <= height <= 8192)
            or width * height > MAX_SUPPORTED_CAPTURE_PIXELS or len(rgb) != width * height * 3):
        raise ValueError("invalid_rgb_frame")

    image = Image.frombytes("RGB", (width, height), rgb)
    source_width, source_height = image.size
    if width * height > MAX_CAPTURE_PIXELS:
        width, height = _size_within_budget(width, height, MAX_CAPTURE_PIXELS)
        image = image.resize((width, height), Image.Resampling.BOX)
    mask = _red_pixel_mask(image)

    # Horizontal and vertical projections keep the full-frame scan in Pillow's
    # native image loops; Python inspects only one score per image row/column.
    minimum_span = max(80, int(width * 0.12))
    x_gap = max(12, min(64, width // 40))
    row_projection = mask.resize((1, height), Image.Resampling.BOX)
    row_scores = list(row_projection.tobytes())
    row_bands: list[tuple[int, int]] = []
    band_start: int | None = None
    for y, score in enumerate(row_scores + [0]):
        if score >= 4 and band_start is None:
            band_start = y
        elif score < 4 and band_start is not None:
            if y - band_start >= 3:
                row_bands.append((band_start, y))
            band_start = None

    candidates: list[tuple[float, int, int, int, int, int, int]] = []
    for top, bottom in row_bands:
        band_height = bottom - top
        if not 6 <= band_height <= max(60, height // 12):
            continue
        x_projection = mask.crop((0, top, width, bottom)).resize(
            (width, 1), Image.Resampling.BOX)
        x_scores = list(x_projection.tobytes())
        active_x = [x for x, score in enumerate(x_scores) if score >= 12]
        regions: list[tuple[int, int, float]] = []
        if active_x:
            region_start = previous = active_x[0]
            for x in active_x[1:] + [width + x_gap + 1]:
                if x - previous > x_gap:
                    right = previous + 1
                    if right - region_start >= minimum_span:
                        fill = sum(x_scores[region_start:right]) / ((right - region_start) * 255)
                        regions.append((region_start, right, fill))
                    region_start = x
                previous = x
        for left, right, fill in regions:
            # A long solid HUD or player bar can outscore a text line. Glyphs
            # vary in column height even when the text is on a single row.
            if right - left > 30 * band_height:
                uniform_columns = max(Counter(x_scores[left:right]).values())
                if uniform_columns >= 0.90 * (right - left):
                    continue
            line_projection = mask.crop((left, top, right, bottom)).resize(
                (1, band_height), Image.Resampling.BOX
            )
            active_y = [y for y, score in enumerate(line_projection.tobytes()) if score >= 8]
            if len(active_y) < 4:
                continue
            text_top, text_bottom = top + active_y[0], top + active_y[-1] + 1
            # Prefer wide, dense lettering over the thinner detached hazard
            # stripes. Screen position contributes nothing to selection.
            candidates.append(
                ((right - left) * fill, top, bottom, left, right, text_top, text_bottom)
            )
    if not candidates:
        return None
    _, _band_top, _band_bottom, left, right, top, bottom = max(
        candidates, key=lambda row: row[0]
    )
    pad_x = max(4, (bottom - top) // 3)
    pad_y = max(3, (bottom - top) // 3)
    return Box(max(0, (left - pad_x) * source_width // width),
               max(0, (top - pad_y) * source_height // height),
               min(source_width, math.ceil((right + pad_x) * source_width / width)),
               min(source_height, math.ceil((bottom + pad_y) * source_height / height)))


def frame_to_pgm(rgb: bytes, width: int, height: int, box: Box, *, upscale: int = 3) -> bytes:
    """Encode an automatically located RGB box as a bounded grayscale PGM."""
    if (type(rgb) is not bytes or not (1 <= width <= 8192 and 1 <= height <= 8192)
            or width * height > MAX_SUPPORTED_CAPTURE_PIXELS or len(rgb) != width * height * 3
            or not isinstance(box, Box) or not (0 <= box.left < box.right <= width)
            or not (0 <= box.top < box.bottom <= height) or not 1 <= upscale <= 4):
        raise ValueError("invalid_roi")
    scale = upscale
    while scale > 1 and box.width * box.height * scale * scale > MAX_ROI_PIXELS:
        scale -= 1
    image = Image.frombytes("RGB", (width, height), rgb)
    cropped = _red_pixel_mask(image.crop((box.left, box.top, box.right, box.bottom)))
    if cropped.width * cropped.height > MAX_ROI_PIXELS:
        cropped = cropped.resize(_size_within_budget(*cropped.size, MAX_ROI_PIXELS),
                                 Image.Resampling.BOX)
    elif scale != 1:
        cropped = cropped.resize(
            (cropped.width * scale, cropped.height * scale), Image.Resampling.NEAREST
        )
    return f"P5\n{cropped.width} {cropped.height}\n255\n".encode("ascii") + cropped.tobytes()


def _striped_banner_legend(mask: Image.Image) -> Box | None:
    """Keep the complete text between two wide, visibly striped decorations."""
    width, height = mask.size
    projection = mask.resize((width, 1), Image.Resampling.BOX).point(_threshold_lut(15))
    runs = [m.span() for m in re.finditer(b"\xff+", projection.tobytes())]
    if len(runs) < 5:
        return None
    left, right = runs[0], runs[-1]
    # Diagonal stripes are solid in vertical projection, but broken on each
    # horizontal scanline. Require that distinction on both sides of the text.
    for start, end in (left, right):
        if end - start < 2 * height:
            return None
        if not any(mask.crop((start, y, end, y + 1)).tobytes().count(b"\x00\xff") >= 4
                   for y in (height // 3, height // 2, 2 * height // 3)):
            return None
    first, last = runs[1][0], runs[-2][1]
    if min(first - left[1], right[0] - last) < max(3, height // 10):
        return None
    return Box((left[1] + first) // 2, 0, (last + right[0] + 1) // 2, height)


def _banner_legend_box(rgb: bytes, width: int, height: int, banner: Box) -> Box | None:
    row = Image.frombytes("RGB", (width, height), rgb).crop((0, banner.top, width, banner.bottom))
    spans = [(banner.left, banner.right)]
    if spans[0] != (0, width):
        spans.append((0, width))
    for left, right in spans:
        region = row if (left, right) == (0, width) else row.crop((left, 0, right, row.height))
        for bright in (True, False):
            legend = _striped_banner_legend(_red_pixel_mask(region, bright=bright))
            if legend is not None:
                return Box(left + legend.left, banner.top, left + legend.right, banner.bottom)
    return None


async def read_frame(path: Path, recognizer: Recognizer) -> ReactorReading:
    """Read one image file using derived localization and the admitted OCR port."""
    started = time.perf_counter()
    try:
        with Image.open(path) as decoded:
            width, height = decoded.size
            if (not 1 <= width <= 8192 or not 1 <= height <= 8192
                    or width * height > MAX_SUPPORTED_CAPTURE_PIXELS):
                return ReactorReading(None, None, None, _elapsed(started), "frame_pixel_limit")
            image = decoded.convert("RGB")
            rgb = image.tobytes()
    except (OSError, ValueError):
        return ReactorReading(None, None, None, _elapsed(started), "frame_unreadable")
    return await _read_native_rgb(rgb, width, height, recognizer, started=started)


async def read_native_frame(
    image: Image.Image, recognizer: Recognizer, *, native_band: bool = False,
) -> ReactorReading:
    """Read a native RGB frame; native-band OCR is an explicit comparison option."""
    started = time.perf_counter()
    if not isinstance(image, Image.Image):
        return ReactorReading(None, None, None, _elapsed(started), "invalid_rgb_frame")
    width, height = image.size
    if (not 1 <= width <= 8192 or not 1 <= height <= 8192
            or width * height > MAX_SUPPORTED_CAPTURE_PIXELS):
        return ReactorReading(None, None, None, _elapsed(started), "frame_pixel_limit")
    try:
        rgb = image.convert("RGB").tobytes()
    except (OSError, ValueError):
        return ReactorReading(None, None, None, _elapsed(started), "frame_unreadable")
    return await _read_native_rgb(
        rgb, width, height, recognizer, started=started, native_band=native_band,
    )


async def _read_native_rgb(
    rgb: bytes, width: int, height: int, recognizer: Recognizer, *, started: float,
    native_band: bool = False,
) -> ReactorReading:
    """Read banner and console context from the exact same native RGB frame."""
    if (type(rgb) is not bytes or type(width) is not int or type(height) is not int
            or not 1 <= width <= 8192 or not 1 <= height <= 8192
            or width * height > MAX_SUPPORTED_CAPTURE_PIXELS or len(rgb) != width * height * 3):
        return ReactorReading(None, None, None, _elapsed(started), "invalid_rgb_frame")
    box = locate_reactor_text(rgb, width, height)
    if box is None:
        return ReactorReading(None, None, None, _elapsed(started), "banner_not_located")
    legend = _banner_legend_box(rgb, width, height, box)
    if legend and (legend.left < box.left or legend.right > box.right):
        # A detached stripe can win the initial colour scan. Use the complete
        # legend's position to identify the adjacent status headings.
        box = legend
    text_box = legend or box
    try:
        pgm = frame_to_pgm(rgb, width, height, text_box)
    except ValueError as exc:
        return ReactorReading(None, None, box, _elapsed(started), f"roi_unavailable:{exc}")
    try:
        result = await recognizer.recognize(pgm, Whitelist.LABEL)
    except TesseractUnavailable as exc:
        return ReactorReading(None, None, box, _elapsed(started), f"ocr_unavailable:{exc}")
    except Exception:
        return ReactorReading(None, None, box, _elapsed(started), "ocr_unavailable")
    primary_banner = _REACTOR_BANNER.search(result.text)
    has_component_text, primary_component = _component_failure_banner(result.text)
    if legend and primary_banner is None and not has_component_text:
        try:
            gray = await recognizer.recognize(
                _gray_box_pgm(rgb, width, height, legend), Whitelist.LABEL)
            has_component_text, primary_component = _component_failure_banner(gray.text)
            if has_component_text:
                result = gray
        except (TesseractUnavailable, ValueError):
            pass
    if primary_banner is None and has_component_text and primary_component is None:
        # A tighter crop can lose negation, recovery or a conflicting fault.
        return ReactorReading(result.text, None, box, _elapsed(started),
                              "component_banner_inactive_or_ambiguous")
    primary_seconds = _countdown_seconds(result.text) if primary_banner else None
    alternate_text = None
    if primary_banner is not None:
        try:
            gray_pgm = _gray_box_pgm(rgb, width, height, text_box)
            alternate_text = (await recognizer.recognize(gray_pgm, Whitelist.LABEL)).text
        except TesseractUnavailable:
            pass
        except Exception:
            pass
    alternate_seconds = (_countdown_seconds(alternate_text)
                         if isinstance(alternate_text, str) else None)
    # A number is current evidence only when both bounded crops agree. A
    # disagreement keeps the literal category available to the warning path,
    # but never substitutes a neighboring frame's countdown.
    seconds = (primary_seconds if primary_seconds is not None
               and primary_seconds == alternate_seconds else None)
    if (primary_seconds is not None and alternate_seconds is not None
            and primary_seconds != alternate_seconds):
        reading_reason = "countdown_ocr_disagreement"
    elif primary_banner is not None and seconds is None:
        reading_reason = "countdown_unconfirmed"
    else:
        reading_reason = None
    source_sha256 = hashlib.sha256(rgb).hexdigest()
    regions: tuple[NativeTextRegion, ...]
    if primary_component is not None:
        # A literal component failure needs only the adjacent same-panel
        # status labels. Avoid an unbounded full-frame TSV proposal here.
        try:
            header_regions = await _bounded_header_regions(
                rgb, width, height, source_sha256, box, recognizer,
                native_band=native_band,
            )
        except TesseractUnavailable as exc:
            return ReactorReading(result.text, None, box, _elapsed(started),
                                  f"console_ocr_unavailable:{exc}")
        except Exception as exc:
            return ReactorReading(result.text, None, box, _elapsed(started),
                                  f"console_ocr_unavailable:{type(exc).__name__}")
        if not header_regions:
            return ReactorReading(result.text, None, box, _elapsed(started),
                                  "console_ocr_unavailable:header_unreadable")
        regions = header_regions
    else:
        try:
            status_pgm = _full_frame_pgm(rgb, width, height)
            regions = await recognizer.propose_regions(
                status_pgm, source_sha256=source_sha256, max_rows=256, max_regions=64,
            )
        except TesseractUnavailable as exc:
            return ReactorReading(result.text, None, box, _elapsed(started),
                                  f"console_ocr_unavailable:{exc}")
        except Exception as exc:
            return ReactorReading(result.text, None, box, _elapsed(started),
                                  f"console_ocr_unavailable:{type(exc).__name__}")
    component_banner = primary_component or await _component_failure_from_regions(
        rgb, width, height, source_sha256, regions, box, recognizer,
    )
    if primary_banner is None and component_banner is None:
        return ReactorReading(result.text, None, box, _elapsed(started), "reactor_text_unmatched")
    header_hits = _header_label_hits(source_sha256, width, height, regions, box)
    if len(header_hits) < 2:
        retry_region = _status_header_retry_region(
            regions, width, height, source_sha256, box)
        if retry_region is not None:
            try:
                retry_pgm = _gray_region_pgm(rgb, width, height, retry_region)
                retry = await recognizer.recognize(retry_pgm, Whitelist.CAPTION)
                if retry.text.strip():
                    regions = regions + (NativeTextRegion(
                        region_id=f"header-gray-{source_sha256[:16]}",
                        source_sha256=source_sha256, image_width=width, image_height=height,
                        line_key=retry_region.line_key, bounds=retry_region.bounds,
                        text=retry.text, words=retry.words,
                    ),)
            except TesseractUnavailable:
                pass
            except Exception:
                pass
    identified, cues = identify_engineering_console(
        rgb, width, height, source_sha256, regions, box,
        allow_cropped_panel=component_banner is not None,
    )
    if not identified:
        return ReactorReading(component_banner or result.text, None, box, _elapsed(started),
                              "engineering_console_unidentified", False, cues)
    return ReactorReading(component_banner or result.text, seconds, box, _elapsed(started),
                          reading_reason, True, cues)


def _component_failure_banner(text: str) -> tuple[bool, str | None]:
    """Preserve context when a longer message mentions a component fault."""
    line = " ".join(text.split()).strip(" .!|:")
    if _failure_strip_subject(line) is not None:
        return True, line.upper()
    return _COMPONENT_MENTION.search(line) is not None, None


def _header_band_bounds(width: int, height: int, banner: Box) -> tuple[int, int, int, int]:
    """Select a wide, shallow panel band relative to the detected banner."""
    center_x = (banner.left + banner.right) // 2
    region_width = min(width, max(round(width * 0.80), round(banner.width * 0.90)))
    region_height = max(1, round(height * 0.18))
    left = max(0, min(width - region_width, center_x - region_width // 2))
    return left, max(0, banner.top - region_height), left + region_width, banner.top


def _bounded_band_size(
    width: int, height: int, *, native_band: bool = False,
) -> tuple[int, int]:
    """Keep normal panel proposals under their OCR budget; allow explicit native detail."""
    pixel_budget = MAX_CAPTURE_PIXELS if native_band else MAX_ROI_PIXELS
    return _size_within_budget(width, height, pixel_budget)


def _size_within_budget(width: int, height: int, pixel_budget: int) -> tuple[int, int]:
    if width * height <= pixel_budget:
        return width, height
    scale = math.sqrt(pixel_budget / (width * height))
    resized = (max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))
    while resized[0] * resized[1] > pixel_budget:
        if resized[0] / width >= resized[1] / height:
            resized = (resized[0] - 1, resized[1])
        else:
            resized = (resized[0], resized[1] - 1)
    return resized


async def _bounded_header_regions(
    rgb: bytes, width: int, height: int, source_sha256: str,
    banner: Box, recognizer: Recognizer, *, native_band: bool = False,
) -> tuple[NativeTextRegion, ...]:
    """Propose source-bound text rows in a bounded band above the banner."""
    bounds = _header_band_bounds(width, height, banner)
    left, top, right, bottom = bounds
    crop_width, crop_height = right - left, bottom - top
    if crop_height < 1:
        return ()
    ocr_width, ocr_height = _bounded_band_size(
        crop_width, crop_height, native_band=native_band,
    )
    image = ImageOps.autocontrast(Image.frombytes(
        "RGB", (width, height), rgb,
    ).crop(bounds).convert("L"))
    if (ocr_width, ocr_height) != (crop_width, crop_height):
        image = image.resize((ocr_width, ocr_height), Image.Resampling.LANCZOS)
    pixels = image.tobytes()
    crop_sha256 = hashlib.sha256(pixels).hexdigest()
    crop_pgm = f"P5\n{ocr_width} {ocr_height}\n255\n".encode("ascii") + pixels
    proposals = await recognizer.propose_regions(
        crop_pgm, source_sha256=crop_sha256, max_rows=256, max_regions=64,
    )
    translated: list[NativeTextRegion] = []
    for index, region in enumerate(proposals):
        if (type(region) is not NativeTextRegion
                or region.source_sha256 != crop_sha256
                or region.image_width != ocr_width or region.image_height != ocr_height):
            continue
        x0, y0, x1, y1 = region.bounds
        if not (0 <= x0 < x1 <= ocr_width and 0 <= y0 < y1 <= ocr_height):
            continue
        scale_x, scale_y = crop_width / ocr_width, crop_height / ocr_height
        full_bounds = (
            left + math.floor(x0 * scale_x), top + math.floor(y0 * scale_y),
            left + math.ceil(x1 * scale_x), top + math.ceil(y1 * scale_y),
        )
        words = tuple(
            replace(
                word,
                left=left + math.floor(word.left * scale_x),
                top=top + math.floor(word.top * scale_y),
                width=max(1, math.ceil((word.left + word.width) * scale_x)
                          - math.floor(word.left * scale_x)),
                height=max(1, math.ceil((word.top + word.height) * scale_y)
                           - math.floor(word.top * scale_y)),
            )
            for word in region.words
            if type(word) is TsvWord and word.left >= 0 and word.top >= 0
            and word.left + word.width <= ocr_width
            and word.top + word.height <= ocr_height
        )
        translated.append(NativeTextRegion(
            region_id=f"header-{source_sha256[:12]}-{index}",
            source_sha256=source_sha256, image_width=width, image_height=height,
            line_key=region.line_key,
            bounds=full_bounds,
            text=region.text, words=words,
        ))
    return tuple(translated)


async def _component_failure_from_regions(
    rgb: bytes, width: int, height: int, source_sha256: str,
    regions: tuple[NativeTextRegion, ...], banner: Box, recognizer: Recognizer,
) -> str | None:
    """Read one OCR row that overlaps the same-frame, red-derived banner box."""
    candidates = tuple(
        region for region in regions
        if (type(region) is NativeTextRegion and region.source_sha256 == source_sha256
            and region.image_width == width and region.image_height == height
            and 0 <= region.bounds[0] < region.bounds[2] <= width
            and 0 <= region.bounds[1] < region.bounds[3] <= height
            and region.bounds[0] <= (banner.left + banner.right) / 2 <= region.bounds[2]
            and region.bounds[1] < banner.bottom and region.bounds[3] > banner.top
            and region.bounds[2] - region.bounds[0] <= width
            and (region.bounds[2] - region.bounds[0])
            * (region.bounds[3] - region.bounds[1]) <= 100_000)
    )
    ordered = sorted(candidates, key=lambda item: (
            (item.bounds[2] - item.bounds[0]) * (item.bounds[3] - item.bounds[1]),
            item.region_id))
    matching = {_component_failure_banner(region.text) for region in ordered}
    matching.discard((False, None))
    if matching:
        if len(matching) == 1:
            return matching.pop()[1]
        return None
    if not ordered:
        return None
    # The full-frame recognizer can merge the red text with its hazard
    # stripes. Retry only the smallest source-derived row overlapping the red
    # banner box; never search caller-selected coordinates.
    try:
        retry = await recognizer.recognize(
            _gray_region_pgm(rgb, width, height, ordered[0]), Whitelist.CAPTION,
        )
        return _component_failure_banner(retry.text)[1]
    except TesseractUnavailable:
        return None
    except Exception:
        return None


def _countdown_seconds(text: str) -> int | None:
    match = _REACTOR_LINE.search(text)
    if match is None:
        return None
    seconds = int(match.group(1))
    return seconds if 0 <= seconds <= 999 else None


def _gray_box_pgm(rgb: bytes, width: int, height: int, box: Box) -> bytes:
    return _gray_crop_pgm(rgb, width, height,
                          (box.left, box.top, box.right, box.bottom), upscale=2)


def _gray_region_pgm(
    rgb: bytes, width: int, height: int, region: NativeTextRegion,
) -> bytes:
    if (type(region) is not NativeTextRegion or region.image_width != width
            or region.image_height != height):
        raise ValueError("invalid_header_region")
    return _gray_crop_pgm(rgb, width, height, region.bounds, upscale=3)


def _gray_crop_pgm(
    rgb: bytes, width: int, height: int, bounds: tuple[int, int, int, int], *, upscale: int,
) -> bytes:
    left, top, right, bottom = bounds
    if (type(rgb) is not bytes or len(rgb) != width * height * 3
            or not (0 <= left < right <= width and 0 <= top < bottom <= height)
            or not 1 <= upscale <= 4):
        raise ValueError("invalid_gray_roi")
    scale = min(upscale, math.isqrt(MAX_ROI_PIXELS // ((right - left) * (bottom - top))))
    image = Image.frombytes("RGB", (width, height), rgb)
    crop = ImageOps.autocontrast(image.crop(bounds).convert("L"))
    if scale == 0:
        crop = crop.resize(_size_within_budget(*crop.size, MAX_ROI_PIXELS),
                           Image.Resampling.LANCZOS)
    elif scale > 1:
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    return f"P5\n{crop.width} {crop.height}\n255\n".encode("ascii") + crop.tobytes()


def _status_header_retry_region(
    regions: tuple[NativeTextRegion, ...], width: int, height: int, source_sha256: str,
    banner: Box,
) -> NativeTextRegion | None:
    """Select one source-derived OCR proposal covering the adjacent status row."""
    center = (banner.left + banner.right) / 2
    candidates = tuple(
        region for region in regions
        if (type(region) is NativeTextRegion and region.image_width == width
            and region.image_height == height and region.source_sha256 == source_sha256
            and region.bounds[0] <= center <= region.bounds[2]
            and 0 <= banner.top - region.bounds[3] <= height * 0.18
            and region.bounds[2] - region.bounds[0] >= banner.width * 0.4
            and (region.bounds[2] - region.bounds[0])
            * (region.bounds[3] - region.bounds[1]) <= 100_000)
    )
    return max(candidates, key=lambda region: (
        region.bounds[2] - region.bounds[0], region.region_id)) if candidates else None


def _header_label_hits(
    source_sha256: str, width: int, height: int,
    regions: tuple[NativeTextRegion, ...], banner: Box,
) -> tuple[str, ...]:
    valid_header_regions = tuple(
        region for region in regions
        if (type(region) is NativeTextRegion and region.source_sha256 == source_sha256
            and region.image_width == width and region.image_height == height
            and 0 <= banner.top - region.bounds[3] <= height * 0.18)
    )
    header_bounds = _union_bounds(tuple(region.bounds for region in valid_header_regions))
    banner_center = (banner.left + banner.right) / 2
    if (header_bounds is None
            or not header_bounds[0] <= banner_center <= header_bounds[2]):
        return ()
    return tuple(label for label in _STATUS_LABELS if any(
        _contains_label(region.text, label) for region in valid_header_regions))


def identify_engineering_console(
    rgb: bytes, width: int, height: int, source_sha256: str,
    regions: tuple[NativeTextRegion, ...], banner: Box, *, allow_cropped_panel: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """Bind a red banner and visible status/filter structure to one console."""
    if (type(rgb) is not bytes or len(rgb) != width * height * 3
            or type(regions) is not tuple or type(banner) is not Box):
        return False, ()
    valid_regions = tuple(
        region for region in regions
        if (type(region) is NativeTextRegion and region.source_sha256 == source_sha256
            and region.image_width == width and region.image_height == height)
    )
    header_regions = tuple(
        region for region in valid_regions
        if any(_contains_label(region.text, label) for label in _STATUS_LABELS)
        and 0 <= banner.top - region.bounds[3] <= height * 0.18
    )
    header_hits = tuple(label for label in _STATUS_LABELS if any(
        _contains_label(region.text, label) for region in header_regions
    ))
    header_bounds = _union_bounds(tuple(region.bounds for region in header_regions))
    banner_center_x = (banner.left + banner.right) / 2
    header_covers_banner = (header_bounds is not None
                            and header_bounds[0] <= banner_center_x <= header_bounds[2])

    filter_headings = tuple(
        region for region in valid_regions
        if _contains_label(region.text, "FILTERS")
        and 0 <= region.bounds[1] - banner.bottom <= height * 0.60
    )
    active_filters = tuple(
        (label, region) for region in valid_regions for label in _FILTER_LABELS
        if _contains_label(region.text, label)
        and region.bounds[1] > banner.bottom
        and _active_label_visible(rgb, width, height, region)
    )
    matched_filters = tuple(
        (label, active) for label, active in active_filters
        for heading in filter_headings
        if abs(_center_x(heading.bounds) - _center_x(active.bounds)) <= width * 0.10
        and 0 <= active.bounds[1] - heading.bounds[3] <= height * 0.16
    )
    filter_bounds = _union_bounds(tuple(
        region.bounds for region in filter_headings
    ) + tuple(region.bounds for _, region in matched_filters))
    same_panel_scale = False
    if header_bounds is not None and filter_bounds is not None:
        panel = _union_bounds((header_bounds, banner_box(banner), filter_bounds))
        same_panel_scale = (panel is not None
                            and panel[2] - panel[0] <= width * 0.85
                            and panel[3] - panel[1] <= height * 0.75
                            and filter_bounds[0] >= header_bounds[0] - width * 0.04
                            and filter_bounds[2] <= header_bounds[2] + width * 0.04)
    cropped_panel_scale = False
    if allow_cropped_panel and header_bounds is not None:
        panel = _union_bounds((header_bounds, banner_box(banner)))
        cropped_panel_scale = (panel is not None
                               and panel[3] - panel[1] <= height * 0.30
                               and 0 <= banner.top - header_bounds[3] <= height * 0.18)
    structure_ok = (len(header_hits) >= 2 and header_covers_banner
                    and ((bool(filter_headings) and bool(matched_filters) and same_panel_scale)
                         or cropped_panel_scale))
    active_hits = tuple(dict.fromkeys(label for label, _ in matched_filters))
    cues = tuple(f"status:{label}" for label in header_hits)
    if filter_headings:
        cues += ("filter_menu:FILTERS",)
    cues += tuple(f"filter_option:{label}" for label, _ in matched_filters)
    cues += tuple(f"active_filter:{label}" for label in active_hits)
    return structure_ok, cues


def banner_box(box: Box) -> tuple[int, int, int, int]:
    return box.left, box.top, box.right, box.bottom


def _union_bounds(bounds: tuple[tuple[int, int, int, int], ...]
                  ) -> tuple[int, int, int, int] | None:
    if not bounds:
        return None
    return (min(row[0] for row in bounds), min(row[1] for row in bounds),
            max(row[2] for row in bounds), max(row[3] for row in bounds))


def _center_x(bounds: tuple[int, int, int, int]) -> float:
    return (bounds[0] + bounds[2]) / 2


def _active_label_visible(
    rgb: bytes, width: int, height: int, region: NativeTextRegion,
) -> bool:
    text = re.sub(r"\s+", " ", region.text.upper()).strip()
    if not any(_contains_label(text, label) for label in _FILTER_LABELS):
        return False
    left, top, right, bottom = region.bounds
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        return False
    if (right - left) * (bottom - top) > 100_000:
        return False
    # Engineering UI selection uses bright cyan text. Measure inside the OCR
    # word bounds, never a caller-provided or screen-position-derived rectangle.
    pixels = memoryview(rgb)
    cyan = total = 0
    for y in range(top, bottom):
        offset = (y * width + left) * 3
        for _x in range(left, right):
            red, green, blue = pixels[offset:offset + 3]
            total += 1
            if blue >= 90 and green >= 70 and blue - red >= 35 and green - red >= 25:
                cyan += 1
            offset += 3
    return total > 0 and cyan / total >= 0.08


def _contains_label(text: str, label: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(label)}(?![A-Z0-9])", text.upper()) is not None


def _full_frame_pgm(rgb: bytes, width: int, height: int) -> bytes:
    """Encode the full native frame for the original bounded scene OCR port."""
    image = Image.frombytes("RGB", (width, height), rgb).convert("L")
    return f"P5\n{width} {height}\n255\n".encode("ascii") + image.tobytes()


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _red_pixel_mask(image: Image.Image, *, bright: bool = False) -> Image.Image:
    red, green, blue = image.split()
    mask = red.point(_threshold_lut(120 if bright else 75))
    channels = [
        ImageChops.subtract(red, green).point(_threshold_lut(35 if bright else 24)),
        ImageChops.subtract(red, blue).point(_threshold_lut(25 if bright else 8)),
    ]
    if not bright:
        channels.append(ImageChops.subtract(blue, green).point(_threshold_lut(2)))
    for channel in channels:
        mask = ImageChops.multiply(mask, channel)
    return mask


def _threshold_lut(value: int) -> tuple[int, ...]:
    return tuple(255 if item >= value else 0 for item in range(256))
