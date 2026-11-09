"""Translated from pi's ``packages/ai/src/api/openai-codex-responses.ts`` (v1.3.0).

MISAKA fork, kept to what the Python platform forces (each is marked where it happens):

- httpx stands in for ``fetch``: the header timeout races the request (``asyncio.wait_for``), undici's idle timeout is
  httpx's read timeout, and a body that stops arriving ends with undici's ``terminated``.
- zstd comes from ``compression.zstd`` (3.14+) or ``zstandard``; Python's ``zlib`` has none. ``zstandard`` is not
  part of the ``openai`` extra, so below 3.14 without it the body goes out uncompressed, as pi's builds without
  ``node:zlib`` send it.
- The ``websockets`` package stands in for the DOM ``WebSocket``: ``connect()`` with ``open_timeout`` replaces the
  open/error/close listeners, a ``recv()`` loop replaces the message queue, the connection's ``state`` (numbered as
  ``readyState`` is) says whether it can be reused, the proxy comes from ``env`` as on Bun, and ``close()`` is a
  coroutine scheduled on the loop. ``websockets`` is part of every install (through ``mcp[ws]``); were it missing, the
  WebSocket transport would be "not available in this runtime" and a WebSocket attempt would fall back to SSE.
- ``originator`` defaults to MISAKA's client name instead of ``pi``.
- The token payload is decoded as base64url.
- Request body fields pi leaves ``undefined`` are left out (a Python ``None`` would be sent as ``null``).
- After each event the loop turns once, as a JavaScript ``for await`` hands control to the consumer.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import math
import re
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from typing import Any, Literal, NamedTuple, TypedDict
from urllib.parse import urlparse

import httpx

from misaka.ai.api._common import (
    _await_with_signal,
    _close_stream,
    _compat_value,
    _create_abort_wait_task,
    _iterate_async_iterable,
    _option,
)
from misaka.ai.api.constrained_sampling import createGrammarToolInputProperties
from misaka.ai.api.openai_prompt_cache import clampOpenAIPromptCacheKey
from misaka.ai.api.openai_responses_shared import (
    convertResponsesMessages,
    convertResponsesTools,
    processResponsesStream,
)
from misaka.ai.api.simple_options import buildBaseOptions
from misaka.ai.models import clampThinkingLevel
from misaka.ai.session_resources import registerSessionResourceCleanup
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    ProviderEnv,
    ProviderHeaders,
    SimpleStreamOptions,
    StartEvent,
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils.diagnostics import (
    appendAssistantMessageDiagnostic,
    createAssistantMessageDiagnostic,
    formatThrownValue,
)
from misaka.ai.utils.error_body import formatProviderError, normalizeProviderError
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headersToRecord
from misaka.ai.utils.node_http_proxy import resolveHttpProxyUrlForTarget
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.text import getSystemMessageText
from misaka.ai.utils.transcript import (
    getDeclaredTools,
    getInitialSystemMessage,
    normalizeContext,
    resolveTranscript,
    resolveTranscriptTools,
)
from misaka.ai.utils.uuid import uuidv7
from misaka.core.provider_attribution import CLIENT_NAME
from misaka.utils.js import (
    UNDEFINED,
    jsNumber,
    jsonParse,
    jsonStringify,
    jsString,
    jsTrim,
    nullishCoalesce,
)
from misaka.utils.values import maybe_await, signal_aborted

# ============================================================================
# Configuration
# ============================================================================

_DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
_JWT_CLAIM_PATH = "https://api.openai.com/auth"
_DEFAULT_MAX_RETRIES = 0
_BASE_DELAY_MS = 1000
_DEFAULT_MAX_RETRY_DELAY_MS = 60_000
_DEFAULT_WEBSOCKET_CONNECT_TIMEOUT_MS = 15_000
# The Codex backend accepts zstd-compressed request bodies on the SSE responses
# endpoint (the same endpoint the official Codex client compresses against).
_REQUEST_COMPRESSION_ZSTD_LEVEL = 3
_CODEX_TOOL_CALL_PROVIDERS = {"openai", "openai-codex", "opencode"}
_WEBSOCKET_MESSAGE_TOO_BIG_CLOSE_CODE = 1009
_WEBSOCKET_CONNECTION_LIMIT_REACHED_CODE = "websocket_connection_limit_reached"
_PREVIOUS_RESPONSE_NOT_FOUND_CODE = "previous_response_not_found"

_CODEX_RESPONSE_STATUSES = {
    "completed",
    "incomplete",
    "failed",
    "cancelled",
    "queued",
    "in_progress",
}

# ============================================================================
# Types
# ============================================================================


class OpenAICodexResponsesOptions(TypedDict, total=False):
    # StreamOptions members.
    temperature: float
    samplingParams: dict[str, Any]
    maxTokens: int
    signal: Any
    telemetryContext: Any
    apiKey: str
    transport: Any
    cacheRetention: CacheRetention
    sessionId: str
    headers: ProviderHeaders
    onPayload: Any
    onResponse: Any
    onProviderStreamEvent: Any
    timeoutMs: int
    websocketConnectTimeoutMs: int
    maxRetries: int
    maxRetryDelayMs: int
    metadata: dict[str, Any]
    env: ProviderEnv
    reasoningEffort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    reasoningSummary: Literal["auto", "concise", "detailed", "off", "on"] | None
    serviceTier: Any
    textVerbosity: Literal["low", "medium", "high"]
    toolChoice: Literal["auto", "none", "required"]


def _assertSuccessfulOutput(output: AssistantMessage) -> None:
    if output.stopReason == "pending":
        raise RuntimeError("Codex stream ended without a stop reason")
    if output.stopReason == "error" or output.stopReason == "aborted":
        raise RuntimeError(output.errorMessage or "An unknown error occurred")


# ============================================================================
# Retry Helpers
# ============================================================================

_TERMINAL_RATE_LIMIT_PATTERN = re.compile(
    r"GoUsageLimitError|FreeUsageLimitError|Monthly usage limit reached|available balance|insufficient_quota|out of budget|quota exceeded|billing",
    re.IGNORECASE,
)
_RETRYABLE_ERROR_PATTERN = re.compile(
    r"rate.?limit|overloaded|service.?unavailable|upstream.?connect|connection.?refused", re.IGNORECASE
)


def _isTerminalRateLimitError(errorText: str) -> bool:
    return bool(_TERMINAL_RATE_LIMIT_PATTERN.search(errorText))


def _isRetryableError(status: int, errorText: str) -> bool:
    if status == 429 and _isTerminalRateLimitError(errorText):
        return False
    if status == 429 or status == 500 or status == 502 or status == 503 or status == 504:
        return True
    return bool(_RETRYABLE_ERROR_PATTERN.search(errorText))


def _getRetryAfterDelayMs(headers: httpx.Headers | Mapping[str, str]) -> float | None:
    retryAfterMs = headers.get("retry-after-ms")
    if retryAfterMs is not None:
        millis = jsNumber(retryAfterMs)
        if math.isfinite(millis):
            return max(0, millis)

    retryAfter = headers.get("retry-after")
    if not retryAfter:
        return None

    seconds = jsNumber(retryAfter)
    if math.isfinite(seconds):
        return max(0, seconds * 1000)

    # MISAKA fork: `Date.parse` reads an HTTP date; `parsedate_to_datetime` is Python's reader for one.
    try:
        date = parsedate_to_datetime(retryAfter).timestamp() * 1000
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, date - time.time() * 1000)


class _RetryDelayExceededError(RuntimeError):
    pass


def _validateRetryDelayMs(delayMs: float, options: Any = None) -> float:
    maxRetryDelayMs = nullishCoalesce(_option(options, "maxRetryDelayMs"), _DEFAULT_MAX_RETRY_DELAY_MS)
    if maxRetryDelayMs > 0 and delayMs > maxRetryDelayMs:
        raise _RetryDelayExceededError(
            f"Server requested {math.ceil(delayMs / 1000)}s retry delay (max: {math.ceil(maxRetryDelayMs / 1000)}s)",
        )
    return delayMs


async def _sleep(ms: float, signal: Any = None) -> None:
    await _await_with_signal(asyncio.sleep(ms / 1000), signal)


def _normalizeTimeoutMs(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise RuntimeError(f"Invalid timeoutMs: {jsString(value)}")
    return math.floor(value)


# ============================================================================
# Request Compression
# ============================================================================


# Returns the zstd-compressed body bytes, or null when compression is
# unavailable (browser/Vite builds). Callers fall back to sending the
# uncompressed JSON when this returns null.
def _compressRequestBodyZstd(bodyJson: str) -> bytes | None:
    # MISAKA fork: pi reaches zstd through `node:zlib` (`loadNodeZlib`). Python's zlib has no zstd; the codec is
    # `compression.zstd` from 3.14 and the third-party `zstandard` before that.
    try:
        from compression import zstd  # type: ignore[import-not-found]

        return zstd.compress(bodyJson.encode("utf-8"), level=_REQUEST_COMPRESSION_ZSTD_LEVEL)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - compression is best effort; the plain body still works
        return None
    try:
        import zstandard  # type: ignore[import-not-found]

        return zstandard.ZstdCompressor(level=_REQUEST_COMPRESSION_ZSTD_LEVEL).compress(bodyJson.encode("utf-8"))
    except Exception:  # noqa: BLE001 - as above, and ImportError when the package is absent
        return None


# ============================================================================
# Main Stream Function
# ============================================================================


def stream(
    model: Model,
    context: TranscriptContext,
    options: OpenAICodexResponsesOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    normalizedContext = resolveTranscript(context, _compat_value(model, "supportsMidConvoSystemMessages"))

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api="openai-codex-responses",
            provider=model.provider,
            model=model.id,
            usage=Usage(
                input=0,
                output=0,
                cacheRead=0,
                cacheWrite=0,
                totalTokens=0,
                cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
            ),
            stopReason="pending",
            timestamp=time.time_ns() // 1_000_000,
        )
        # MISAKA fork: the response this request opened, closed in `finally`.
        response: httpx.Response | None = None

        try:
            apiKey = _option(options, "apiKey")
            if not apiKey:
                raise RuntimeError(f"No API key for provider: {model.provider}")

            accountId = _extractAccountId(apiKey)
            grammarToolInputProperties = createGrammarToolInputProperties(
                getDeclaredTools(normalizedContext.messages),
                nullishCoalesce(_compat_value(model, "supportsOpenAIGrammarTools"), False),
            )
            cacheSessionId = None if _option(options, "cacheRetention") == "none" else _option(options, "sessionId")
            codexSessionId = clampOpenAIPromptCacheKey(cacheSessionId)
            body = _buildRequestBody(model, normalizedContext, options, codexSessionId, grammarToolInputProperties)
            onPayload = _option(options, "onPayload")
            nextBody = await maybe_await(onPayload(body, model)) if onPayload else None
            if nextBody is not None:
                body = nextBody
            websocketRequestId = codexSessionId or uuidv7()
            sseHeaders = _buildSSEHeaders(model.headers, _option(options, "headers"), accountId, apiKey, codexSessionId)
            websocketHeaders = _buildWebSocketHeaders(
                model.headers,
                _option(options, "headers"),
                accountId,
                apiKey,
                websocketRequestId,
            )
            bodyJson = jsonStringify(body)
            httpTimeoutMs = _normalizeTimeoutMs(_option(options, "timeoutMs"))
            websocketConnectTimeoutMs = _normalizeTimeoutMs(_option(options, "websocketConnectTimeoutMs"))
            transport = _option(options, "transport") or "auto"
            startEmitted = False
            websocketDisabledForSession = transport != "sse" and _isWebSocketSseFallbackActive(cacheSessionId)
            if websocketDisabledForSession:
                _recordWebSocketSseFallback(cacheSessionId)

            if transport != "sse" and not websocketDisabledForSession:
                websocketStarted = False
                retriedWebSocketConnectionLimit = False
                retriedMissingWebSocketContinuation = False

                def onStart() -> None:
                    nonlocal websocketStarted, startEmitted
                    websocketStarted = True
                    if not startEmitted:
                        startEmitted = True
                        stream.push(StartEvent(partial=output))

                while True:
                    websocketStarted = False
                    try:
                        await _processWebSocketStream(
                            _resolveCodexWebSocketUrl(model.baseUrl),
                            body,
                            websocketHeaders,
                            output,
                            stream,
                            model,
                            onStart,
                            httpTimeoutMs,
                            websocketConnectTimeoutMs,
                            cacheSessionId,
                            accountId,
                            grammarToolInputProperties,
                            options,
                        )

                        if signal_aborted(_option(options, "signal")):
                            raise RuntimeError("Request was aborted")
                        _assertSuccessfulOutput(output)
                        stream.push(
                            DoneEvent(
                                reason=output.stopReason,
                                message=output,
                            )
                        )
                        stream.end()
                        return
                    except Exception as error:
                        aborted = signal_aborted(_option(options, "signal"))
                        connectionLimitBeforeStart = not websocketStarted and _isWebSocketConnectionLimitReachedError(
                            error
                        )
                        previousResponseNotFound = _isPreviousResponseNotFoundError(error)
                        if not aborted and previousResponseNotFound and not retriedMissingWebSocketContinuation:
                            retriedMissingWebSocketContinuation = True
                            continue
                        if not aborted and connectionLimitBeforeStart and not retriedWebSocketConnectionLimit:
                            retriedWebSocketConnectionLimit = True
                            continue
                        if aborted or (_isCodexNonTransportError(error) and not connectionLimitBeforeStart):
                            raise
                        appendAssistantMessageDiagnostic(
                            output,
                            createAssistantMessageDiagnostic(
                                "provider_transport_failure",
                                error,
                                {
                                    "configuredTransport": transport,
                                    **({} if websocketStarted else {"fallbackTransport": "sse"}),
                                    "eventsEmitted": websocketStarted,
                                    "phase": "after_message_stream_start"
                                    if websocketStarted
                                    else "before_message_stream_start",
                                    "requestBytes": len(bodyJson.encode("utf-8")),
                                },
                            ),
                        )
                        _recordWebSocketFailure(cacheSessionId, error)
                        if websocketStarted:
                            raise
                        _recordWebSocketSseFallback(cacheSessionId)
                        break

            # Compress the request body once for the SSE path. The Codex backend
            # decodes Content-Encoding: zstd; the WebSocket transport above sends the
            # uncompressed JSON frame, matching the official Codex client.
            compressedBody = _compressRequestBodyZstd(bodyJson)
            if compressedBody:
                sseHeaders["content-encoding"] = "zstd"
            sseBody: bytes = nullishCoalesce(compressedBody, bodyJson.encode("utf-8"))

            # Fetch with retry logic for rate limits and transient errors
            lastError: BaseException | None = None
            maxRetries = nullishCoalesce(_option(options, "maxRetries"), _DEFAULT_MAX_RETRIES)

            # MISAKA fork: pi's `fetch` runs on undici, whose idle timeout is the HTTP idle
            # setting that also arrives here as `timeoutMs` (coding-agent's http-dispatcher.ts);
            # httpx gets it as its read timeout, and nothing else.
            readTimeout = httpTimeoutMs / 1000 if httpTimeoutMs is not None and httpTimeoutMs > 0 else None
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(None, read=readTimeout)) as client:
                attempt = 0
                while attempt <= maxRetries:
                    if signal_aborted(_option(options, "signal")):
                        raise RuntimeError("Request was aborted")

                    try:
                        # MISAKA fork: the header timeout races the request instead of an `AbortSignal.timeout`.
                        request = client.build_request(
                            "POST", _resolveCodexUrl(model.baseUrl), headers=sseHeaders, content=sseBody
                        )
                        send = client.send(request, stream=True)
                        try:
                            response = await _await_with_signal(
                                asyncio.wait_for(send, httpTimeoutMs / 1000)
                                if httpTimeoutMs is not None and httpTimeoutMs > 0
                                else send,
                                _option(options, "signal"),
                            )
                        except (TimeoutError, httpx.ReadTimeout) as error:
                            if not signal_aborted(_option(options, "signal")):
                                raise RuntimeError(f"Codex SSE response headers timed out after {httpTimeoutMs}ms") from error
                            raise
                        onResponse = _option(options, "onResponse")
                        if onResponse:
                            await maybe_await(
                                onResponse({"status": response.status_code, "headers": headersToRecord(response.headers)}, model)
                            )

                        if response.is_success:
                            break

                        errorText = (await response.aread()).decode("utf-8", errors="replace")
                        await response.aclose()
                        if attempt < maxRetries and _isRetryableError(response.status_code, errorText):
                            retryAfterDelayMs = _getRetryAfterDelayMs(response.headers)
                            delayMs = (
                                _BASE_DELAY_MS * 2**attempt
                                if retryAfterDelayMs is None
                                else _validateRetryDelayMs(retryAfterDelayMs, options)
                            )

                            await _sleep(delayMs, _option(options, "signal"))
                            attempt += 1
                            continue

                        # Parse error for friendly message on final attempt or non-retryable error
                        fakeResponse = httpx.Response(
                            response.status_code, content=errorText.encode("utf-8"), extensions={"reason_phrase": response.reason_phrase.encode()}
                        )
                        info = await _parseErrorResponse(fakeResponse)
                        raise RuntimeError(info.get("friendlyMessage") or info["message"])
                    except Exception as error:
                        if getattr(error, "name", None) == "AbortError" or str(error) == "Request was aborted":
                            raise RuntimeError("Request was aborted") from error
                        lastError = error
                        # Network errors are retryable
                        if (
                            attempt < maxRetries
                            and not isinstance(lastError, _RetryDelayExceededError)
                            and "usage limit" not in str(lastError)
                        ):
                            delayMs = _BASE_DELAY_MS * 2**attempt
                            await _sleep(delayMs, _option(options, "signal"))
                            attempt += 1
                            continue
                        raise

                if response is None or not response.is_success:
                    raise lastError or RuntimeError("Failed after retries")

                # MISAKA fork: httpx always hands over a body stream; fetch has none for these statuses.
                if response.status_code in (204, 205):
                    raise RuntimeError("No response body")

                if not startEmitted:
                    startEmitted = True
                    stream.push(StartEvent(partial=output))
                await _processStream(response, output, stream, model, grammarToolInputProperties, options)

                if signal_aborted(_option(options, "signal")):
                    raise RuntimeError("Request was aborted")

                _assertSuccessfulOutput(output)
                stream.push(DoneEvent(reason=output.stopReason, message=output))
                stream.end()
        except Exception as error:  # noqa: BLE001 - every failure becomes the stream's error event
            # MISAKA fork: streaming scratch buffers live beside the blocks
            # (`openai_responses_shared._StreamingToolCallScratch`), so there is nothing to strip here.
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            output.errorMessage = formatProviderError(normalizeProviderError(error))
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
            stream.end()
        finally:
            if response is not None:
                await response.aclose()

    spawn_stream_task(run(), stream=stream)
    return stream


def streamSimple(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    apiKey = _option(options, "apiKey")
    if not apiKey:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = {
        **dict(buildBaseOptions(model, context, options, apiKey)),
        "toolChoice": _option(options, "toolChoice"),
    }
    clampedReasoning = clampThinkingLevel(model, _option(options, "reasoning")) if _option(options, "reasoning") else None
    reasoningEffort = None if clampedReasoning == "off" else clampedReasoning

    return stream(
        model,
        context,
        {
            **base,
            "reasoningEffort": reasoningEffort,
        },
    )


# ============================================================================
# Request Building
# ============================================================================


def _buildRequestBody(
    model: Model,
    context: TranscriptContext,
    options: OpenAICodexResponsesOptions | Mapping[str, Any] | None,
    cacheSessionId: str | None,
    grammarToolInputProperties: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if grammarToolInputProperties is None:
        grammarToolInputProperties = createGrammarToolInputProperties(
            getDeclaredTools(context.messages),
            nullishCoalesce(_compat_value(model, "supportsOpenAIGrammarTools"), False),
        )
    supportsStrictMode = nullishCoalesce(_compat_value(model, "supportsStrictMode"), True)
    supportsOpenAIGrammarTools = nullishCoalesce(_compat_value(model, "supportsOpenAIGrammarTools"), False)
    supportsAdditionalTools = nullishCoalesce(_compat_value(model, "supportsAdditionalTools"), False)
    supportsToolSearch = nullishCoalesce(_compat_value(model, "supportsToolSearch"), False)
    transcriptTools = resolveTranscriptTools(context.messages, supportsAdditionalTools or supportsToolSearch)
    messages = convertResponsesMessages(
        model,
        context,
        _CODEX_TOOL_CALL_PROVIDERS,
        {
            "includeSystemPrompt": False,
            "grammarToolInputProperties": grammarToolInputProperties,
            "supportsMidConvoSystemMessages": nullishCoalesce(_compat_value(model, "supportsMidConvoSystemMessages"), False),
            "supportsAdditionalTools": supportsAdditionalTools,
            "supportsToolSearch": supportsToolSearch,
            "toolOptions": {"strict": None, "supportsStrictMode": supportsStrictMode, "supportsOpenAIGrammarTools": supportsOpenAIGrammarTools},
        },
    )

    initialSystemMessage = getInitialSystemMessage(context.messages)
    instructions = getSystemMessageText(initialSystemMessage) if initialSystemMessage else ""
    body: dict[str, Any] = {
        "model": model.id,
        "store": False,
        "stream": True,
        "instructions": instructions or "You are a helpful assistant.",
        "input": messages,
        "text": {"verbosity": _option(options, "textVerbosity") or "low"},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": cacheSessionId,
        "tool_choice": nullishCoalesce(_option(options, "toolChoice"), "auto"),
        "parallel_tool_calls": True,
    }

    if _option(options, "temperature") is not None:
        body["temperature"] = _option(options, "temperature")

    if _option(options, "serviceTier") is not None:
        body["service_tier"] = _option(options, "serviceTier")

    if len(transcriptTools.requestTools) > 0:
        body["tools"] = convertResponsesTools(
            transcriptTools.requestTools,
            {
                "strict": None,
                "supportsStrictMode": supportsStrictMode,
                "supportsOpenAIGrammarTools": supportsOpenAIGrammarTools,
            },
        )

    thinkingLevelMap = model.thinkingLevelMap
    reasoningEffort = _option(options, "reasoningEffort")
    if reasoningEffort is not None:
        offEffort = UNDEFINED if thinkingLevelMap is None else thinkingLevelMap.get("off", UNDEFINED)
        effort = (
            ("none" if offEffort is UNDEFINED else offEffort)
            if reasoningEffort == "none"
            else nullishCoalesce(None if thinkingLevelMap is None else thinkingLevelMap.get(reasoningEffort), reasoningEffort)
        )
        if effort is not None:
            body["reasoning"] = {
                "effort": effort,
                "summary": nullishCoalesce(_option(options, "reasoningSummary"), "auto"),
            }
    elif model.reasoning and (thinkingLevelMap is None or thinkingLevelMap.get("off", UNDEFINED) is not None):
        body["reasoning"] = {"effort": nullishCoalesce(None if thinkingLevelMap is None else thinkingLevelMap.get("off"), "none")}

    # MISAKA fork: pi's `undefined` fields are left out of the JSON; a Python `None` would be
    # sent as `null`.
    return {key: value for key, value in body.items() if value is not None}


def _getServiceTierCostMultiplier(model: Model, serviceTier: str | None) -> float:
    match serviceTier:
        case "flex":
            return 0.5
        case "priority":
            return 2.5 if model.id == "gpt-5.5" else 2
        case _:
            return 1


def _applyServiceTierPricing(usage: Usage, serviceTier: str | None, model: Model) -> None:
    multiplier = _getServiceTierCostMultiplier(model, serviceTier)
    if multiplier == 1:
        return

    usage.cost.input *= multiplier
    usage.cost.output *= multiplier
    usage.cost.cacheRead *= multiplier
    usage.cost.cacheWrite *= multiplier
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite


def _resolveCodexServiceTier(responseServiceTier: str | None, requestServiceTier: str | None) -> str | None:
    if responseServiceTier == "default" and (requestServiceTier == "flex" or requestServiceTier == "priority"):
        return requestServiceTier
    return nullishCoalesce(responseServiceTier, requestServiceTier)


def _resolveCodexUrl(baseUrl: str | None = None) -> str:
    raw = baseUrl if baseUrl and len(jsTrim(baseUrl)) > 0 else _DEFAULT_CODEX_BASE_URL
    normalized = re.sub(r"/+$", "", raw)
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


def _resolveCodexWebSocketUrl(baseUrl: str | None = None) -> str:
    # MISAKA fork: `urllib.parse` stands in for the WHATWG `URL`.
    url = urlparse(_resolveCodexUrl(baseUrl))
    if url.scheme == "https":
        url = url._replace(scheme="wss")
    if url.scheme == "http":
        url = url._replace(scheme="ws")
    return url.geturl()


# ============================================================================
# Response Processing
# ============================================================================


async def _processStream(
    response: httpx.Response,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: Model,
    grammarToolInputProperties: Mapping[str, str],
    options: OpenAICodexResponsesOptions | Mapping[str, Any] | None = None,
) -> None:
    await processResponsesStream(
        _mapCodexEvents(
            _parseSSE(response, _option(options, "signal")), output, model, _option(options, "onProviderStreamEvent")
        ),
        output,
        stream,
        model,
        {
            "serviceTier": _option(options, "serviceTier"),
            "grammarToolInputProperties": grammarToolInputProperties,
            "resolveServiceTier": _resolveCodexServiceTier,
            "applyServiceTierPricing": lambda usage, serviceTier: _applyServiceTierPricing(usage, serviceTier, model),
        },
    )


class _CodexApiError(RuntimeError):
    def __init__(self, message: str, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.name = "CodexApiError"
        self.code = None if options is None else options.get("code")
        self.payload = None if options is None else options.get("payload")
        self.__cause__ = None if options is None else options.get("cause")


class _CodexProtocolError(RuntimeError):
    def __init__(self, message: str, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.name = "CodexProtocolError"
        self.payload = None if options is None else options.get("payload")
        self.__cause__ = None if options is None else options.get("cause")


class _ProviderStreamEventCallbackError(RuntimeError):
    def __init__(self, cause: Any) -> None:
        super().__init__(formatThrownValue(cause))
        self.name = "ProviderStreamEventCallbackError"
        self.__cause__ = cause if isinstance(cause, BaseException) else None


def _isCodexNonTransportError(error: Any) -> bool:
    return (
        isinstance(error, _CodexApiError)  # noqa: SIM101 - upstream spells out the three instanceof checks
        or isinstance(error, _CodexProtocolError)
        or isinstance(error, _ProviderStreamEventCallbackError)
    )


def _isWebSocketConnectionLimitReachedError(error: Any) -> bool:
    return isinstance(error, _CodexApiError) and error.code == _WEBSOCKET_CONNECTION_LIMIT_REACHED_CODE


def _isPreviousResponseNotFoundError(error: Any) -> bool:
    return isinstance(error, _CodexApiError) and error.code == _PREVIOUS_RESPONSE_NOT_FOUND_CODE


def _extractCodexEventError(event: Mapping[str, Any]) -> dict[str, str | None]:
    nested = event.get("error") if isinstance(event.get("error"), Mapping) else None
    return {
        "code": event["code"]
        if isinstance(event.get("code"), str)
        else nested["code"]
        if nested is not None and isinstance(nested.get("code"), str)
        else None,
        "message": event["message"]
        if isinstance(event.get("message"), str)
        else nested["message"]
        if nested is not None and isinstance(nested.get("message"), str)
        else None,
    }


async def _mapCodexEvents(
    events: AsyncIterable[dict[str, Any]],
    output: AssistantMessage,
    model: Model,
    onProviderStreamEvent: Any,
) -> AsyncIterator[dict[str, Any]]:
    # MISAKA fork: a JavaScript `for await` that stops early closes its source (`return()`); Python's
    # `async for` leaves it open, so it is closed here.
    try:
        async for event in events:
            try:
                if onProviderStreamEvent:
                    await maybe_await(onProviderStreamEvent(event, model))
            except Exception as error:
                # Keep callback failures out of Codex's WebSocket retry and SSE fallback path.
                raise _ProviderStreamEventCallbackError(error) from error
            type = event["type"] if isinstance(event.get("type"), str) else None
            if not type:
                continue

            if type == "error":
                extracted = _extractCodexEventError(event)
                code, message = extracted["code"], extracted["message"]
                raise _CodexApiError(
                    f"Codex error: {message or code or jsonStringify(event)}",
                    {
                        "code": code,
                        "payload": event,
                    },
                )

            if type == "response.failed":
                response = event.get("response") if isinstance(event.get("response"), Mapping) else None
                responseError = response.get("error") if response is not None and isinstance(response.get("error"), Mapping) else None
                code = None if responseError is None else responseError.get("code")
                message = None if responseError is None else responseError.get("message")
                raise _CodexApiError(message or "Codex response failed", {"code": code, "payload": event})

            if type == "response.done" or type == "response.completed" or type == "response.incomplete":
                response = event.get("response")
                if isinstance(response, Mapping) and isinstance(response.get("end_turn"), bool):
                    output.endTurn = response["end_turn"]
                normalizedResponse = (
                    {**response, "status": _normalizeCodexStatus(response.get("status"))} if isinstance(response, Mapping) else response
                )
                yield {**event, "type": "response.completed", "response": normalizedResponse}
                return

            yield event
    finally:
        await _close_stream(events)


def _normalizeCodexStatus(status: Any) -> str | None:
    if not isinstance(status, str):
        return None
    return status if status in _CODEX_RESPONSE_STATUSES else None


# ============================================================================
# SSE Parsing
# ============================================================================


async def _parseSSE(response: httpx.Response, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    # MISAKA fork: httpx's text iterator stands in for the body reader and its streaming `TextDecoder`; one abort task
    # for the whole body closes the response, as the abort listener cancels the reader.
    reader = _iterate_async_iterable(response.aiter_text(), signal, on_abort=lambda: response.aclose())
    buffer = ""

    try:
        while True:
            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")
            try:
                value = await reader.__anext__()
                done = False
            except StopAsyncIteration:
                value = ""
                done = True
            except httpx.ReadTimeout as error:
                # MISAKA fork: undici's body timeout ends a fetch body with "terminated".
                raise RuntimeError("terminated") from error
            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")
            buffer += value
            # Treat EOF as terminating the residual SSE frame.
            if done and jsTrim(buffer):
                buffer += "\n\n"

            idx = buffer.find("\n\n")
            while idx != -1:
                chunk = buffer[:idx]
                buffer = buffer[idx + 2 :]

                # `line`, not upstream's `l`: ruff's E741.
                dataLines = [jsTrim(line[5:]) for line in chunk.split("\n") if line.startswith("data:")]
                if len(dataLines) > 0:
                    data = jsTrim("\n".join(dataLines))
                    if data and data != "[DONE]":
                        try:
                            parsed = jsonParse(data)
                        except ValueError as cause:
                            raise _CodexProtocolError(
                                f"Invalid Codex SSE JSON: {formatThrownValue(cause)}",
                                {
                                    "cause": cause,
                                    "payload": data,
                                },
                            ) from cause
                        yield parsed
                        await asyncio.sleep(0)
                idx = buffer.find("\n\n")

            if done:
                break
    finally:
        await reader.aclose()


# ============================================================================
# WebSocket Parsing
# ============================================================================

_OPENAI_BETA_RESPONSES_WEBSOCKETS = "responses_websockets=2026-02-06"
_SESSION_WEBSOCKET_CACHE_TTL_MS = 5 * 60 * 1000
_SESSION_WEBSOCKET_MAX_AGE_MS = 55 * 60 * 1000


@dataclass(slots=True)
class _CachedWebSocketContinuationState:
    lastRequestBody: dict[str, Any]
    lastResponseId: str
    lastResponseItems: list[dict[str, Any]]


@dataclass(slots=True)
class _CachedWebSocketConnection:
    socket: Any
    busy: bool
    createdAt: int
    # MISAKA fork: the loop's timer handle stands in for `setTimeout`'s.
    idleTimer: asyncio.TimerHandle | None = None
    continuation: _CachedWebSocketContinuationState | None = None


@dataclass(slots=True)
class OpenAICodexWebSocketDebugStats:
    requests: int = 0
    connectionsCreated: int = 0
    connectionsReused: int = 0
    cachedContextRequests: int = 0
    storeTrueRequests: int = 0
    fullContextRequests: int = 0
    deltaRequests: int = 0
    lastInputItems: int = 0
    lastDeltaInputItems: int | None = None
    lastPreviousResponseId: str | None = None
    websocketFailures: int = 0
    sseFallbacks: int = 0
    websocketFallbackActive: bool | None = None
    lastWebSocketError: str | None = None


_websocketSessionCache: dict[str, dict[str, _CachedWebSocketConnection]] = {}
_websocketDebugStats: dict[str, OpenAICodexWebSocketDebugStats] = {}
_websocketSseFallbackSessions: set[str] = set()


def _getOrCreateWebSocketDebugStats(sessionId: str) -> OpenAICodexWebSocketDebugStats:
    stats = _websocketDebugStats.get(sessionId)
    if not stats:
        stats = OpenAICodexWebSocketDebugStats(
            requests=0,
            connectionsCreated=0,
            connectionsReused=0,
            cachedContextRequests=0,
            storeTrueRequests=0,
            fullContextRequests=0,
            deltaRequests=0,
            lastInputItems=0,
            websocketFailures=0,
            sseFallbacks=0,
        )
        _websocketDebugStats[sessionId] = stats
    return stats


def getOpenAICodexWebSocketDebugStats(sessionId: str) -> OpenAICodexWebSocketDebugStats | None:
    stats = _websocketDebugStats.get(sessionId)
    return replace(stats) if stats else None


def resetOpenAICodexWebSocketDebugStats(sessionId: str | None = None) -> None:
    if sessionId:
        _websocketDebugStats.pop(sessionId, None)
        _websocketSseFallbackSessions.discard(sessionId)
        return
    _websocketDebugStats.clear()
    _websocketSseFallbackSessions.clear()


def closeOpenAICodexWebSocketSessions(sessionId: str | None = None) -> None:
    def closeEntry(entry: _CachedWebSocketConnection) -> None:
        if entry.idleTimer:
            entry.idleTimer.cancel()
        _closeWebSocketSilently(entry.socket, 1000, "debug_close")

    if sessionId:
        for entry in list((_websocketSessionCache.get(sessionId) or {}).values()):
            closeEntry(entry)
        _websocketSessionCache.pop(sessionId, None)
        return
    for accountEntries in _websocketSessionCache.values():
        for entry in accountEntries.values():
            closeEntry(entry)
    _websocketSessionCache.clear()


registerSessionResourceCleanup(closeOpenAICodexWebSocketSessions)


def _isWebSocketSseFallbackActive(sessionId: str | None) -> bool:
    return sessionId in _websocketSseFallbackSessions if sessionId else False


def _recordWebSocketSseFallback(sessionId: str | None) -> None:
    if not sessionId:
        return
    stats = _getOrCreateWebSocketDebugStats(sessionId)
    stats.sseFallbacks += 1
    stats.websocketFallbackActive = _isWebSocketSseFallbackActive(sessionId)


def _recordWebSocketFailure(sessionId: str | None, error: Any) -> None:
    if not sessionId:
        return
    _websocketSseFallbackSessions.add(sessionId)

    stats = _getOrCreateWebSocketDebugStats(sessionId)
    stats.websocketFailures += 1
    stats.lastWebSocketError = formatThrownValue(error)
    stats.websocketFallbackActive = True


_cachedWebsocket: Callable[..., Any] | None = None


async def _getWebSocketConstructor(env: ProviderEnv | None = None) -> Callable[..., Any] | None:
    """MISAKA fork: pi picks a `WebSocket` constructor (with a proxy shim under Bun); Python has none built in, so the
    ``websockets`` package's client stands in, when installed. The proxy is chosen per connection
    (``_connectWebSocket``), so the client is cached whatever ``env`` is."""
    global _cachedWebsocket
    if _cachedWebsocket:
        return _cachedWebsocket
    try:
        module = importlib.import_module("websockets.asyncio.client")
    except ImportError:
        return None
    ctor = getattr(module, "connect", None)
    if not callable(ctor):
        return None
    _cachedWebsocket = ctor
    return ctor


class _WebSocketCloseError(RuntimeError):
    def __init__(self, message: str, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.name = "WebSocketCloseError"
        self.code = None if options is None else options.get("code")
        self.reason = None if options is None else options.get("reason")
        self.wasClean = None if options is None else options.get("wasClean")


def _getWebSocketReadyState(socket: Any) -> int | None:
    # MISAKA fork: a websockets connection reports `state`, whose values (CONNECTING 0 .. CLOSED 3) are numbered as the
    # DOM `readyState` is.
    readyState = getattr(socket, "state", None)
    return int(readyState) if isinstance(readyState, int) and not isinstance(readyState, bool) else None


def _isWebSocketReusable(socket: Any) -> bool:
    readyState = _getWebSocketReadyState(socket)
    # If readyState is unavailable, assume the runtime keeps it open/reusable.
    return readyState is None or readyState == 1


def _isWebSocketSessionExpired(entry: _CachedWebSocketConnection) -> bool:
    return int(time.time() * 1000) - entry.createdAt >= _SESSION_WEBSOCKET_MAX_AGE_MS


def _closeWebSocketSilently(socket: Any, code: int = 1000, reason: str = "done") -> None:
    try:
        result = socket.close(code, reason)
    except Exception:  # noqa: BLE001 - closing a dead websocket must not raise
        return
    # MISAKA fork: a websockets `close()` is a coroutine; it is scheduled, as the DOM close returns at once.
    if not hasattr(result, "__await__"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(result)
        except Exception:  # noqa: BLE001 - as above
            return
    else:
        loop.create_task(result)


def _scheduleSessionWebSocketExpiry(sessionId: str, accountId: str, entry: _CachedWebSocketConnection) -> None:
    if entry.idleTimer:
        entry.idleTimer.cancel()

    def expire() -> None:
        if entry.busy:
            return
        _closeWebSocketSilently(entry.socket, 1000, "idle_timeout")
        accountEntries = _websocketSessionCache.get(sessionId)
        if accountEntries is not None and accountEntries.get(accountId) is entry:
            del accountEntries[accountId]
        if accountEntries is not None and len(accountEntries) == 0:
            _websocketSessionCache.pop(sessionId, None)

    entry.idleTimer = asyncio.get_running_loop().call_later(_SESSION_WEBSOCKET_CACHE_TTL_MS / 1000, expire)


async def _connectWebSocket(
    url: str,
    headers: httpx.Headers,
    signal: Any = None,
    connectTimeoutMs: int | None = _DEFAULT_WEBSOCKET_CONNECT_TIMEOUT_MS,
    env: ProviderEnv | None = None,
) -> Any:
    WebSocketCtor = await _getWebSocketConstructor(env)
    if not WebSocketCtor:
        raise RuntimeError("WebSocket transport is not available in this runtime")

    wsHeaders = headersToRecord(headers)
    # The record's names are lower-cased, so this leaves the websocket beta header in place, as upstream.
    wsHeaders.pop("OpenAI-Beta", None)
    connectTimeoutMs = _DEFAULT_WEBSOCKET_CONNECT_TIMEOUT_MS if connectTimeoutMs is None else connectTimeoutMs

    # MISAKA fork: `websockets.connect` with `open_timeout` replaces the open/error/close listeners and the timer; the
    # proxy is resolved from `env` (and the process environment) for every runtime, as pi's Bun shim does.
    proxyUrl = resolveHttpProxyUrlForTarget(re.sub(r"^ws:", "http:", re.sub(r"^wss:", "https:", url)), env)
    if signal_aborted(signal):
        raise RuntimeError("Request was aborted")
    try:
        return await _await_with_signal(
            maybe_await(
                WebSocketCtor(
                    url,
                    additional_headers=wsHeaders,
                    max_size=None,
                    proxy=proxyUrl.geturl() if proxyUrl else None,
                    open_timeout=connectTimeoutMs / 1000 if connectTimeoutMs > 0 else None,
                )
            ),
            signal,
        )
    except TimeoutError as error:
        raise RuntimeError(f"WebSocket connect timeout after {connectTimeoutMs}ms") from error


class _AcquiredWebSocket(NamedTuple):
    """The object ``acquireWebSocket`` resolves to."""

    socket: Any
    entry: _CachedWebSocketConnection | None
    reused: bool
    release: Callable[..., None]


async def _acquireWebSocket(
    url: str,
    headers: httpx.Headers,
    sessionId: str | None,
    accountId: str,
    signal: Any = None,
    connectTimeoutMs: int | None = None,
    env: ProviderEnv | None = None,
) -> _AcquiredWebSocket:
    if not sessionId:
        socket = await _connectWebSocket(url, headers, signal, connectTimeoutMs, env)
        return _AcquiredWebSocket(
            socket=socket,
            entry=None,
            reused=False,
            release=lambda keep=False: _closeWebSocketSilently(socket),
        )

    accountEntries = _websocketSessionCache.get(sessionId)
    cached = None if accountEntries is None else accountEntries.get(accountId)
    if cached:
        if cached.idleTimer:
            cached.idleTimer.cancel()
            cached.idleTimer = None
        if not cached.busy and _isWebSocketSessionExpired(cached):
            _closeWebSocketSilently(cached.socket, 1000, "connection_age_limit")
            accountEntries.pop(accountId, None)
            if len(accountEntries) == 0:
                _websocketSessionCache.pop(sessionId, None)
        elif not cached.busy and _isWebSocketReusable(cached.socket):
            cached.busy = True

            def releaseCached(keep: bool = False) -> None:
                if not keep or not _isWebSocketReusable(cached.socket):
                    _closeWebSocketSilently(cached.socket)
                    currentEntries = _websocketSessionCache.get(sessionId)
                    if currentEntries is not None and currentEntries.get(accountId) is cached:
                        del currentEntries[accountId]
                    if currentEntries is not None and len(currentEntries) == 0:
                        _websocketSessionCache.pop(sessionId, None)
                    return
                cached.busy = False
                _scheduleSessionWebSocketExpiry(sessionId, accountId, cached)

            return _AcquiredWebSocket(
                socket=cached.socket,
                entry=cached,
                reused=True,
                release=releaseCached,
            )
        if cached.busy:
            socket = await _connectWebSocket(url, headers, signal, connectTimeoutMs, env)
            return _AcquiredWebSocket(
                socket=socket,
                entry=None,
                reused=False,
                release=lambda keep=False: _closeWebSocketSilently(socket),
            )
        if not _isWebSocketReusable(cached.socket):
            _closeWebSocketSilently(cached.socket)
            accountEntries.pop(accountId, None)
            if len(accountEntries) == 0:
                _websocketSessionCache.pop(sessionId, None)

    socket = await _connectWebSocket(url, headers, signal, connectTimeoutMs, env)
    entry = _CachedWebSocketConnection(socket=socket, busy=True, createdAt=int(time.time() * 1000))
    accountEntries = _websocketSessionCache.get(sessionId)
    if accountEntries is None:
        accountEntries = {}
        _websocketSessionCache[sessionId] = accountEntries
    accountEntries[accountId] = entry

    def releaseEntry(keep: bool = False) -> None:
        if not keep or not _isWebSocketReusable(entry.socket):
            _closeWebSocketSilently(entry.socket)
            if entry.idleTimer:
                entry.idleTimer.cancel()
            currentEntries = _websocketSessionCache.get(sessionId)
            if currentEntries is not None and currentEntries.get(accountId) is entry:
                del currentEntries[accountId]
            if currentEntries is not None and len(currentEntries) == 0:
                _websocketSessionCache.pop(sessionId, None)
            return
        entry.busy = False
        _scheduleSessionWebSocketExpiry(sessionId, accountId, entry)

    return _AcquiredWebSocket(
        socket=socket,
        entry=entry,
        reused=False,
        release=releaseEntry,
    )


def _extractWebSocketError(event: Any) -> BaseException:
    # MISAKA fork: the failure arrives as the exception websockets raised, not as a DOM error event.
    if isinstance(event, BaseException) and len(str(event)) > 0:
        return event
    message = getattr(event, "message", None)
    if isinstance(message, str) and len(message) > 0:
        return RuntimeError(message)
    return RuntimeError("WebSocket error")


def _extractWebSocketCloseError(event: Any) -> BaseException:
    # MISAKA fork: websockets keeps the peer's close frame on `rcvd` (None when the connection dropped without one:
    # code 1006); `code`/`reason` on the exception are deprecated aliases. `wasClean` is not reported.
    if event is not None:
        if hasattr(event, "rcvd"):
            rcvd = event.rcvd
            code = getattr(rcvd, "code", None) if rcvd is not None else 1006
            reason = getattr(rcvd, "reason", None) if rcvd is not None else ""
        else:
            code = getattr(event, "code", None)
            reason = getattr(event, "reason", None)
        wasClean = getattr(event, "wasClean", None)
        codeText = f" {code}" if isinstance(code, int) else ""
        reasonText = f" {reason}" if isinstance(reason, str) and len(reason) > 0 else ""
        if not reasonText and code == _WEBSOCKET_MESSAGE_TOO_BIG_CLOSE_CODE:
            reasonText = " message too big"
        return _WebSocketCloseError(
            jsTrim(f"WebSocket closed{codeText}{reasonText}"),
            {
                "code": code if isinstance(code, int) else None,
                "reason": reason if isinstance(reason, str) and len(reason) > 0 else None,
                "wasClean": wasClean if isinstance(wasClean, bool) else None,
            },
        )
    return RuntimeError("WebSocket closed")


def _decodeWebSocketData(data: Any) -> str | None:
    # MISAKA fork: websockets hands over `str` or `bytes`; there is no Blob or ArrayBuffer.
    if isinstance(data, str):
        return data
    if isinstance(data, bytes | bytearray | memoryview):
        return bytes(data).decode("utf-8", errors="replace")
    return None


async def _parseWebSocket(socket: Any, signal: Any = None, idleTimeoutMs: int | None = None) -> AsyncIterator[dict[str, Any]]:
    # MISAKA fork: a `recv()` loop replaces the message/error/close listeners and their queue. One abort task serves the
    # whole stream; the idle timeout races each receive.
    sawCompletion = False
    abortTask = _create_abort_wait_task(signal)
    try:
        while True:
            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")
            try:
                receive = maybe_await(socket.recv())
                data = await _await_with_signal(
                    asyncio.wait_for(receive, idleTimeoutMs / 1000) if idleTimeoutMs is not None and idleTimeoutMs > 0 else receive,
                    signal,
                    abort_task=abortTask,
                )
            except TimeoutError as error:
                _closeWebSocketSilently(socket, 1000, "idle_timeout")
                raise RuntimeError(f"WebSocket idle timeout after {idleTimeoutMs}ms") from error
            except RuntimeError as error:
                if str(error) == "Request was aborted":
                    raise
                raise _extractWebSocketError(error) from error
            except Exception as error:
                if sawCompletion:
                    return
                if error.__class__.__name__.startswith("ConnectionClosed"):
                    raise _extractWebSocketCloseError(error) from error
                raise _extractWebSocketError(error) from error

            text = _decodeWebSocketData(data)
            if not text:
                continue
            try:
                parsed = jsonParse(text)
            except ValueError as cause:
                raise _CodexProtocolError(
                    f"Invalid Codex WebSocket JSON: {formatThrownValue(cause)}",
                    {
                        "cause": cause,
                        "payload": text,
                    },
                ) from cause
            type = parsed["type"] if isinstance(parsed, dict) and isinstance(parsed.get("type"), str) else ""
            if type == "response.completed" or type == "response.done" or type == "response.incomplete":
                sawCompletion = True
            yield parsed
            await asyncio.sleep(0)
            if sawCompletion:
                return
    finally:
        if abortTask is not None:
            abortTask.cancel()


def _requestBodyWithoutInput(body: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "input" and key != "previous_response_id"}


def _responseInputsEqual(a: Any, b: Any) -> bool:
    return jsonStringify(nullishCoalesce(a, [])) == jsonStringify(nullishCoalesce(b, []))


def _requestBodiesMatchExceptInput(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return jsonStringify(_requestBodyWithoutInput(a)) == jsonStringify(_requestBodyWithoutInput(b))


def _getCachedWebSocketInputDelta(
    body: Mapping[str, Any],
    continuation: _CachedWebSocketContinuationState,
) -> list[Any] | None:
    if not _requestBodiesMatchExceptInput(body, continuation.lastRequestBody):
        return None

    currentInput = list(nullishCoalesce(body.get("input"), []))
    baseline = [*nullishCoalesce(continuation.lastRequestBody.get("input"), []), *continuation.lastResponseItems]
    if len(currentInput) < len(baseline):
        return None

    prefix = currentInput[: len(baseline)]
    if not _responseInputsEqual(prefix, baseline):
        return None

    return currentInput[len(baseline) :]


def _buildCachedWebSocketRequestBody(entry: _CachedWebSocketConnection, body: dict[str, Any]) -> dict[str, Any]:
    continuation = entry.continuation
    if not continuation:
        return body

    delta = _getCachedWebSocketInputDelta(body, continuation)
    # `is None`, not falsiness: an empty delta is still a continuation (`![]` is false in JS).
    if delta is None or not continuation.lastResponseId:
        entry.continuation = None
        return body

    return {
        **body,
        "previous_response_id": continuation.lastResponseId,
        "input": delta,
    }


async def _startWebSocketOutputOnFirstEvent(
    events: AsyncIterable[dict[str, Any]],
    onStart: Callable[[], None],
) -> AsyncIterator[dict[str, Any]]:
    started = False
    # MISAKA fork: a JavaScript `for await` that stops early closes its source (`return()`); Python's
    # `async for` leaves it open, so it is closed here.
    try:
        async for event in events:
            if not started:
                started = True
                onStart()
            yield event
    finally:
        await _close_stream(events)


async def _processWebSocketStream(
    url: str,
    body: dict[str, Any],
    headers: httpx.Headers,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: Model,
    onStart: Callable[[], None],
    idleTimeoutMs: int | None,
    websocketConnectTimeoutMs: int | None,
    cacheSessionId: str | None,
    accountId: str,
    grammarToolInputProperties: Mapping[str, str],
    options: OpenAICodexResponsesOptions | Mapping[str, Any] | None = None,
) -> None:
    socket, entry, reused, release = await _acquireWebSocket(
        url,
        headers,
        cacheSessionId,
        accountId,
        _option(options, "signal"),
        websocketConnectTimeoutMs,
        _option(options, "env"),
    )
    keepConnection = True
    useCachedContext = _option(options, "transport") == "websocket-cached" or _option(options, "transport") == "auto"
    # ChatGPT Codex Responses rejects `store: true` ("Store must be set to false").
    # WebSocket continuation still works via connection-scoped previous_response_id state.
    fullBody = body
    requestBody = _buildCachedWebSocketRequestBody(entry, fullBody) if useCachedContext and entry else fullBody
    stats = _getOrCreateWebSocketDebugStats(cacheSessionId) if cacheSessionId else None
    if stats:
        stats.requests += 1
        if reused:
            stats.connectionsReused += 1
        else:
            stats.connectionsCreated += 1
        if useCachedContext:
            stats.cachedContextRequests += 1
        if requestBody.get("store") is True:
            stats.storeTrueRequests += 1
        stats.lastInputItems = len(nullishCoalesce(requestBody.get("input"), []))
        if requestBody.get("previous_response_id"):
            stats.deltaRequests += 1
            stats.lastDeltaInputItems = len(nullishCoalesce(requestBody.get("input"), []))
            stats.lastPreviousResponseId = requestBody["previous_response_id"]
        else:
            stats.fullContextRequests += 1
            stats.lastDeltaInputItems = None
            stats.lastPreviousResponseId = None
    try:
        await maybe_await(socket.send(jsonStringify({"type": "response.create", **requestBody})))
        await processResponsesStream(
            _startWebSocketOutputOnFirstEvent(
                _mapCodexEvents(
                    _parseWebSocket(socket, _option(options, "signal"), idleTimeoutMs),
                    output,
                    model,
                    _option(options, "onProviderStreamEvent"),
                ),
                onStart,
            ),
            output,
            stream,
            model,
            {
                "serviceTier": _option(options, "serviceTier"),
                "grammarToolInputProperties": grammarToolInputProperties,
                "resolveServiceTier": _resolveCodexServiceTier,
                "applyServiceTierPricing": lambda usage, serviceTier: _applyServiceTierPricing(usage, serviceTier, model),
            },
        )
        if signal_aborted(_option(options, "signal")):
            keepConnection = False
        elif useCachedContext and entry and output.responseId:
            responseItems = [
                item
                for item in convertResponsesMessages(
                    model,
                    normalizeContext(Context(messages=[output])),
                    _CODEX_TOOL_CALL_PROVIDERS,
                    {
                        "includeSystemPrompt": False,
                        "grammarToolInputProperties": grammarToolInputProperties,
                    },
                )
                if item.get("type") != "function_call_output" and item.get("type") != "custom_tool_call_output"
            ]
            entry.continuation = _CachedWebSocketContinuationState(
                lastRequestBody=fullBody,
                lastResponseId=output.responseId,
                lastResponseItems=responseItems,
            )
    except BaseException:
        if entry:
            entry.continuation = None
        keepConnection = False
        raise
    finally:
        release(keep=keepConnection)


# ============================================================================
# Error Handling
# ============================================================================


async def _parseErrorResponse(response: httpx.Response) -> dict[str, str]:
    raw = response.text
    message = raw or response.reason_phrase or "Request failed"
    friendlyMessage: str | None = None

    try:
        parsed = jsonParse(raw)
        err = parsed.get("error") if isinstance(parsed, Mapping) else None
        if err:
            code = err.get("code") or err.get("type") or ""
            if re.search(r"usage_limit_reached|usage_not_included|rate_limit_exceeded", jsString(code), re.IGNORECASE) or (
                response.status_code == 429
            ):
                plan = f" ({err['plan_type'].lower()} plan)" if err.get("plan_type") else ""
                mins = (
                    max(0, round((err["resets_at"] * 1000 - time.time() * 1000) / 60000)) if err.get("resets_at") else None
                )
                when = f" Try again in ~{mins} min." if mins is not None else ""
                friendlyMessage = jsTrim(f"You have hit your ChatGPT usage limit{plan}.{when}")
            message = err.get("message") or friendlyMessage or message
    except Exception:  # noqa: BLE001, S110 - `catch {}`: a body that is not the expected JSON keeps the raw text
        pass

    return {"message": message, **({} if friendlyMessage is None else {"friendlyMessage": friendlyMessage})}


# ============================================================================
# Auth & Headers
# ============================================================================


def _extractAccountId(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise RuntimeError("Invalid token")
        # MISAKA fork: decoded as base64url (with its padding restored), the alphabet a JWT segment is written in;
        # `atob` takes standard base64 only.
        payload = jsonParse(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8"))
        claims = payload.get(_JWT_CLAIM_PATH) if isinstance(payload, Mapping) else None
        accountId = claims.get("chatgpt_account_id") if isinstance(claims, Mapping) else None
        if not accountId:
            raise RuntimeError("No account ID in token")
        return accountId
    except Exception as error:
        raise RuntimeError("Failed to extract accountId from token") from error


def _buildBaseCodexHeaders(
    initHeaders: Mapping[str, str] | None,
    additionalHeaders: ProviderHeaders | None,
    accountId: str,
    token: str,
) -> httpx.Headers:
    # Defaults first so model and caller headers can override them, matching the other providers.
    # MISAKA fork: the default originator is MISAKA's client name; sending pi's would credit this traffic to it, which
    # is what CLIENT_NAME exists to prevent.
    headers = httpx.Headers({"originator": CLIENT_NAME, "User-Agent": getPiUserAgent()})
    for key, value in (initHeaders or {}).items():
        headers[key] = value
    for key, value in (additionalHeaders or {}).items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    headers["Authorization"] = f"Bearer {token}"
    headers["chatgpt-account-id"] = accountId
    return headers


def _buildSSEHeaders(
    initHeaders: Mapping[str, str] | None,
    additionalHeaders: ProviderHeaders | None,
    accountId: str,
    token: str,
    sessionId: str | None = None,
) -> httpx.Headers:
    headers = _buildBaseCodexHeaders(initHeaders, additionalHeaders, accountId, token)
    headers["OpenAI-Beta"] = "responses=experimental"
    headers["accept"] = "text/event-stream"
    headers["content-type"] = "application/json"

    if sessionId:
        headers["session-id"] = sessionId
        headers["x-client-request-id"] = sessionId

    return headers


def _buildWebSocketHeaders(
    initHeaders: Mapping[str, str] | None,
    additionalHeaders: ProviderHeaders | None,
    accountId: str,
    token: str,
    requestId: str,
) -> httpx.Headers:
    headers = _buildBaseCodexHeaders(initHeaders, additionalHeaders, accountId, token)
    headers.pop("accept", None)
    headers.pop("content-type", None)
    headers.pop("OpenAI-Beta", None)
    headers.pop("openai-beta", None)
    headers["OpenAI-Beta"] = _OPENAI_BETA_RESPONSES_WEBSOCKETS
    headers["x-client-request-id"] = requestId
    headers["session-id"] = requestId
    return headers


__all__ = [
    "OpenAICodexResponsesOptions",
    "OpenAICodexWebSocketDebugStats",
    "closeOpenAICodexWebSocketSessions",
    "getOpenAICodexWebSocketDebugStats",
    "resetOpenAICodexWebSocketDebugStats",
    "stream",
    "streamSimple",
]
