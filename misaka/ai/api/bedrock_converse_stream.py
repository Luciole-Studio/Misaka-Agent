"""Translated from pi's ``packages/ai/src/api/bedrock-converse-stream.ts`` (v1.3.0).

MISAKA fork, kept to what boto3 and the platform force (each is marked where it happens):

- ``BedrockRuntimeClient`` is ``misaka.ai.api._bedrock_sdk``'s boto3 wrapper: the same config keys, ``build`` and
  ``deserialize`` middleware, ``send`` raced with the abort signal, botocore's stream errors turned back into what the
  JavaScript SDK throws.
- Stream events are botocore's dicts; pi's per-block scratch fields (``index``, ``partialJson``, ``redactedChunks``)
  live beside the strict content blocks (``_Blocks``).
- A tool call's streamed arguments are parsed sparingly (``StreamingArgs``) and finalized through ``finish_into``
  (upstream's ``parseToolCallArguments``).
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

from misaka.ai.api import _bedrock_sdk
from misaka.ai.api._common import _close_stream, _option
from misaka.ai.api.constrained_sampling import (
    getJsonSchemaToolParameters,
    resolveJsonSchemaStrictSampling,
)
from misaka.ai.api.simple_options import (
    adjustMaxTokensForThinking,
    buildBaseOptions,
    clampMaxTokensToContext,
    clampReasoning,
)
from misaka.ai.api.transform_messages import (
    splitErrorToolResultImages,
    transformMessages,
)
from misaka.ai.models import calculateCost
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    Model,
    ProviderEnv,
    SimpleStreamOptions,
    StartEvent,
    StopReason,
    StreamOptions,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingLevel,
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
from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.diagnostics import (
    AssistantMessageDiagnostic,
    appendAssistantMessageDiagnostic,
)
from misaka.ai.utils.error_body import normalizeProviderError
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import providerHeadersToRecord
from misaka.ai.utils.node_http_proxy import resolveHttpProxyUrlForTarget
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.ai.utils.text import getSystemMessageText
from misaka.ai.utils.transcript import (
    collapseSystemMessages,
    getCurrentTools,
    getInitialSystemMessage,
    withoutInitialSystemMessage,
)
from misaka.utils.js import jsLength, jsTrim, nullishCoalesce
from misaka.utils.values import maybe_await, signal_aborted

BedrockThinkingDisplay = Literal["summarized", "omitted"]


class BedrockOptions(TypedDict, total=False):
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
    headers: dict[str, str | None]
    onPayload: Any
    onResponse: Any
    onProviderStreamEvent: Any
    timeoutMs: int
    websocketConnectTimeoutMs: int
    maxRetries: int
    maxRetryDelayMs: int
    metadata: dict[str, Any]
    env: ProviderEnv
    region: str
    profile: str
    toolChoice: Literal["auto", "any", "none"] | dict[str, str]
    # See https://docs.aws.amazon.com/bedrock/latest/userguide/inference-reasoning.html for supported models.
    reasoning: ThinkingLevel
    # Custom token budgets per thinking level. Overrides default budgets.
    thinkingBudgets: ThinkingBudgets | dict[str, int]
    # Only supported by Claude 4.x models, see https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html#claude-messages-extended-thinking-tool-use-interleaved
    interleavedThinking: bool
    # Controls how Claude's thinking content is returned in responses.
    # - "summarized": Thinking blocks contain summarized thinking text (default here).
    # - "omitted": Thinking content is redacted but the signature still travels back
    #   for multi-turn continuity, reducing time-to-first-text-token.
    #
    # Note: Anthropic's API default for Claude Opus 4.8 and Mythos Preview is
    # "omitted". We default to "summarized" here to keep behavior consistent with
    # older Claude 4 models. Only applies to Claude models on Bedrock.
    thinkingDisplay: BedrockThinkingDisplay
    # Key-value pairs attached to the inference request for cost allocation tagging.
    # Keys: max 64 chars, no `aws:` prefix. Values: max 256 chars. Max 50 pairs.
    # Tags appear in AWS Cost Explorer split cost allocation data.
    # @see https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ConverseStream.html
    requestMetadata: dict[str, str]
    # Bearer token for Bedrock API key authentication.
    # When set, bypasses SigV4 signing and sends Authorization: Bearer <token> instead.
    # Requires `bedrock:CallWithBearerToken` IAM permission on the token's identity.
    # Set via AWS_BEARER_TOKEN_BEDROCK env var or pass directly.
    # @see https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonbedrock.html
    bearerToken: str


@dataclass
class _BlockScratch:
    index: int | None = None
    partialJson: StreamingArgs | None = None
    # Scratch buffer for encrypted reasoning deltas, joined into `thinkingSignature`.
    redactedChunks: list[bytes] | None = None


class _Blocks:
    """MISAKA fork: pi's ``Block`` is the content block plus its scratch fields; the pydantic blocks forbid extra
    fields, so each block's scratch lives here, by content index."""

    def __init__(self, output: AssistantMessage) -> None:
        self.output = output
        self.scratch: dict[int, _BlockScratch] = {}

    def __len__(self) -> int:
        return len(self.output.content)

    def findIndex(self, contentBlockIndex: Any) -> int:
        """``blocks.findIndex((b) => b.index === contentBlockIndex)``."""
        return next((i for i, scratch in self.scratch.items() if scratch.index == contentBlockIndex), -1)

    def at(self, index: int) -> Any:
        """``blocks[index]``: undefined for -1."""
        return self.output.content[index] if 0 <= index < len(self.output.content) else None

    def scratchOf(self, index: int) -> _BlockScratch:
        return self.scratch.setdefault(index, _BlockScratch())


_EMPTY_TEXT_PLACEHOLDER = "<empty>"

_THINKING_BINDING_CONTROLS_BETA = "thinking-binding-controls-2026-08-01"

# Matches the placeholder the Anthropic API path uses for redacted thinking.
_REDACTED_THINKING_PLACEHOLDER = "[Reasoning redacted]"


def stream(
    model: Model,
    context: TranscriptContext,
    options: BedrockOptions | StreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    # Bedrock has no mid-conversation system messages; fold them into the leading prompt.
    normalizedContext = collapseSystemMessages(context)

    async def run() -> None:
        output = AssistantMessage(
            role="assistant",
            content=[],
            api="bedrock-converse-stream",
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

        blocks = _Blocks(output)
        env = _option(options, "env")
        signal = _option(options, "signal")

        # A profile explicitly configured through pi's auth flow (the `profile`
        # option or scoped `AWS_PROFILE` on the stored credential's env) must win
        # over ambient AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY. The SDK default
        # chain already prefers a configured profile over env keys, but only when
        # `credentials` is not set on the client config. See #6957.
        optionsProfile = _option(options, "profile") or (env or {}).get("AWS_PROFILE")
        config: dict[str, Any] = {
            "profile": optionsProfile or getProviderEnvValue("AWS_PROFILE", env),
        }
        configuredRegion = _getConfiguredBedrockRegion(options)
        hasAmbientConfiguredProfile = bool(getProviderEnvValue("AWS_PROFILE"))
        endpointRegion = _getStandardBedrockEndpointRegion(model.baseUrl)
        useExplicitEndpoint = _shouldUseExplicitBedrockEndpoint(
            model.baseUrl,
            configuredRegion,
            hasAmbientConfiguredProfile,
        )

        # Only pin standard AWS Bedrock runtime endpoints when no region or ambient AWS_PROFILE is configured.
        # This preserves custom endpoints (VPC/proxy) from #3402 without forcing built-in
        # catalog defaults such as us-east-1 to override AWS_REGION/AWS_PROFILE.
        if useExplicitEndpoint:
            config["endpoint"] = model.baseUrl

        # Resolve bearer token for Bedrock API key auth.
        skipAuth = getProviderEnvValue("AWS_BEDROCK_SKIP_AUTH", env) == "1"
        bearerToken = (
            _option(options, "bearerToken")
            or _option(options, "apiKey")
            or getProviderEnvValue("AWS_BEARER_TOKEN_BEDROCK", env)
            or None
        )
        useBearerToken = bearerToken is not None and not skipAuth

        # MISAKA fork: upstream's Node.js/Bun branch; Python always resolves config files and env.
        # Region resolution: ARN-embedded > explicit option > env vars > SDK default chain.
        # When the model ID is an inference profile ARN, extract the region from it.
        # This avoids conflicts with AWS_REGION set for other services.
        arnRegionMatch = re.match(r"^arn:aws(?:-[a-z0-9-]+)?:bedrock:([a-z0-9-]+):", model.id)
        if arnRegionMatch:
            config["region"] = arnRegionMatch.group(1)
        elif configuredRegion:
            config["region"] = configuredRegion
        elif endpointRegion and useExplicitEndpoint:
            config["region"] = endpointRegion
        elif not hasAmbientConfiguredProfile:
            config["region"] = "us-east-1"

        # Support proxies that don't need authentication
        if skipAuth:
            config["credentials"] = {
                "accessKeyId": "dummy-access-key",
                "secretAccessKey": "dummy-secret-key",
            }

        credentials = _getConfiguredBedrockCredentials(env)
        if not skipAuth and credentials and not optionsProfile:
            config["credentials"] = credentials

        proxyUrl = resolveHttpProxyUrlForTarget(model.baseUrl, env)
        if proxyUrl:
            # MISAKA fork: botocore's `proxies` config stands for the NodeHttpHandler with proxy agents.
            config["requestHandler"] = _bedrock_sdk.proxyRequestHandler(proxyUrl.geturl())
        elif getProviderEnvValue("AWS_BEDROCK_FORCE_HTTP1", env) == "1":
            # Some custom endpoints require HTTP/1.1 instead of HTTP/2
            # MISAKA fork: botocore only speaks HTTP/1.1, so there is no handler to swap.
            pass

        if useBearerToken:
            config["token"] = {"token": bearerToken}
            config["authSchemePreference"] = ["httpBearerAuth"]

        # Kept outside the try so the catch can still correlate a mid-stream failure:
        # exceptions delivered as stream events carry no HTTP metadata of their own.
        responseRequestId: str | None = None
        # MISAKA fork: the response this request opened, closed in `finally`.
        response: Any = None

        try:
            supportsStrictMode = nullishCoalesce(getattr(model.compat, "supportsStrictMode", None), False)
            client = _bedrock_sdk.BedrockRuntimeClient(config)
            observedRawResponse = False
            onResponse = _option(options, "onResponse")
            if onResponse:

                def onObserved() -> None:
                    nonlocal observedRawResponse
                    observedRawResponse = True

                _addResponseHeadersMiddleware(client, onResponse, model, onObserved)
            customHeaders = providerHeadersToRecord(_option(options, "headers"))
            if customHeaders:
                _addCustomHeadersMiddleware(client, customHeaders)
            cacheRetention = _resolveCacheRetention(_option(options, "cacheRetention"), env)
            inferenceMaxTokens = nullishCoalesce(
                _option(options, "maxTokens"), model.maxTokens if _isAnthropicClaudeModel(model) else None
            )
            initialSystemMessage = getInitialSystemMessage(normalizedContext.messages)
            initialSystemPrompt = getSystemMessageText(initialSystemMessage) if initialSystemMessage else None
            commandInput: dict[str, Any] = {
                "modelId": model.id,
                "messages": _convertMessages(normalizedContext, model, cacheRetention, env),
                "system": _buildSystemPrompt(initialSystemPrompt, model, cacheRetention, env),
                "inferenceConfig": {
                    **({"maxTokens": inferenceMaxTokens} if inferenceMaxTokens is not None else {}),
                    **(
                        {"temperature": _option(options, "temperature")}
                        if _option(options, "temperature") is not None
                        else {}
                    ),
                },
                "toolConfig": _convertToolConfig(
                    getCurrentTools(normalizedContext.messages),
                    _option(options, "toolChoice"),
                    supportsStrictMode,
                ),
                "additionalModelRequestFields": _buildAdditionalModelRequestFields(model, options),
                **(
                    {"requestMetadata": _option(options, "requestMetadata")}
                    if _option(options, "requestMetadata") is not None
                    else {}
                ),
            }
            onPayload = _option(options, "onPayload")
            nextCommandInput = await maybe_await(onPayload(commandInput, model)) if onPayload else None
            if nextCommandInput is not None:
                commandInput = nextCommandInput

            response = await client.send(commandInput, signal)
            responseRequestId = _normalizeDiagnosticValue(response.metadata["requestId"])
            if not observedRawResponse and response.metadata["httpStatusCode"] is not None:
                responseHeaders: dict[str, str] = {}
                if response.metadata["requestId"]:
                    responseHeaders["x-amzn-requestid"] = response.metadata["requestId"]
                if onResponse:
                    await maybe_await(
                        onResponse({"status": response.metadata["httpStatusCode"], "headers": responseHeaders}, model)
                    )

            onProviderStreamEvent = _option(options, "onProviderStreamEvent")
            async for item in response.stream:
                if onProviderStreamEvent:
                    await maybe_await(onProviderStreamEvent(item, model))
                # MISAKA fork: botocore's stream does not stop on the abort signal by itself.
                if signal_aborted(signal):
                    raise RuntimeError("Request was aborted")
                if "messageStart" in item:
                    if item["messageStart"].get("role") != "assistant":
                        raise RuntimeError("Unexpected assistant message start but got user message start instead")
                    stream.push(StartEvent(partial=output))
                elif "contentBlockStart" in item:
                    _handleContentBlockStart(item["contentBlockStart"], blocks, output, stream)
                elif "contentBlockDelta" in item:
                    _handleContentBlockDelta(item["contentBlockDelta"], blocks, output, stream)
                elif "contentBlockStop" in item:
                    _handleContentBlockStop(item["contentBlockStop"], blocks, output, stream)
                elif "messageStop" in item:
                    output.rawStopReason = item["messageStop"].get("stopReason")
                    stopReasonResult = _mapStopReason(item["messageStop"].get("stopReason"))
                    output.stopReason = stopReasonResult["stopReason"]
                    if stopReasonResult.get("errorMessage"):
                        output.errorMessage = stopReasonResult["errorMessage"]
                elif "metadata" in item:
                    _handleMetadata(item["metadata"], model, output)
                elif "internalServerException" in item:
                    raise _bedrock_sdk._ThrownStreamValue(item["internalServerException"])
                elif "modelStreamErrorException" in item:
                    raise _bedrock_sdk._ThrownStreamValue(item["modelStreamErrorException"])
                elif "validationException" in item:
                    raise _bedrock_sdk._ThrownStreamValue(item["validationException"])
                elif "throttlingException" in item:
                    raise _bedrock_sdk._ThrownStreamValue(item["throttlingException"])
                elif "serviceUnavailableException" in item:
                    raise _bedrock_sdk._ThrownStreamValue(item["serviceUnavailableException"])

            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")

            if output.stopReason == "pending":
                raise RuntimeError("Bedrock stream ended without a stop reason")
            if output.stopReason == "error" or output.stopReason == "aborted":
                raise RuntimeError(output.errorMessage or "An unknown error occurred")

            # A stream can settle without stopping every block, so finalize here too.
            for index in range(len(output.content)):
                _finalizeStreamingBlock(blocks, index)
            stream.push(DoneEvent(reason=output.stopReason, message=output))
            stream.end()
        except Exception as error:  # noqa: BLE001 - every failure becomes an error event on the stream
            for index in range(len(output.content)):
                _finalizeStreamingBlock(blocks, index)
            # MISAKA fork: what the JavaScript SDK would have thrown (see `_bedrock_sdk._thrown_value`).
            thrown = _bedrock_sdk._thrown_value(error)
            output.stopReason = "aborted" if signal_aborted(signal) else "error"
            output.errorMessage = _formatBedrockError(thrown)
            if output.stopReason == "error":
                _appendBedrockFailureDiagnostic(output, thrown, responseRequestId)
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
            stream.end()
        finally:
            # MISAKA fork: release the event stream's connection while the loop still runs.
            await _close_stream(response)

    spawn_stream_task(run(), stream=stream)
    return stream


# Human-readable prefixes for Bedrock SDK exception names.
# The downstream retry logic in agent-session matches patterns like
# `server.?error` and `service.?unavailable`, so we preserve the legacy
# prefix format rather than using the raw SDK exception name.
_BEDROCK_ERROR_PREFIXES: dict[str, str] = {
    "InternalServerException": "Internal server error",
    "ModelStreamErrorException": "Model stream error",
    "ValidationException": "Validation error",
    "ThrottlingException": "Throttling error",
    "ServiceUnavailableException": "Service unavailable",
}

# Some models reject the account/profile's configured Bedrock data retention mode
# (e.g. "data retention mode 'default' is not available for this model"). Point
# users at the AWS docs explaining how to configure a supported mode.
_BEDROCK_DATA_RETENTION_DOCS_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html"


def _formatBedrockError(error: Any) -> str:
    """Format a Bedrock error with a human-readable prefix.
    AWS SDK exceptions (both from `client.send()` and from stream event items)
    extend BedrockRuntimeServiceException. We map the `.name` to a stable
    human-readable prefix so downstream consumers (retry logic, context-overflow
    detection) can distinguish error categories via simple string matching."""
    # MISAKA fork: botocore's `ClientError` stands for `BedrockRuntimeServiceException`; its `Error.Message` is the
    # message, and the parsed `Error` object is no HTTP body (the JS SDK's `$response.body` is an unread stream).
    serviceException = _bedrock_sdk._service_exception(error)
    if serviceException is not None:
        core = serviceException[1]
    else:
        norm = normalizeProviderError(error)
        # Surface the raw HTTP body (with status) when the SDK did not fold it into
        # the message; otherwise fall back to the message. This is what stops a
        # gateway 403 from collapsing to `Unknown: UnknownError`.
        core = (
            f"{norm.status}: {norm.body}"
            if not norm.messageCarriesBody and norm.status is not None and norm.body is not None
            else norm.message
        )
    dataRetentionHint = (
        f" See {_BEDROCK_DATA_RETENTION_DOCS_URL} for supported data retention modes."
        if re.search(r"data retention mode", core, re.IGNORECASE)
        else ""
    )
    if serviceException is not None:
        prefix = nullishCoalesce(_BEDROCK_ERROR_PREFIXES.get(serviceException[0]), serviceException[0])
        return f"{prefix}: {core}{dataRetentionHint}"
    return f"{core}{dataRetentionHint}"


# Over-long header values are dropped rather than truncated: a truncated request id is not a request id.
_MAX_BEDROCK_DIAGNOSTIC_VALUE_CHARS = 200


def _normalizeDiagnosticValue(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = jsTrim(value)
    if jsLength(trimmed) == 0 or jsLength(trimmed) > _MAX_BEDROCK_DIAGNOSTIC_VALUE_CHARS:
        return None
    return trimmed


def _extractBedrockErrorCode(error: Any) -> str | None:
    """The SDK puts the modeled code on `error.name` for service exceptions and unmodeled stream errors alike, so
    do not narrow to `BedrockRuntimeServiceException`. Modeled Bedrock errors all end in `Exception`, unlike
    transport names such as `TimeoutError`."""
    # MISAKA fork: `_bedrock_sdk._error_name` reads botocore's spellings of `error.name`.
    name = _bedrock_sdk._error_name(error)
    if name is None or not name.endswith("Exception"):
        return None
    return _normalizeDiagnosticValue(name)


def _appendBedrockFailureDiagnostic(output: AssistantMessage, error: Any, fallbackRequestId: str | None) -> None:
    """Structured metadata alongside `errorMessage`, which stays byte-identical because `isRetryableAssistantError`
    matches against it. Unknown fields are omitted, never guessed: a modeled mid-stream exception reaches us as
    a bare object literal, leaving only `fallbackRequestId`. `details` only, as the throw is not always `Error`."""
    # MISAKA fork: `error.$metadata` is botocore's `ResponseMetadata` (`_bedrock_sdk._sdk_error_metadata`).
    metadata = _bedrock_sdk._sdk_error_metadata(error)
    details: dict[str, Any] = {}

    httpStatusCode = metadata.get("httpStatusCode")
    if isinstance(httpStatusCode, int) and not isinstance(httpStatusCode, bool):
        details["status"] = httpStatusCode

    errorCode = _extractBedrockErrorCode(error)
    if errorCode is not None:
        details["errorCode"] = errorCode

    requestId = nullishCoalesce(_normalizeDiagnosticValue(metadata.get("requestId")), fallbackRequestId)
    if requestId is not None:
        details["requestId"] = requestId

    if len(details) == 0:
        return

    appendAssistantMessageDiagnostic(
        output,
        AssistantMessageDiagnostic(type="bedrock_response_failure", timestamp=time.time_ns() // 1_000_000, details=details),
    )


# Header keys that must never be overwritten by caller-supplied headers.
# `host` and `x-amz-*` participate in the SigV4 canonical request; `authorization`
# is owned by SigV4 or the bearer-token path (config.token + authSchemePreference).
# Compared case-insensitively (caller key is lower-cased before lookup).
_RESERVED_HEADER_EXACT = frozenset({"authorization", "host"})


def _isReservedHeader(key: str) -> bool:
    lower = key.lower()
    return lower.startswith("x-amz-") or lower in _RESERVED_HEADER_EXACT


def _addCustomHeadersMiddleware(client: _bedrock_sdk.BedrockRuntimeClient, headers: dict[str, str]) -> None:
    """Attach caller-supplied headers to the outgoing Bedrock request via a Smithy
    `build`-step middleware. The `build` step runs after request serialisation but
    before SigV4 signing, so injected headers are covered by the signature. Reserved
    SigV4 / auth headers (`x-amz-*`, `authorization`, `host`) are silently skipped;
    all other caller headers override any existing same-named header on the request."""

    def middleware(request: Any) -> None:
        if request is not None and hasattr(request, "headers"):
            for key, value in headers.items():
                if not _isReservedHeader(key):
                    _bedrock_sdk._set_header(request, key, value)

    client.middlewareStack.add(middleware, {"step": "build", "name": "pi-ai-custom-headers", "priority": "low"})


def _isSmithyHttpResponse(response: Any) -> bool:
    if not response:
        return False
    return (
        isinstance(getattr(response, "statusCode", None), int)
        and not isinstance(response.statusCode, bool)
        and isinstance(getattr(response, "headers", None), dict)
    )


def _toProviderResponse(response: Any) -> dict[str, Any] | None:
    if not _isSmithyHttpResponse(response):
        return None
    return {"status": response.statusCode, "headers": {**response.headers}}


def _addResponseHeadersMiddleware(
    client: _bedrock_sdk.BedrockRuntimeClient,
    onResponse: Any,
    model: Model,
    onObserved: Any,
) -> None:
    """Bedrock's modeled `$metadata` only preserves selected HTTP metadata (for example
    requestId), so custom gateway headers are otherwise lost before callers see
    `onResponse`. Capture the raw Smithy HTTP response at the deserialize step,
    after the SDK receives the response but before the event stream is consumed."""

    async def middleware(response: Any) -> None:
        providerResponse = _toProviderResponse(response)
        if providerResponse:
            onObserved()
            await maybe_await(onResponse(providerResponse, model))

    client.middlewareStack.add(middleware, {"step": "deserialize", "name": "pi-ai-response-headers"})


def _budgetsDict(thinkingBudgets: Any) -> dict[str, Any]:
    """MISAKA fork: thinking budgets arrive as the model or as a settings mapping; this is the object spread."""
    if thinkingBudgets is None:
        return {}
    if isinstance(thinkingBudgets, ThinkingBudgets):
        return thinkingBudgets.model_dump(exclude_none=True)
    return {key: value for key, value in dict(thinkingBudgets).items() if value is not None}


def streamSimple(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    base = {
        **dict(buildBaseOptions(model, context, options, None)),
        "toolChoice": _option(options, "toolChoice"),
    }
    reasoning = _option(options, "reasoning")
    if not reasoning:
        return stream(model, context, {**base, "reasoning": None})

    if _isAnthropicClaudeModel(model):
        if _supportsAdaptiveThinking(model.id, model.name):
            return stream(
                model,
                context,
                {**base, "reasoning": reasoning, "thinkingBudgets": _option(options, "thinkingBudgets")},
            )

        # Undefined means the caller did not request an output cap; let the helper use the model cap.
        # Do not coerce to 0 here, or the thinking budget would become the entire maxTokens value.
        adjusted = adjustMaxTokensForThinking(
            base["maxTokens"],
            model.maxTokens,
            reasoning,
            _option(options, "thinkingBudgets"),
        )

        maxTokens = clampMaxTokensToContext(model, context, adjusted.maxTokens)

        return stream(
            model,
            context,
            {
                **base,
                "maxTokens": maxTokens,
                "reasoning": reasoning,
                "thinkingBudgets": {
                    **_budgetsDict(_option(options, "thinkingBudgets")),
                    clampReasoning(reasoning): min(adjusted.thinkingBudget, max(0, maxTokens - 1024)),
                },
            },
        )

    return stream(
        model,
        context,
        {**base, "reasoning": reasoning, "thinkingBudgets": _option(options, "thinkingBudgets")},
    )


def _handleContentBlockStart(
    event: dict[str, Any],
    blocks: _Blocks,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    index = event["contentBlockIndex"]
    start = event.get("start")

    if start is not None and start.get("toolUse") is not None:
        output.content.append(
            ToolCall(
                type="toolCall",
                id=start["toolUse"].get("toolUseId") or "",
                name=start["toolUse"].get("name") or "",
                arguments={},
            )
        )
        blocks.scratch[len(blocks) - 1] = _BlockScratch(index=index, partialJson=StreamingArgs())
        stream.push(ToolCallStartEvent(contentIndex=len(blocks) - 1, partial=output))


def _handleContentBlockDelta(
    event: dict[str, Any],
    blocks: _Blocks,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    contentBlockIndex = event["contentBlockIndex"]
    delta = event.get("delta") or {}
    index = blocks.findIndex(contentBlockIndex)
    block = blocks.at(index)

    if delta.get("text") is not None:
        # If no text block exists yet, create one, as `handleContentBlockStart` is not sent for text blocks
        if not block:
            output.content.append(TextContent(type="text", text=""))
            index = len(blocks) - 1
            blocks.scratch[index] = _BlockScratch(index=contentBlockIndex)
            block = blocks.at(index)
            stream.push(TextStartEvent(contentIndex=index, partial=output))
        if block.type == "text":
            block.text += delta["text"]
            stream.push(TextDeltaEvent(contentIndex=index, delta=delta["text"], partial=output))
    elif delta.get("toolUse") is not None and block and block.type == "toolCall":
        # MISAKA fork: the buffer is a StreamingArgs (sparing re-parses); `arguments` is its current view.
        partialJson = blocks.scratchOf(index).partialJson or StreamingArgs()
        blocks.scratchOf(index).partialJson = partialJson
        partialJson.append(delta["toolUse"].get("input") or "")
        block.arguments = partialJson.arguments
        stream.push(ToolCallDeltaEvent(contentIndex=index, delta=delta["toolUse"].get("input") or "", partial=output))
    elif delta.get("reasoningContent") is not None:
        thinkingBlock = block
        thinkingIndex = index

        if not thinkingBlock:
            output.content.append(ThinkingContent(type="thinking", thinking="", thinkingSignature=""))
            thinkingIndex = len(blocks) - 1
            blocks.scratch[thinkingIndex] = _BlockScratch(index=contentBlockIndex)
            thinkingBlock = blocks.at(thinkingIndex)
            stream.push(ThinkingStartEvent(contentIndex=thinkingIndex, partial=output))

        if thinkingBlock and thinkingBlock.type == "thinking":
            reasoningContent = delta["reasoningContent"]
            if reasoningContent.get("text"):
                thinkingBlock.thinking += reasoningContent["text"]
                stream.push(
                    ThinkingDeltaEvent(contentIndex=thinkingIndex, delta=reasoningContent["text"], partial=output)
                )
            # `thinkingSignature` holds either an Anthropic signature or an opaque redacted
            # payload, never both: mixing them would corrupt whichever arrived first.
            if reasoningContent.get("signature") and not thinkingBlock.redacted:
                thinkingBlock.thinkingSignature = (thinkingBlock.thinkingSignature or "") + reasoningContent["signature"]
            if reasoningContent.get("redactedContent"):
                # Encrypted reasoning from non-Anthropic models on Bedrock (e.g. OpenAI GPT-5.6).
                # The payload is opaque, so keep it verbatim in `thinkingSignature` the way the
                # Anthropic path stores redacted thinking, and replay it on the next turn.
                if not thinkingBlock.redacted:
                    thinkingBlock.redacted = True
                    thinkingBlock.thinkingSignature = ""
                    thinkingBlock.thinking += _REDACTED_THINKING_PLACEHOLDER
                    stream.push(
                        ThinkingDeltaEvent(
                            contentIndex=thinkingIndex, delta=_REDACTED_THINKING_PLACEHOLDER, partial=output
                        )
                    )
                scratch = blocks.scratchOf(thinkingIndex)
                if scratch.redactedChunks is None:
                    scratch.redactedChunks = []
                scratch.redactedChunks.append(bytes(reasoningContent["redactedContent"]))


def _flushRedactedContent(blocks: _Blocks, index: int) -> None:
    """Encodes buffered encrypted reasoning into `thinkingSignature` and drops the scratch
    buffer, which must never reach a persisted message: `Uint8Array` serializes to an
    index-keyed object roughly ten times the size of the base64 payload."""
    block = blocks.at(index)
    scratch = blocks.scratch.get(index)
    if block is None or block.type != "thinking" or scratch is None or not scratch.redactedChunks:
        return
    block.thinkingSignature = _bytesToBase64(scratch.redactedChunks)
    scratch.redactedChunks = None


def _finalizeStreamingBlock(blocks: _Blocks, index: int) -> None:
    """Strips every streaming scratch field. Runs from the terminal paths as well as
    `contentBlockStop`, because a stream can settle without stopping each block."""
    scratch = blocks.scratch.get(index)
    if scratch is None:
        return
    scratch.index = None
    # partialJson is only a streaming scratch buffer; never persist it.
    # MISAKA fork: a still-open buffer gets its exact parse first (the streamed view is throttled).
    block = blocks.at(index)
    if scratch.partialJson is not None and scratch.partialJson.raw and block is not None and block.type == "toolCall":
        scratch.partialJson.finish_into(block)
    scratch.partialJson = None
    _flushRedactedContent(blocks, index)


def _handleMetadata(event: dict[str, Any], model: Model, output: AssistantMessage) -> None:
    usage = event.get("usage")
    if usage:
        output.usage.input = usage.get("inputTokens") or 0
        output.usage.output = usage.get("outputTokens") or 0
        output.usage.cacheRead = usage.get("cacheReadInputTokens") or 0
        output.usage.cacheWrite = usage.get("cacheWriteInputTokens") or 0
        cacheDetails = usage.get("cacheDetails")
        output.usage.cacheWrite1h = (
            sum(nullishCoalesce(detail.get("inputTokens"), 0) if detail.get("ttl") == "1h" else 0 for detail in cacheDetails)
            if cacheDetails is not None
            else None
        )
        output.usage.totalTokens = usage.get("totalTokens") or output.usage.input + output.usage.output
        calculateCost(model, output.usage)


def _handleContentBlockStop(
    event: dict[str, Any],
    blocks: _Blocks,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    index = blocks.findIndex(event.get("contentBlockIndex"))
    block = blocks.at(index)
    if not block:
        return
    scratch = blocks.scratchOf(index)
    scratch.index = None

    match block.type:
        case "text":
            stream.push(TextEndEvent(contentIndex=index, content=block.text, partial=output))
        case "thinking":
            _flushRedactedContent(blocks, index)
            stream.push(ThinkingEndEvent(contentIndex=index, content=block.thinking, partial=output))
        case "toolCall":
            # MISAKA fork: only a streamed buffer replaces the block's arguments (a start block that inlined the
            # input and got no delta keeps it), and the exact parse goes through `finish_into`.
            if scratch.partialJson is not None and scratch.partialJson.raw:
                scratch.partialJson.finish_into(block)
            # Finalize in-place and strip the scratch buffer so replay only
            # carries parsed arguments.
            scratch.partialJson = None
            stream.push(ToolCallEndEvent(contentIndex=index, toolCall=block, partial=output))


def _getModelMatchCandidates(modelId: str, modelName: str | None = None) -> list[str]:
    """Check if the model supports adaptive thinking (Opus 4.6+, Sonnet 4.6).
    Checks both model ID and model name to support application inference profiles
    whose ARNs don't contain the model name."""
    values = [modelId, modelName] if modelName else [modelId]
    return [candidate for value in values for candidate in (value.lower(), re.sub(r"[\s_.:]+", "-", value.lower()))]


def _supportsAdaptiveThinking(modelId: str, modelName: str | None = None) -> bool:
    candidates = _getModelMatchCandidates(modelId, modelName)
    return any(
        "opus-4-6" in s
        or "opus-4-7" in s
        or "opus-4-8" in s
        or "opus-5" in s
        or "sonnet-4-6" in s
        or "sonnet-5" in s
        or "haiku-5" in s
        or "fable-5" in s
        for s in candidates
    )


def _supportsNativeXhighEffort(model: Model) -> bool:
    candidates = _getModelMatchCandidates(model.id, model.name)
    return any(
        "opus-4-7" in s or "opus-4-8" in s or "opus-5" in s or "sonnet-5" in s or "haiku-5" in s or "fable-5" in s
        for s in candidates
    )


def _supportsThinkingBlockBinding(model: Model) -> bool:
    """Check if the model accepts `thinking.block_binding`. Opus 4.6 and Sonnet 4.6 reject it with
    "thinking.adaptive.block_binding: Extra inputs are not permitted"."""
    candidates = _getModelMatchCandidates(model.id, model.name)
    return any(
        "opus-4-7" in s or "opus-4-8" in s or "opus-5" in s or "sonnet-5" in s or "haiku-5" in s or "fable-5" in s
        for s in candidates
    )


def _mapThinkingLevelToEffort(model: Model, level: str | None) -> Literal["low", "medium", "high", "xhigh", "max"]:
    if level == "xhigh" and _supportsNativeXhighEffort(model):
        return "xhigh"

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


def _resolveCacheRetention(cacheRetention: CacheRetention | None = None, env: ProviderEnv | None = None) -> CacheRetention:
    """Resolve cache retention preference.
    Defaults to "short" and uses PI_CACHE_RETENTION for backward compatibility."""
    if cacheRetention:
        return cacheRetention
    if getProviderEnvValue("PI_CACHE_RETENTION", env) == "long":
        return "long"
    return "short"


def _isAnthropicClaudeModel(model: Model) -> bool:
    """Check if the model is an Anthropic Claude model on Bedrock.
    Checks both model ID and model name to support application inference profiles
    whose ARNs don't contain the model name."""
    id = model.id.lower()
    name = nullishCoalesce(model.name.lower() if model.name else None, "")
    return (
        "anthropic.claude" in id
        or "anthropic/claude" in id
        or "anthropic.claude" in name
        or "anthropic/claude" in name
        or "claude" in name
    )


def _supportsPromptCaching(model: Model, env: ProviderEnv | None = None) -> bool:
    """Check if the model supports prompt caching.
    Supported: Claude 3.5 Haiku, Claude 3.7 Sonnet, Claude 4.x models, Claude 5 models

    For base models and system-defined inference profiles the model ID / ARN
    contains the model name, so we can decide locally.

    For application inference profiles (whose ARNs don't contain the model name),
    also checks model.name which is user-controlled via models.json or registerProvider.
    As a last resort, set AWS_BEDROCK_FORCE_CACHE=1 to enable cache points.
    Amazon Nova models have automatic caching and don't need explicit cache points."""
    candidates = _getModelMatchCandidates(model.id, model.name)

    hasClaudeRef = any("claude" in s for s in candidates)
    if not hasClaudeRef:
        # Application inference profiles don't contain the model name in the ARN.
        # Allow users to force cache points via environment variable.
        return getProviderEnvValue("AWS_BEDROCK_FORCE_CACHE", env) == "1"
    # Claude 5 models (fable-5, opus-5, sonnet-5, haiku-5)
    if any("fable-5" in s or "opus-5" in s or "sonnet-5" in s or "haiku-5" in s for s in candidates):
        return True
    # Claude 4.x models (opus-4, sonnet-4, haiku-4)
    if any("-4-" in s for s in candidates):
        return True
    # Claude 3.7 Sonnet
    if any("claude-3-7-sonnet" in s for s in candidates):
        return True
    # Claude 3.5 Haiku
    return any("claude-3-5-haiku" in s for s in candidates)


def _supportsThinkingSignature(model: Model) -> bool:
    """Check if the model supports thinking signatures in reasoningContent.
    Only Anthropic Claude models support the signature field.
    Other models (OpenAI, Qwen, Minimax, Moonshot, etc.) reject it with:
    "This model doesn't support the reasoningContent.reasoningText.signature field"

    Checks both model ID and model name to support application inference profiles."""
    return _isAnthropicClaudeModel(model)


def _buildSystemPrompt(
    systemPrompt: str | None,
    model: Model,
    cacheRetention: CacheRetention,
    env: ProviderEnv | None = None,
) -> list[dict[str, Any]] | None:
    if not systemPrompt:
        return None

    blocks: list[dict[str, Any]] = [{"text": sanitizeSurrogates(systemPrompt)}]

    # Add cache point for supported Claude models when caching is enabled
    if cacheRetention != "none" and _supportsPromptCaching(model, env):
        blocks.append({"cachePoint": {"type": "default", **({"ttl": "1h"} if cacheRetention == "long" else {})}})

    return blocks


def _normalizeToolCallId(id: str, *_: Any) -> str:
    """MISAKA fork: ``transformMessages`` passes three arguments, which JavaScript drops; a character outside the
    BMP is two UTF-16 units, so two underscores, as the JavaScript regex replaces each unit."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", lambda match: "_" * jsLength(match.group()), id)
    return sanitized[:64] if len(sanitized) > 64 else sanitized


def _createNonBlankTextBlock(text: str) -> dict[str, Any] | None:
    sanitized = sanitizeSurrogates(text)
    return None if jsLength(jsTrim(sanitized)) == 0 else {"text": sanitized}


def _createRequiredTextBlock(text: str) -> dict[str, Any]:
    return nullishCoalesce(_createNonBlankTextBlock(text), {"text": _EMPTY_TEXT_PLACEHOLDER})


def _sanitizeBedrockDocument(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitizeBedrockDocument(item) for item in value]
    if value is not None and isinstance(value, dict):
        return {key: _sanitizeBedrockDocument(nestedValue) for key, nestedValue in value.items() if len(key) > 0}
    return value


def _convertToolResultContent(content: Sequence[TextContent | ImageContent]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for c in content:
        if c.type == "image":
            result.append({"image": _createImageBlock(c.mimeType, c.data)})
        else:
            textBlock = _createNonBlankTextBlock(c.text)
            if textBlock:
                result.append(textBlock)
    if len(result) == 0:
        result.append({"text": _EMPTY_TEXT_PLACEHOLDER})
    return result


def _convertMessages(
    context: TranscriptContext,
    model: Model,
    cacheRetention: CacheRetention,
    env: ProviderEnv | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    transformedMessages = transformMessages(
        withoutInitialSystemMessage(context.messages),
        model,
        _normalizeToolCallId,
    )

    i = 0
    while i < len(transformedMessages):
        m: Any = transformedMessages[i]

        match m.role:
            case "user":
                content: list[dict[str, Any]] = []
                if isinstance(m.content, str):
                    content.append(_createRequiredTextBlock(m.content))
                else:
                    for c in m.content:
                        match c.type:
                            case "text":
                                textBlock = _createNonBlankTextBlock(c.text)
                                if textBlock:
                                    content.append(textBlock)
                            case "image":
                                content.append({"image": _createImageBlock(c.mimeType, c.data)})
                            case _:
                                continue
                    if len(content) == 0:
                        content.append({"text": _EMPTY_TEXT_PLACEHOLDER})
                result.append({"role": "user", "content": content})
            case "assistant":
                # Skip assistant messages with empty content (e.g., from aborted requests)
                # Bedrock rejects messages with empty content arrays
                if len(m.content) == 0:
                    i += 1
                    continue
                contentBlocks: list[dict[str, Any]] = []
                for c in m.content:
                    match c.type:
                        case "text":
                            # Skip empty text blocks
                            textBlock = _createNonBlankTextBlock(c.text)
                            if not textBlock:
                                continue
                            contentBlocks.append(textBlock)
                        case "toolCall":
                            contentBlocks.append(
                                {
                                    "toolUse": {
                                        "toolUseId": c.id,
                                        "name": c.name,
                                        "input": _sanitizeBedrockDocument(c.arguments),
                                    }
                                }
                            )
                        case "thinking":
                            # Encrypted reasoning is opaque: replay the stored payload as the
                            # `redactedContent` member instead of lowering it to reasoning text.
                            if c.redacted:
                                redactedContent = _decodeRedactedContent(c.thinkingSignature)
                                if redactedContent:
                                    contentBlocks.append({"reasoningContent": {"redactedContent": redactedContent}})
                                continue
                            # Skip empty thinking blocks
                            thinking = sanitizeSurrogates(c.thinking)
                            if jsLength(jsTrim(thinking)) == 0:
                                continue
                            # Only Anthropic models support the signature field in reasoningText.
                            # For other models, we omit the signature to avoid errors like:
                            # "This model doesn't support the reasoningContent.reasoningText.signature field"
                            if _supportsThinkingSignature(model):
                                # Signatures arrive after thinking deltas. If a partial or externally
                                # persisted message lacks a signature, Bedrock rejects the replayed
                                # reasoning block. Fall back to plain text, matching Anthropic.
                                if not c.thinkingSignature or jsLength(jsTrim(c.thinkingSignature)) == 0:
                                    contentBlocks.append({"text": thinking})
                                else:
                                    contentBlocks.append(
                                        {
                                            "reasoningContent": {
                                                "reasoningText": {"text": thinking, "signature": c.thinkingSignature}
                                            }
                                        }
                                    )
                            else:
                                contentBlocks.append({"reasoningContent": {"reasoningText": {"text": thinking}}})
                        case _:
                            continue
                # Skip if all content blocks were filtered out
                if len(contentBlocks) == 0:
                    i += 1
                    continue
                result.append({"role": "assistant", "content": contentBlocks})
            case "toolResult":
                # Collect all consecutive toolResult messages into a single user message
                # Bedrock requires all tool results to be in one message
                toolResults: list[dict[str, Any]] = []
                # Claude rejects error tool results that contain anything but text, so for Claude their
                # images follow all tool results of the turn (#10794). Other models keep them in place.
                errorImages: list[dict[str, Any]] = []
                j = i
                while j < len(transformedMessages) and transformedMessages[j].role == "toolResult":
                    toolMsg: ToolResultMessage = transformedMessages[j]  # type: ignore[assignment]
                    split = splitErrorToolResultImages(toolMsg) if _isAnthropicClaudeModel(model) else None
                    toolResults.append(
                        {
                            "toolResult": {
                                "toolUseId": toolMsg.toolCallId,
                                "content": _convertToolResultContent(split.content if split else toolMsg.content),
                                "status": "error" if toolMsg.isError else "success",
                            }
                        }
                    )
                    if split:
                        errorImages.extend(
                            [
                                {"text": split.caption},
                                *({"image": _createImageBlock(image.mimeType, image.data)} for image in split.images),
                            ]
                        )
                    j += 1

                # Skip the messages we've already processed
                i = j - 1

                result.append({"role": "user", "content": [*toolResults, *errorImages]})
            case _:
                i += 1
                continue
        i += 1

    # Add cache point to the last user message for supported Claude models when caching is enabled
    if cacheRetention != "none" and _supportsPromptCaching(model, env) and len(result) > 0:
        lastMessage = result[-1]
        if lastMessage["role"] == "user" and lastMessage.get("content"):
            lastMessage["content"].append(
                {"cachePoint": {"type": "default", **({"ttl": "1h"} if cacheRetention == "long" else {})}}
            )

    return result


def _convertToolConfig(
    tools: Sequence[Tool] | None,
    toolChoice: Any,
    supportsStrictMode: bool,
) -> dict[str, Any] | None:
    if not tools:
        return None
    if toolChoice == "none":
        return None

    def convert(tool: Tool) -> dict[str, Any]:
        strict = resolveJsonSchemaStrictSampling(tool, supportsStrictMode)
        return {
            "toolSpec": {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {"json": getJsonSchemaToolParameters(tool, strict)},
                **({"strict": True} if strict is True else {}),
            }
        }

    bedrockTools = [convert(tool) for tool in tools]

    bedrockToolChoice: dict[str, Any] | None = None
    match toolChoice:
        case "auto":
            bedrockToolChoice = {"auto": {}}
        case "any":
            bedrockToolChoice = {"any": {}}
        case _:
            if isinstance(toolChoice, Mapping) and toolChoice.get("type") == "tool":
                bedrockToolChoice = {"tool": {"name": toolChoice.get("name")}}

    # MISAKA fork: botocore rejects an explicit `toolChoice: None`, which the Smithy serializer drops.
    return {"tools": bedrockTools, **({"toolChoice": bedrockToolChoice} if bedrockToolChoice is not None else {})}


class _StopReasonResult(TypedDict, total=False):
    stopReason: StopReason
    errorMessage: str


def _mapStopReason(reason: str | None) -> _StopReasonResult:
    match reason:
        case "end_turn" | "stop_sequence":
            return {"stopReason": "stop"}
        case "max_tokens" | "model_context_window_exceeded":
            return {"stopReason": "length"}
        case "tool_use":
            return {"stopReason": "toolUse"}
        case _:
            return (
                {"stopReason": "error", "errorMessage": f"Provider stopped with: {reason}"}
                if reason
                else {"stopReason": "error"}
            )


def _getConfiguredBedrockRegion(options: Any) -> str | None:
    env = _option(options, "env")
    return (
        _option(options, "region")
        or getProviderEnvValue("AWS_REGION", env)
        or getProviderEnvValue("AWS_DEFAULT_REGION", env)
        or None
    )


def _getConfiguredBedrockCredentials(env: ProviderEnv | None = None) -> dict[str, str] | None:
    accessKeyId = getProviderEnvValue("AWS_ACCESS_KEY_ID", env)
    secretAccessKey = getProviderEnvValue("AWS_SECRET_ACCESS_KEY", env)
    if not accessKeyId or not secretAccessKey:
        return None
    sessionToken = getProviderEnvValue("AWS_SESSION_TOKEN", env)
    return {
        "accessKeyId": accessKeyId,
        "secretAccessKey": secretAccessKey,
        **({"sessionToken": sessionToken} if sessionToken else {}),
    }


def _getStandardBedrockEndpointRegion(baseUrl: str | None) -> str | None:
    if not baseUrl:
        return None

    try:
        hostname = urlparse(baseUrl).hostname or ""
        match = re.match(r"^bedrock-runtime(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$", hostname.lower())
        return match.group(1) if match else None
    except ValueError:
        return None


def _shouldUseExplicitBedrockEndpoint(
    baseUrl: str,
    configuredRegion: str | None,
    hasAmbientConfiguredProfile: bool,
) -> bool:
    endpointRegion = _getStandardBedrockEndpointRegion(baseUrl)
    if not endpointRegion:
        return True

    return not configuredRegion and not hasAmbientConfiguredProfile


def _isGovCloudBedrockTarget(model: Model, options: Any) -> bool:
    region = _getConfiguredBedrockRegion(options)
    if region and region.lower().startswith("us-gov-"):
        return True

    modelId = model.id.lower()
    return modelId.startswith("us-gov.") or modelId.startswith("arn:aws-us-gov:")  # noqa: PIE810 - upstream tests the two prefixes apart


def _buildAdditionalModelRequestFields(model: Model, options: Any) -> dict[str, Any] | None:
    reasoning = _option(options, "reasoning")
    if not reasoning or not model.reasoning:
        return None

    if _isAnthropicClaudeModel(model):
        # GovCloud Bedrock currently rejects the Claude thinking.display field.
        # Omit it there until the GovCloud Converse schema catches up.
        isGovCloud = _isGovCloudBedrockTarget(model, options)
        display = None if isGovCloud else nullishCoalesce(_option(options, "thinkingDisplay"), "summarized")
        # Replayed signed thinking blocks are bound to the system prompt and tools they were
        # created with. Bedrock 400s on replay after either changes unless stale blocks are
        # dropped, matching the Anthropic provider. Skipped on GovCloud like display.
        useBlockBinding = not isGovCloud and _supportsThinkingBlockBinding(model)
        if _supportsAdaptiveThinking(model.id, model.name):
            result: dict[str, Any] = {
                "thinking": {
                    "type": "adaptive",
                    **({"display": display} if display is not None else {}),
                    **({"block_binding": {"prefix_mismatch_behavior": "drop_block"}} if useBlockBinding else {}),
                },
                "output_config": {"effort": _mapThinkingLevelToEffort(model, reasoning)},
                **({"anthropic_beta": [_THINKING_BINDING_CONTROLS_BETA]} if useBlockBinding else {}),
            }
        else:
            defaultBudgets: dict[str, int] = {
                "minimal": 1024,
                "low": 2048,
                "medium": 8192,
                "high": 16384,
                "xhigh": 16384,  # Budget-based Claude clamps extended levels to high
                "max": 16384,
            }

            # Custom budgets only cover token-based levels through high.
            level = "high" if reasoning == "xhigh" or reasoning == "max" else reasoning
            budget = nullishCoalesce(_budgetsDict(_option(options, "thinkingBudgets")).get(level), defaultBudgets[reasoning])

            result = {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": budget,
                    **({"display": display} if display is not None else {}),
                },
            }

        if not _supportsAdaptiveThinking(model.id, model.name) and nullishCoalesce(
            _option(options, "interleavedThinking"), True
        ):
            result["anthropic_beta"] = ["interleaved-thinking-2025-05-14"]

        return result

    candidates = _getModelMatchCandidates(model.id, model.name)

    if any("gpt-oss" in s for s in candidates):
        return {"reasoning_effort": _OPENAI_GPT_OSS_EFFORT[reasoning]}

    if any("gpt-" in s for s in candidates):
        mapped = (model.thinkingLevelMap or {}).get(reasoning)
        return {"reasoning": {"effort": mapped if isinstance(mapped, str) else _OPENAI_GPT_EFFORT[reasoning]}}

    return None


_OpenAIGptEffort = Literal["low", "medium", "high", "xhigh", "max"]
_OpenAIGptOssEffort = Literal["low", "medium", "high"]

# OpenAI GPT models (GPT-5.x, GPT-6) take a nested `reasoning.effort` and reject `minimal`.
_OPENAI_GPT_EFFORT: dict[str, _OpenAIGptEffort] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# gpt-oss takes a flat `reasoning_effort` and only accepts low, medium and high.
_OPENAI_GPT_OSS_EFFORT: dict[str, _OpenAIGptOssEffort] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _createImageBlock(mimeType: str, data: str) -> dict[str, Any]:
    format: str
    match mimeType:
        case "image/jpeg" | "image/jpg":
            format = "jpeg"
        case "image/png":
            format = "png"
        case "image/gif":
            format = "gif"
        case "image/webp":
            format = "webp"
        case _:
            raise RuntimeError(f"Unknown image type: {mimeType}")

    return {"source": {"bytes": _base64ToBytes(data)}, "format": format}


def _base64ToBytes(data: str) -> bytes:
    """MISAKA fork: ``atob``'s forgiving decode -- ASCII whitespace is dropped and the padding is optional, while any
    other stray character is an error (a ``ValueError`` for the ``InvalidCharacterError``) rather than skipped."""
    stripped = re.sub(r"[\t\n\f\r ]", "", data)
    if len(stripped) % 4 == 0:
        stripped = stripped.removesuffix("==") if stripped.endswith("==") else stripped.removesuffix("=")
    if len(stripped) % 4 == 1 or re.fullmatch(r"[A-Za-z0-9+/]*", stripped) is None:
        raise ValueError("The string to be decoded is not correctly encoded.")
    return base64.b64decode(stripped + "=" * (-len(stripped) % 4))


def _decodeRedactedContent(signature: str | None) -> bytes | None:
    """Decodes a stored redacted payload. The AWS SDK hands the blob over as bytes, but a
    persisted session carries it as base64. A hand-edited or externally produced session
    can hold a signature that is not base64; drop that block instead of failing the
    whole request."""
    if not signature:
        return None
    try:
        return _base64ToBytes(signature)
    except ValueError:
        return None


def _bytesToBase64(chunks: Sequence[bytes]) -> str:
    # MISAKA fork: Python encodes the joined bytes at once (no argument-count limit to slice around).
    return base64.b64encode(b"".join(chunks)).decode("ascii")


__all__ = ["BedrockOptions", "BedrockThinkingDisplay", "stream", "streamSimple"]
