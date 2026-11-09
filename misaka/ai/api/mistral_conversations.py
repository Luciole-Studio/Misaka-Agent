"""Translated from pi's ``packages/ai/src/api/mistral-conversations.ts`` (v1.3.0).

MISAKA fork, kept to what the Python platform forces (each is marked where it happens):

- httpx stands in for ``fetch``: its read timeout is the configured dispatcher's idle timeout (undici's, which bounds
  body stalls upstream), a 204/205 stands in for a null body, and the event reader closes the response and the client
  it was handed.
- A tool call's ``partialArgs`` scratch buffer rides beside the block (``StreamingArgs``), and the final parse is
  upstream's ``parseToolCallArguments`` (``finish_into``).
- The event iterator is closed with the reply, and after each event the loop turns once, as a JavaScript ``for await``
  hands control to the consumer.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit, urlunsplit

import httpx

from misaka.ai.api._common import _await_with_signal, _create_abort_wait_task, _option
from misaka.ai.api.constrained_sampling import (
    getJsonSchemaToolParameters,
    resolveJsonSchemaStrictSampling,
)
from misaka.ai.api.simple_options import buildBaseOptions
from misaka.ai.api.transform_messages import transformMessages
from misaka.ai.models import calculateCost, clampThinkingLevel
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    DoneEvent,
    ErrorEvent,
    MessageValue,
    Model,
    ProviderEnv,
    ProviderHeaders,
    SimpleStreamOptions,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    Tool,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.abort_signals import combineAbortSignals
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.hash import shortHash
from misaka.ai.utils.headers import headersToRecord
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.ai.utils.text import getSystemMessageText, renderSystemMessageUpdate
from misaka.ai.utils.transcript import getCurrentTools, resolveTranscript
from misaka.core.http_dispatcher import dispatcherIdleTimeout
from misaka.utils._abort_signal import TimeoutAbortSignal, abort_reason
from misaka.utils.js import (
    jsonParse,
    jsonStringify,
    jsString,
    jsTrim,
    jsTrimStart,
    nullishCoalesce,
)
from misaka.utils.values import maybe_await, signal_aborted

_MISTRAL_TOOL_CALL_ID_LENGTH = 9
_MAX_MISTRAL_ERROR_BODY_CHARS = 4000

# Provider-specific options for the Mistral API.
_MistralReasoningEffort = Literal["none", "low", "medium", "high", "max"]


class _MistralToolChoiceFunction(TypedDict):
    name: str


class _MistralToolChoiceObject(TypedDict):
    type: Literal["function"]
    function: _MistralToolChoiceFunction


class MistralOptions(TypedDict, total=False):
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
    toolChoice: Literal["auto", "none", "any", "required"] | _MistralToolChoiceObject
    promptMode: Literal["reasoning"]
    reasoningEffort: _MistralReasoningEffort


def stream(
    model: Model,
    context: TranscriptContext,
    options: MistralOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    """Stream responses from the native Mistral Chat Completions endpoint."""
    stream = AssistantMessageEventStream()
    normalizedContext = resolveTranscript(context, getattr(model.compat, "supportsMidConvoSystemMessages", None))

    async def run() -> None:
        output = _createOutput(model)

        try:
            apiKey = _option(options, "apiKey")
            if not apiKey:
                raise RuntimeError(f"No API key for provider: {model.provider}")

            normalizeMistralToolCallId = _createMistralToolCallIdNormalizer()
            transformedMessages = transformMessages(
                normalizedContext.messages, model, lambda id, *_: normalizeMistralToolCallId(id)
            )

            payload = _buildChatPayload(model, normalizedContext, transformedMessages, options)
            onPayload = _option(options, "onPayload")
            nextPayload = await maybe_await(onPayload(payload, model)) if onPayload else None
            if nextPayload is not None:
                payload = nextPayload
            mistralStream = await _requestMistralStream(model, payload, apiKey, options)
            stream.push(StartEvent(partial=output))
            try:
                await _consumeChatStream(model, output, stream, mistralStream, _option(options, "onProviderStreamEvent"))
            finally:
                # MISAKA fork: an async generator left mid-iteration is not released until the
                # loop finalizes it; closing it here releases the response straight away.
                await mistralStream.aclose()

            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")

            if output.stopReason == "pending":
                raise RuntimeError("Mistral stream ended without a finish reason")
            if output.stopReason == "aborted" or output.stopReason == "error":
                raise RuntimeError(output.errorMessage or "An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
            stream.end()
        except Exception as error:  # noqa: BLE001 - every failure becomes the stream's error event
            # MISAKA fork: partialArgs lives beside the blocks (`StreamingArgs`), so there is nothing to strip here.
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            output.errorMessage = _formatMistralError(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
            stream.end()

    spawn_stream_task(run(), stream=stream)
    return stream


def streamSimple(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    """Maps provider-agnostic `SimpleStreamOptions` to Mistral options."""
    apiKey = _option(options, "apiKey")
    if not apiKey:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = {
        **dict(buildBaseOptions(model, context, options, apiKey)),
        "toolChoice": _option(options, "toolChoice"),
    }
    clampedReasoning = clampThinkingLevel(model, _option(options, "reasoning")) if _option(options, "reasoning") else None
    reasoning = None if clampedReasoning == "off" else clampedReasoning
    # Models with a thinking level map use `reasoning_effort`; other reasoning models use `prompt_mode`.
    effortMap = model.thinkingLevelMap if model.reasoning else None
    reasoningEffort = (
        (nullishCoalesce(effortMap.get(reasoning), "high") if reasoning else nullishCoalesce(effortMap.get("off"), None))
        if effortMap is not None
        else None
    )

    return stream(
        model,
        context,
        {
            **base,
            "promptMode": "reasoning" if model.reasoning and not effortMap and reasoning else None,
            "reasoningEffort": reasoningEffort,
        },
    )


def _createOutput(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
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


def _createMistralToolCallIdNormalizer() -> Any:
    idMap: dict[str, str] = {}
    reverseMap: dict[str, str] = {}

    def normalize(id: str) -> str:
        existing = idMap.get(id)
        if existing:
            return existing

        attempt = 0
        while True:
            candidate = _deriveMistralToolCallId(id, attempt)
            owner = reverseMap.get(candidate)
            if not owner or owner == id:
                idMap[id] = candidate
                reverseMap[candidate] = id
                return candidate
            attempt += 1

    return normalize


def _deriveMistralToolCallId(id: str, attempt: int) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]", "", id)
    if attempt == 0 and len(normalized) == _MISTRAL_TOOL_CALL_ID_LENGTH:
        return normalized
    seedBase = normalized or id
    seed = seedBase if attempt == 0 else f"{seedBase}:{attempt}"
    return re.sub(r"[^a-zA-Z0-9]", "", shortHash(seed))[:_MISTRAL_TOOL_CALL_ID_LENGTH]


def _formatMistralError(error: Any) -> str:
    if isinstance(error, BaseException):
        statusCode = getattr(error, "statusCode", None)
        statusCode = statusCode if isinstance(statusCode, int | float) and not isinstance(statusCode, bool) else None
        body = getattr(error, "body", None)
        bodyText = jsTrim(body) if isinstance(body, str) else None
        if statusCode is not None and bodyText:
            return f"Mistral API error ({jsString(statusCode)}): {_truncateErrorText(bodyText, _MAX_MISTRAL_ERROR_BODY_CHARS)}"
        if statusCode is not None:
            return f"Mistral API error ({jsString(statusCode)}): {error}"
        return str(error)
    return _safeJsonStringify(error)


def _truncateErrorText(text: str, maxChars: int) -> str:
    if len(text) <= maxChars:
        return text
    return f"{text[:maxChars]}... [truncated {len(text) - maxChars} chars]"


def _safeJsonStringify(value: Any) -> str:
    try:
        return jsonStringify(value)
    except Exception:  # noqa: BLE001 - `catch`: whatever cannot be serialized prints as `String(value)`
        return jsString(value)


async def _requestMistralStream(
    model: Model,
    payload: Mapping[str, Any],
    apiKey: str,
    options: MistralOptions | Mapping[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    baseUrl = urlsplit(model.baseUrl)
    url = urlunsplit((baseUrl.scheme, baseUrl.netloc, f"{re.sub(r'/+$', '', baseUrl.path)}/v1/chat/completions", "", ""))
    headers = _buildMistralHeaders(model, apiKey, options)
    # The timeout covers only the wait for response headers. Long streams (e.g. extended thinking)
    # must not be cut off by a fixed deadline; body stalls are left to the HTTP client idle timeout.
    timeoutMs = nullishCoalesce(_option(options, "timeoutMs"), 60_000)
    headerTimeoutSignal = TimeoutAbortSignal(timeoutMs)
    combinedSignal = combineAbortSignals([_option(options, "signal"), headerTimeoutSignal])
    # MISAKA fork: undici's idle timeout, which bounds a stalled body upstream, is httpx's read timeout here; httpx's
    # defaults would add a five-second one.
    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(None, read=dispatcherIdleTimeout()))
    try:
        request = client.build_request(
            "POST",
            url,
            headers=headers,
            content=jsonStringify(_toMistralWirePayload(payload)).encode("utf-8"),
        )
        try:
            response = await _awaitOrThrowReason(client.send(request, stream=True), combinedSignal.signal)
        except Exception:
            if headerTimeoutSignal.aborted and not signal_aborted(_option(options, "signal")):
                raise RuntimeError(f"Mistral response headers timed out after {jsString(timeoutMs)}ms") from None
            raise
        finally:
            combinedSignal.cleanup()

        onResponse = _option(options, "onResponse")
        if onResponse:
            await maybe_await(onResponse({"status": response.status_code, "headers": headersToRecord(response.headers)}, model))

        if not response.is_success:
            # MISAKA fork: `response.text()` reads under no signal once the header wait is over; the caller's still
            # aborts it, as it would cancel the fetch body.
            body = (await _awaitOrThrowReason(response.aread(), _option(options, "signal"))).decode("utf-8", errors="replace")
            await response.aclose()
            raise _MistralHttpError(response.status_code, body, response.reason_phrase)
        # MISAKA fork: a fetch Response has a null body exactly for these statuses; httpx always has a stream.
        if response.status_code in (204, 205):
            await response.aclose()
            raise RuntimeError("Mistral response has no body")
    except BaseException:
        await client.aclose()
        raise

    return _readMistralEvents(response, _option(options, "signal"), client)


class _MistralHttpError(Exception):
    def __init__(self, statusCode: int, body: str, statusText: str) -> None:
        super().__init__(statusText or f"Request failed with status {statusCode}")
        self.name = "MistralHttpError"
        self.statusCode = statusCode
        self.body = body


def _buildMistralHeaders(model: Model, apiKey: str, options: MistralOptions | Mapping[str, Any] | None = None) -> httpx.Headers:
    headers = httpx.Headers({
        "User-Agent": getPiUserAgent(),
        "accept": "text/event-stream",
        "authorization": f"Bearer {apiKey}",
        "content-type": "application/json",
    })
    _applyMistralHeaderOverrides(headers, model.headers)
    _applyMistralHeaderOverrides(headers, _option(options, "headers"))

    hasExplicitAffinity = _hasMistralHeaderOverride(model.headers, "x-affinity") or _hasMistralHeaderOverride(
        _option(options, "headers"), "x-affinity"
    )
    if _shouldUsePromptCaching(options) and not hasExplicitAffinity:
        headers["x-affinity"] = _option(options, "sessionId")

    return headers


def _applyMistralHeaderOverrides(headers: httpx.Headers, overrides: Mapping[str, str | None] | None = None) -> None:
    if not overrides:
        return
    for name, value in overrides.items():
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value


def _hasMistralHeaderOverride(overrides: Mapping[str, Any] | None, target: str) -> bool:
    return bool(overrides) and any(name.lower() == target for name in overrides)


def _toMistralWirePayload(payload: Mapping[str, Any]) -> dict[str, Any]:
    wirePayload: dict[str, Any] = dict(payload)
    for source, target in (
        ("topP", "top_p"),
        ("maxTokens", "max_tokens"),
        ("randomSeed", "random_seed"),
        ("responseFormat", "response_format"),
        ("toolChoice", "tool_choice"),
        ("presencePenalty", "presence_penalty"),
        ("frequencyPenalty", "frequency_penalty"),
        ("parallelToolCalls", "parallel_tool_calls"),
        ("reasoningEffort", "reasoning_effort"),
        ("promptMode", "prompt_mode"),
        ("promptCacheKey", "prompt_cache_key"),
        ("safePrompt", "safe_prompt"),
    ):
        _remapMistralProperty(wirePayload, source, target)
    wirePayload["messages"] = [_toMistralWireMessage(message) for message in payload["messages"]]

    responseFormat = wirePayload.get("response_format")
    if _isMistralRecord(responseFormat):
        wireResponseFormat = dict(responseFormat)
        _remapMistralProperty(wireResponseFormat, "jsonSchema", "json_schema")
        jsonSchema = wireResponseFormat.get("json_schema")
        if _isMistralRecord(jsonSchema):
            wireJsonSchema = dict(jsonSchema)
            _remapMistralProperty(wireJsonSchema, "schemaDefinition", "schema")
            wireResponseFormat["json_schema"] = wireJsonSchema
        wirePayload["response_format"] = wireResponseFormat

    return wirePayload


def _toMistralWireMessage(message: Mapping[str, Any]) -> dict[str, Any]:
    wireMessage: dict[str, Any] = dict(message)
    _remapMistralProperty(wireMessage, "toolCalls", "tool_calls")
    _remapMistralProperty(wireMessage, "toolCallId", "tool_call_id")
    if isinstance(message.get("content"), list):
        wireMessage["content"] = [_toMistralWireContentChunk(chunk) for chunk in message["content"]]
    return wireMessage


def _toMistralWireContentChunk(chunk: Mapping[str, Any]) -> dict[str, Any]:
    wireChunk: dict[str, Any] = dict(chunk)
    for source, target in (
        ("imageUrl", "image_url"),
        ("documentUrl", "document_url"),
        ("documentName", "document_name"),
        ("fileId", "file_id"),
        ("referenceIds", "reference_ids"),
        ("inputAudio", "input_audio"),
    ):
        _remapMistralProperty(wireChunk, source, target)
    return wireChunk


def _remapMistralProperty(record: dict[str, Any], source: str, target: str) -> None:
    if source not in record:
        return
    record[target] = record[source]
    del record[source]


def _isMistralRecord(value: Any) -> bool:
    return isinstance(value, Mapping)


_MISTRAL_STREAM_DONE = object()


async def _readMistralEvents(
    response: httpx.Response, signal: Any | None = None, client: httpx.AsyncClient | None = None
) -> AsyncIterator[dict[str, Any]]:
    # MISAKA fork: httpx's text iterator stands in for the body reader and its streaming `TextDecoder`; the response
    # and the client are closed here, as the reader is cancelled. One abort task serves the whole body.
    buffer = ""
    reader = response.aiter_text().__aiter__()
    abortTask = _create_abort_wait_task(signal)

    try:
        while True:
            if signal_aborted(signal):
                raise abort_reason(signal)
            try:
                value = await _awaitOrThrowReason(reader.__anext__(), signal, abortTask)
                done = False
            except StopAsyncIteration:
                value = ""
                done = True
            if signal_aborted(signal):
                raise abort_reason(signal)
            buffer += value

            boundary = _findMistralEventBoundary(buffer)
            while boundary:
                event = _parseMistralEvent(buffer[: boundary["index"]])
                buffer = buffer[boundary["index"] + boundary["length"] :]
                if event is _MISTRAL_STREAM_DONE:
                    return
                if event:
                    yield event
                    await asyncio.sleep(0)
                boundary = _findMistralEventBoundary(buffer)

            if done:
                break

        if jsTrim(buffer):
            event = _parseMistralEvent(buffer)
            if event is not _MISTRAL_STREAM_DONE and event:
                yield event
    finally:
        if abortTask is not None:
            abortTask.cancel()
        await response.aclose()
        if client is not None:
            await client.aclose()


def _findMistralEventBoundary(buffer: str) -> dict[str, int] | None:
    match = re.search(r"\r\n\r\n|\r\n\r|\r\n\n|\r\r\n|\n\r\n|\r\r|\n\r|\n\n", buffer)
    return None if match is None else {"index": match.start(), "length": len(match.group(0))}


def _parseMistralEvent(raw: str) -> dict[str, Any] | object | None:
    data = jsTrim("\n".join(jsTrimStart(line[5:]) for line in re.split(r"\r\n|\r|\n", raw) if line.startswith("data:")))
    if not data:
        return None
    if data == "[DONE]":
        return _MISTRAL_STREAM_DONE

    parsed = jsonParse(data)
    if not _isMistralRecord(parsed) or not isinstance(parsed.get("choices"), list):
        raise RuntimeError("Invalid Mistral streaming event")
    return {"data": parsed}


async def _awaitOrThrowReason(awaitable: Any, signal: Any, abortTask: asyncio.Task[None] | None = None) -> Any:
    """MISAKA fork: await under ``signal``, raising its reason when it fires -- what a fetch or a read rejects with
    once its signal aborts."""
    try:
        return await _await_with_signal(awaitable, signal, abort_task=abortTask)
    except RuntimeError:
        if signal_aborted(signal):
            raise abort_reason(signal) from None
        raise


def _buildChatPayload(
    model: Model,
    context: TranscriptContext,
    messages: Sequence[MessageValue],
    options: MistralOptions | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model.id,
        "stream": True,
        "messages": _toChatMessages(messages, "image" in model.input),
    }

    currentTools = getCurrentTools(context.messages)
    if len(currentTools) > 0:
        payload["tools"] = _toFunctionTools(currentTools)
    if _option(options, "temperature") is not None:
        payload["temperature"] = _option(options, "temperature")
    if _option(options, "maxTokens") is not None:
        payload["maxTokens"] = _option(options, "maxTokens")
    if _option(options, "toolChoice"):
        payload["toolChoice"] = _mapToolChoice(_option(options, "toolChoice"))
    if _option(options, "promptMode"):
        payload["promptMode"] = _option(options, "promptMode")
    if _option(options, "reasoningEffort"):
        payload["reasoningEffort"] = _option(options, "reasoningEffort")
    if _shouldUsePromptCaching(options):
        payload["promptCacheKey"] = _option(options, "sessionId")

    return payload


def _shouldUsePromptCaching(options: MistralOptions | Mapping[str, Any] | None = None) -> bool:
    return _option(options, "cacheRetention") != "none" and bool(_option(options, "sessionId"))


def _getMistralCachedPromptTokens(usage: Any, promptTokens: float) -> float:
    def field(value: Any, key: str) -> Any:
        return value.get(key) if isinstance(value, Mapping) else None

    rawUsage = usage
    rawCachedTokens = nullishCoalesce(
        field(field(rawUsage, "promptTokensDetails"), "cachedTokens"),
        nullishCoalesce(
            field(field(rawUsage, "prompt_tokens_details"), "cached_tokens"),
            nullishCoalesce(
                field(field(rawUsage, "promptTokenDetails"), "cachedTokens"),
                nullishCoalesce(
                    field(field(rawUsage, "prompt_token_details"), "cached_tokens"),
                    nullishCoalesce(field(rawUsage, "numCachedTokens"), nullishCoalesce(field(rawUsage, "num_cached_tokens"), 0)),
                ),
            ),
        ),
    )
    cachedTokens = (
        rawCachedTokens
        if isinstance(rawCachedTokens, int | float) and not isinstance(rawCachedTokens, bool) and math.isfinite(rawCachedTokens)
        else 0
    )
    return min(promptTokens, max(0, cachedTokens))


async def _consumeChatStream(
    model: Model,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    mistralStream: AsyncIterable[dict[str, Any]],
    onProviderStreamEvent: Any,
) -> None:
    currentBlock: TextContent | ThinkingContent | None = None
    blocks = output.content
    toolBlocksByKey: dict[str | int, int] = {}
    # MISAKA fork: the arguments accumulate in StreamingArgs beside the block, not in a
    # `partialArgs` field on it, and the final parse flags malformed JSON.
    partialArgsByIndex: dict[int, StreamingArgs] = {}

    def blockIndex() -> int:
        return len(blocks) - 1

    def finishCurrentBlock(block: TextContent | ThinkingContent | None = None) -> None:
        if not block:
            return
        if block.type == "text":
            stream.push(
                TextEndEvent(
                    contentIndex=blockIndex(),
                    content=block.text,
                    partial=output,
                )
            )
            return
        if block.type == "thinking":
            stream.push(
                ThinkingEndEvent(
                    contentIndex=blockIndex(),
                    content=block.thinking,
                    partial=output,
                )
            )

    async for event in mistralStream:
        chunk = event["data"]
        if onProviderStreamEvent:
            await maybe_await(onProviderStreamEvent(chunk, model))
        # Mistral's streamed CompletionChunk carries an id field. Keep the first non-empty one,
        # mirroring how OpenAI-style streaming exposes a stable response identifier per stream.
        output.responseId = output.responseId or chunk.get("id")

        if chunk.get("usage") is not None:
            promptTokens = chunk["usage"].get("prompt_tokens") or 0
            cachedPromptTokens = _getMistralCachedPromptTokens(chunk["usage"], promptTokens)

            output.usage.input = max(0, promptTokens - cachedPromptTokens)
            output.usage.output = chunk["usage"].get("completion_tokens") or 0
            output.usage.cacheRead = cachedPromptTokens
            output.usage.cacheWrite = 0
            output.usage.totalTokens = chunk["usage"].get("total_tokens") or (
                output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite
            )
            calculateCost(model, output.usage)

        choice = chunk["choices"][0] if chunk["choices"] else None
        if not choice:
            continue

        if choice.get("finish_reason"):
            output.rawStopReason = choice["finish_reason"]
            stopReasonResult = _mapChatStopReason(choice["finish_reason"])
            output.stopReason = stopReasonResult["stopReason"]
            if stopReasonResult.get("errorMessage"):
                output.errorMessage = stopReasonResult["errorMessage"]

        delta = choice["delta"]
        if delta.get("content") is not None:
            contentItems = [delta["content"]] if isinstance(delta["content"], str) else delta["content"]
            for item in contentItems:
                if isinstance(item, str):
                    textDelta = sanitizeSurrogates(item)
                    # GLM models on Mistral send empty content deltas around thinking and tool calls.
                    # Opening a block for them splits thinking into multiple blocks, which Mistral rejects on replay.
                    if not textDelta:
                        continue
                    if not currentBlock or currentBlock.type != "text":
                        finishCurrentBlock(currentBlock)
                        currentBlock = TextContent(text="")
                        output.content.append(currentBlock)
                        stream.push(TextStartEvent(contentIndex=blockIndex(), partial=output))
                    currentBlock.text += textDelta
                    stream.push(
                        TextDeltaEvent(
                            contentIndex=blockIndex(),
                            delta=textDelta,
                            partial=output,
                        )
                    )
                    continue

                if item.get("type") == "thinking":
                    deltaText = "".join(
                        text
                        for text in (nullishCoalesce(part.get("text"), "") for part in nullishCoalesce(item.get("thinking"), []))
                        if len(text) > 0
                    )
                    thinkingDelta = sanitizeSurrogates(deltaText)
                    if not thinkingDelta:
                        continue
                    if not currentBlock or currentBlock.type != "thinking":
                        finishCurrentBlock(currentBlock)
                        currentBlock = ThinkingContent(thinking="")
                        output.content.append(currentBlock)
                        stream.push(ThinkingStartEvent(contentIndex=blockIndex(), partial=output))
                    currentBlock.thinking += thinkingDelta
                    stream.push(
                        ThinkingDeltaEvent(
                            contentIndex=blockIndex(),
                            delta=thinkingDelta,
                            partial=output,
                        )
                    )
                    continue

                if item.get("type") == "text":
                    textDelta = sanitizeSurrogates(nullishCoalesce(item.get("text"), ""))
                    if not textDelta:
                        continue
                    if not currentBlock or currentBlock.type != "text":
                        finishCurrentBlock(currentBlock)
                        currentBlock = TextContent(text="")
                        output.content.append(currentBlock)
                        stream.push(TextStartEvent(contentIndex=blockIndex(), partial=output))
                    currentBlock.text += textDelta
                    stream.push(
                        TextDeltaEvent(
                            contentIndex=blockIndex(),
                            delta=textDelta,
                            partial=output,
                        )
                    )

        toolCalls = delta.get("tool_calls") or []
        for toolCall in toolCalls:
            if currentBlock:
                finishCurrentBlock(currentBlock)
                currentBlock = None
            callId = (
                toolCall["id"]
                if toolCall.get("id") and toolCall["id"] != "null"
                else _deriveMistralToolCallId(f"toolcall:{jsString(nullishCoalesce(toolCall.get('index'), 0))}", 0)
            )
            key = nullishCoalesce(toolCall.get("index"), callId)
            existingIndex = toolBlocksByKey.get(key)
            block: ToolCall | None = None

            if existingIndex is not None:
                existing = output.content[existingIndex] if existingIndex < len(output.content) else None
                if existing is not None and existing.type == "toolCall":
                    block = existing

            if not block:
                block = ToolCall(
                    id=callId,
                    name=toolCall["function"]["name"],
                    arguments={},
                )
                output.content.append(block)
                toolBlocksByKey[key] = len(output.content) - 1
                partialArgsByIndex[len(output.content) - 1] = StreamingArgs("")
                stream.push(ToolCallStartEvent(contentIndex=len(output.content) - 1, partial=output))

            argsDelta = (
                toolCall["function"]["arguments"]
                if isinstance(toolCall["function"].get("arguments"), str)
                else jsonStringify(toolCall["function"].get("arguments") or {})
            )
            partialArgs = partialArgsByIndex.setdefault(toolBlocksByKey[key], StreamingArgs(""))
            partialArgs.append(argsDelta)
            block.arguments = partialArgs.arguments
            stream.push(
                ToolCallDeltaEvent(
                    contentIndex=toolBlocksByKey[key],
                    delta=argsDelta,
                    partial=output,
                )
            )

    finishCurrentBlock(currentBlock)
    for index in toolBlocksByKey.values():
        block = output.content[index]
        if block.type != "toolCall":
            continue
        toolBlock = block
        # `Object.assign(toolBlock, parseToolCallArguments(toolBlock.partialArgs))`.
        partialArgsByIndex.get(index, StreamingArgs("")).finish_into(toolBlock)
        # Finalize in-place and strip the scratch buffer so replay only
        # carries parsed arguments.
        partialArgsByIndex.pop(index, None)
        stream.push(
            ToolCallEndEvent(
                contentIndex=index,
                toolCall=toolBlock,
                partial=output,
            )
        )


def _toFunctionTools(tools: Sequence[Tool]) -> list[dict[str, Any]]:
    def convert(tool: Tool) -> dict[str, Any]:
        strict = resolveJsonSchemaStrictSampling(tool, True)
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": _stripSymbolKeys(getJsonSchemaToolParameters(tool, strict)),
                "strict": nullishCoalesce(strict, False),
            },
        }

    return [convert(tool) for tool in tools]


def _stripSymbolKeys(value: Any) -> Any:
    # Python has no Symbol keys; this copies the schema the way `Object.entries` does.
    if isinstance(value, list):
        return [_stripSymbolKeys(item) for item in value]

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, entry in value.items():
            result[str(key)] = _stripSymbolKeys(entry)
        return result

    return value


def _toChatMessages(messages: Sequence[MessageValue], supportsImages: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for index, msg in enumerate(messages):
        if msg.role == "system":
            text = getSystemMessageText(msg) if index == 0 else renderSystemMessageUpdate(msg)
            if len(text) > 0:
                result.append({"role": "system", "content": sanitizeSurrogates(text)})
            continue

        if msg.role == "user":
            if isinstance(msg.content, str):
                result.append({"role": "user", "content": sanitizeSurrogates(msg.content)})
                continue
            hadImages = any(item.type == "image" for item in msg.content)
            content: list[dict[str, Any]] = [
                {"type": "text", "text": sanitizeSurrogates(item.text)}
                if item.type == "text"
                else {"type": "image_url", "imageUrl": f"data:{item.mimeType};base64,{item.data}"}
                for item in msg.content
                if item.type == "text" or supportsImages
            ]
            if len(content) > 0:
                result.append({"role": "user", "content": content})
                continue
            if hadImages and not supportsImages:
                result.append({"role": "user", "content": "(image omitted: model does not support images)"})
            continue

        if msg.role == "assistant":
            contentParts: list[dict[str, Any]] = []
            toolCalls: list[dict[str, Any]] = []

            for block in msg.content:
                if block.type == "text":
                    if len(jsTrim(block.text)) > 0:
                        contentParts.append({"type": "text", "text": sanitizeSurrogates(block.text)})
                    continue
                if block.type == "thinking":
                    if len(jsTrim(block.thinking)) > 0:
                        contentParts.append({
                            "type": "thinking",
                            "thinking": [{"type": "text", "text": sanitizeSurrogates(block.thinking)}],
                        })
                    continue
                toolCalls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": jsonStringify(block.arguments or {})},
                    "index": 0,
                })

            assistantMessage: dict[str, Any] = {"role": "assistant", "prefix": False}
            if len(contentParts) > 0:
                assistantMessage["content"] = contentParts
            if len(toolCalls) > 0:
                assistantMessage["toolCalls"] = toolCalls
            if len(contentParts) > 0 or len(toolCalls) > 0:
                result.append(assistantMessage)
            continue

        toolContent: list[dict[str, Any]] = []
        textResult = "\n".join(sanitizeSurrogates(part.text) if part.type == "text" else "" for part in msg.content if part.type == "text")
        hasImages = any(part.type == "image" for part in msg.content)
        toolText = _buildToolResultText(textResult, hasImages, supportsImages, msg.isError)
        toolContent.append({"type": "text", "text": toolText})
        for part in msg.content:
            if not supportsImages:
                continue
            if part.type != "image":
                continue
            toolContent.append({
                "type": "image_url",
                "imageUrl": f"data:{part.mimeType};base64,{part.data}",
            })
        result.append({
            "role": "tool",
            "toolCallId": msg.toolCallId,
            "name": msg.toolName,
            "content": toolContent,
        })

    return result


def _buildToolResultText(text: str, hasImages: bool, supportsImages: bool, isError: bool) -> str:
    trimmed = jsTrim(text)
    errorPrefix = "[tool error] " if isError else ""

    if len(trimmed) > 0:
        imageSuffix = "\n[tool image omitted: model does not support images]" if hasImages and not supportsImages else ""
        return f"{errorPrefix}{trimmed}{imageSuffix}"

    if hasImages:
        if supportsImages:
            return "[tool error] (see attached image)" if isError else "(see attached image)"
        return (
            "[tool error] (image omitted: model does not support images)"
            if isError
            else "(image omitted: model does not support images)"
        )

    return "[tool error] (no tool output)" if isError else "(no tool output)"


def _mapToolChoice(choice: Any) -> Any:
    if not choice:
        return None
    if choice == "auto" or choice == "none" or choice == "any" or choice == "required":
        return choice
    return {
        "type": "function",
        "function": {"name": choice["function"]["name"]},
    }


def _mapChatStopReason(reason: str | None) -> dict[str, str]:
    if reason is None:
        return {"stopReason": "stop"}
    match reason:
        case "stop":
            return {"stopReason": "stop"}
        case "length" | "model_length":
            return {"stopReason": "length"}
        case "tool_calls":
            return {"stopReason": "toolUse"}
        case "error":
            # Mistral reports transient server failures this way; "server error" makes the message retryable.
            return {"stopReason": "error", "errorMessage": "Provider stopped with: error (server error)"}
        case _:
            return {"stopReason": "error", "errorMessage": f"Provider stopped with: {reason}"}


__all__ = ["MistralOptions", "stream", "streamSimple"]
