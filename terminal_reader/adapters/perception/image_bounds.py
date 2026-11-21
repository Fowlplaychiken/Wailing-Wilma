"""Shared bounds for reading captured pixels and text regions."""

MAX_CAPTURE_PIXELS = 4_194_304
MAX_SUPPORTED_CAPTURE_PIXELS = 33_554_432
MAX_ROI_PIXELS = 262_144
MAX_ENCODED_BYTES = 1_048_576


class PixelUnavailable(ValueError):
    pass


def _validate_roi(
    roi: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    if type(roi) is not tuple or len(roi) != 4 or any(type(v) is not int for v in roi):
        raise PixelUnavailable("roi_invalid")
    x0, y0, x1, y1 = roi
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise PixelUnavailable("roi_out_of_bounds")
    if (x1 - x0) * (y1 - y0) > MAX_ROI_PIXELS:
        raise PixelUnavailable("roi_too_large")
    return roi
