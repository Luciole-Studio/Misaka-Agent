"""OpenAI Codex Responses provider adapter."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import math
import platform
import re
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

import httpx

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import clamp_thinking_level
from misaka.ai.providers.openai_prompt_cache import clamp_openai_prompt_cache_key
from misaka.ai.providers.openai_responses_shared import (
    convert_responses_messages,
    convert_responses_tools,
    process_responses_stream,
)
from misaka.ai.providers.simple_options import build_base_options
from misaka.ai.session_resources import register_session_resource_cleanup
from misaka.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StreamOptions,
    Usage,
)
from misaka.ai.utils.diagnostics import (
    append_assistant_message_diagnostic,
    create_assistant_message_diagnostic,
    format_thrown_value,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.headers import headers_to_record

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
MAX_RETRIES = 3
BASE_DELAY_MS = 1000
CODEX_TOOL_CALL_PROVIDERS = {"openai", "openai-codex", "opencode"}
CODEX_RESPONSE_STATUSES = {"completed", "incomplete", "failed", "cancelled", "queued", "in_progress"}
OPENAI_BETA_RESPONSES_WEBSOCKETS = "responses_websockets=2026-02-06"
SESSION_WEBSOCKET_CACHE_TTL_MS = 5 * 60 * 1000
WEBSOCKET_MESSAGE_TOO_BIG_CLOSE_CODE = 1009
_RETRYABLE_CODEX_ERROR_PATTERN = re.compile(
    r"rate.?limit|overloaded|service.?unavailable|upstream.?connect|connection.?refused",
    re.IGNORECASE,
)


class OpenAICodexResponsesOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    reasoningEffort: Literal["none", "minimal", "low", "medium", "high", "xhigh"]
    reasoningSummary: Literal["auto", "concise", "detailed", "off", "on"] | None
    serviceTier: str
    textVerbosity: Literal["low", "medium", "high"]


class OpenAICodexWebSocketDebugStats(TypedDict, total=False):
    requests: int
    connectionsCreated: int
    connectionsReused: int
    cachedContextRequests: int
    storeTrueRequests: int
    fullContextRequests: int
    deltaRequests: int
    lastInputItems: int
    lastDeltaInputItems: int
    lastPreviousResponseId: str
    websocketFailures: int
    sseFallbacks: int
    websocketFallbackActive: bool
    lastWebSocketError: str

_websocket_debug_stats: dict[str, dict[str, Any]] = {}
_websocket_fallback_sessions: set[str] = set()
_websocket_session_cache: dict[str, _CachedWebSocketConnection] = {}
_cached_websocket_connector: Callable[..., Any] | None = None


class CodexApiError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


class CodexProtocolError(RuntimeError):
    def __init__(self, message: str, *, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload


class WebSocketCloseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        reason: str | None = None,
        was_clean: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.was_clean = was_clean


@dataclass(slots=True)
class _CachedWebSocketContinuationState:
    last_request_body: dict[str, Any]
    last_response_id: str
    last_response_items: list[dict[str, Any]]


@dataclass(slots=True)
class _CachedWebSocketConnection:
    socket: Any
    busy: bool
    idle_handle: asyncio.TimerHandle | None = None
    continuation: _CachedWebSocketContinuationState | None = None


def _option(options: Any, name: str, default: Any = None) -> Any:
    if options is None:
        return default
    if isinstance(options, Mapping):
        return options.get(name, default)
    return getattr(options, name, default)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _is_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    if getattr(signal, "aborted", False):
        return True
    return bool(getattr(signal, "is_set", lambda: False)())


async def _sleep(ms: int, signal: Any = None) -> None:
    await _await_with_abort(asyncio.sleep(ms / 1000), signal)


def _is_retryable_error(status: int, error_text: str) -> bool:
    if status in {429, 500, 502, 503, 504}:
        return True
    return bool(_RETRYABLE_CODEX_ERROR_PATTERN.search(error_text))


def _parse_retry_after_delay_ms(response: httpx.Response, default_delay_ms: int) -> int:
    retry_after_ms = response.headers.get("retry-after-ms")
    if retry_after_ms is not None:
        try:
            millis = float(retry_after_ms)
        except ValueError:
            millis = float("nan")
        if math.isfinite(millis):
            return max(0, int(millis))

    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            seconds = float(retry_after)
        except ValueError:
            seconds = float("nan")
        if math.isfinite(seconds):
            return max(0, int(seconds * 1000))
        try:
            retry_at = parsedate_to_datetime(retry_after).timestamp()
        except (TypeError, ValueError, OverflowError):
            retry_at = None
        if retry_at is not None:
            return max(0, int((retry_at - time.time()) * 1000))

    return default_delay_ms


def _create_abort_wait_task(signal: Any) -> asyncio.Task[None] | None:
    if signal is None or not hasattr(signal, "wait"):
        return None
    return asyncio.create_task(signal.wait())


async def _await_with_abort(awaitable: Any, signal: Any, *, on_abort: Any = None) -> Any:
    if _is_aborted(signal):
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
        else:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
        if on_abort is not None:
            await _maybe_await(on_abort())
        raise RuntimeError("Request was aborted")

    task = asyncio.ensure_future(awaitable)
    abort_task = _create_abort_wait_task(signal)
    try:
        if abort_task is not None:
            done, _ = await asyncio.wait({task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if on_abort is not None:
                    await _maybe_await(on_abort())
                raise RuntimeError("Request was aborted")
        return await task
    finally:
        if abort_task is not None:
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)


def _create_debug_stats(session_id: str) -> dict[str, Any]:
    stats = _websocket_debug_stats.get(session_id)
    if stats is None:
        stats = {
            "requests": 0,
            "connectionsCreated": 0,
            "connectionsReused": 0,
            "cachedContextRequests": 0,
            "storeTrueRequests": 0,
            "fullContextRequests": 0,
            "deltaRequests": 0,
            "lastInputItems": 0,
            "websocketFailures": 0,
            "sseFallbacks": 0,
        }
        _websocket_debug_stats[session_id] = stats
    return stats


def get_openai_codex_websocket_debug_stats(session_id: str) -> dict[str, Any] | None:
    stats = _websocket_debug_stats.get(session_id)
    return dict(stats) if stats else None


def reset_openai_codex_websocket_debug_stats(session_id: str | None = None) -> None:
    if session_id is None:
        _websocket_debug_stats.clear()
        _websocket_fallback_sessions.clear()
        return
    _websocket_debug_stats.pop(session_id, None)
    _websocket_fallback_sessions.discard(session_id)


def _run_socket_close_nowait(socket: Any, code: int = 1000, reason: str = "done") -> None:
    try:
        result = socket.close(code=code, reason=reason)
    except TypeError:
        try:
            result = socket.close(code, reason)
        except Exception:
            return
    except Exception:
        return

    if not hasattr(result, "__await__"):
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(result)
    else:
        loop.create_task(result)


def close_openai_codex_websocket_sessions(session_id: str | None = None) -> None:
    def close_entry(entry: _CachedWebSocketConnection) -> None:
        if entry.idle_handle is not None:
            entry.idle_handle.cancel()
            entry.idle_handle = None
        _run_socket_close_nowait(entry.socket, 1000, "debug_close")

    if session_id is not None:
        entry = _websocket_session_cache.pop(session_id, None)
        if entry is not None:
            close_entry(entry)
        return

    for entry in list(_websocket_session_cache.values()):
        close_entry(entry)
    _websocket_session_cache.clear()


def _record_sse_fallback(session_id: str | None) -> None:
    if not session_id:
        return
    stats = _create_debug_stats(session_id)
    stats["sseFallbacks"] += 1
    stats["websocketFallbackActive"] = session_id in _websocket_fallback_sessions


def _record_websocket_failure(session_id: str | None, error: Any) -> None:
    if not session_id:
        return
    _websocket_fallback_sessions.add(session_id)
    stats = _create_debug_stats(session_id)
    stats["websocketFailures"] += 1
    stats["lastWebSocketError"] = format_thrown_value(error)
    stats["websocketFallbackActive"] = True


register_session_resource_cleanup(close_openai_codex_websocket_sessions)


def build_request_body(
    model: Model,
    context: Context,
    options: StreamOptions | dict[str, Any] | None = None,
) -> dict[str, Any]:
    text_verbosity = _option(options, "textVerbosity") or "low"
    messages = convert_responses_messages(model, context, CODEX_TOOL_CALL_PROVIDERS, {"includeSystemPrompt": False})
    body: dict[str, Any] = {
        "model": model.id,
        "store": False,
        "stream": True,
        "instructions": context.systemPrompt or "You are a helpful assistant.",
        "input": messages,
        "text": {"verbosity": text_verbosity},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": clamp_openai_prompt_cache_key(_option(options, "sessionId")),
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    if _option(options, "temperature") is not None:
        body["temperature"] = _option(options, "temperature")
    if _option(options, "serviceTier") is not None:
        body["service_tier"] = _option(options, "serviceTier")
    if context.tools:
        body["tools"] = convert_responses_tools(context.tools, {"strict": None})

    reasoning_effort = _option(options, "reasoningEffort")
    if reasoning_effort is not None:
        reasoning_summary = _option(options, "reasoningSummary")
        effort = (
            model.thinkingLevelMap.get("off", "none")
            if reasoning_effort == "none" and model.thinkingLevelMap
            else model.thinkingLevelMap.get(reasoning_effort, reasoning_effort)
            if model.thinkingLevelMap
            else reasoning_effort
        )
        if effort is not None:
            body["reasoning"] = {
                "effort": effort,
                "summary": "auto" if reasoning_summary is None else reasoning_summary,
            }

    return {key: value for key, value in body.items() if value is not None}


def get_service_tier_cost_multiplier(model: Model, service_tier: str | None) -> float:
    if service_tier == "flex":
        return 0.5
    if service_tier == "priority":
        return 2.5 if model.id == "gpt-5.5" else 2.0
    return 1.0


def apply_service_tier_pricing(usage: Usage, service_tier: str | None, model: Model) -> None:
    multiplier = get_service_tier_cost_multiplier(model, service_tier)
    if multiplier == 1:
        return
    usage.cost.input *= multiplier
    usage.cost.output *= multiplier
    usage.cost.cacheRead *= multiplier
    usage.cost.cacheWrite *= multiplier
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite


def resolve_codex_service_tier(response_service_tier: str | None, request_service_tier: str | None) -> str | None:
    if response_service_tier == "default" and request_service_tier in {"flex", "priority"}:
        return request_service_tier
    return response_service_tier or request_service_tier


def resolve_codex_url(base_url: str | None = None) -> str:
    raw = base_url.strip() if base_url and base_url.strip() else DEFAULT_CODEX_BASE_URL
    normalized = raw.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


def resolve_codex_websocket_url(base_url: str | None = None) -> str:
    url = urlparse(resolve_codex_url(base_url))
    scheme = "wss" if url.scheme == "https" else "ws" if url.scheme == "http" else url.scheme
    return url._replace(scheme=scheme).geturl()


async def process_stream(
    response: httpx.Response,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: Model,
    options: StreamOptions | dict[str, Any] | None = None,
) -> None:
    await process_responses_stream(
        map_codex_events(parse_sse(response, _option(options, "signal"))),
        output,
        stream,
        model,
        {
            "serviceTier": _option(options, "serviceTier"),
            "resolveServiceTier": resolve_codex_service_tier,
            "applyServiceTierPricing": lambda usage, tier: apply_service_tier_pricing(usage, tier, model),
        },
    )


def is_codex_non_transport_error(error: BaseException | Any) -> bool:
    return isinstance(error, (CodexApiError, CodexProtocolError))


async def map_codex_events(events: AsyncIterable[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    async for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue

        if event_type == "error":
            code = event.get("code")
            message = event.get("message")
            raise CodexApiError(
                f"Codex error: {message or code or json.dumps(event)}",
                code=str(code) if code else None,
                payload=event,
            )

        if event_type == "response.failed":
            response = event.get("response")
            response = response if isinstance(response, Mapping) else {}
            error = response.get("error")
            error = error if isinstance(error, Mapping) else {}
            raise CodexApiError(
                str(error.get("message") or "Codex response failed"),
                code=str(error.get("code") or "") or None,
                payload=event,
            )

        if event_type in {"response.done", "response.completed", "response.incomplete"}:
            response = event.get("response")
            if isinstance(response, Mapping):
                normalized_response = dict(response)
                normalized_response["status"] = normalize_codex_status(response.get("status"))
            else:
                normalized_response = response
            yield {"type": "response.completed", "response": normalized_response}
            return

        yield event


def normalize_codex_status(status: Any) -> str | None:
    return status if isinstance(status, str) and status in CODEX_RESPONSE_STATUSES else None


async def parse_sse(response: httpx.Response, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    buffer = ""
    iterator = response.aiter_text().__aiter__()
    while True:
        try:
            chunk = await _await_with_abort(iterator.__anext__(), signal, on_abort=lambda: response.aclose())
        except StopAsyncIteration:
            break

        buffer += chunk
        while True:
            separator = buffer.find("\n\n")
            if separator == -1:
                break
            chunk_text = buffer[:separator]
            buffer = buffer[separator + 2 :]

            data_lines = [
                line[5:].strip()
                for line in chunk_text.split("\n")
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            data = "\n".join(data_lines).strip()
            if not data or data == "[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except Exception as error:  # noqa: BLE001
                raise CodexProtocolError(
                    f"Invalid Codex SSE JSON: {format_thrown_value(error)}",
                    payload=data,
                ) from error
            if isinstance(parsed, dict):
                yield parsed


def _get_codex_user_agent() -> str:
    try:
        system = platform.system().lower()
        platform_name = "win32" if system == "windows" else system
        arch = platform.machine().lower()
        arch = {
            "x86_64": "x64",
            "amd64": "x64",
            "i386": "ia32",
            "i686": "ia32",
            "aarch64": "arm64",
        }.get(arch, arch)
        return f"harn ({platform_name} {platform.release()}; {arch})"
    except Exception:
        return "harn (browser)"


def _build_base_codex_headers(
    model_headers: Mapping[str, str] | None,
    option_headers: Mapping[str, str] | None,
    account_id: str,
    api_key: str,
) -> dict[str, str]:
    headers = dict(model_headers or {})
    headers.update(dict(option_headers or {}))
    headers["authorization"] = f"Bearer {api_key}"
    headers["chatgpt-account-id"] = account_id
    headers["originator"] = "harn"
    headers["User-Agent"] = _get_codex_user_agent()
    return headers


async def parse_error_response(response: httpx.Response) -> dict[str, str]:
    text = await response.aread()
    text_value = text.decode("utf-8", errors="replace")
    message = text_value or response.reason_phrase or "Request failed"
    friendly_message: str | None = None

    try:
        payload = json.loads(text_value)
    except Exception:
        return {"message": message}

    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or error.get("type") or "")
            if re.search(r"usage_limit_reached|usage_not_included|rate_limit_exceeded", code, re.IGNORECASE) or response.status_code == 429:
                plan_type = error.get("plan_type")
                plan = f" ({str(plan_type).lower()} plan)" if isinstance(plan_type, str) and plan_type else ""
                resets_at = error.get("resets_at")
                mins: int | None = None
                if isinstance(resets_at, (int, float)):
                    mins = max(0, round((resets_at * 1000 - time.time() * 1000) / 60000))
                when = f" Try again in ~{mins} min." if mins is not None else ""
                friendly_message = f"You have hit your ChatGPT usage limit{plan}.{when}".strip()
            message = str(error.get("message") or friendly_message or message)
    result = {"message": message}
    if friendly_message is not None:
        result["friendlyMessage"] = friendly_message
    return result


async def _get_websocket_connector() -> Callable[..., Any] | None:
    global _cached_websocket_connector
    if _cached_websocket_connector is not None:
        return _cached_websocket_connector

    try:
        module = importlib.import_module("websockets.asyncio.client")
    except Exception:
        return None

    connector = getattr(module, "connect", None)
    if connector is None:
        return None
    _cached_websocket_connector = connector
    return connector


def _get_websocket_ready_state(socket: Any) -> Any:
    return getattr(socket, "state", None)


def _is_websocket_reusable(socket: Any) -> bool:
    closed = getattr(socket, "closed", None)
    if isinstance(closed, bool):
        return not closed

    state = _get_websocket_ready_state(socket)
    state_name = getattr(state, "name", None)
    if isinstance(state_name, str):
        return state_name == "OPEN"
    if isinstance(state, int):
        return state == 1
    return True


def _extract_websocket_error(error: Any) -> RuntimeError:
    if isinstance(error, WebSocketCloseError):
        return error
    if isinstance(error, RuntimeError):
        return error
    if isinstance(error, Exception):
        message = str(error) or error.__class__.__name__
        return RuntimeError(message)
    return RuntimeError("WebSocket error")


def _extract_websocket_close_error(error: Any) -> WebSocketCloseError:
    code = getattr(error, "code", None)
    reason = getattr(error, "reason", None)
    was_clean = getattr(error, "rcvd_then_sent", None)
    code_text = f" {code}" if isinstance(code, int) else ""
    reason_text = f" {reason}" if isinstance(reason, str) and reason else ""
    if not reason_text and code == WEBSOCKET_MESSAGE_TOO_BIG_CLOSE_CODE:
        reason_text = " message too big"
    return WebSocketCloseError(
        f"WebSocket closed{code_text}{reason_text}".strip(),
        code=code if isinstance(code, int) else None,
        reason=reason if isinstance(reason, str) and reason else None,
        was_clean=bool(was_clean) if isinstance(was_clean, bool) else None,
    )


def _decode_websocket_data(data: Any) -> str | None:
    if isinstance(data, str):
        return data
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    if isinstance(data, bytearray):
        return bytes(data).decode("utf-8", errors="replace")
    if isinstance(data, memoryview):
        return data.tobytes().decode("utf-8", errors="replace")
    return None


def _request_body_without_input(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key not in {"input", "previous_response_id"}}


def _response_inputs_equal(a: Any, b: Any) -> bool:
    return json.dumps(a or []) == json.dumps(b or [])


def _request_bodies_match_except_input(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return json.dumps(_request_body_without_input(a)) == json.dumps(_request_body_without_input(b))


def _get_cached_websocket_input_delta(
    body: dict[str, Any],
    continuation: _CachedWebSocketContinuationState,
) -> list[dict[str, Any]] | None:
    if not _request_bodies_match_except_input(body, continuation.last_request_body):
        return None

    current_input = list(body.get("input") or [])
    baseline = [*(continuation.last_request_body.get("input") or []), *continuation.last_response_items]
    if len(current_input) < len(baseline):
        return None

    prefix = current_input[: len(baseline)]
    if not _response_inputs_equal(prefix, baseline):
        return None

    return current_input[len(baseline) :]


def _build_cached_websocket_request_body(
    entry: _CachedWebSocketConnection,
    body: dict[str, Any],
) -> dict[str, Any]:
    continuation = entry.continuation
    if continuation is None:
        return body

    delta = _get_cached_websocket_input_delta(body, continuation)
    if not delta or not continuation.last_response_id:
        entry.continuation = None
        return body

    return {**body, "previous_response_id": continuation.last_response_id, "input": delta}


def _schedule_session_websocket_expiry(session_id: str, entry: _CachedWebSocketConnection) -> None:
    if entry.idle_handle is not None:
        entry.idle_handle.cancel()

    loop = asyncio.get_running_loop()

    def expire() -> None:
        if entry.busy:
            return
        _run_socket_close_nowait(entry.socket, 1000, "idle_timeout")
        _websocket_session_cache.pop(session_id, None)

    entry.idle_handle = loop.call_later(SESSION_WEBSOCKET_CACHE_TTL_MS / 1000, expire)


async def _connect_websocket(
    url: str,
    headers: Mapping[str, str],
    signal: Any = None,
    timeout_ms: int | None = None,
) -> Any:
    connector = await _get_websocket_connector()
    if connector is None:
        raise RuntimeError("WebSocket transport is not available in this runtime")
    if _is_aborted(signal):
        raise RuntimeError("Request was aborted")

    websocket_headers = dict(headers)
    websocket_headers.pop("OpenAI-Beta", None)
    websocket_headers.pop("openai-beta", None)

    connectable = connector(
        url,
        additional_headers=websocket_headers,
        max_size=None,
        open_timeout=(timeout_ms / 1000) if timeout_ms is not None else None,
    )
    connect_task = asyncio.create_task(_maybe_await(connectable))
    abort_task = _create_abort_wait_task(signal)
    try:
        if abort_task is not None:
            done, _ = await asyncio.wait({connect_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done and not connect_task.done():
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
                raise RuntimeError("Request was aborted")
        socket = await connect_task
    finally:
        if abort_task is not None:
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)

    if _is_aborted(signal):
        _run_socket_close_nowait(socket, 1000, "aborted")
        raise RuntimeError("Request was aborted")
    return socket


async def _acquire_websocket(
    url: str,
    headers: Mapping[str, str],
    session_id: str | None,
    signal: Any = None,
    timeout_ms: int | None = None,
) -> tuple[Any, _CachedWebSocketConnection | None, bool, Callable[[bool], None]]:
    if not session_id:
        socket = await _connect_websocket(url, headers, signal, timeout_ms)

        def release(keep: bool = True) -> None:
            if not keep:
                _run_socket_close_nowait(socket)
                return
            _run_socket_close_nowait(socket)

        return socket, None, False, release

    cached = _websocket_session_cache.get(session_id)
    if cached is not None:
        if cached.idle_handle is not None:
            cached.idle_handle.cancel()
            cached.idle_handle = None

        if not cached.busy and _is_websocket_reusable(cached.socket):
            cached.busy = True

            def release(keep: bool = True) -> None:
                if not keep or not _is_websocket_reusable(cached.socket):
                    _run_socket_close_nowait(cached.socket)
                    _websocket_session_cache.pop(session_id, None)
                    return
                cached.busy = False
                _schedule_session_websocket_expiry(session_id, cached)

            return cached.socket, cached, True, release

        if cached.busy:
            socket = await _connect_websocket(url, headers, signal, timeout_ms)

            def release(keep: bool = True) -> None:
                _run_socket_close_nowait(socket)

            return socket, None, False, release

        if not _is_websocket_reusable(cached.socket):
            _run_socket_close_nowait(cached.socket)
            _websocket_session_cache.pop(session_id, None)

    socket = await _connect_websocket(url, headers, signal, timeout_ms)
    entry = _CachedWebSocketConnection(socket=socket, busy=True)
    _websocket_session_cache[session_id] = entry

    def release(keep: bool = True) -> None:
        if not keep or not _is_websocket_reusable(entry.socket):
            _run_socket_close_nowait(entry.socket)
            if entry.idle_handle is not None:
                entry.idle_handle.cancel()
                entry.idle_handle = None
            if _websocket_session_cache.get(session_id) is entry:
                _websocket_session_cache.pop(session_id, None)
            return
        entry.busy = False
        _schedule_session_websocket_expiry(session_id, entry)

    return socket, entry, False, release


async def parse_websocket(socket: Any, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    while True:
        if _is_aborted(signal):
            raise RuntimeError("Request was aborted")

        recv_task = asyncio.create_task(_maybe_await(socket.recv()))
        abort_task = _create_abort_wait_task(signal)
        try:
            if abort_task is not None:
                done, _ = await asyncio.wait({recv_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
                if abort_task in done and not recv_task.done():
                    recv_task.cancel()
                    await asyncio.gather(recv_task, return_exceptions=True)
                    raise RuntimeError("Request was aborted")
            raw_message = await recv_task
        except Exception as error:  # noqa: BLE001
            if hasattr(error, "code") or error.__class__.__name__.startswith("ConnectionClosed"):
                raise _extract_websocket_close_error(error) from error
            raise _extract_websocket_error(error) from error
        finally:
            if abort_task is not None:
                abort_task.cancel()
                await asyncio.gather(abort_task, return_exceptions=True)

        text = _decode_websocket_data(raw_message)
        if not text:
            continue

        try:
            parsed = json.loads(text)
        except Exception as error:  # noqa: BLE001
            raise CodexProtocolError(
                f"Invalid Codex WebSocket JSON: {format_thrown_value(error)}",
                payload=text,
            ) from error

        if not isinstance(parsed, dict):
            continue

        yield parsed
        event_type = parsed.get("type")
        if event_type in {"response.completed", "response.done", "response.incomplete"}:
            return


async def _start_websocket_output_on_first_event(
    events: AsyncIterable[dict[str, Any]],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    on_start: Callable[[], None],
) -> AsyncIterator[dict[str, Any]]:
    started = False
    async for event in events:
        if not started:
            started = True
            on_start()
            stream.push(StartEvent(partial=output))
        yield event


def extract_account_id(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        raise RuntimeError("Failed to extract accountId from token")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception as error:
        raise RuntimeError("Failed to extract accountId from token") from error
    if not isinstance(data, Mapping):
        raise RuntimeError("Failed to extract accountId from token")
    auth_claim = data.get(JWT_CLAIM_PATH)
    if not isinstance(auth_claim, Mapping):
        raise RuntimeError("Failed to extract accountId from token")
    account_id = auth_claim.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id:
        raise RuntimeError("Failed to extract accountId from token")
    return account_id


def create_codex_request_id() -> str:
    return str(uuid.uuid4())


def build_sse_headers(
    model_headers: Mapping[str, str] | None,
    option_headers: Mapping[str, str] | None,
    account_id: str,
    api_key: str,
    session_id: str | None = None,
) -> dict[str, str]:
    headers = _build_base_codex_headers(model_headers, option_headers, account_id, api_key)
    headers["OpenAI-Beta"] = "responses=experimental"
    headers["accept"] = "text/event-stream"
    headers["content-type"] = "application/json"
    if session_id:
        headers["session_id"] = session_id
        headers["x-client-request-id"] = session_id
    return headers


def build_websocket_headers(
    model_headers: Mapping[str, str] | None,
    option_headers: Mapping[str, str] | None,
    account_id: str,
    api_key: str,
    request_id: str,
) -> dict[str, str]:
    headers = _build_base_codex_headers(model_headers, option_headers, account_id, api_key)
    headers.pop("accept", None)
    headers.pop("content-type", None)
    headers.pop("OpenAI-Beta", None)
    headers.pop("openai-beta", None)
    headers["OpenAI-Beta"] = OPENAI_BETA_RESPONSES_WEBSOCKETS
    headers["x-client-request-id"] = request_id
    headers["session_id"] = request_id
    return headers


async def process_websocket_stream(
    url: str,
    body: dict[str, Any],
    headers: Mapping[str, str],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: Model,
    on_start: Callable[[], None],
    options: StreamOptions | dict[str, Any] | None = None,
) -> None:
    session_id = _option(options, "sessionId")
    socket, entry, reused, release = await _acquire_websocket(
        url,
        headers,
        session_id,
        _option(options, "signal"),
    )
    keep_connection = True
    use_cached_context = _option(options, "transport") in {"websocket-cached", "auto"}
    full_body = body
    request_body = _build_cached_websocket_request_body(entry, full_body) if use_cached_context and entry else full_body
    stats = _create_debug_stats(session_id) if session_id else None
    if stats is not None:
        stats["requests"] += 1
        if reused:
            stats["connectionsReused"] += 1
        else:
            stats["connectionsCreated"] += 1
        if use_cached_context:
            stats["cachedContextRequests"] += 1
        if request_body.get("store") is True:
            stats["storeTrueRequests"] += 1
        stats["lastInputItems"] = len(request_body.get("input") or [])
        if request_body.get("previous_response_id"):
            stats["deltaRequests"] += 1
            stats["lastDeltaInputItems"] = len(request_body.get("input") or [])
            stats["lastPreviousResponseId"] = request_body["previous_response_id"]
        else:
            stats["fullContextRequests"] += 1
            stats["lastDeltaInputItems"] = None
            stats["lastPreviousResponseId"] = None

    try:
        await _maybe_await(socket.send(json.dumps({"type": "response.create", **request_body})))
        await process_responses_stream(
            map_codex_events(
                _start_websocket_output_on_first_event(
                    parse_websocket(socket, _option(options, "signal")),
                    output,
                    stream,
                    on_start,
                )
            ),
            output,
            stream,
            model,
            {
                "serviceTier": _option(options, "serviceTier"),
                "resolveServiceTier": resolve_codex_service_tier,
                "applyServiceTierPricing": lambda usage, tier: apply_service_tier_pricing(usage, tier, model),
            },
        )
        if _is_aborted(_option(options, "signal")):
            keep_connection = False
        elif use_cached_context and entry is not None and output.responseId:
            response_items = [
                item
                for item in convert_responses_messages(
                    model,
                    Context(messages=[output]),
                    CODEX_TOOL_CALL_PROVIDERS,
                    {"includeSystemPrompt": False},
                )
                if item.get("type") != "function_call_output"
            ]
            entry.continuation = _CachedWebSocketContinuationState(
                last_request_body=full_body,
                last_response_id=output.responseId,
                last_response_items=response_items,
            )
    except Exception:
        if entry is not None:
            entry.continuation = None
        keep_connection = False
        raise
    finally:
        release(keep_connection)


def stream_openai_codex_responses(
    model: Model,
    context: Context,
    options: StreamOptions | dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage={
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )

        response: httpx.Response | None = None
        try:
            api_key = _option(options, "apiKey") or get_env_api_key(model.provider) or ""
            if not api_key:
                raise RuntimeError(f"No API key for provider: {model.provider}")

            account_id = extract_account_id(api_key)
            body = build_request_body(model, context, options)
            next_body = _option(options, "onPayload")
            if callable(next_body):
                updated = await _maybe_await(next_body(body, model))
                if updated is not None:
                    body = updated

            session_id = _option(options, "sessionId")
            transport = _option(options, "transport", "auto")
            websocket_request_id = session_id or create_codex_request_id()
            sse_headers = build_sse_headers(model.headers, _option(options, "headers"), account_id, api_key, session_id)
            websocket_headers = build_websocket_headers(
                model.headers,
                _option(options, "headers"),
                account_id,
                api_key,
                websocket_request_id,
            )
            body_json = json.dumps(body)
            websocket_disabled_for_session = transport != "sse" and session_id in _websocket_fallback_sessions
            if websocket_disabled_for_session:
                _record_sse_fallback(session_id)

            if transport != "sse" and not websocket_disabled_for_session:
                websocket_state = {"started": False}
                try:
                    await process_websocket_stream(
                        resolve_codex_websocket_url(model.baseUrl),
                        body,
                        websocket_headers,
                        output,
                        stream,
                        model,
                        lambda: websocket_state.__setitem__("started", True),
                        options,
                    )
                    if _is_aborted(_option(options, "signal")):
                        raise RuntimeError("Request was aborted")
                    stream.push(DoneEvent(reason=output.stopReason, message=output))
                    stream.end()
                    return
                except Exception as error:  # noqa: BLE001
                    aborted = _is_aborted(_option(options, "signal"))
                    if aborted or is_codex_non_transport_error(error):
                        raise
                    append_assistant_message_diagnostic(
                        output,
                        create_assistant_message_diagnostic(
                            "provider_transport_failure",
                            error,
                            {
                                "configuredTransport": transport,
                                "fallbackTransport": None if websocket_state["started"] else "sse",
                                "eventsEmitted": websocket_state["started"],
                                "phase": "after_message_stream_start"
                                if websocket_state["started"]
                                else "before_message_stream_start",
                                "requestBytes": len(body_json.encode("utf-8")),
                            },
                        ),
                    )
                    _record_websocket_failure(session_id, error)
                    if websocket_state["started"]:
                        raise
                    _record_sse_fallback(session_id)

            url = resolve_codex_url(model.baseUrl)
            signal = _option(options, "signal")

            async with httpx.AsyncClient(follow_redirects=True) as client:
                last_error: RuntimeError | None = None
                for attempt in range(MAX_RETRIES + 1):
                    if _is_aborted(signal):
                        raise RuntimeError("Request was aborted")
                    try:
                        request = client.build_request("POST", url, headers=sse_headers, json=body)
                        response = await _await_with_abort(client.send(request, stream=True), signal)
                        on_response = _option(options, "onResponse")
                        if callable(on_response):
                            await _maybe_await(
                                on_response(
                                    {"status": response.status_code, "headers": headers_to_record(response.headers)},
                                    model,
                                )
                            )
                        if response.is_success:
                            break

                        error_info = await parse_error_response(response)
                        error_text = error_info.get("message", "")
                        if attempt < MAX_RETRIES and _is_retryable_error(response.status_code, error_text):
                            delay_ms = _parse_retry_after_delay_ms(response, BASE_DELAY_MS * (2**attempt))
                            await response.aclose()
                            await _sleep(delay_ms, signal)
                            continue
                        await response.aclose()
                        raise CodexApiError(error_info.get("friendlyMessage") or error_info["message"])
                    except (httpx.HTTPError, CodexApiError, CodexProtocolError, RuntimeError) as error:
                        if isinstance(error, RuntimeError) and str(error) == "Request was aborted":
                            raise
                        if is_codex_non_transport_error(error):
                            raise
                        last_error = error if isinstance(error, RuntimeError) else RuntimeError(str(error))
                        if attempt < MAX_RETRIES:
                            await _sleep(BASE_DELAY_MS * (2**attempt), signal)
                            continue
                        raise last_error from error

                if response is None or not response.is_success:
                    raise RuntimeError("Failed after retries")

                stream.push(StartEvent(partial=output))
                await process_stream(response, output, stream, model, options)

                if _is_aborted(signal):
                    raise RuntimeError("Request was aborted")
                stream.push(DoneEvent(reason=output.stopReason, message=output))
            stream.end()
        except Exception as error:  # noqa: BLE001
            for block in output.content:
                if isinstance(block, dict):
                    block.pop("partialJson", None)
                    continue
                try:
                    delattr(block, "partialJson")
                except AttributeError:
                    continue
            output.stopReason = "aborted" if _is_aborted(_option(options, "signal")) else "error"
            output.errorMessage = str(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
            stream.end()
        finally:
            if response is not None:
                await response.aclose()

    asyncio.create_task(run())
    return stream


def stream_simple_openai_codex_responses(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    clamped_reasoning = clamp_thinking_level(model, options.reasoning) if options and options.reasoning else None
    reasoning_effort = None if clamped_reasoning == "off" else clamped_reasoning
    return stream_openai_codex_responses(
        model,
        context,
        {**base.model_dump(), "reasoningEffort": reasoning_effort},
    )


streamOpenAICodexResponses = stream_openai_codex_responses
streamSimpleOpenAICodexResponses = stream_simple_openai_codex_responses
getServiceTierCostMultiplier = get_service_tier_cost_multiplier
applyServiceTierPricing = apply_service_tier_pricing

__all__ = [
    "DEFAULT_CODEX_BASE_URL",
    "JWT_CLAIM_PATH",
    "OpenAICodexResponsesOptions",
    "OpenAICodexWebSocketDebugStats",
    "applyServiceTierPricing",
    "getServiceTierCostMultiplier",
    "streamOpenAICodexResponses",
    "streamSimpleOpenAICodexResponses",
    "apply_service_tier_pricing",
    "build_request_body",
    "build_sse_headers",
    "build_websocket_headers",
    "close_openai_codex_websocket_sessions",
    "create_codex_request_id",
    "extract_account_id",
    "get_openai_codex_websocket_debug_stats",
    "get_service_tier_cost_multiplier",
    "is_codex_non_transport_error",
    "map_codex_events",
    "normalize_codex_status",
    "parse_error_response",
    "parse_sse",
    "process_stream",
    "process_websocket_stream",
    "reset_openai_codex_websocket_debug_stats",
    "resolve_codex_service_tier",
    "resolve_codex_url",
    "resolve_codex_websocket_url",
    "stream_openai_codex_responses",
    "stream_simple_openai_codex_responses",
]
