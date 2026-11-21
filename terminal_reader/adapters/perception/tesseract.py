"""Optional finite Tesseract adapter with exact local artifact admission."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from adapters.perception.image_bounds import MAX_ENCODED_BYTES, MAX_SUPPORTED_CAPTURE_PIXELS

if TYPE_CHECKING:
    from adapters.perception.native_regions import NativeTextRegion

ENGINE_COMMIT = "7e548e6eff172f38f97664afe7f7ce40db36c8d6"
ENGINE_VERSION = "5.5.4"
_ENGINE_PINS = (
    (ENGINE_VERSION, ENGINE_COMMIT),
    ("5.5.3", "db0ec62f81b0737fbbe184d8fea40af5738f8eef"),
)
MODEL_COMMIT = "87416418657359cb625c412a48b6e1d6d41c29bd"
TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)
STDOUT_LIMIT = 65_536
STDERR_LIMIT = 16_384
INPUT_LIMIT = 1_048_576
SCENE_INPUT_LIMIT = MAX_SUPPORTED_CAPTURE_PIXELS + 64
SCENE_STDOUT_LIMIT = 512 * 1024
EXECUTABLE_BYTES_LIMIT = 128 * 1_048_576
MODEL_BYTES_LIMIT = 16 * 1_048_576
PARSER_VERSION = "tesseract-tsv-v1"
_PGM_HEADER = re.compile(rb"P5\n([1-9][0-9]{0,4}) ([1-9][0-9]{0,4})\n255\n")


class Whitelist(StrEnum):
    NUMERIC = "0123456789."
    PERCENT = "0123456789.%"
    LABEL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -_"
    CAPTION = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz "


class TesseractUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractionIdentity:
    kind: str
    parser_version: str
    engine_sha256: str | None = None
    model_sha256: str | None = None
    config_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "tesseract":
            if (
                self.parser_version != PARSER_VERSION
                or not _is_sha256(self.engine_sha256)
                or not _is_sha256(self.model_sha256)
                or self.config_sha256 is not None
            ):
                raise ValueError("Tesseract extraction identity is incomplete")
        elif self.kind == "vision_text":
            if (
                self.parser_version != "blind-region-text-v1"
                or not _is_sha256(self.config_sha256)
                or self.engine_sha256 is not None
                or self.model_sha256 is not None
            ):
                raise ValueError("vision text extraction identity is incomplete")
        elif self.kind == "bar":
            if (
                self.parser_version != "fixed-color-bar-v1"
                or not _is_sha256(self.config_sha256)
                or self.engine_sha256 is not None
                or self.model_sha256 is not None
            ):
                raise ValueError("bar extraction identity is incomplete")
        elif self.kind == "color_interval":
            if (
                self.parser_version != "ordered-color-interval-v1"
                or not _is_sha256(self.config_sha256)
                or self.engine_sha256 is not None
                or self.model_sha256 is not None
            ):
                raise ValueError("color-interval extraction identity is incomplete")
        else:
            raise ValueError("unsupported extraction identity kind")


@dataclass(frozen=True, slots=True)
class TesseractArtifact:
    executable: Path
    tessdata_directory: Path
    executable_sha256: str
    model_sha256: str
    engine_source_pin: str
    model_source_pin: str
    self_test_ref: str
    source_id: str = "tesseract-ocr"
    engine_version: str = ENGINE_VERSION

    def __post_init__(self) -> None:
        for path, name in (
            (self.executable, "executable"),
            (self.tessdata_directory, "tessdata_directory"),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        for value, name in (
            (self.executable_sha256, "executable_sha256"),
            (self.model_sha256, "model_sha256"),
        ):
            if not _is_sha256(value):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if (
            (self.engine_version, self.engine_source_pin) not in _ENGINE_PINS
            or self.model_source_pin != MODEL_COMMIT
        ):
            raise ValueError("unsupported Tesseract source pins")
        if not self.source_id or not self.self_test_ref:
            raise ValueError(
                "artifact requires the pinned engine and a local pipe self-test reference"
            )


class ArtifactAdmissionPort(Protocol):
    def admit(
        self, artifact: TesseractArtifact, executable_sha256: str, model_sha256: str
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class TsvWord:
    page_num: int
    block_num: int
    par_num: int
    line_num: int
    word_num: int
    left: int
    top: int
    width: int
    height: int
    score: float
    text: str


@dataclass(frozen=True, slots=True)
class TesseractResult:
    text: str
    words: tuple[TsvWord, ...]
    engine_sha256: str
    model_sha256: str
    source_version: str
    extraction_identity: ExtractionIdentity


def validate_pgm(pgm: bytes, *, scene: bool = False) -> tuple[int, int, bytes]:
    if (type(scene) is not bool or type(pgm) is not bytes
            or not 1 <= len(pgm) <= (SCENE_INPUT_LIMIT if scene else INPUT_LIMIT)):
        raise TesseractUnavailable("pgm_input_invalid")
    match = _PGM_HEADER.match(pgm)
    if match is None:
        raise TesseractUnavailable("pgm_header_invalid")
    width, height = int(match.group(1)), int(match.group(2))
    pixels = pgm[match.end() :]
    if (scene and (width > 16_384 or height > 16_384)
            or width * height > (MAX_SUPPORTED_CAPTURE_PIXELS if scene else MAX_ENCODED_BYTES)
            or len(pixels) != width * height):
        raise TesseractUnavailable("pgm_payload_invalid")
    return width, height, pixels


def parse_tsv(data: bytes, roi_width: int, roi_height: int) -> tuple[TsvWord, ...]:
    if (
        type(data) is not bytes
        or len(data) > STDOUT_LIMIT
        or not (1 <= roi_width <= 16_384 and 1 <= roi_height <= 16_384)
    ):
        raise TesseractUnavailable("tsv_output_invalid")
    try:
        rows = data.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise TesseractUnavailable("tsv_encoding_invalid") from exc
    if not rows or rows[0].removesuffix("\r") != TSV_HEADER or len(rows) > 257:
        raise TesseractUnavailable("tsv_header_or_row_count_invalid")
    words: list[TsvWord] = []
    line_keys: set[tuple[int, int, int, int]] = set()
    for row in rows[1:]:
        columns = row.split("\t")
        if len(columns) != 12:
            raise TesseractUnavailable("tsv_column_count_invalid")
        try:
            level = int(columns[0])
            if level != 5:
                if columns[-1] or level not in (1, 2, 3, 4):
                    raise ValueError
                continue
            numeric = tuple(int(columns[i]) for i in range(1, 10))
            score = float(columns[10])
        except ValueError as exc:
            raise TesseractUnavailable("tsv_numeric_field_invalid") from exc
        page, block, par, line, word, left, top, width, height = numeric
        text = columns[11]
        if (
            min(page, block, par, line, word) < 1
            or min(left, top) < 0
            or width <= 0
            or height <= 0
            or left + width > roi_width
            or top + height > roi_height
            or not math.isfinite(score)
            or not 0.0 <= score <= 100.0
            or not text
            or len(text) > 128
            or any(not c.isprintable() for c in text)
        ):
            raise TesseractUnavailable("tsv_word_invalid")
        line_keys.add((page, block, par, line))
        if len(line_keys) > 1:
            raise TesseractUnavailable("tsv_multiple_lines")
        words.append(TsvWord(page, block, par, line, word, left, top, width, height, score, text))
    if not words or any(
        left.word_num >= right.word_num for left, right in zip(words[:-1], words[1:], strict=True)
    ):
        raise TesseractUnavailable("tsv_no_stable_words")
    return tuple(words)


def _parse_batch_tsv(
    data: bytes, dimensions: tuple[tuple[int, int], ...],
    identity: ExtractionIdentity, version: str,
) -> tuple[TesseractResult | None, ...]:
    if type(data) is not bytes or len(data) > SCENE_STDOUT_LIMIT:
        raise TesseractUnavailable("tsv_output_invalid")
    try:
        rows = data.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise TesseractUnavailable("tsv_encoding_invalid") from exc
    if not rows or rows[0] != TSV_HEADER or len(rows) > 8193:
        raise TesseractUnavailable("tsv_header_or_row_count_invalid")
    pages: list[list[str]] = [[] for _ in dimensions]
    last_page = 0
    page_headers: set[int] = set()
    for row in rows[1:]:
        columns = row.split("\t")
        if len(columns) != 12:
            raise TesseractUnavailable("tsv_column_count_invalid")
        try:
            level, page = int(columns[0]), int(columns[1])
        except ValueError as exc:
            raise TesseractUnavailable("tsv_numeric_field_invalid") from exc
        if level not in (1, 2, 3, 4, 5) or not 1 <= page <= len(pages) or page < last_page:
            raise TesseractUnavailable("tsv_page_invalid")
        if level == 1:
            if page in page_headers:
                raise TesseractUnavailable("tsv_page_invalid")
            page_headers.add(page)
        elif page not in page_headers:
            raise TesseractUnavailable("tsv_page_invalid")
        last_page = page
        pages[page - 1].append(row)
    output = []
    for rows_for_page, (width, height) in zip(pages, dimensions, strict=True):
        try:
            words = parse_tsv((TSV_HEADER + "\n" + "\n".join(rows_for_page)).encode(),
                              width, height)
        except TesseractUnavailable as exc:
            if str(exc) in ("tsv_no_stable_words", "tsv_multiple_lines"):
                output.append(None)
                continue
            raise
        output.append(TesseractResult(" ".join(word.text for word in words), words,
            cast(str, identity.engine_sha256), cast(str, identity.model_sha256), version, identity))
    return tuple(output)


class _Child(Protocol):
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    returncode: int | None

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


SpawnFactory = Callable[..., Awaitable[Any]]


class TesseractRecognizer:
    """Runs at most one pinned Tesseract child and retains unsettled cleanup ownership."""

    def __init__(
        self,
        artifact: TesseractArtifact,
        admission: ArtifactAdmissionPort,
        *,
        deadline_ms: int = 500,
        scene_deadline_ms: int = 2_000,
        spawn: SpawnFactory | None = None,
    ) -> None:
        if (type(deadline_ms) is not int or not 1 <= deadline_ms <= 2_000
                or type(scene_deadline_ms) is not int
                or not 1 <= scene_deadline_ms <= 2_000):
            raise ValueError("field and scene deadlines must be within 1..2000")
        self._artifact, self._admission = artifact, admission
        self._deadline = deadline_ms / 1000
        self._scene_deadline = scene_deadline_ms / 1000
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._child: _Child | None = None
        self._closing: asyncio.Task[None] | None = None
        self._readers: tuple[asyncio.Task[bytes], asyncio.Task[bytes]] | None = None
        self._wait_task: asyncio.Task[int] | None = None
        self._communication: asyncio.Task[tuple[bytes, bytes]] | None = None
        self._reservation: object | None = None
        self._fenced_reservation: object | None = None
        self._preflight_task: asyncio.Task[tuple[str, str]] | None = None
        self._spawn_task: asyncio.Future[Any] | None = None
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        self._closed = False

    @property
    def child_owned(self) -> bool:
        return self._child is not None

    @property
    def operation_idle(self) -> bool:
        """Read-only closure evidence, including work without a child handle yet."""
        tasks = (self._preflight_task, self._spawn_task, self._closing,
                 self._wait_task, self._communication)
        return (
            self._reservation is None and self._fenced_reservation is None
            and self._child is None and self._readers is None and self._scratch is None
            and all(task is None or task.done() for task in tasks)
        )

    @property
    def extraction_identity(self) -> ExtractionIdentity:
        return ExtractionIdentity(
            "tesseract",
            PARSER_VERSION,
            engine_sha256=self._artifact.executable_sha256,
            model_sha256=self._artifact.model_sha256,
        )

    async def recognize(self, pgm: bytes, whitelist: Whitelist) -> TesseractResult:
        return cast(TesseractResult, await self._run(pgm, whitelist=whitelist))

    async def recognize_many(self, pgms: tuple[bytes, ...], whitelist: Whitelist
                             ) -> tuple[TesseractResult | None, ...]:
        if (type(pgms) is not tuple or not 1 <= len(pgms) <= 64
                or not isinstance(whitelist, Whitelist)
                or any(type(pgm) is not bytes for pgm in pgms)
                or sum(map(len, pgms)) > SCENE_INPUT_LIMIT):
            raise TesseractUnavailable("batch_input_invalid")
        for pgm in pgms:
            validate_pgm(pgm)
        return cast(tuple[TesseractResult | None, ...], await self._run(
            b"", whitelist=whitelist, batch=pgms))

    async def propose_regions(
        self, pgm: bytes, *, source_sha256: str, max_rows: int, max_regions: int
    ) -> tuple[NativeTextRegion, ...]:
        if (not _is_sha256(source_sha256)
                or type(max_rows) is not int or max_rows < 1
                or type(max_regions) is not int or max_regions < 1):
            raise ValueError("valid source SHA-256 and positive integer budgets required")
        return cast("tuple[NativeTextRegion, ...]", await self._run(
            pgm, source_sha256=source_sha256, max_rows=max_rows, max_regions=max_regions
        ))

    async def recognize_status_sheet(
        self, pgm: bytes, *, source_sha256: str, captions: bool,
    ) -> tuple[NativeTextRegion, ...]:
        if not _is_sha256(source_sha256) or type(captions) is not bool:
            raise ValueError("status sheet source and caption flag required")
        validate_pgm(pgm)
        return cast("tuple[NativeTextRegion, ...]", await self._run(
            pgm, source_sha256=source_sha256, max_rows=256, max_regions=64,
            whitelist=Whitelist.CAPTION if captions else None, status_sheet=True,
        ))

    async def _run(
        self, pgm: bytes, *, whitelist: Whitelist | None = None,
        source_sha256: str | None = None, max_rows: int = 0, max_regions: int = 0,
        batch: tuple[bytes, ...] | None = None,
        status_sheet: bool = False,
    ) -> TesseractResult | tuple[NativeTextRegion, ...] | tuple[TesseractResult | None, ...]:
        if self._closed:
            raise TesseractUnavailable("recognizer_closed")
        if self._reservation is not None:
            raise TesseractUnavailable("recognizer_busy")
        if self._closing is not None and not self._closing.done():
            raise TesseractUnavailable("previous_child_closing")
        if self._child is not None:
            raise TesseractUnavailable("child_already_owned")
        if self._scratch is not None:
            raise TesseractUnavailable("previous_scratch_cleanup_unconfirmed")
        reservation = object()
        self._reservation = reservation
        try:
            return await self._recognize_reserved(
                pgm, whitelist, reservation, source_sha256, max_rows, max_regions, batch,
                status_sheet,
            )
        finally:
            if (
                self._reservation is reservation
                and self._child is None
                and (self._closing is None or self._closing.done())
            ):
                self._cleanup_scratch()
                self._reservation = None

    async def _recognize_reserved(
        self, pgm: bytes, whitelist: Whitelist | None, reservation: object,
        source_sha256: str | None, max_rows: int, max_regions: int,
        batch: tuple[bytes, ...] | None, status_sheet: bool,
    ) -> TesseractResult | tuple[NativeTextRegion, ...] | tuple[TesseractResult | None, ...]:
        loop = asyncio.get_running_loop()
        scene = source_sha256 is not None
        deadline = loop.time() + (self._scene_deadline if scene else self._deadline)
        dimensions = tuple(validate_pgm(item)[:2] for item in batch) if batch else ()
        width, height = (dimensions[0] if batch else validate_pgm(pgm, scene=scene)[:2])
        if not scene and not isinstance(whitelist, Whitelist):
            raise TesseractUnavailable("whitelist_not_admitted")
        executable, model = (
            self._artifact.executable,
            self._artifact.tessdata_directory / "eng.traineddata",
        )
        try:
            preflight = asyncio.create_task(asyncio.to_thread(_hash_artifacts, executable, model))
            self._preflight_task = preflight
            try:
                engine_hash, model_hash = await asyncio.wait_for(
                    asyncio.shield(preflight), max(0.0, deadline - loop.time())
                )
            except asyncio.CancelledError as cancelled:
                self._fenced_reservation = reservation
                self._closing = asyncio.create_task(self._settle_preflight(preflight, reservation))
                await self._wait_for_close_briefly()
                raise cancelled
            except TimeoutError as exc:
                self._fenced_reservation = reservation
                self._closing = asyncio.create_task(self._settle_preflight(preflight, reservation))
                await self._wait_for_close_briefly()
                raise TesseractUnavailable("artifact_hash_timeout") from exc
            finally:
                if preflight.done() and self._preflight_task is preflight:
                    self._preflight_task = None
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                self._fenced_reservation = reservation
                self._closing = asyncio.create_task(self._settle_preflight(preflight, reservation))
                await self._wait_for_close_briefly()
                raise asyncio.CancelledError
            if self._reservation is not reservation or self._fenced_reservation is reservation:
                raise TesseractUnavailable("recognition_cancelled_before_spawn")
            if (
                engine_hash != self._artifact.executable_sha256
                or model_hash != self._artifact.model_sha256
            ):
                raise TesseractUnavailable("artifact_hash_mismatch")
            if not self._admission.admit(self._artifact, engine_hash, model_hash):
                raise TesseractUnavailable("artifact_or_self_test_not_admitted")
        except OSError as exc:
            raise TesseractUnavailable("artifact_unreadable") from exc
        if batch is not None:
            try:
                self._scratch = tempfile.TemporaryDirectory(prefix="sc-tesseract-")
                directory = Path(self._scratch.name)
                paths = tuple(directory / f"page-{i:02d}.pgm" for i in range(len(batch)))
                for path, content in zip(paths, batch, strict=True):
                    path.write_bytes(content)
                listing = directory / "images.txt"
                listing.write_text("".join(str(path) + "\n" for path in paths), encoding="utf-8")
            except OSError as exc:
                raise TesseractUnavailable("temporary_storage_unavailable") from exc
        args = (
            str(executable),
            str(listing) if batch is not None else "stdin",
            "stdout",
            "--tessdata-dir",
            str(self._artifact.tessdata_directory),
            "-l",
            "eng",
            "--oem",
            "1",
            "--psm",
            "6" if status_sheet else ("11" if scene else "7"),
            *(("-c", f"tessedit_char_whitelist={whitelist.value}")
              if whitelist is not None else ()),
            "-c", "tessedit_create_tsv=1",
        )
        child_env = {"PATH": str(executable.parent)}
        if sys.platform == "win32":
            system_root = os.environ.get("SystemRoot")
            if not system_root:
                raise TesseractUnavailable("system_root_unavailable")
            child_env["SystemRoot"] = system_root
            # Leptonica's Windows memory reader uses a temporary file. Without
            # TEMP it falls back to the usually unwritable Windows directory.
            if self._scratch is None:
                try:
                    self._scratch = tempfile.TemporaryDirectory(prefix="sc-tesseract-")
                except OSError as exc:
                    raise TesseractUnavailable("temporary_storage_unavailable") from exc
            child_env["TEMP"] = child_env["TMP"] = self._scratch.name
        if loop.time() >= deadline or self._closed:
            raise TesseractUnavailable("recognition_expired_before_spawn")
        spawn_task = asyncio.ensure_future(
            self._spawn(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._artifact.tessdata_directory),
                env=child_env,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
        )
        self._spawn_task = spawn_task
        try:
            child = cast(_Child, await asyncio.wait_for(
                asyncio.shield(spawn_task), max(0.0, deadline - loop.time()),
            ))
        except asyncio.CancelledError as cancelled:
            self._fenced_reservation = reservation
            self._closing = asyncio.create_task(
                self._settle_cancelled_spawn(spawn_task, reservation)
            )
            await self._wait_for_close_briefly()
            raise cancelled
        except TimeoutError as exc:
            self._fenced_reservation = reservation
            self._closing = asyncio.create_task(
                self._settle_cancelled_spawn(spawn_task, reservation)
            )
            await self._wait_for_close_briefly()
            raise TesseractUnavailable("child_start_timeout") from exc
        except (OSError, ValueError) as exc:
            self._spawn_task = None
            raise TesseractUnavailable("child_start_failed") from exc
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            # wait_for may return a just-completed result while its caller is
            # being cancelled. Retain and reap the acquired handle regardless.
            self._fenced_reservation = reservation
            self._closing = asyncio.create_task(
                self._settle_cancelled_spawn(spawn_task, reservation)
            )
            await self._wait_for_close_briefly()
            raise asyncio.CancelledError
        self._spawn_task = None
        if self._reservation is not reservation or self._fenced_reservation is reservation:
            await self._wait_for_close_briefly()
            raise TesseractUnavailable("recognition_cancelled_before_input")
        self._child = child
        self._communication = asyncio.create_task(
            self._communicate_bounded(child, pgm,
                                      SCENE_STDOUT_LIMIT if scene or batch else STDOUT_LIMIT)
        )
        try:
            output, _stderr = await asyncio.wait_for(
                asyncio.shield(self._communication), max(0.0, deadline - loop.time())
            )
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise asyncio.CancelledError
            if self._closed or self._fenced_reservation is reservation:
                raise TesseractUnavailable("recognition_closed_during_work")
            if child.returncode != 0:
                raise TesseractUnavailable("tesseract_nonzero_exit")
            if scene:
                from adapters.perception.native_regions import decode_native_tsv_regions

                result = decode_native_tsv_regions(
                    output, source_sha256=cast(str, source_sha256), width=width, height=height,
                    max_bytes=SCENE_STDOUT_LIMIT, max_rows=max_rows, max_regions=max_regions,
                )
                if loop.time() >= deadline:
                    raise TesseractUnavailable("tesseract_timeout")
            elif batch is not None:
                identity = ExtractionIdentity("tesseract", PARSER_VERSION,
                    engine_sha256=engine_hash, model_sha256=model_hash)
                result = _parse_batch_tsv(output, dimensions, identity,
                                          self._artifact.engine_version)
                if loop.time() >= deadline:
                    raise TesseractUnavailable("tesseract_timeout")
            else:
                words = parse_tsv(output, width, height)
                result = TesseractResult(
                    " ".join(w.text for w in words), words, engine_hash, model_hash,
                    self._artifact.engine_version,
                    ExtractionIdentity("tesseract", PARSER_VERSION, engine_sha256=engine_hash,
                                       model_sha256=model_hash),
                )
            self._child = None
            self._readers = None
            self._wait_task = None
            self._communication = None
            return result
        except asyncio.CancelledError:
            await self._cancel_owned_child()
            raise
        except TimeoutError as exc:
            await self._cancel_owned_child()
            raise TesseractUnavailable("tesseract_timeout") from exc
        except (TesseractUnavailable, ValueError):
            await self._cancel_owned_child()
            raise
        except Exception as exc:
            await self._cancel_owned_child()
            raise TesseractUnavailable("tesseract_pipe_failed") from exc

    async def _settle_cancelled_spawn(
        self, spawn_task: asyncio.Future[Any], reservation: object
    ) -> None:
        try:
            try:
                child = cast(_Child, await asyncio.shield(spawn_task))
            except BaseException:
                return
            self._child = child
            self._wait_task = asyncio.create_task(child.wait())
            self._readers = (
                asyncio.create_task(_read_capped(child.stdout, STDOUT_LIMIT)),
                asyncio.create_task(_read_capped(child.stderr, STDERR_LIMIT)),
            )
            await self._close_child(child)
        finally:
            if self._spawn_task is spawn_task:
                self._spawn_task = None
            if self._fenced_reservation is reservation:
                self._fenced_reservation = None
            if self._reservation is reservation and self._child is None:
                self._cleanup_scratch()
                self._reservation = None

    async def _settle_preflight(
        self, preflight: asyncio.Task[tuple[str, str]], reservation: object
    ) -> None:
        try:
            try:
                await asyncio.shield(preflight)
            except BaseException:
                pass
        finally:
            if self._preflight_task is preflight:
                self._preflight_task = None
            if self._fenced_reservation is reservation:
                self._fenced_reservation = None
            if self._reservation is reservation and self._child is None:
                self._reservation = None

    async def _wait_for_close_briefly(self) -> None:
        closing = self._closing
        if closing is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(closing), 0.25)
        except TimeoutError:
            pass

    async def _communicate_bounded(
        self, child: _Child, pgm: bytes, stdout_limit: int
    ) -> tuple[bytes, bytes]:
        out_task = asyncio.create_task(_read_capped(child.stdout, stdout_limit))
        err_task = asyncio.create_task(_read_capped(child.stderr, STDERR_LIMIT))
        self._readers = out_task, err_task
        self._wait_task = asyncio.create_task(child.wait())
        stdin = child.stdin
        stdin.write(pgm)
        await stdin.drain()
        stdin.close()
        await asyncio.gather(self._wait_task, out_task, err_task)
        return out_task.result(), err_task.result()

    async def _cancel_owned_child(self) -> None:
        self._fenced_reservation = self._reservation
        child = self._child
        if child is None:
            return
        if self._closing is None or self._closing.done():
            self._closing = asyncio.create_task(self._close_child(child))
        await self._wait_for_close_briefly()

    async def _close_child(self, child: _Child) -> None:
        try:
            if child.returncode is None:
                child.kill()
            wait_task = self._wait_task
            if wait_task is None or wait_task.cancelled():
                wait_task = asyncio.create_task(child.wait())
                self._wait_task = wait_task
            await asyncio.shield(wait_task)
            communication = self._communication
            if communication is not None:
                try:
                    await asyncio.shield(communication)
                except (TesseractUnavailable, OSError):
                    pass
            if self._readers is not None:
                await asyncio.gather(*self._readers, return_exceptions=True)
            for pipe in (child.stdin, child.stdout, child.stderr):
                close = getattr(pipe, "close", None)
                if callable(close):
                    close()
            if self._child is child:
                self._child = None
            self._wait_task = None
            self._readers = None
            self._communication = None
            if self._fenced_reservation is self._reservation:
                self._fenced_reservation = None
            self._reservation = None
            self._cleanup_scratch()
        except OSError as exc:
            raise TesseractUnavailable("child_closure_unconfirmed") from exc

    def _cleanup_scratch(self) -> None:
        scratch = self._scratch
        if scratch is not None:
            scratch.cleanup()
            self._scratch = None

    async def aclose(self) -> None:
        self._closed = True
        reservation = self._reservation
        if self._child is not None:
            self._fenced_reservation = reservation
            await self._cancel_owned_child()
            return
        if reservation is None:
            self._cleanup_scratch()
            return
        self._fenced_reservation = reservation
        if self._spawn_task is not None:
            if self._closing is None or self._closing.done():
                self._closing = asyncio.create_task(
                    self._settle_cancelled_spawn(self._spawn_task, reservation)
                )
        elif self._preflight_task is not None and not self._preflight_task.done():
            if self._closing is None or self._closing.done():
                self._closing = asyncio.create_task(
                    self._settle_preflight(self._preflight_task, reservation)
                )
        await self._wait_for_close_briefly()

        if (self._child is None and (self._closing is None or self._closing.done())
                and (self._spawn_task is None or self._spawn_task.done())
                and (self._preflight_task is None or self._preflight_task.done())):
            self._cleanup_scratch()
            self._reservation = None


async def _read_capped(pipe: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    reader = pipe
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            return bytes(output)
        if len(output) + len(chunk) > limit:
            raise TesseractUnavailable("tesseract_output_limit")
        output.extend(chunk)


def _hash_artifacts(executable: Path, model: Path) -> tuple[str, str]:
    return (
        _sha256_file(executable, EXECUTABLE_BYTES_LIMIT),
        _sha256_file(model, MODEL_BYTES_LIMIT),
    )


def _sha256_file(path: Path, max_bytes: int) -> str:
    digest = hashlib.sha256()
    before = path.stat()
    if not path.is_file() or before.st_size < 0 or before.st_size > max_bytes:
        raise TesseractUnavailable("artifact_size_invalid")
    total = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _stat_identity(before) != _stat_identity(opened):
            raise TesseractUnavailable("artifact_changed_before_hash")
        while True:
            chunk = stream.read(min(65_536, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise TesseractUnavailable("artifact_size_invalid")
            digest.update(chunk)
        after_open = os.fstat(stream.fileno())
    after_path = path.stat()
    if (
        total != before.st_size
        or _stat_identity(before) != _stat_identity(after_open)
        or _stat_identity(before) != _stat_identity(after_path)
    ):
        raise TesseractUnavailable("artifact_changed_during_hash")
    return digest.hexdigest()


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _is_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
