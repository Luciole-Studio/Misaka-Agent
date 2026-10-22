"""Translated from pi's ``packages/ai/src/api/anthropic-messages.ts`` (v1.3.0).

MISAKA fork, kept to what the Python SDK and platform force (each is marked where it happens):

- The SDK glue lives in ``misaka.ai.api._anthropic_sdk`` (no ``PiAnthropic`` subclass, ``omit`` for suppressed
  headers, ``extra_body`` for undeclared fields, a pool per request for a federation clone, the 401 token drop).
- Stream events are the parsed JSON dicts; pi's per-block ``index`` and ``partialJson`` scratch fields live in side
  tables because the content blocks are strict models.
- A tool call's streamed arguments are parsed sparingly (``StreamingArgs``) and finalized through ``finish_into``
  (upstream's ``parseToolCallArguments``).
- httpx has no ``AbortSignal``: a read races the signal (``_common._iterate_async_iterable``).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from types import SimpleNamespace
from typing import Any, Literal, NamedTuple, NotRequired, TypedDict

from misaka.ai.api import _anthropic_sdk
from misaka.ai.api._common import (
    _close_stream,
    _compat_value,
    _iterate_async_iterable,
    _option,
)
from misaka.ai.api.constrained_sampling import (
    getJsonSchemaToolParameters,
    resolveJsonSchemaStrictSampling,
)
from misaka.ai.api.github_copilot_headers import (
    buildCopilotDynamicHeaders,
    hasCopilotVisionInput,
)
from misaka.ai.api.simple_options import (
    adjustMaxTokensForThinking,
    buildBaseOptions,
    clampMaxTokensToContext,
)
from misaka.ai.api.transform_messages import (
    splitErrorToolResultImages,
    transformMessages,
)
from misaka.ai.env_api_keys import (
    ANTHROPIC_FEDERATION_RULE_ID_ENV,
    ANTHROPIC_IDENTITY_TOKEN_FILE_ENV,
    ANTHROPIC_ORGANIZATION_ID_ENV,
    ANTHROPIC_SERVICE_ACCOUNT_ID_ENV,
    ANTHROPIC_WORKSPACE_ID_ENV,
)
from misaka.ai.models import calculateCost
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    MessageValue,
    Model,
    ProviderEnv,
    ProviderHeaders,
    SimpleStreamOptions,
    StartEvent,
    StopReason,
    StreamOptions,
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
    ToolResultMessage,
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils._sdk_error import provider_error_status
from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.diagnostics import (
    AssistantMessageDiagnostic,
    DiagnosticErrorInfo,
    appendAssistantMessageDiagnostic,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headersToRecord
from misaka.ai.utils.json_parse import parseJsonWithRepair
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.ai.utils.text import getSystemMessageText, renderSystemMessageUpdate
from misaka.ai.utils.transcript import (
    getCurrentTools,
    getInitialSystemMessage,
    resolveTranscript,
)
from misaka.utils.js import (
    UNDEFINED,
    Utf8Decoder,
    jsLength,
    jsonStringify,
    jsTrim,
    nullishCoalesce,
    throwIfAborted,
)
from misaka.utils.values import maybe_await, signal_aborted


def _resolveCacheRetention(cacheRetention: CacheRetention | None = None, env: ProviderEnv | None = None) -> CacheRetention:
    """Resolve cache retention preference.
    Defaults to "short" and uses PI_CACHE_RETENTION for backward compatibility."""
    if cacheRetention:
        return cacheRetention
    if getProviderEnvValue("PI_CACHE_RETENTION", env) == "long":
        return "long"
    return "short"


def _getCacheControl(
    model: Model,
    cacheRetention: CacheRetention | None = None,
    env: ProviderEnv | None = None,
) -> dict[str, Any]:
    retention = _resolveCacheRetention(cacheRetention, env)
    if retention == "none":
        return {"retention": retention}
    ttl = "1h" if retention == "long" and _getAnthropicCompat(model).supportsLongCacheRetention else None
    return {
        "retention": retention,
        "cacheControl": {"type": "ephemeral", **({"ttl": ttl} if ttl else {})},
    }


# Stealth mode: Mimic Claude Code's tool naming exactly
_claudeCodeVersion = "2.1.280"

# Claude Code 2.x tool names (canonical casing)
# Source: https://cchistory.mariozechner.at/data/prompts-2.1.11.md
# To update: https://github.com/badlogic/cchistory
_claudeCodeTools = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Grep",
    "Glob",
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "KillShell",
    "NotebookEdit",
    "Skill",
    "Task",
    "TaskOutput",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
]

_ccToolLookup = {t.lower(): t for t in _claudeCodeTools}


# Convert tool name to CC canonical casing if it matches (case-insensitive)
def _toClaudeCodeName(name: str) -> str:
    return nullishCoalesce(_ccToolLookup.get(name.lower()), name)


def _fromClaudeCodeName(name: str, tools: Sequence[Tool] | None = None) -> str:
    if tools and len(tools) > 0:
        lowerName = name.lower()
        matchedTool = next((tool for tool in tools if tool.name.lower() == lowerName), None)
        if matchedTool:
            return matchedTool.name
    return name


def _convertImageBlock(image: ImageContent) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": image.mimeType, "data": image.data},
    }


def _convertContentBlocks(content: Sequence[TextContent | ImageContent]) -> str | list[dict[str, Any]]:
    """Convert content blocks to Anthropic API format"""
    # If only text blocks, return as concatenated string for simplicity
    hasImages = any(c.type == "image" for c in content)
    if not hasImages:
        return sanitizeSurrogates("\n".join(c.text for c in content))  # type: ignore[union-attr]

    # If we have images, convert to content block array
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": sanitizeSurrogates(block.text)} if block.type == "text" else _convertImageBlock(block)
        for block in content
    ]

    # If only images (no text), add placeholder text block
    hasText = any(b["type"] == "text" for b in blocks)
    if not hasText:
        blocks.insert(0, {"type": "text", "text": "(see attached image)"})

    return blocks


AnthropicEffort = Literal["low", "medium", "high", "xhigh", "max"]

AnthropicThinkingDisplay = Literal["summarized", "omitted"]

_FINE_GRAINED_TOOL_STREAMING_BETA = "fine-grained-tool-streaming-2025-05-14"
_INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"
_SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_MID_CONVERSATION_OUTPUT_CONFIG_BETA = "mid-conversation-output-config-2026-07-01"
_THINKING_BINDING_CONTROLS_BETA = "thinking-binding-controls-2026-08-01"
_INLINE_TOOLS_BETA = "inline-tools-2026-09-15"
# MISAKA fork: the messages of a 400 `invalid_request_error` that `stream` retries with preferred strict tools
# downgraded, named (upstream lists them inline).
_STRICT_TOOL_LIMIT_ERRORS = (
    "The compiled grammar is too large",
    "Too many strict tools (",
    "Schemas contains too many parameters with union types (",
)

# Stable deferred tool declared whenever native tool changes are in use. Anthropic adds
# hidden prompt scaffolding for mid-conversation tool changes; declaring this placeholder
# from the first request keeps that scaffolding in the cached prefix, so the first tool
# change does not invalidate the cache (measured: full miss without it). It is never
# activated and the model cannot see it.
_DEFERRED_TOOL_PLACEHOLDER: dict[str, Any] = {
    "name": "__pi_deferred_placeholder__",
    "description": "Reserved placeholder. Never available. Never call this.",
    "input_schema": {"type": "object", "properties": {}, "required": []},
    "defer_loading": True,
}


def _shouldUseServerSideFallbackBeta(model: Model) -> bool:
    return len(_compat_value(model, "allowedFallbackModels") or []) > 0


def _getAnthropicCompat(model: Model) -> SimpleNamespace:
    isOpenRouter = model.provider == "openrouter" or "openrouter.ai" in model.baseUrl
    return SimpleNamespace(
        supportsEagerToolInputStreaming=nullishCoalesce(_compat_value(model, "supportsEagerToolInputStreaming"), True),
        supportsLongCacheRetention=nullishCoalesce(_compat_value(model, "supportsLongCacheRetention"), True),
        sendSessionAffinityHeaders=nullishCoalesce(_compat_value(model, "sendSessionAffinityHeaders"), isOpenRouter),
        sessionAffinityFormat=nullishCoalesce(
            _compat_value(model, "sessionAffinityFormat"), "openrouter" if isOpenRouter else None
        ),
        supportsCacheControlOnTools=nullishCoalesce(_compat_value(model, "supportsCacheControlOnTools"), True),
        supportsTemperature=nullishCoalesce(_compat_value(model, "supportsTemperature"), True),
        allowEmptySignature=nullishCoalesce(_compat_value(model, "allowEmptySignature"), False),
        supportsStrictTools=nullishCoalesce(_compat_value(model, "supportsStrictTools"), False),
        supportsMidConvoSystemMessages=nullishCoalesce(_compat_value(model, "supportsMidConvoSystemMessages"), False),
        supportsMidConvoToolChanges=nullishCoalesce(_compat_value(model, "supportsMidConvoToolChanges"), False),
    )


class AnthropicOptions(TypedDict, total=False):
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
    # Enable extended thinking.
    # For adaptive thinking models: the model decides when/how much to think.
    # For older models: uses budget-based thinking with thinkingBudgetTokens.
    # Default: undefined (thinking is omitted unless `streamSimple()` maps
    # a simple reasoning level to this option, or callers set it explicitly).
    thinkingEnabled: bool
    # Token budget for extended thinking (older models only).
    # Ignored for adaptive thinking models.
    # Default: 1024 when `thinkingEnabled` is true and no budget is provided.
    thinkingBudgetTokens: int
    # Effort level for adaptive thinking models.
    # Controls how much thinking Claude allocates:
    # - "max": Always thinks with no constraints (Opus 4.6 only)
    # - "xhigh": Highest reasoning level (Opus 4.7+, Fable 5)
    # - "high": Always thinks, deep reasoning
    # - "medium": Moderate thinking, may skip for simple queries
    # - "low": Minimal thinking, skips for simple tasks
    # Ignored for older models.
    # Default: omitted unless `streamSimple()` maps a simple reasoning
    # level to this option.
    effort: AnthropicEffort
    # Controls how thinking content is returned in API responses.
    # - "summarized": Thinking blocks contain summarized thinking text.
    # - "omitted": Thinking blocks return an empty thinking field; the encrypted
    #   signature still travels back for multi-turn continuity. Use for faster
    #   time-to-first-text-token when your UI does not surface thinking.
    #
    # Note: Anthropic's API default for Claude Opus 4.7 and Claude Mythos Preview
    # is "omitted". We default to "summarized" here to keep behavior consistent
    # with older Claude 4 models. Set this explicitly to "omitted" to opt in.
    # Default: "summarized" when thinking is enabled.
    thinkingDisplay: AnthropicThinkingDisplay
    # Whether to request the interleaved thinking beta header for non-adaptive
    # thinking models. Adaptive thinking models have interleaved thinking built in,
    # so the header is skipped for them regardless of this setting.
    # Default: true.
    interleavedThinking: bool
    # Anthropic tool choice behavior. String values map to Anthropic's built-in
    # choices; `{ type: "tool", name }` forces a specific tool.
    # Default: omitted (Anthropic default behavior, currently equivalent to auto).
    toolChoice: Literal["auto", "any", "none"] | dict[str, str]
    # Pre-built Anthropic client instance. When provided, skips internal client
    # construction entirely. Use this to inject alternative SDK clients such as
    # `AnthropicVertex` that shares the same messaging API.
    client: Any


def _mergeHeaders(*headerSources: Mapping[str, Any] | None) -> dict[str, Any]:
    """MISAKA fork: names are matched case-insensitively. Upstream merges with `Object.assign` and relies on the
    `Headers` object downstream to collapse `User-Agent` and `user-agent` into one; a dict handed to httpx collapses
    nothing, so the OAuth path -- which overrides the client string with `claude-cli/...` -- would send both."""
    merged: dict[str, Any] = {}
    for headers in headerSources:
        if headers:
            for key, value in headers.items():
                for existing in [name for name in merged if name.lower() == key.lower()]:
                    del merged[existing]
                merged[key] = value
    return merged


def _mergeClientHeaders(*headerSources: Mapping[str, Any] | None) -> dict[str, Any]:
    return _mergeHeaders({"User-Agent": getPiUserAgent()}, *headerSources)


def _hasHeader(headers: Mapping[str, Any] | None, name: str) -> bool:
    if not headers:
        return False
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected and value is not None and jsLength(jsTrim(value)) > 0:
            return True
    return False


def _hasRequestAuth(apiKey: str | None, headers: Mapping[str, Any] | None) -> bool:
    return (
        bool(apiKey)
        or _hasHeader(headers, "authorization")
        or _hasHeader(headers, "x-api-key")
        or _hasHeader(headers, "cf-aig-authorization")
    )


def _assertRequestAuth(provider: str, apiKey: str | None, headers: Mapping[str, Any] | None) -> None:
    if not _hasRequestAuth(apiKey, headers):
        raise RuntimeError(f"No API key for provider: {provider}")


# MISAKA fork: pi's `PiAnthropic` subclass is `_anthropic_sdk._sdk_client` (see that module).

type _AnthropicFederationConfig = dict[str, Any]


def _getAnthropicFederation(
    model: Model,
    apiKey: str | None,
    headers: Mapping[str, Any] | None,
    env: ProviderEnv | None,
) -> _AnthropicFederationConfig | None:
    """Workload identity federation config from the ANTHROPIC_* variables the
    Anthropic SDK documents; the SDK performs the token exchange and refresh.
    Only for the anthropic provider, since the exchange is an Anthropic API
    endpoint, and only when no key or auth header was resolved."""
    if model.provider != "anthropic" or _hasRequestAuth(apiKey, headers):
        return None
    federationRuleId = getProviderEnvValue(ANTHROPIC_FEDERATION_RULE_ID_ENV, env)
    organizationId = getProviderEnvValue(ANTHROPIC_ORGANIZATION_ID_ENV, env)
    identityTokenFile = getProviderEnvValue(ANTHROPIC_IDENTITY_TOKEN_FILE_ENV, env)
    if not federationRuleId or not organizationId or not identityTokenFile:
        return None
    return {
        "organization_id": organizationId,
        "workspace_id": getProviderEnvValue(ANTHROPIC_WORKSPACE_ID_ENV, env),
        "authentication": {
            "type": "oidc_federation",
            "federation_rule_id": federationRuleId,
            "service_account_id": getProviderEnvValue(ANTHROPIC_SERVICE_ACCOUNT_ID_ENV, env),
            "identity_token": {"source": "file", "path": identityTokenFile},
        },
    }


# The SDK caches the federated access token per client, but pi creates a client
# per request. Keep one client for the current federation config and fetch, and
# clone it per request with `withOptions()`, which shares the token cache.
# MISAKA fork: there is no per-request `fetch` here (the key alone decides).
_federationClient: dict[str, Any] | None = None


class _ServerSentEvent(TypedDict):
    event: str | None
    data: str
    raw: list[str]


class _SseDecoderState(TypedDict):
    event: str | None
    data: list[str]
    raw: list[str]


_ANTHROPIC_MESSAGE_EVENTS = frozenset(
    {
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
    }
)


def _flushSseEvent(state: _SseDecoderState) -> _ServerSentEvent | None:
    if not state["event"] and len(state["data"]) == 0:
        return None

    event: _ServerSentEvent = {
        "event": state["event"],
        "data": "\n".join(state["data"]),
        "raw": [*state["raw"]],
    }
    state["event"] = None
    state["data"] = []
    state["raw"] = []
    return event


def _decodeSseLine(line: str, state: _SseDecoderState) -> _ServerSentEvent | None:
    if line == "":
        return _flushSseEvent(state)

    state["raw"].append(line)
    if line.startswith(":"):
        return None

    delimiterIndex = line.find(":")
    fieldName = line if delimiterIndex == -1 else line[:delimiterIndex]
    value = "" if delimiterIndex == -1 else line[delimiterIndex + 1 :]
    value = value.removeprefix(" ")

    if fieldName == "event":
        state["event"] = value
    elif fieldName == "data":
        state["data"].append(value)

    return None


def _nextLineBreakIndex(text: str) -> int:
    carriageReturnIndex = text.find("\r")
    newlineIndex = text.find("\n")
    if carriageReturnIndex == -1:
        return newlineIndex
    if newlineIndex == -1:
        return carriageReturnIndex
    return min(carriageReturnIndex, newlineIndex)


class _ConsumedLine(TypedDict):
    line: str
    rest: str


def _consumeLine(text: str) -> _ConsumedLine | None:
    lineBreakIndex = _nextLineBreakIndex(text)
    if lineBreakIndex == -1:
        return None

    nextIndex = lineBreakIndex + 1
    if text[lineBreakIndex] == "\r" and text[nextIndex : nextIndex + 1] == "\n":
        nextIndex += 1

    return {"line": text[:lineBreakIndex], "rest": text[nextIndex:]}


async def _iterateSseMessages(body: Any, signal: Any = None) -> AsyncIterator[_ServerSentEvent]:
    """MISAKA fork: ``body`` is the streaming httpx response; its byte chunks stand in for the reader of a
    ``ReadableStream``, and a read races the abort signal."""
    decoder = Utf8Decoder()
    state: _SseDecoderState = {"event": None, "data": [], "raw": []}
    buffer = ""

    # MISAKA fork: no reader lock to release; the response is closed by the request that opened it.
    async for value in _iterate_async_iterable(body.aiter_bytes(), signal, on_abort=lambda: _close_stream(body)):
        if signal_aborted(signal):
            raise RuntimeError("Request was aborted")

        buffer += decoder.decode(value)
        consumed = _consumeLine(buffer)
        while consumed:
            buffer = consumed["rest"]
            event = _decodeSseLine(consumed["line"], state)
            if event:
                yield event
            consumed = _consumeLine(buffer)

    buffer += decoder.decode(b"", final=True)
    consumed = _consumeLine(buffer)
    while consumed:
        buffer = consumed["rest"]
        event = _decodeSseLine(consumed["line"], state)
        if event:
            yield event
        consumed = _consumeLine(buffer)

    if len(buffer) > 0:
        event = _decodeSseLine(buffer, state)
        if event:
            yield event

    trailingEvent = _flushSseEvent(state)
    if trailingEvent:
        yield trailingEvent


async def _iterateAnthropicEvents(response: Any, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    if response is None:
        raise RuntimeError("Attempted to iterate over an Anthropic response with no body")

    sawMessageStart = False
    sawMessageEnd = False

    async for sse in _iterateSseMessages(response, signal):
        if sse["event"] == "error":
            raise RuntimeError(sse["data"])

        if nullishCoalesce(sse["event"], "") not in _ANTHROPIC_MESSAGE_EVENTS:
            continue

        try:
            event = parseJsonWithRepair(sse["data"])
            if event["type"] == "message_start":
                sawMessageStart = True
            elif event["type"] == "message_stop":
                sawMessageEnd = True
        except Exception as error:
            message = str(error) if isinstance(error, Exception) else jsonStringify(error)
            raise RuntimeError(
                f"Could not parse Anthropic SSE event {sse['event']}: {message}; data={sse['data']}; "
                f"raw={'\\n'.join(sse['raw'])}"
            ) from error
        # MISAKA fork: JavaScript's `for await` hands control to the consumer at every event, so it sees the
        # partial as each event left it; a chunk's events would otherwise all be applied before it runs.
        await asyncio.sleep(0)
        yield event

    if sawMessageStart and not sawMessageEnd:
        raise RuntimeError("Anthropic stream ended before message_stop")


def stream(
    model: Model,
    context: TranscriptContext,
    options: AnthropicOptions | StreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    normalizedContext = resolveTranscript(context, _getAnthropicCompat(model).supportsMidConvoSystemMessages)
    currentTools = getCurrentTools(normalizedContext.messages)

    async def run() -> None:
        output = AssistantMessage(
            role="assistant",
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
        # MISAKA fork: what this request opened, closed in `finally`.
        response: Any = None
        created: _CreatedClient | None = None
        federation: _AnthropicFederationConfig | None = None

        try:
            client: Any
            isOAuth: bool
            usageModel = model
            inputTransformations: list[Any] | None = None
            requestHeaders: dict[str, Any] = {}

            if _option(options, "client"):
                client = _option(options, "client")
                isOAuth = False
            else:
                apiKey = _option(options, "apiKey")
                federation = _getAnthropicFederation(model, apiKey, _option(options, "headers"), _option(options, "env"))
                if not federation:
                    _assertRequestAuth(model.provider, apiKey, _option(options, "headers"))

                copilotDynamicHeaders: dict[str, str] | None = None
                if model.provider == "github-copilot":
                    hasImages = hasCopilotVisionInput(normalizedContext.messages)
                    copilotDynamicHeaders = buildCopilotDynamicHeaders(
                        {"messages": normalizedContext.messages, "hasImages": hasImages}
                    )

                cacheRetention = _resolveCacheRetention(_option(options, "cacheRetention"), _option(options, "env"))
                cacheSessionId = None if cacheRetention == "none" else _option(options, "sessionId")

                created = _createClient(
                    model,
                    apiKey,
                    _option(options, "headers"),
                    copilotDynamicHeaders,
                    cacheSessionId,
                    federation,
                )
                client = created.client
                isOAuth = created.isOAuthToken
                requestHeaders = created.requestHeaders
            requestOptions = {
                **({"signal": _option(options, "signal")} if _option(options, "signal") else {}),
                **({"timeout": _option(options, "timeoutMs")} if _option(options, "timeoutMs") is not None else {}),
                "maxRetries": 0,
            }
            disablePreferredStrictTools = False
            onPayload = _option(options, "onPayload")
            while True:
                throwIfAborted(_option(options, "signal"))
                built = _buildParams(model, normalizedContext, isOAuth, options, disablePreferredStrictTools)
                if built.providerThinkingLevel:
                    output.providerThinkingLevel = built.providerThinkingLevel
                params = built.params
                nextParams = await maybe_await(onPayload(params, model)) if onPayload else None
                if nextParams is not None:
                    params = {**nextParams, "stream": True}
                try:
                    response = await retryProviderRequest(
                        lambda params=params: _anthropic_sdk._create_raw_response(
                            client, params, requestOptions, requestHeaders
                        ),
                        {
                            "maxRetries": _option(options, "maxRetries"),
                            "maxRetryDelayMs": _option(options, "maxRetryDelayMs"),
                            "signal": _option(options, "signal"),
                        },
                    )
                    break
                except Exception as error:
                    # Only downgrade a rejected request, never an already-started stream.
                    if (
                        signal_aborted(_option(options, "signal"))
                        or disablePreferredStrictTools
                        or not built.hasPreferredStrictTools
                        or not _anthropic_sdk._is_api_status_error(error)
                        or error.status_code != 400  # type: ignore[attr-defined]
                        or error.type != "invalid_request_error"  # type: ignore[attr-defined]
                        or not any(message in str(error) for message in _STRICT_TOOL_LIMIT_ERRORS)
                    ):
                        raise
                    disablePreferredStrictTools = True
                    appendAssistantMessageDiagnostic(
                        output,
                        AssistantMessageDiagnostic(
                            type="anthropic_strict_tools_fallback",
                            timestamp=time.time_ns() // 1_000_000,
                            error=DiagnosticErrorInfo(message=str(error)),
                            details={"reason": "strict_tool_limits_exceeded"},
                        ),
                    )
            onResponse = _option(options, "onResponse")
            if onResponse:
                await maybe_await(
                    onResponse({"status": response.status_code, "headers": headersToRecord(response.headers)}, model)
                )
            stream.push(StartEvent(partial=output))

            # MISAKA fork: pi keeps the provider block index and the tool's partial JSON on the block itself
            # (`type Block`); the pydantic blocks forbid extra fields, so both live beside it. A block's index is
            # dropped at its stop event, as upstream deletes `block.index`.
            blockIndexes: dict[int, int] = {}
            partialJsons: dict[int, StreamingArgs] = {}

            def blockAt(providerIndex: Any) -> int:
                """``blocks.findIndex((b) => b.index === event.index)``."""
                return blockIndexes.get(providerIndex, -1) if isinstance(providerIndex, int) else -1

            onProviderStreamEvent = _option(options, "onProviderStreamEvent")
            async for event in _iterateAnthropicEvents(response, _option(options, "signal")):
                if onProviderStreamEvent:
                    await maybe_await(onProviderStreamEvent(event, model))
                if event["type"] == "message_start":
                    message = event["message"]
                    output.responseId = message.get("id")
                    transformations = message.get("input_transformations")
                    if isinstance(transformations, list):
                        inputTransformations = transformations
                    responseModel = message.get("model")
                    if responseModel != model.id:
                        output.responseModel = responseModel
                    fallbackCost = (
                        None
                        if responseModel == model.id
                        else next(
                            (
                                fallback.cost
                                for fallback in _compat_value(model, "allowedFallbackModels") or []
                                if fallback.provider == model.provider and fallback.model == responseModel
                            ),
                            None,
                        )
                    )
                    usageModel = model.model_copy(update={"id": responseModel, "cost": fallbackCost}) if fallbackCost else model
                    usage = message["usage"]
                    # Capture initial token usage from message_start event
                    # This ensures we have input token counts even if the stream is aborted early
                    output.usage.input = usage.get("input_tokens") or 0
                    output.usage.output = usage.get("output_tokens") or 0
                    output.usage.cacheRead = usage.get("cache_read_input_tokens") or 0
                    output.usage.cacheWrite = usage.get("cache_creation_input_tokens") or 0
                    output.usage.cacheWrite1h = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
                    # Anthropic doesn't provide total_tokens, compute from components
                    output.usage.totalTokens = (
                        output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite
                    )
                    calculateCost(usageModel, output.usage)
                elif event["type"] == "content_block_start":
                    contentBlock = event["content_block"]
                    if contentBlock["type"] == "fallback":
                        if len(output.content) > 0:
                            raise RuntimeError("Anthropic performed an unsupported mid-output model fallback")
                        continue
                    if contentBlock["type"] == "text":
                        output.content.append(TextContent(type="text", text=nullishCoalesce(contentBlock.get("text"), "")))
                        blockIndexes[event["index"]] = len(output.content) - 1
                        stream.push(TextStartEvent(contentIndex=len(output.content) - 1, partial=output))
                    elif contentBlock["type"] == "thinking":
                        output.content.append(
                            ThinkingContent(
                                type="thinking",
                                thinking=nullishCoalesce(contentBlock.get("thinking"), ""),
                                thinkingSignature=nullishCoalesce(contentBlock.get("signature"), ""),
                            )
                        )
                        blockIndexes[event["index"]] = len(output.content) - 1
                        stream.push(ThinkingStartEvent(contentIndex=len(output.content) - 1, partial=output))
                    elif contentBlock["type"] == "redacted_thinking":
                        output.content.append(
                            ThinkingContent(
                                type="thinking",
                                thinking="[Reasoning redacted]",
                                thinkingSignature=contentBlock.get("data"),
                                redacted=True,
                            )
                        )
                        blockIndexes[event["index"]] = len(output.content) - 1
                        stream.push(ThinkingStartEvent(contentIndex=len(output.content) - 1, partial=output))
                    elif contentBlock["type"] == "tool_use":
                        initialInput = contentBlock.get("input")
                        output.content.append(
                            ToolCall(
                                type="toolCall",
                                id=contentBlock.get("id"),
                                name=(
                                    _fromClaudeCodeName(contentBlock.get("name"), currentTools)
                                    if isOAuth
                                    else contentBlock.get("name")
                                ),
                                # MISAKA fork: arguments are a dict here, so a non-object input reads as `{}`.
                                arguments=initialInput if isinstance(initialInput, dict) else {},
                            )
                        )
                        blockIndexes[event["index"]] = len(output.content) - 1
                        partialJsons[event["index"]] = StreamingArgs()
                        stream.push(ToolCallStartEvent(contentIndex=len(output.content) - 1, partial=output))
                elif event["type"] == "content_block_delta":
                    delta = event["delta"]
                    index = blockAt(event["index"])
                    if index == -1:
                        continue
                    block = output.content[index]
                    if delta["type"] == "text_delta":
                        if block.type == "text":
                            block.text += delta["text"]
                            stream.push(TextDeltaEvent(contentIndex=index, delta=delta["text"], partial=output))
                    elif delta["type"] == "thinking_delta":
                        if block.type == "thinking":
                            block.thinking += delta["thinking"]
                            stream.push(ThinkingDeltaEvent(contentIndex=index, delta=delta["thinking"], partial=output))
                    elif delta["type"] == "input_json_delta":
                        if block.type == "toolCall":
                            partialJson = partialJsons.setdefault(event["index"], StreamingArgs())
                            partialJson.append(delta["partial_json"])
                            block.arguments = partialJson.arguments
                            stream.push(
                                ToolCallDeltaEvent(contentIndex=index, delta=delta["partial_json"], partial=output)
                            )
                    elif delta["type"] == "signature_delta":  # noqa: SIM102 - upstream nests the block check
                        if block.type == "thinking":
                            block.thinkingSignature = block.thinkingSignature or ""
                            block.thinkingSignature += delta["signature"]
                elif event["type"] == "content_block_stop":
                    index = blockAt(event["index"])
                    if index != -1:
                        block = output.content[index]
                        del blockIndexes[event["index"]]
                        if block.type == "text":
                            stream.push(TextEndEvent(contentIndex=index, content=block.text, partial=output))
                        elif block.type == "thinking":
                            stream.push(ThinkingEndEvent(contentIndex=index, content=block.thinking, partial=output))
                        elif block.type == "toolCall":
                            # MISAKA fork: only streamed deltas replace what content_block_start inlined --
                            # compatible endpoints put the whole input in the start block and send no
                            # input_json_delta, where parsing the empty buffer would hand the tool `{}`.
                            partialJson = partialJsons.pop(event["index"], None)
                            if partialJson is not None and partialJson.raw:
                                partialJson.finish_into(block)
                            # Finalize in-place and strip the scratch buffer so replay only
                            # carries parsed arguments.
                            stream.push(ToolCallEndEvent(contentIndex=index, toolCall=block, partial=output))
                elif event["type"] == "message_delta":
                    transformations = event.get("input_transformations")
                    if isinstance(transformations, list):
                        inputTransformations = transformations
                    if event["delta"].get("stop_reason"):
                        output.rawStopReason = event["delta"]["stop_reason"]
                        stopReasonResult = _mapStopReason(event["delta"]["stop_reason"], event["delta"].get("stop_details"))
                        output.stopReason = stopReasonResult["stopReason"]
                        if stopReasonResult.get("errorMessage"):
                            output.errorMessage = stopReasonResult["errorMessage"]
                    # Only update usage fields if present (not null).
                    # Preserves input_tokens from message_start when proxies omit it in message_delta.
                    usage = event.get("usage")
                    if usage:
                        if usage.get("input_tokens") is not None:
                            output.usage.input = usage["input_tokens"]
                        if usage.get("output_tokens") is not None:
                            output.usage.output = usage["output_tokens"]
                        if usage.get("cache_read_input_tokens") is not None:
                            output.usage.cacheRead = usage["cache_read_input_tokens"]
                        if usage.get("cache_creation_input_tokens") is not None:
                            output.usage.cacheWrite = usage["cache_creation_input_tokens"]
                        # Vercel AI Gateway includes the TTL breakdown in deltas, though the SDK only types it on message_start.
                        cacheCreation = usage.get("cache_creation")
                        if cacheCreation and cacheCreation.get("ephemeral_1h_input_tokens") is not None:
                            output.usage.cacheWrite1h = cacheCreation["ephemeral_1h_input_tokens"]
                        # Anthropic reports reasoning tokens as a subset of output tokens.
                        thinkingTokens = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
                        if thinkingTokens is not None:
                            output.usage.reasoning = thinkingTokens
                    # Anthropic doesn't provide total_tokens, compute from components
                    output.usage.totalTokens = (
                        output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite
                    )
                    calculateCost(usageModel, output.usage)

            # MISAKA fork: a compatible endpoint may omit a block-stop event; finalize every raw buffer before
            # dispatch, since a streaming preview alone is never executable.
            for providerIndex, partialJson in partialJsons.items():
                index = blockAt(providerIndex)
                if index != -1 and partialJson.raw:
                    block = output.content[index]
                    if block.type == "toolCall":
                        partialJson.finish_into(block)

            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")

            if output.stopReason == "pending":
                raise RuntimeError("Anthropic stream ended without a stop reason")
            if output.stopReason == "aborted" or output.stopReason == "error":
                raise RuntimeError(output.errorMessage or "An unknown error occurred")
            if inputTransformations and len(inputTransformations) > 0:
                appendAssistantMessageDiagnostic(
                    output,
                    AssistantMessageDiagnostic(
                        type="anthropic_input_transformations",
                        timestamp=time.time_ns() // 1_000_000,
                        details={
                            "transformations": [
                                {
                                    key: transformation.get(key)
                                    for key in ("type", "path", "reason")
                                    if transformation.get(key) is not None
                                }
                                for transformation in inputTransformations
                            ]
                        },
                    ),
                )

            stream.push(DoneEvent(reason=output.stopReason, message=output))
            stream.end()
        except Exception as error:  # noqa: BLE001 - every failure becomes an error event on the stream
            # MISAKA fork: see `_anthropic_sdk._invalidate_federation_token`.
            if federation is not None and created is not None and provider_error_status(error) == 401:
                _anthropic_sdk._invalidate_federation_token(created.client)
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            output.errorMessage = str(error) if isinstance(error, Exception) else jsonStringify(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
            stream.end()
        finally:
            # MISAKA fork: close the response and the client this request opened while their loop still runs.
            await _close_stream(response)
            if created is not None:
                await _anthropic_sdk._close_client(created.client, federation=federation is not None)

    spawn_stream_task(run(), stream=stream)
    return stream


def _mapThinkingLevelToEffort(model: Model, level: str | None) -> AnthropicEffort:
    """Map ThinkingLevel to Anthropic effort levels for adaptive thinking.
    Note: effort "max" is available on all adaptive-thinking Claude models, while native
    "xhigh" is only available on Opus 4.7/4.8, Sonnet 5, and Fable 5."""
    mapped = (model.thinkingLevelMap or {}).get(level) if level else None
    if isinstance(mapped, str):
        return mapped  # type: ignore[return-value]

    match level:
        case "minimal" | "low":
            return "low"
        case "medium":
            return "medium"
        case "high":
            return "high"
        case _:
            return "high"


def streamSimple(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    if not _getAnthropicFederation(model, _option(options, "apiKey"), _option(options, "headers"), _option(options, "env")):
        _assertRequestAuth(model.provider, _option(options, "apiKey"), _option(options, "headers"))

    base = {
        **dict(buildBaseOptions(model, context, options, _option(options, "apiKey"))),
        "toolChoice": _option(options, "toolChoice"),
    }
    if not _option(options, "reasoning"):
        return stream(model, context, {**base, "thinkingEnabled": False})

    # For models with adaptive thinking: use an effort level.
    # For older models: use budget-based thinking.
    if _compat_value(model, "forceAdaptiveThinking") is True:
        effort = _mapThinkingLevelToEffort(model, _option(options, "reasoning"))
        return stream(model, context, {**base, "thinkingEnabled": True, "effort": effort})

    # Undefined means the caller did not request an output cap; let the helper use the model cap.
    # Do not coerce to 0 here, or the thinking budget would become the entire max_tokens value.
    adjusted = adjustMaxTokensForThinking(
        base["maxTokens"],
        model.maxTokens,
        _option(options, "reasoning"),
        _option(options, "thinkingBudgets"),
    )

    maxTokens = clampMaxTokensToContext(model, context, adjusted.maxTokens)

    return stream(
        model,
        context,
        {
            **base,
            "maxTokens": maxTokens,
            "thinkingEnabled": True,
            "thinkingBudgetTokens": min(adjusted.thinkingBudget, max(0, maxTokens - 1024)),
        },
    )


def _isOAuthToken(apiKey: str) -> bool:
    return "sk-ant-oat" in apiKey


class _CreatedClient(NamedTuple):
    client: Any
    isOAuthToken: bool
    # MISAKA fork: the headers every request has to carry (see `_anthropic_sdk._sdk_client`).
    requestHeaders: dict[str, Any]


def _createClient(
    model: Model,
    apiKey: str | None,
    optionsHeaders: ProviderHeaders | None = None,
    dynamicHeaders: Mapping[str, str] | None = None,
    sessionId: str | None = None,
    federation: _AnthropicFederationConfig | None = None,
) -> _CreatedClient:
    """MISAKA fork: no `fetch` parameter (Python has no fetch override)."""
    global _federationClient
    # Copilot: Bearer auth.
    if model.provider == "github-copilot":
        client, requestHeaders = _anthropic_sdk._sdk_client(
            api_key=None,
            auth_token=apiKey,
            base_url=model.baseUrl,
            headers=_mergeClientHeaders(
                {
                    "accept": "application/json",
                    "anthropic-dangerous-direct-browser-access": "true",
                },
                model.headers,
                dynamicHeaders,
                optionsHeaders,
            ),
        )

        return _CreatedClient(client, False, requestHeaders)

    # OAuth: Bearer auth, Claude Code identity headers
    if apiKey and _isOAuthToken(apiKey):
        client, requestHeaders = _anthropic_sdk._sdk_client(
            api_key=None,
            auth_token=apiKey,
            base_url=model.baseUrl,
            headers=_mergeClientHeaders(
                {
                    "accept": "application/json",
                    "anthropic-dangerous-direct-browser-access": "true",
                    "user-agent": f"claude-cli/{_claudeCodeVersion}",
                    "x-app": "cli",
                },
                model.headers,
                optionsHeaders,
            ),
        )

        return _CreatedClient(client, True, requestHeaders)

    # API key, header-owned auth, or workload identity federation.
    compat = _getAnthropicCompat(model)
    sessionAffinityHeaders: dict[str, Any] = {}
    if sessionId and compat.sendSessionAffinityHeaders:
        header = "x-session-id" if compat.sessionAffinityFormat == "openrouter" else "x-session-affinity"
        sessionAffinityHeaders[header] = sessionId
    defaultHeaders = _mergeClientHeaders(
        {
            "accept": "application/json",
            "anthropic-dangerous-direct-browser-access": "true",
        },
        sessionAffinityHeaders,
        model.headers,
        optionsHeaders,
    )
    if federation:
        key = jsonStringify([model.baseUrl, federation])
        if (_federationClient["key"] if _federationClient else None) != key:
            client = _anthropic_sdk._federation_sdk_client(base_url=model.baseUrl, federation=federation)
            _federationClient = {"key": key, "client": client}
        client, requestHeaders = _anthropic_sdk._with_options(_federationClient["client"], defaultHeaders)  # type: ignore[index]
        return _CreatedClient(client, False, requestHeaders)

    client, requestHeaders = _anthropic_sdk._sdk_client(
        api_key=apiKey or None,
        auth_token=None,
        base_url=model.baseUrl,
        headers=defaultHeaders,
    )

    return _CreatedClient(client, False, requestHeaders)


def _getBetaFeatures(
    model: Model,
    context: TranscriptContext,
    isOAuthToken: bool,
    nativeToolChanges: bool,
    options: Any = None,
) -> list[str]:
    configuredFeatures: Any = UNDEFINED
    for headers in (model.headers, _option(options, "headers")):
        for name, value in (headers or {}).items():
            if name.lower() == "anthropic-beta":
                configuredFeatures = value
    if configuredFeatures is None:
        return []
    if configuredFeatures is not UNDEFINED:
        return list(
            dict.fromkeys(
                feature for feature in (jsTrim(feature) for feature in configuredFeatures.split(",")) if len(feature) > 0
            )
        )

    features: list[str] = []
    if isOAuthToken:
        features.extend(["claude-code-20250219", "oauth-2025-04-20"])
    if _shouldUseFineGrainedToolStreamingBeta(model, context):
        features.append(_FINE_GRAINED_TOOL_STREAMING_BETA)
    if (
        model.reasoning
        and _option(options, "thinkingEnabled") is True
        and nullishCoalesce(_option(options, "interleavedThinking"), True)
        and _compat_value(model, "forceAdaptiveThinking") is not True
    ):
        features.append(_INTERLEAVED_THINKING_BETA)
    if _shouldUseServerSideFallbackBeta(model):
        features.append(_SERVER_SIDE_FALLBACK_BETA)
    if _compat_value(model, "supportsMidConvoEffort") is True:
        features.extend([_MID_CONVERSATION_OUTPUT_CONFIG_BETA, _THINKING_BINDING_CONTROLS_BETA])
    if nativeToolChanges:
        features.append(_INLINE_TOOLS_BETA)
    return list(dict.fromkeys(features))


class _BuiltParams(NamedTuple):
    """MISAKA fork: ``buildParams``'s result object, named."""

    params: dict[str, Any]
    hasPreferredStrictTools: bool
    # Effort of the final effort marker, recorded on the reply so later requests can replay it.
    providerThinkingLevel: AnthropicEffort | None


def _buildParams(
    model: Model,
    context: TranscriptContext,
    isOAuthToken: bool,
    options: Any = None,
    disablePreferredStrictTools: bool = False,
) -> _BuiltParams:
    cacheControl = _getCacheControl(model, _option(options, "cacheRetention"), _option(options, "env")).get("cacheControl")
    compat = _getAnthropicCompat(model)
    initialSystemMessage = getInitialSystemMessage(context.messages)
    initialSystemText = getSystemMessageText(initialSystemMessage) if initialSystemMessage else ""
    transformedMessages = transformMessages(context.messages, model, _normalizeToolCallId)
    conversationMessages = transformedMessages[1:] if initialSystemMessage else transformedMessages
    # Native tool changes keep the request-level tool list fixed and define every later tool
    # by value in a `tool_addition` block, which also expresses same-name redefinitions.
    # Anthropic rejects a tool list where every tool is deferred, so there must be an initial
    # active tool to anchor the placeholder. Otherwise the current tool list is sent.
    hasPreferredStrictTools = False

    def convertToolDefinitions(tools: Sequence[Tool], toolCacheControl: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        nonlocal hasPreferredStrictTools
        definitions = _convertTools(
            tools,
            isOAuthToken,
            compat.supportsEagerToolInputStreaming,
            compat.supportsStrictTools,
            toolCacheControl,
            disablePreferredStrictTools,
        )
        if any(
            definition.get("strict") is True and _isPreferredStrictJsonSchema(tools[index])
            for index, definition in enumerate(definitions)
        ):
            hasPreferredStrictTools = True
        return definitions

    initialTools = nullishCoalesce(initialSystemMessage.toolsAdded if initialSystemMessage else None, [])
    nativeToolChanges = bool(
        compat.supportsMidConvoSystemMessages and compat.supportsMidConvoToolChanges and len(initialTools) > 0
    )
    thinkingDisabled = bool(
        model.reasoning
        and _option(options, "thinkingEnabled") is False
        and (model.thinkingLevelMap or {}).get("off", UNDEFINED) is not None
    )
    # Effort cannot change between messages while thinking is disabled, so "off" sends no effort markers.
    effortMessages = _compat_value(model, "supportsMidConvoEffort") is True and not thinkingDisabled
    converted = _convertMessages(
        conversationMessages,
        isOAuthToken,
        cacheControl,
        compat.allowEmptySignature,
        model.provider if effortMessages else None,
        convertToolDefinitions if nativeToolChanges else None,
    )
    activeEffort = nullishCoalesce(_option(options, "effort"), "high")
    betaFeatures = _getBetaFeatures(model, context, isOAuthToken, nativeToolChanges, options)
    params: dict[str, Any] = {
        "model": model.id,
        "messages": _insertThinkingLevelMessages(converted, activeEffort) if effortMessages else converted["messages"],
        "max_tokens": nullishCoalesce(_option(options, "maxTokens"), model.maxTokens),
        "stream": True,
        **({"betas": betaFeatures} if len(betaFeatures) > 0 else {}),
    }

    # For OAuth tokens, we MUST include Claude Code identity
    if isOAuthToken:
        params["system"] = [
            {
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                **({"cache_control": cacheControl} if cacheControl else {}),
            }
        ]
        if initialSystemText:
            params["system"].append(
                {
                    "type": "text",
                    "text": sanitizeSurrogates(initialSystemText),
                    **({"cache_control": cacheControl} if cacheControl else {}),
                }
            )
    elif initialSystemText:
        # Add cache control to system prompt for non-OAuth tokens
        params["system"] = [
            {
                "type": "text",
                "text": sanitizeSurrogates(initialSystemText),
                **({"cache_control": cacheControl} if cacheControl else {}),
            }
        ]

    # Temperature is incompatible with extended thinking and unsupported on Claude Opus 4.7+.
    if (
        _option(options, "temperature") is not None
        and not _option(options, "thinkingEnabled")
        and _compat_value(model, "supportsMidConvoEffort") is not True
        and compat.supportsTemperature
    ):
        params["temperature"] = _option(options, "temperature")

    toolCacheControl = cacheControl if compat.supportsCacheControlOnTools else None
    if nativeToolChanges:
        # Initial tools stay active with the cache breakpoint on the last one, followed by the
        # placeholder. The list never changes afterwards: later tools are defined by value in
        # `tool_addition` blocks and withdrawn by `tool_removal`, so the cached prefix survives
        # every tool change.
        params["tools"] = [*convertToolDefinitions(initialTools, toolCacheControl), _DEFERRED_TOOL_PLACEHOLDER]
    else:
        tools = getCurrentTools(context.messages)
        if len(tools) > 0:
            params["tools"] = convertToolDefinitions(tools, toolCacheControl)

    if thinkingDisabled:
        # No effort is sent, so the model default applies. Anthropic rejects `block_binding`
        # with disabled thinking.
        params["thinking"] = {"type": "disabled"}
    elif effortMessages:
        # Managed effort models use adaptive thinking so prefix mismatches can
        # be dropped instead of surfacing as persistent 400 responses.
        params["thinking"] = {
            "type": "adaptive",
            "display": nullishCoalesce(_option(options, "thinkingDisplay"), "summarized"),
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        }
        params["output_config"] = {"effort": "high"}
    elif model.reasoning and _option(options, "thinkingEnabled"):
        # Default to "summarized" so Opus 4.7 and Mythos Preview behave like
        # older Claude 4 models (whose API default is also "summarized").
        display: AnthropicThinkingDisplay = nullishCoalesce(_option(options, "thinkingDisplay"), "summarized")
        if _compat_value(model, "forceAdaptiveThinking") is True:
            # Adaptive thinking: Claude decides when and how much to think.
            params["thinking"] = {"type": "adaptive", "display": display}
            if _option(options, "effort"):
                params["output_config"] = {"effort": _option(options, "effort")}
        else:
            # Budget-based thinking for older models
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": _option(options, "thinkingBudgetTokens") or 1024,
                "display": display,
            }

    metadata = _option(options, "metadata")
    if metadata:
        userId = metadata.get("user_id")
        if isinstance(userId, str):
            params["metadata"] = {"user_id": userId}

    toolChoice = _option(options, "toolChoice")
    if toolChoice:
        if isinstance(toolChoice, str):
            params["tool_choice"] = {"type": toolChoice}
        else:
            params["tool_choice"] = toolChoice

    allowedFallbackModels = _compat_value(model, "allowedFallbackModels")
    if allowedFallbackModels and len(allowedFallbackModels) > 0:
        params["fallbacks"] = [{"model": fallback.model} for fallback in allowedFallbackModels]

    return _BuiltParams(params, hasPreferredStrictTools, activeEffort if effortMessages else None)


_TOOL_CALL_ID_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


# Normalize tool call IDs to match Anthropic's required pattern and length
def _normalizeToolCallId(id: str, *_: Any) -> str:
    """MISAKA fork: ``transformMessages`` passes three arguments, which JavaScript drops; a character outside the
    BMP is two UTF-16 units, so two underscores, as the JavaScript regex replaces each unit."""
    return _TOOL_CALL_ID_DISALLOWED.sub(lambda match: "_" * jsLength(match.group()), id)[:64]


def _convertToolResult(msg: ToolResultMessage, content: Sequence[TextContent | ImageContent] | None = None) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": msg.toolCallId,
        "content": _convertContentBlocks(msg.content if content is None else content),
        "is_error": msg.isError,
    }


class _ConvertedAnthropicMessages(TypedDict):
    messages: list[dict[str, Any]]
    assistantLevels: dict[int, AnthropicEffort]


def _convertMessages(
    transformedMessages: list[MessageValue],
    isOAuthToken: bool,
    cacheControl: dict[str, Any] | None = None,
    allowEmptySignature: bool = False,
    managedProvider: str | None = None,
    # Converts tool definitions for native `tool_addition` blocks; undefined when tool changes are not native.
    convertToolDefinitions: Any = None,
) -> _ConvertedAnthropicMessages:
    params: list[dict[str, Any]] = []
    assistantLevels: dict[int, AnthropicEffort] = {}
    # Later system messages are held back and emitted directly before the next assistant
    # message (or at the end of the transcript). Anthropic requires `tool_result` blocks to
    # immediately follow their `tool_use`, so a system message between them is rejected; this
    # also mirrors where the managed-effort system messages are inserted. As a result an
    # update placed before a user message in the transcript lands after it on the wire.
    pendingSystemMessages: list[dict[str, Any]] = []

    def flushPendingSystemMessages() -> None:
        params.extend(pendingSystemMessages)
        pendingSystemMessages.clear()

    i = 0
    while i < len(transformedMessages):
        msg: Any = transformedMessages[i]

        if msg.role == "system":
            # Later system messages only reach this point when the model accepts them natively;
            # otherwise the transcript was collapsed into the leading message before conversion.
            text = renderSystemMessageUpdate(msg)
            blocks: list[dict[str, Any]] = []
            if len(text) > 0:
                blocks.append({"type": "text", "text": sanitizeSurrogates(text)})
            if convertToolDefinitions:
                added = nullishCoalesce(msg.toolsAdded, [])
                redefined = {tool.name for tool in added}
                for tool in nullishCoalesce(msg.toolsRemoved, []):
                    # A new definition under the same name replaces the old one, so no removal is needed.
                    if tool.name in redefined:
                        continue
                    blocks.append(
                        {
                            "type": "tool_removal",
                            "tool": {
                                "type": "tool_reference",
                                "name": _toClaudeCodeName(tool.name) if isOAuthToken else tool.name,
                            },
                        }
                    )
                for definition in convertToolDefinitions(added):
                    blocks.append({"type": "tool_addition", "tool": {"type": "tool_definition", "definition": definition}})
            if len(blocks) > 0:
                pendingSystemMessages.append({"role": "system", "content": blocks})
        elif msg.role == "user":
            if isinstance(msg.content, str):
                if jsLength(jsTrim(msg.content)) > 0:
                    params.append({"role": "user", "content": sanitizeSurrogates(msg.content)})
            else:
                blocks = [
                    {"type": "text", "text": sanitizeSurrogates(item.text)} if item.type == "text" else _convertImageBlock(item)
                    for item in msg.content
                ]
                filteredBlocks = [b for b in blocks if b["type"] != "text" or jsLength(jsTrim(b["text"])) > 0]
                if len(filteredBlocks) == 0:
                    i += 1
                    continue
                params.append({"role": "user", "content": filteredBlocks})
        elif msg.role == "assistant":
            flushPendingSystemMessages()
            blocks = []

            for block in msg.content:
                if block.type == "text":
                    if jsLength(jsTrim(block.text)) == 0:
                        continue
                    blocks.append({"type": "text", "text": sanitizeSurrogates(block.text)})
                elif block.type == "thinking":
                    # Redacted thinking: pass the opaque payload back as redacted_thinking
                    if block.redacted:
                        blocks.append({"type": "redacted_thinking", "data": block.thinkingSignature})
                        continue
                    thinkingSignature = block.thinkingSignature
                    hasThinkingSignature = bool(thinkingSignature) and jsLength(jsTrim(thinkingSignature)) > 0
                    if jsLength(jsTrim(block.thinking)) == 0 and not hasThinkingSignature:
                        continue
                    # If thinking signature is missing/empty (e.g., from aborted stream),
                    # convert to plain text for Anthropic. Some compatible providers emit
                    # and accept empty signatures, so let marked models preserve the block.
                    if not hasThinkingSignature:
                        blocks.append(
                            {"type": "thinking", "thinking": sanitizeSurrogates(block.thinking), "signature": ""}
                            if allowEmptySignature
                            else {"type": "text", "text": sanitizeSurrogates(block.thinking)}
                        )
                    else:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": sanitizeSurrogates(block.thinking),
                                "signature": thinkingSignature,
                            }
                        )
                elif block.type == "toolCall":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": _toClaudeCodeName(block.name) if isOAuthToken else block.name,
                            "input": nullishCoalesce(block.arguments, {}),
                        }
                    )
            if len(blocks) == 0:
                i += 1
                continue
            messageIndex = len(params)
            params.append({"role": "assistant", "content": blocks})
            if (
                managedProvider is not None
                and msg.api == "anthropic-messages"
                and msg.provider == managedProvider
                and _isAnthropicEffort(msg.providerThinkingLevel)
            ):
                assistantLevels[messageIndex] = msg.providerThinkingLevel
        elif msg.role == "toolResult":
            # Collect all consecutive toolResult messages, needed for z.ai Anthropic endpoint.
            toolResults: list[dict[str, Any]] = []
            # Anthropic rejects error tool results that contain anything but text, so their images follow
            # all tool results of the turn, which must come first in the user message (#10794).
            errorImages: list[dict[str, Any]] = []
            j = i
            while j < len(transformedMessages) and transformedMessages[j].role == "toolResult":
                toolMsg: Any = transformedMessages[j]
                split = splitErrorToolResultImages(toolMsg)
                toolResults.append(_convertToolResult(toolMsg, split.content if split else None))
                if split:
                    errorImages.extend([{"type": "text", "text": split.caption}, *map(_convertImageBlock, split.images)])
                j += 1

            # Skip the messages we've already processed.
            i = j - 1

            params.append({"role": "user", "content": [*toolResults, *errorImages]})
        i += 1

    flushPendingSystemMessages()

    # Add cache_control to the last user or system message to cache conversation history
    if cacheControl and len(params) > 0:
        lastMessage = params[-1]
        if lastMessage["role"] == "user" or lastMessage["role"] == "system":
            if isinstance(lastMessage["content"], list):
                lastBlock = lastMessage["content"][-1] if lastMessage["content"] else None
                if lastBlock and lastBlock["type"] in ("text", "image", "tool_result", "tool_addition", "tool_removal"):
                    # MISAKA fork: a lone text block may carry a stable-prefix breakpoint (`_applyStablePrefixCacheMarker`).
                    if len(lastMessage["content"]) == 1 and lastBlock["type"] == "text":
                        _applyStablePrefixCacheMarker(lastMessage, lastBlock["text"], cacheControl)
                    else:
                        lastBlock["cache_control"] = cacheControl
            elif isinstance(lastMessage["content"], str):
                # MISAKA fork: pi's `[{type: "text", text, cache_control}]`, unless a stable prefix is declared.
                _applyStablePrefixCacheMarker(lastMessage, lastMessage["content"], cacheControl)

    return {"messages": params, "assistantLevels": assistantLevels}


def _applyStablePrefixCacheMarker(message: dict[str, Any], text: str, cacheControl: dict[str, Any]) -> None:
    """MISAKA fork: pi marks the whole last text. A user message whose builder declared a stable prefix (the research
    scaffold) carries the breakpoint on that prefix instead, and its volatile tail rides unmarked, so the next request
    still hits the cached prefix."""
    from misaka.utils.prompt_cache_wire import _apply_cache_marker

    textMessage = {"role": message["role"], "content": text}
    _apply_cache_marker(textMessage, cacheControl, native_anthropic=True)
    message["content"] = textMessage["content"]


def _isAnthropicEffort(value: Any) -> bool:
    return value == "low" or value == "medium" or value == "high" or value == "xhigh" or value == "max"


def _insertThinkingLevelMessages(
    converted: _ConvertedAnthropicMessages,
    activeEffort: AnthropicEffort,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index in range(len(converted["messages"])):
        historicalEffort = converted["assistantLevels"].get(index)
        if historicalEffort is not None:
            messages.append({"role": "system", "content": [], "output_config": {"effort": historicalEffort}})
        messages.append(converted["messages"][index])
    messages.append({"role": "system", "content": [], "output_config": {"effort": activeEffort}})
    return messages


def _shouldUseFineGrainedToolStreamingBeta(model: Model, context: TranscriptContext) -> bool:
    return len(getCurrentTools(context.messages)) > 0 and not _getAnthropicCompat(model).supportsEagerToolInputStreaming


# Keywords Anthropic strict tool use rejects with a 400 for the whole request.
# https://platform.claude.com/docs/en/build-with-claude/structured-outputs#json-schema-limitations
_ANTHROPIC_STRICT_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "maxItems",
        "uniqueItems",
        "minContains",
        "maxContains",
        "minProperties",
        "maxProperties",
    }
)
_ANTHROPIC_STRICT_STRING_FORMATS = frozenset(
    {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "uri",
        "ipv4",
        "ipv6",
        "uuid",
    }
)


def _isAnthropicStrictUnsupportedKeyword(key: str, value: Any) -> bool:
    if key in _ANTHROPIC_STRICT_UNSUPPORTED_KEYWORDS:
        return True
    if key == "minItems":
        # `!== 0 && !== 1`: a boolean is neither (in Python `True == 1`).
        return isinstance(value, bool) or (value != 0 and value != 1)
    if key == "format":
        return not isinstance(value, str) or value not in _ANTHROPIC_STRICT_STRING_FORMATS
    return False


# MISAKA fork: the non-strict schema keeps `$defs`/`definitions`, which MCP servers' tool schemas reference from
# their properties; pi sends only type/properties/required and drops them. Kept until pi lands the local `$ref`
# inlining discussed in pi #9557 / #10270, then follow pi.
_SCHEMA_DEFINITION_KEYS = ("$defs", "definitions")


def _isPreferredStrictJsonSchema(tool: Tool) -> bool:
    """MISAKA fork: ``config && config.type === "json_schema" && config.strict === "prefer"`` for
    ``config = tool.constrainedSampling``, which ``buildParams`` and ``convertTools`` both test, named."""
    config = tool.constrainedSampling
    return bool(config) and config.type == "json_schema" and getattr(config, "strict", None) == "prefer"


def _convertTools(
    tools: Sequence[Tool],
    isOAuthToken: bool,
    supportsEagerToolInputStreaming: bool,
    supportsStrictTools: bool,
    cacheControl: dict[str, Any] | None = None,
    disablePreferredStrictTools: bool = False,
) -> list[dict[str, Any]]:
    if not tools:
        return []

    def convert(index: int, tool: Tool) -> dict[str, Any]:
        strict = (
            None
            if disablePreferredStrictTools and _isPreferredStrictJsonSchema(tool)
            else resolveJsonSchemaStrictSampling(tool, supportsStrictTools, _isAnthropicStrictUnsupportedKeyword)
        )
        parameters = getJsonSchemaToolParameters(tool, strict)
        schema = parameters
        legacyInputSchema = {
            "type": "object",
            "properties": nullishCoalesce(schema.get("properties"), {}),
            "required": nullishCoalesce(schema.get("required"), []),
            **{key: schema[key] for key in _SCHEMA_DEFINITION_KEYS if key in schema},
        }
        inputSchema = {**parameters, **legacyInputSchema} if strict is True else legacyInputSchema

        return {
            "name": _toClaudeCodeName(tool.name) if isOAuthToken else tool.name,
            "description": tool.description,
            **({"eager_input_streaming": True} if supportsEagerToolInputStreaming else {}),
            **({"strict": True} if strict is True else {}),
            "input_schema": inputSchema,
            **({"cache_control": cacheControl} if cacheControl and index == len(tools) - 1 else {}),
        }

    return [convert(index, tool) for index, tool in enumerate(tools)]


class _StopReasonResult(TypedDict):
    stopReason: StopReason
    errorMessage: NotRequired[str]


def _mapStopReason(reason: str, stopDetails: Mapping[str, Any] | None = None) -> _StopReasonResult:
    match reason:
        case "end_turn":
            return {"stopReason": "stop"}
        case "max_tokens":
            return {"stopReason": "length"}
        case "tool_use":
            return {"stopReason": "toolUse"}
        case "refusal":
            return {
                "stopReason": "error",
                "errorMessage": (stopDetails or {}).get("explanation") or "The model refused to complete the request",
            }
        case "pause_turn":  # Stop is good enough -> resubmit
            return {"stopReason": "stop"}
        case "stop_sequence":
            return {"stopReason": "stop"}  # We don't supply stop sequences, so this should never happen
        case "sensitive":  # Content flagged by safety filters (not yet in SDK types)
            return {"stopReason": "error", "errorMessage": "Provider stopped with: sensitive"}
        case _:
            # Handle unknown stop reasons gracefully (API may add new values)
            raise RuntimeError(f"Unhandled stop reason: {reason}")


__all__ = ["AnthropicEffort", "AnthropicOptions", "AnthropicThinkingDisplay", "stream", "streamSimple"]
