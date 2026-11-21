"""Unqualified native OCR text regions and strict bounded TSV decoding."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

from adapters.perception.image_bounds import _validate_roi
from adapters.perception.tesseract import TSV_HEADER, TsvWord

TitleBox = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class NativeTextRegion:
    """Untrusted diagnostic text and geometry; never field or crop admission."""

    region_id: str
    source_sha256: str
    image_width: int
    image_height: int
    line_key: tuple[int, int, int, int]
    bounds: TitleBox
    text: str
    words: tuple[TsvWord, ...]

    def crop_roi(self) -> TitleBox:
        """Apply the original pixel budget before offering these native pixels."""
        return _validate_roi(self.bounds, self.image_width, self.image_height)


MAX_NEIGHBOR_PAIRS = 1024


@dataclass(frozen=True, slots=True)
class NativeNeighborProposal:
    """Offline geometry only; original OCR regions and bounds remain unqualified."""

    proposal_id: str
    source_sha256: str
    image_width: int
    image_height: int
    parent_region_ids: tuple[str, str]
    parent_bounds: tuple[TitleBox, TitleBox]
    proposed_bounds: TitleBox
    expansion_each_side: int


@dataclass(frozen=True, slots=True)
class RefusedNativeNeighborProposal:
    proposal: NativeNeighborProposal
    reason: str


@dataclass(frozen=True, slots=True)
class NativeNeighborProposalSet:
    proposals: tuple[NativeNeighborProposal, ...]
    refusals: tuple[RefusedNativeNeighborProposal, ...]


def _edge_gap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, b0 - a1, a0 - b1)


def _neighbor_score(a: NativeTextRegion, b: NativeTextRegion) -> tuple[int, int, str]:
    ax0, ay0, ax1, ay1 = a.bounds
    bx0, by0, bx1, by1 = b.bounds
    gap_x = _edge_gap(ax0, ax1, bx0, bx1)
    gap_y = _edge_gap(ay0, ay1, by0, by1)
    center_distance_squared = ((ax0 + ax1 - bx0 - bx1) ** 2
                               + (ay0 + ay1 - by0 - by1) ** 2)
    return gap_x * gap_x + gap_y * gap_y, center_distance_squared, b.region_id


def _neighbor_eligible(a: NativeTextRegion, b: NativeTextRegion) -> bool:
    if a.region_id == b.region_id:
        return False
    ax0, ay0, ax1, ay1 = a.bounds
    bx0, by0, bx1, by1 = b.bounds
    max_height = max(ay1 - ay0, by1 - by0)
    return (abs(ay0 + ay1 - by0 - by1) <= 2 * max_height
            and _edge_gap(ax0, ax1, bx0, bx1) <= max_height)


def _neighbor_proposal_id(
    source_sha256: str, width: int, height: int, parents: tuple[str, str],
    parent_bounds: tuple[TitleBox, TitleBox], proposed_bounds: TitleBox, margin: int,
) -> str:
    identity = json.dumps(
        [source_sha256, width, height, parents, parent_bounds, proposed_bounds, margin],
        separators=(",", ":"),
    ).encode("ascii")
    # Keep IDs within the original native region/offered-choice 64-character
    # ceiling while retaining a stable, source-bound 220-bit digest prefix.
    return "neighbor-" + hashlib.sha256(identity).hexdigest()[:55]


def propose_neighbor_regions(
    regions: tuple[NativeTextRegion, ...], *,
    max_regions: int,
    max_pair_budget: int = MAX_NEIGHBOR_PAIRS,
) -> NativeNeighborProposalSet:
    """Propose bounded neighboring-region unions without inspecting image pixels or text.

    Each original region chooses one nearest eligible region. Unordered pairs are
    deduplicated, and every proposed rectangle retains both source parents. The
    result is a diagnostic candidate only; it carries no localization admission.
    """
    if (type(regions) is not tuple or type(max_regions) is not int or max_regions < 1
            or len(regions) > max_regions or type(max_pair_budget) is not int
            or max_pair_budget < 1):
        raise ValueError("invalid native neighbor input, region budget or pair budget")

    source: tuple[str, int, int] | None = None
    by_id: dict[str, NativeTextRegion] = {}
    for region in regions:
        if type(region) is not NativeTextRegion:
            raise ValueError("native text region type invalid")
        if (type(region.region_id) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}",
                                region.region_id, re.ASCII) is None
                or type(region.source_sha256) is not str or len(region.source_sha256) != 64
                or any(c not in "0123456789abcdef" for c in region.source_sha256)
                or type(region.image_width) is not int or type(region.image_height) is not int
                or not 1 <= region.image_width <= 16_384
                or not 1 <= region.image_height <= 16_384):
            raise ValueError("native text source identity or dimensions invalid")
        binding = (region.source_sha256, region.image_width, region.image_height)
        if source is None:
            source = binding
        elif binding != source:
            raise ValueError("mixed native text source frames")
        if region.region_id in by_id:
            raise ValueError("duplicate native text region ID")
        bounds = region.bounds
        if (type(bounds) is not tuple or len(bounds) != 4
                or any(type(value) is not int for value in bounds)):
            raise ValueError("invalid original native text ROI")
        x0, y0, x1, y1 = bounds
        if not (0 <= x0 < x1 <= region.image_width and 0 <= y0 < y1 <= region.image_height):
            raise ValueError("invalid original native text ROI")
        by_id[region.region_id] = region

    nearest: dict[str, NativeTextRegion] = {}
    for region in regions:
        candidates = (other for other in regions if _neighbor_eligible(region, other))
        neighbor = min(candidates, key=lambda other: _neighbor_score(region, other), default=None)
        if neighbor is not None:
            nearest[region.region_id] = neighbor

    pair_ids = sorted({tuple(sorted((region_id, neighbor.region_id)))
                       for region_id, neighbor in nearest.items()})
    if len(pair_ids) > max_pair_budget:
        raise ValueError(
            f"whole-case pair budget exceeded: {len(pair_ids)} > {max_pair_budget}"
        )

    if source is None:
        return NativeNeighborProposalSet((), ())
    source_sha256, width, height = source
    proposals: list[NativeNeighborProposal] = []
    refusals: list[RefusedNativeNeighborProposal] = []
    for parent_ids in pair_ids:
        left, right = (by_id[parent_id] for parent_id in parent_ids)
        left_bounds, right_bounds = left.bounds, right.bounds
        x0 = min(left_bounds[0], right_bounds[0])
        y0 = min(left_bounds[1], right_bounds[1])
        x1 = max(left_bounds[2], right_bounds[2])
        y1 = max(left_bounds[3], right_bounds[3])
        max_height = max(left_bounds[3] - left_bounds[1], right_bounds[3] - right_bounds[1])
        margin = (max_height + 2) // 3
        bounds = (x0 - margin, y0 - margin, x1 + margin, y1 + margin)
        ordered_parent_bounds = (left_bounds, right_bounds)
        proposal = NativeNeighborProposal(
            _neighbor_proposal_id(source_sha256, width, height, parent_ids,
                                  ordered_parent_bounds, bounds, margin),
            source_sha256, width, height, parent_ids, ordered_parent_bounds, bounds, margin,
        )
        try:
            _validate_roi(bounds, width, height)
        except ValueError as exc:
            reason = ("outside_frame" if "roi_out_of_bounds" in str(exc)
                      else "original_roi_overbudget")
            refusals.append(RefusedNativeNeighborProposal(proposal, reason))
        else:
            proposals.append(proposal)
    return NativeNeighborProposalSet(tuple(proposals), tuple(refusals))


def decode_native_tsv_regions(
    raw: bytes, *, source_sha256: str, width: int, height: int,
    max_bytes: int, max_rows: int, max_regions: int,
) -> tuple[NativeTextRegion, ...]:
    """Group literal TSV words; caller budgets count data rows, excluding header.

    Reuse Tesseract's header/word DTO and hierarchy. Unlike production parse_tsv,
    allow multiple lines and printable Unicode diagnostic glyphs. Large line
    unions remain metadata until crop_roi applies the original ROI guard.
    """
    if (type(raw) is not bytes
            or any(type(n) is not int or n < 1 for n in (max_bytes, max_rows, max_regions))
            or len(raw) > max_bytes
            or any(type(n) is not int or not 1 <= n <= 16_384 for n in (width, height))
            or type(source_sha256) is not str or len(source_sha256) != 64
            or any(c not in "0123456789abcdef" for c in source_sha256)):
        raise ValueError("bounded native TSV, image dimensions and source SHA-256 required")
    rows = raw.decode("utf-8", "strict").splitlines()
    if not rows or rows[0] != TSV_HEADER or len(rows) - 1 > max_rows:
        raise ValueError("native TSV header or row budget differs")
    groups: dict[tuple[int, int, int, int], list[TsvWord]] = {}
    previous: tuple[int, ...] | None = None
    for row in rows[1:]:
        columns = row.split("\t")  # Quote glyphs are data, never CSV quoting.
        if len(columns) != 12 or any(
            re.fullmatch(r"-?[0-9]+", value, re.ASCII) is None for value in columns[:10]
        ):
            raise ValueError("native TSV row shape or integer syntax differs")
        level, *numbers = (int(value) for value in columns[:10])
        page, block, paragraph, line, word, left, top, word_width, word_height = numbers
        hierarchy = tuple(numbers[:5])
        score = float(columns[10])
        text = columns[11]
        if (not 1 <= level <= 5 or any(n < 1 for n in hierarchy[:level])
                or any(n != 0 for n in hierarchy[level:])
                or previous is not None and hierarchy <= previous
                or not math.isfinite(score) or columns[10].strip() != columns[10]
                or min(left, top, word_width, word_height) < 0
                or left + word_width > width or top + word_height > height):
            raise ValueError("native TSV hierarchy, order, confidence or image bounds differ")
        previous = hierarchy
        if level != 5:
            if text or score != -1.0:
                raise ValueError("native TSV nonword row carries text or confidence")
            continue
        if (word_width == 0 or word_height == 0 or not 0 <= score <= 100
                or not text.strip() or len(text) > 128 or not text.isprintable()):
            raise ValueError("native TSV word geometry, confidence or text differs")
        key = (page, block, paragraph, line)
        if key not in groups:
            if len(groups) >= max_regions:
                raise ValueError("native TSV region budget exceeded")
            groups[key] = []
        groups[key].append(TsvWord(
            page, block, paragraph, line, word, left, top, word_width, word_height, score, text,
        ))
    regions = []
    for key, items in sorted(groups.items()):
        bounds = (min(w.left for w in items), min(w.top for w in items),
                  max(w.left + w.width for w in items), max(w.top + w.height for w in items))
        identity = json.dumps(
            [source_sha256, width, height, key, bounds], separators=(",", ":"),
        ).encode("ascii")
        region_id = "ocr-" + hashlib.sha256(identity).hexdigest()[:60]
        regions.append(NativeTextRegion(
            region_id, source_sha256, width, height, key, bounds,
            " ".join(w.text for w in items), tuple(items),
        ))
    return tuple(regions)
