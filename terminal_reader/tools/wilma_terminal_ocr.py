"""Standalone, receipt-pinned local OCR server for Wilma terminal warnings.

This exposes only the OpenAI-compatible image-reading surface Wilma already
uses. It accepts a single JPEG data URL, runs the existing native terminal
reader, and returns its existing conservative report. It does not send input
to Star Citizen or log prompts or media.
"""

# ruff: noqa: E402 -- Direct-file invocation needs the repository root first.

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hmac
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from io import BytesIO
from itertools import combinations
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adapters.perception.image_bounds import MAX_SUPPORTED_CAPTURE_PIXELS
from adapters.perception.tesseract import TesseractRecognizer, TesseractUnavailable
from adapters.perception.tesseract_artifacts import TesseractArtifactAdmission
from adapters.perception.wilma_personal_ocr import read_personal_banner
from adapters.perception.wilma_terminal_ocr import read_native_frame
from integrations.wingman_wilma.prompt import build_prompt
from integrations.wingman_wilma.terminal_panel import native_reactor_reply
from PIL import Image, UnidentifiedImageError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 49154
DEFAULT_MODEL = "wilma-terminal-ocr"
MAX_REQUEST_BYTES = 8_388_608
MAX_JSON_DEPTH = 32
REQUEST_TIMEOUT_SECONDS = 3.0
UNKNOWN_REPLY = '{"scene":"unknown","readings":[]}'
_JPEG_PREFIX = "data:image/jpeg;base64,"
_PERSONAL_PROMPT = "read only the player's own visible personal status"
_PERSONAL_METRICS = ("suit_oxygen", "hydration", "nutrition")
# The skill sends these only for focused on-foot HUD reading. Several banner
# categories share a transcription prompt; Wilma enforces the selected warnings.
_BANNER_PROMPTS = frozenset(
    build_prompt(("incapacitation", *metrics))
    for count in range(len(_PERSONAL_METRICS) + 1)
    for metrics in combinations(_PERSONAL_METRICS, count)
)
_INCAPACITATED_REPLY = '{"scene":"fps","readings":[],"hud_alerts":["You Are Incapacitated"]}'
_BEARER = re.compile(r"Bearer ([\x21-\x7e]{1,512})\Z", re.ASCII)


class _RequestError(ValueError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _depth(value: Any, level: int = 0) -> None:
    if level > MAX_JSON_DEPTH:
        raise ValueError("JSON nesting too deep")
    if isinstance(value, dict):
        for child in value.values():
            _depth(child, level + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, level + 1)


async def _read_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > MAX_REQUEST_BYTES):
        raise _RequestError(413, "request_too_large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise _RequestError(413, "request_too_large")
        body.extend(chunk)
    if not body:
        raise _RequestError(400, "request_body_required")
    return bytes(body)


def _parse_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_json_pairs, parse_constant=_no_constant)
        _depth(value)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _RequestError(400, "invalid_json") from None
    if type(value) is not dict:
        raise _RequestError(400, "invalid_request")
    return value


def _user_parts(payload: dict[str, Any], model: str) -> tuple[str, str]:
    if payload.get("model") != model or payload.get("stream") is not False:
        raise _RequestError(400, "unsupported_request")
    messages = payload.get("messages")
    if type(messages) is not list or len(messages) != 1 or type(messages[0]) is not dict:
        raise _RequestError(400, "invalid_messages")
    message = messages[0]
    if message.get("role") != "user" or type(message.get("content")) is not list:
        raise _RequestError(400, "invalid_messages")
    parts = message["content"]
    if len(parts) != 2:
        raise _RequestError(400, "invalid_content")
    prompt: str | None = None
    image: str | None = None
    for part in parts:
        if type(part) is not dict:
            raise _RequestError(400, "invalid_content")
        if part.get("type") == "text" and prompt is None and type(part.get("text")) is str:
            prompt = part["text"]
        elif part.get("type") == "image_url" and image is None:
            image_url = part.get("image_url")
            if type(image_url) is dict:
                image = image_url.get("url")
        else:
            raise _RequestError(400, "invalid_content")
    if (prompt is None or not prompt.strip() or len(prompt) > 16_384
            or image is None or type(image) is not str):
        raise _RequestError(400, "invalid_content")
    return prompt, image


def _decode_jpeg(value: str) -> Image.Image:
    if not value.startswith(_JPEG_PREFIX) or len(value) > MAX_REQUEST_BYTES * 2:
        raise _RequestError(400, "jpeg_required")
    encoded = value[len(_JPEG_PREFIX):]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise _RequestError(400, "jpeg_invalid") from None
    if not raw.startswith(b"\xff\xd8\xff") or base64.b64encode(raw).decode("ascii") != encoded:
        raise _RequestError(400, "jpeg_invalid")
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format != "JPEG":
                raise _RequestError(400, "jpeg_required")
            width, height = source.size
            if (not 1 <= width <= 8192 or not 1 <= height <= 8192
                    or width * height > MAX_SUPPORTED_CAPTURE_PIXELS):
                raise _RequestError(413, "image_pixel_limit")
            source.load()
            image = source.convert("RGB")
            image.load()
            return image
    except _RequestError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise _RequestError(400, "jpeg_invalid") from None


def _completion(content: str, model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def create_app(
    recognizer: TesseractRecognizer,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    personal_incapacitation: bool = False,
) -> Starlette:
    """Create the ASGI app around an already receipt-admitted recognizer.

    The app owns ``recognizer`` and closes it through its normal ASGI lifespan.
    If ``api_key`` is configured every endpoint requires its bearer token.
    """
    if not isinstance(recognizer, TesseractRecognizer):
        raise TypeError("recognizer must be a TesseractRecognizer")
    if (type(model) is not str or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", model)
            or (api_key is not None and (
                type(api_key) is not str or not 1 <= len(api_key) <= 512
                or not api_key.isascii()
                or any(ord(char) < 33 or ord(char) > 126 for char in api_key)
            ))
            or type(personal_incapacitation) is not bool
            or type(request_timeout_seconds) not in (int, float)
            or not 0 < request_timeout_seconds <= 30):
        raise ValueError("invalid server configuration")
    serial = asyncio.Lock()

    async def authorize(request: Request) -> Response | None:
        if api_key is None:
            return None
        match = _BEARER.fullmatch(request.headers.get("authorization", ""))
        if match is None or not hmac.compare_digest(match.group(1), api_key):
            return JSONResponse({"error": {"message": "authentication required",
                                             "type": "authentication_error",
                                             "code": "unauthorized"}}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return None

    async def models(request: Request) -> Response:
        denied = await authorize(request)
        if denied is not None:
            return denied
        return JSONResponse({"object": "list", "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "local"}
        ]})

    async def chat(request: Request) -> Response:
        denied = await authorize(request)
        if denied is not None:
            return denied
        if serial.locked():
            return JSONResponse({"error": {"message": "reader busy", "type": "server_error",
                                             "code": "reader_busy"}}, status_code=503,
                                headers={"Retry-After": "1"})
        await serial.acquire()
        try:
            async with asyncio.timeout(request_timeout_seconds):
                payload = _parse_json(await _read_body(request))
                prompt, encoded_image = _user_parts(payload, model)
                banner_prompt = prompt in _BANNER_PROMPTS
                personal = banner_prompt or _PERSONAL_PROMPT in prompt.casefold()
                if personal and not (personal_incapacitation and banner_prompt):
                    return JSONResponse(_completion(UNKNOWN_REPLY, model))
                image = _decode_jpeg(encoded_image)
                try:
                    if personal:
                        active = await read_personal_banner(image, recognizer)
                        reply = _INCAPACITATED_REPLY if active else UNKNOWN_REPLY
                    else:
                        reading = await read_native_frame(image, recognizer, native_band=True)
                        reply = native_reactor_reply(reading)
                finally:
                    image.close()
                return JSONResponse(_completion(reply, model))
        except _RequestError as exc:
            return JSONResponse({"error": {"message": exc.code, "type": "invalid_request_error",
                                             "code": exc.code}}, status_code=exc.status)
        except TimeoutError:
            return JSONResponse({"error": {"message": "request deadline exceeded",
                                             "type": "server_error", "code": "deadline_exceeded"}},
                                status_code=504)
        except (TesseractUnavailable, OSError, ValueError):
            return JSONResponse({"error": {"message": "reader unavailable",
                                             "type": "server_error", "code": "reader_unavailable"}},
                                status_code=503)
        finally:
            serial.release()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        try:
            yield
        finally:
            await recognizer.aclose()

    return Starlette(
        routes=[Route("/v1/models", models, methods=["GET"]),
                Route("/v1/chat/completions", chat, methods=["POST"])],
        lifespan=lifespan,
    )


def _loopback(host: str) -> bool:
    return host.casefold() in {"127.0.0.1", "localhost", "::1"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tesseract-executable", type=Path, required=True)
    parser.add_argument("--tessdata-directory", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True,
                        help="externally pinned native OCR receipt")
    parser.add_argument("--receipt-sha256", required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allow-lan", action="store_true",
                        help="allow non-loopback HTTP with bearer auth (HTTP is unencrypted)")
    parser.add_argument("--api-key-env", metavar="NAME",
                        help="environment variable containing the bearer key")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--personal-incapacitation", action="store_true",
                        help="also read the incapacitation banner on focused personal HUD requests")
    parser.add_argument("--request-timeout", type=float, default=REQUEST_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535 or not 0 < args.request_timeout <= 30:
        raise SystemExit("port and timeout are outside their allowed ranges")
    if args.allow_lan and not args.api_key_env:
        raise SystemExit("--allow-lan requires --api-key-env")
    if not _loopback(args.host) and (not args.allow_lan or not args.api_key_env):
        raise SystemExit("non-loopback binds require --allow-lan and --api-key-env")
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)
        if (api_key is None or not 1 <= len(api_key) <= 512 or not api_key.isascii()
                or any(ord(char) < 33 or ord(char) > 126 for char in api_key)):
            raise SystemExit("the named API key environment variable is unavailable or invalid")
    else:
        api_key = None
    try:
        admission = TesseractArtifactAdmission(
            args.tesseract_executable.resolve(strict=True),
            args.tessdata_directory.resolve(strict=True),
            args.receipt.resolve(strict=True), args.receipt_sha256,
        )
        artifact = admission.artifact()
        recognizer = TesseractRecognizer(artifact, admission)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"selected native OCR artifact is unavailable: {exc}") from None
    app = create_app(recognizer, model=args.model, api_key=api_key,
                     personal_incapacitation=args.personal_incapacitation,
                     request_timeout_seconds=args.request_timeout)
    import uvicorn

    # Access logging is deliberately off; it is not needed for this private reader.
    config = uvicorn.Config(app, host=args.host, port=args.port, access_log=False,
                            log_level="warning")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
