"""Optional direct OpenAI-compatible image reader; never executes actions."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 8_388_608
MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_RESPONSE_BYTES = MAX_RESPONSE_BYTES
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class ReaderUnavailable(Exception):
    """Public string is static; optional metadata is for private diagnostics."""

    def __init__(
        self,
        reason,
        *,
        http_status=None,
        request_id=None,
        provider_code=None,
        provider_message=None,
        context_exceeded=False,
    ):
        super().__init__(reason)
        self.http_status = http_status
        self.request_id = request_id
        self.provider_code = provider_code
        self.provider_message = provider_message
        self.context_exceeded = context_exceeded

    def __str__(self):
        reason = super().__str__()
        return f"{reason} (HTTP {self.http_status})" if self.http_status is not None else reason


def _safe_metadata(value, *, limit, secret=None, code=False):
    if type(value) is int and code:
        value = str(value)
    if type(value) is not str:
        return None
    if secret:
        value = value.replace(secret, "[redacted]")
    value = value[:limit]
    value = re.sub(r"(?i)\b(?:https?://|www\.)\S+", "[url]", value)
    value = "".join(c if 32 <= ord(c) < 127 else " " for c in value).strip()
    if code and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return None
    return value or None


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _bounded_length(value, limit):
    return min(len(value), limit) if type(value) is str else None


def _output_shape(message, *, limit):
    content = message.get("content") if type(message) is dict else None
    reasoning = message.get("reasoning") if type(message) is dict else None
    reasoning_content = message.get("reasoning_content") if type(message) is dict else None
    shape = {
        "content_chars": _bounded_length(content, limit),
        "trailing_whitespace_chars": (
            min(len(content) - len(content.rstrip()), limit)
            if type(content) is str else None
        ),
        "reasoning_chars": _bounded_length(reasoning, limit),
        "reasoning_content_chars": _bounded_length(reasoning_content, limit),
        "complete_json": False,
        "readings_count": None,
    }
    if type(content) is not str:
        return shape
    try:
        payload = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (ValueError, RecursionError):
        return shape
    shape["complete_json"] = True
    if type(payload) is dict and type(payload.get("readings")) is list:
        shape["readings_count"] = min(len(payload["readings"]), limit)
    return shape


@dataclass(frozen=True, slots=True)
class ReaderSettings:
    provider: str = "wingman"
    model: str = ""
    endpoint: str = ""
    structured_output: bool = False
    disable_reasoning: bool = False

    def __post_init__(self):
        if type(self.provider) is not str or self.provider not in {
            "wingman",
            "openrouter",
            "ollama",
            "local",
            "waterfall",
        }:
            raise ValueError("unsupported reader provider")
        if type(self.structured_output) is not bool or type(self.disable_reasoning) is not bool:
            raise ValueError("reader options must be boolean")
        if type(self.model) is not str or type(self.endpoint) is not str:
            raise ValueError("reader settings must be text")
        if self.provider == "wingman":
            if self.model or self.endpoint or self.structured_output or self.disable_reasoning:
                raise ValueError("wingman reader does not accept direct settings")
            return
        if not self.model.strip() or self.model != self.model.strip() or len(self.model) > 256:
            raise ValueError("reader model is required")
        if any(ord(c) < 32 or ord(c) == 127 for c in self.model):
            raise ValueError("invalid reader model")
        if self.provider == "openrouter":
            if self.endpoint:
                raise ValueError("openrouter endpoint is fixed")
            return
        value = self.endpoint
        if (
            not value
            or len(value) > 2048
            or value != value.strip()
            or any(ord(c) < 33 or ord(c) == 127 for c in value)
        ):
            raise ValueError("invalid local endpoint")
        try:
            parsed = urlsplit(value)
            port = parsed.port
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or (port is not None and port == 0)
                or not (
                    parsed.path.rstrip("/").endswith("/v1")
                    or parsed.path.rstrip("/").endswith("/chat/completions")
                )
            ):
                raise ValueError("invalid local endpoint")
            if self.provider == "waterfall" and (
                parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.path.rstrip("/") not in {"/v1", "/v1/vision/chat/completions"}
            ):
                raise ValueError("Waterfall requires its same-machine vision endpoint")
        except ValueError:
            raise ValueError("invalid local endpoint") from None


def _image_url(image: str, limit: int) -> str:
    if type(image) is not str:
        raise ReaderUnavailable("invalid_image")
    if len(image) > limit:
        raise ReaderUnavailable("request_too_large")
    prefixes = {
        "data:image/jpeg;base64,": b"\xff\xd8\xff",
        "data:image/png;base64,": b"\x89PNG\r\n\x1a\n",
    }
    for prefix, magic in prefixes.items():
        if image.startswith(prefix):
            data = image[len(prefix) :]
            try:
                raw = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                break
            if raw.startswith(magic) and base64.b64encode(raw).decode("ascii") == data:
                return image
            break
    raise ReaderUnavailable("invalid_image")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _ollama_context_exceeded(error):
    """Recognize a typed context-limit response, including a wrapped runner error."""
    if not isinstance(error, dict):
        return False
    if error.get("type") == "exceed_context_size_error" or error.get("code") == "context_length_exceeded":
        return True
    # Some Ollama versions return the runner's JSON as the outer error message.
    message = error.get("message")
    if not isinstance(message, str) or not message.lstrip().startswith("{"):
        return False
    try:
        wrapped = json.loads(message, object_pairs_hook=_pairs)
    except (ValueError, RecursionError):
        return False
    inner = wrapped.get("error") if isinstance(wrapped, dict) else None
    return isinstance(inner, dict) and inner.get("type") == "exceed_context_size_error"


async def read_image(
    settings: ReaderSettings,
    prompt: str,
    image: str,
    *,
    api_key=None,
    client_factory=None,
    timeout_seconds=30.0,
    deadline_monotonic_seconds=None,
    max_request_bytes=MAX_REQUEST_BYTES,
    max_response_bytes=DEFAULT_RESPONSE_BYTES,
    diagnostics=None,
) -> str:
    """Read one image. HTTP local endpoints may expose images and keys on the LAN."""
    if type(diagnostics) is dict:
        diagnostics.clear()
    if not isinstance(settings, ReaderSettings) or settings.provider == "wingman":
        raise ReaderUnavailable("route_unavailable")
    if (
        type(prompt) is not str
        or not prompt.strip()
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or (deadline_monotonic_seconds is not None and (
            type(deadline_monotonic_seconds) not in (int, float)
            or not math.isfinite(deadline_monotonic_seconds)
        ))
        or type(max_request_bytes) is not int
        or max_request_bytes < 1
        or type(max_response_bytes) is not int
        or max_response_bytes < 1
    ):
        raise ReaderUnavailable("invalid_request")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    if deadline_monotonic_seconds is not None:
        deadline = min(deadline, deadline_monotonic_seconds)
    if loop.time() >= deadline:
        raise ReaderUnavailable("deadline_exceeded")
    if len(prompt) > max_request_bytes:
        raise ReaderUnavailable("request_too_large")
    if api_key is not None and (
        type(api_key) is not str
        or not api_key
        or not api_key.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in api_key)
    ):
        raise ReaderUnavailable("invalid_credential")
    if settings.provider in {"openrouter", "waterfall"} and api_key is None:
        raise ReaderUnavailable("credential_unavailable")
    url = (
        OPENROUTER_URL
        if settings.provider == "openrouter"
        else (
            settings.endpoint.rstrip("/")
            if settings.endpoint.rstrip("/").endswith("/chat/completions")
            else settings.endpoint.rstrip("/") + (
                "/vision/chat/completions" if settings.provider == "waterfall"
                else "/chat/completions"
            )
        )
    )
    text_part = {"type": "text", "text": prompt}
    image_part = {"type": "image_url", "image_url": {"url": _image_url(image, max_request_bytes)}}
    content = [text_part, image_part] if settings.provider == "openrouter" else [
        image_part, text_part
    ]
    body = {
        "model": settings.model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 512,
        "stream": False,
    }
    if settings.provider in {"local", "ollama", "waterfall"}:
        body["temperature"] = 0
    if settings.disable_reasoning:
        if settings.provider == "openrouter":
            body["reasoning"] = {"effort": "none"}
        elif settings.provider == "ollama":
            body["reasoning_effort"] = "none"
        else:
            body["chat_template_kwargs"] = {"enable_thinking": False}
    if settings.structured_output:
        body["response_format"] = {"type": "json_object"}
        if settings.provider == "openrouter":
            body["provider"] = {"require_parameters": True}
    encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > max_request_bytes:
        raise ReaderUnavailable("request_too_large")
    headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
    if api_key is not None:
        headers["Authorization"] = "Bearer " + api_key
    if settings.provider == "waterfall":
        headers["x-wingman-waterfall-frame-deadline-ns"] = str(
            time.monotonic_ns() + int((deadline - loop.time()) * 1_000_000_000)
        )
    if loop.time() >= deadline:
        raise ReaderUnavailable("deadline_exceeded")
    import httpx

    factory = client_factory or httpx.AsyncClient
    try:
        async with asyncio.timeout_at(deadline):
            async with factory(trust_env=False, follow_redirects=False, timeout=None) as client:
                async with client.stream("POST", url, content=encoded, headers=headers) as response:
                    request_id = _safe_metadata(
                        response.headers.get("x-request-id") or response.headers.get("request-id")
                        or (response.headers.get("x-wingman-waterfall-request-id")
                            if settings.provider == "waterfall" else None),
                        limit=128,
                        secret=api_key,
                        code=True,
                    )
                    if type(diagnostics) is dict:
                        diagnostics.update(
                            {"http_status": response.status_code, "request_id": request_id}
                        )
                    if not 200 <= response.status_code < 300:
                        status = response.status_code
                        request_id = _safe_metadata(
                            response.headers.get("x-request-id")
                            or response.headers.get("request-id"),
                            limit=128,
                            secret=api_key,
                            code=True,
                        )
                        provider_code = provider_message = None
                        context_exceeded = False
                        try:
                            encoding = response.headers.get("content-encoding", "identity").lower()
                            length = response.headers.get("content-length")
                            if encoding not in {"", "identity"} or (
                                length is not None
                                and (not length.isdecimal() or int(length) > max_response_bytes)
                            ):
                                raise ValueError
                            error_body = bytearray()
                            async for chunk in response.aiter_raw():
                                if len(error_body) + len(chunk) > max_response_bytes:
                                    raise ValueError
                                error_body.extend(chunk)
                            error_data = json.loads(error_body, object_pairs_hook=_pairs)
                            error = error_data.get("error") if type(error_data) is dict else None
                            if type(error) is dict:
                                if settings.provider == "ollama" and status == 400:
                                    context_exceeded = _ollama_context_exceeded(error)
                                provider_code = _safe_metadata(
                                    error.get("code"), limit=128, secret=api_key, code=True
                                )
                                provider_message = _safe_metadata(
                                    error.get("message"), limit=512, secret=api_key
                                )
                        except Exception:
                            pass
                        raise ReaderUnavailable(
                            "http_error",
                            http_status=status,
                            request_id=request_id,
                            provider_code=provider_code,
                            provider_message=provider_message,
                            context_exceeded=context_exceeded,
                        )
                    if settings.provider == "waterfall":
                        if response.headers.get("x-wingman-waterfall-frame-deadline") != "applied":
                            raise ReaderUnavailable("waterfall_frame_deadline_unsupported")
                        route = _safe_metadata(
                            response.headers.get("x-wingman-waterfall-route"),
                            limit=128, secret=api_key, code=True,
                        )
                        if not route:
                            raise ReaderUnavailable("waterfall_route_unavailable")
                        if type(diagnostics) is dict:
                            diagnostics["waterfall_route"] = route
                    if response.headers.get("content-encoding", "identity").lower() not in {
                        "",
                        "identity",
                    }:
                        raise ReaderUnavailable("response_encoding_unsupported")
                    length = response.headers.get("content-length")
                    if length is not None and (
                        not length.isdecimal() or int(length) > max_response_bytes
                    ):
                        raise ReaderUnavailable("response_too_large")
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw) + len(chunk) > max_response_bytes:
                            raise ReaderUnavailable("response_too_large")
                        raw.extend(chunk)
                    if api_key and api_key.encode("ascii") in raw:
                        raise ReaderUnavailable("credential_reflected")
                    data = json.loads(
                        raw,
                        object_pairs_hook=_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                    )
                    if type(data) is not dict or "error" in data:
                        raise ReaderUnavailable("invalid_response")
                    choices = data.get("choices")
                    if type(diagnostics) is dict:
                        model = _safe_metadata(
                            data.get("model"), limit=128, secret=api_key, code=True
                        )
                        usage = data.get("usage")
                        safe_usage = {}
                        if type(usage) is dict:
                            for name in (
                                "prompt_tokens",
                                "completion_tokens",
                                "total_tokens",
                                "cost",
                            ):
                                value = usage.get(name)
                                if (
                                    type(value) in (int, float)
                                    and math.isfinite(value)
                                    and value >= 0
                                ):
                                    safe_usage[name] = value
                        diagnostics.update({"model": model, "usage": safe_usage})
                    if (
                        type(choices) is not list
                        or len(choices) != 1
                        or type(choices[0]) is not dict
                    ):
                        raise ReaderUnavailable("invalid_response")
                    choice = choices[0]
                    finish = choice.get("finish_reason")
                    if type(diagnostics) is dict:
                        diagnostics["finish_reason"] = _safe_metadata(
                            finish, limit=128, secret=api_key, code=True
                        )
                    message = choice.get("message")
                    content = message.get("content") if type(message) is dict else None
                    if type(content) is str and api_key and api_key in content:
                        raise ReaderUnavailable("credential_reflected")
                    if type(diagnostics) is dict:
                        diagnostics["output_shape"] = _output_shape(
                            message, limit=max_response_bytes
                        )
                    if finish == "length":
                        raise ReaderUnavailable("output_truncated")
                    if (
                        type(message) is dict
                        and not message.get("content")
                        and message.get("reasoning")
                    ):
                        raise ReaderUnavailable("reasoning_only")
                    if type(message) is dict and message.get("content") in (None, ""):
                        raise ReaderUnavailable("empty_content")
                    if (
                        finish != "stop"
                        or type(message) is not dict
                        or message.get("tool_calls")
                        or message.get("function_call")
                        or type(message.get("content")) is not str
                        or not message["content"].strip()
                    ):
                        raise ReaderUnavailable("invalid_response")
                    return content
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise ReaderUnavailable("deadline_exceeded") from None
    except ReaderUnavailable:
        raise
    except Exception:
        raise ReaderUnavailable("transport_or_response_error") from None
