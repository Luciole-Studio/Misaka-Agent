"""Translated from pi's ``packages/ai/src/api/openai-completions.ts`` (v1.3.0).

MISAKA fork, kept to what the Python SDK and platform force (each is marked where it happens):

- The SDK glue lives in ``misaka.ai.api._openai_sdk`` (``omit`` for suppressed headers, ``extra_body`` for undeclared
  fields, the request and every stream read raced against the signal). There is no ``fetch`` option.
- A streaming tool call's ``partialArgs`` / ``customInput`` / ``streamIndex`` scratch fields ride beside the block,
  because the content blocks are strict models.
- A tool call's streamed arguments are parsed sparingly (``StreamingArgs``) and finalized through ``finish_into``
  (upstream's ``parseToolCallArguments``).
- A connection error names the transport failure behind it (``_openai_sdk._format_completion_error``), and the
  OpenRouter ``metadata.raw`` is read from ``error.body``, where openai-python keeps what openai-node calls
  ``error.error``.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from misaka.ai.api import _openai_sdk
from misaka.ai.api._common import _compat_value, _option
from misaka.ai.api.constrained_sampling import (
    GrammarToolInputJsonBuffer,
    appendGrammarToolInputJsonDelta,
    createGrammarToolInputProperties,
    getGrammarToolInput,
    getJsonSchemaToolParameters,
    resolveGrammarConstrainedSampling,
    resolveJsonSchemaStrictSampling,
)
from misaka.ai.api.github_copilot_headers import (
    buildCopilotDynamicHeaders,
    hasCopilotVisionInput,
)
from misaka.ai.api.openai_prompt_cache import clampOpenAIPromptCacheKey
from misaka.ai.api.simple_options import (
    buildBaseOptions,
    clampThinkingBudgetToAnswerRoom,
    resolveSamplingParams,
    thinkingBudgetForLevel,
)
from misaka.ai.api.transform_messages import transformMessages
from misaka.ai.models import calculateCost, clampThinkingLevel
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    DoneEvent,
    ErrorEvent,
    Message,
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
    ToolResultMessage,
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.hash import shortHash
from misaka.ai.utils.headers import headersToRecord
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.ai.utils.text import getSystemMessageText, renderSystemMessageUpdate
from misaka.ai.utils.transcript import (
    getDeclaredTools,
    resolveTranscript,
    resolveTranscriptTools,
)
from misaka.utils.js import (
    UNDEFINED,
    isNumber,
    jsonParse,
    jsonStringify,
    jsString,
    jsTrim,
    nullishCoalesce,
)
from misaka.utils.values import maybe_await, read_field, signal_aborted


# Check if conversation messages contain tool calls or tool results.
# This is needed because Anthropic (via proxy) requires the tools param
# to be present when messages include tool_calls or tool role messages.
def _hasHeader(headers: ProviderHeaders | None, name: str) -> bool:
    if not headers:
        return False
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected and value is not None and len(value.strip()) > 0:
            return True
    return False


def _getClientApiKey(provider: str, apiKey: str | None, headers: ProviderHeaders | None) -> str:
    if apiKey:
        return apiKey
    if _hasHeader(headers, "authorization") or _hasHeader(headers, "cf-aig-authorization"):
        return "unused"
    raise RuntimeError(f"No API key for provider: {provider}")


def _hasToolHistory(messages: Sequence[Message]) -> bool:
    for msg in messages:
        if msg.role == "toolResult":
            return True
        if msg.role == "assistant" and any(block.type == "toolCall" for block in msg.content):
            return True
    return False


def _isTextContentBlock(block: Any) -> bool:
    return block.type == "text"


def _isThinkingContentBlock(block: Any) -> bool:
    return block.type == "thinking"


def _isToolCallBlock(block: Any) -> bool:
    return block.type == "toolCall"


def _isImageContentBlock(block: Any) -> bool:
    return block.type == "image"


def _isReasoningDetailObject(detail: Any) -> bool:
    return isinstance(detail, Mapping)


def _hasValidCommonReasoningDetailFields(candidate: Mapping[str, Any]) -> bool:
    return (
        (candidate.get("id") is None or isinstance(candidate.get("id"), str))
        and ("format" not in candidate or isinstance(candidate["format"], str))
        and ("index" not in candidate or isNumber(candidate["index"]))
    )


def _isOpenAIReasoningDetail(detail: Any) -> bool:
    if not _isReasoningDetailObject(detail) or not _hasValidCommonReasoningDetailFields(detail):
        return False
    match detail.get("type"):
        case "reasoning.summary":
            return isinstance(detail.get("summary"), str)
        case "reasoning.encrypted":
            return isinstance(detail.get("data"), str)
        case "reasoning.text":
            return isinstance(detail.get("text"), str) and (
                detail.get("signature") is None or isinstance(detail.get("signature"), str)
            )
        case _:
            return False


class _OpenAICompletionsToolChoiceFunction(TypedDict):
    name: str


class _OpenAICompletionsToolChoiceObject(TypedDict):
    type: Literal["function"]
    function: _OpenAICompletionsToolChoiceFunction


class OpenAICompletionsOptions(TypedDict, total=False):
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
    toolChoice: Literal["auto", "none", "required"] | _OpenAICompletionsToolChoiceObject
    reasoningEffort: Literal["minimal", "low", "medium", "high", "xhigh", "max"]
    # Token budgets per thinking level. Used when `compat.thinkingTokenBudgetField` or `compat.supportsThinkingTokenBudget` is set, or by `{ "$var": "thinking.budget" }`.
    thinkingBudgets: Any


class ConvertCompletionsMessagesOptions(TypedDict, total=False):
    grammarToolInputProperties: Mapping[str, str]


def _parseOpenAIReasoningDetails(signature: str | None) -> list[dict[str, Any]] | None:
    if not signature:
        return None
    try:
        parsed = jsonParse(signature)
        return parsed if isinstance(parsed, list) and len(parsed) > 0 and all(map(_isOpenAIReasoningDetail, parsed)) else None
    except ValueError:
        return None


def _parseLegacyEncryptedReasoningDetail(signature: str | None) -> dict[str, Any] | None:
    if not signature:
        return None
    try:
        parsed = jsonParse(signature)
        return (
            parsed
            if _isOpenAIReasoningDetail(parsed)
            and parsed["type"] == "reasoning.encrypted"
            and isinstance(parsed.get("id"), str)
            and len(parsed["id"]) > 0
            and len(parsed["data"]) > 0
            else None
        )
    except ValueError:
        return None


def _assignFrom(target: dict[str, Any], source: Mapping[str, Any], key: str) -> None:
    """``target[key] = source[key]`` where an absent source key is JS ``undefined``, which
    ``JSON.stringify`` leaves out; a JSON ``null`` the source carries is kept."""
    if key in source:
        target[key] = source[key]
    else:
        target.pop(key, None)


def _fillMissingCommonReasoningDetailFields(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    if target.get("id") is None:
        _assignFrom(target, source, "id")
    if not target.get("format"):
        _assignFrom(target, source, "format")
    if target.get("index") is None:
        _assignFrom(target, source, "index")


def _appendOpenAIReasoningDetail(details: list[dict[str, Any]], detail: Mapping[str, Any]) -> None:
    lastDetail = details[-1] if details else None
    if detail["type"] == "reasoning.text" and lastDetail is not None and lastDetail.get("type") == "reasoning.text":
        lastDetail["text"] += detail["text"]
        if not lastDetail.get("signature"):
            _assignFrom(lastDetail, detail, "signature")
        _fillMissingCommonReasoningDetailFields(lastDetail, detail)
        return
    if detail["type"] == "reasoning.summary" and lastDetail is not None and lastDetail.get("type") == "reasoning.summary":
        lastDetail["summary"] += detail["summary"]
        _fillMissingCommonReasoningDetailFields(lastDetail, detail)
        return
    details.append(dict(detail))


_OPENAI_COMPLETIONS_REASONING_FIELDS = ("reasoning", "reasoning_content", "reasoning_text")


def _isOpenAICompletionsReasoningField(field: str) -> bool:
    return field in _OPENAI_COMPLETIONS_REASONING_FIELDS


def _resolveCacheRetention(cacheRetention: CacheRetention | None = None, env: ProviderEnv | None = None) -> CacheRetention:
    if cacheRetention:
        return cacheRetention
    if getProviderEnvValue("PI_CACHE_RETENTION", env) == "long":
        return "long"
    return "short"


@dataclass
class _CustomToolCallInput:
    property: str
    jsonBuffer: GrammarToolInputJsonBuffer


@dataclass
class _StreamingToolCallScratch:
    """MISAKA fork: pi's ``StreamingToolCallBlock`` widens the block with ``partialArgs``/``customInput``/
    ``streamIndex`` and deletes them when the call finishes; pydantic blocks forbid extra fields, so they live here,
    beside the block, and ``None`` stands for a deleted field. ``partialArgs`` is a ``StreamingArgs`` so the live view
    does not re-parse the whole buffer on every delta."""

    partialArgs: StreamingArgs | None = None
    customInput: _CustomToolCallInput | None = None
    streamIndex: int | None = None


def _field(value: Any, key: str) -> Any:
    """``value?.[key]`` on a parsed JSON object."""
    return value.get(key) if isinstance(value, Mapping) else None


def stream(
    model: Model,
    context: TranscriptContext,
    options: OpenAICompletionsOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    normalizedContext = resolveTranscript(context, _getCompat(model).get("supportsMidConvoSystemMessages"))

    async def run() -> None:
        output = AssistantMessage(
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
        # MISAKA fork: the client this request opened, closed in `finally`.
        client: Any = None

        # `reasoning_details` are replay metadata, not user-visible stream deltas.
        # Keep them in memory during streaming and serialize once when the block is finalized.
        streamedReasoningDetails: list[dict[str, Any]] | None = None

        def applyStreamedReasoningDetails(block: ThinkingContent) -> None:
            if streamedReasoningDetails is not None:
                block.thinkingSignature = jsonStringify(streamedReasoningDetails)

        try:
            apiKey = _getClientApiKey(model.provider, _option(options, "apiKey"), _option(options, "headers"))
            compat = _getCompat(model)
            grammarToolInputProperties = createGrammarToolInputProperties(
                getDeclaredTools(normalizedContext.messages),
                compat["supportsOpenAIGrammarTools"],
            )
            cacheRetention = _resolveCacheRetention(_option(options, "cacheRetention"), _option(options, "env"))
            cacheSessionId = None if cacheRetention == "none" else _option(options, "sessionId")
            client, requestHeaders = _createClient(
                model,
                normalizedContext,
                apiKey,
                _option(options, "headers"),
                cacheSessionId,
                compat,
            )
            params = _buildParams(
                model,
                normalizedContext,
                options,
                compat,
                cacheRetention,
                grammarToolInputProperties,
            )
            onPayload = _option(options, "onPayload")
            nextParams = await maybe_await(onPayload(params, model)) if onPayload else None
            if nextParams is not None:
                params = nextParams
            requestOptions = {
                **({"signal": _option(options, "signal")} if _option(options, "signal") else {}),
                **({"timeout": _option(options, "timeoutMs")} if _option(options, "timeoutMs") is not None else {}),
                "maxRetries": 0,
            }
            openaiStream, response = await retryProviderRequest(
                lambda: _openai_sdk._create_with_response(client, "chat.completions", params, requestOptions, requestHeaders),
                {
                    "maxRetries": _option(options, "maxRetries"),
                    "maxRetryDelayMs": _option(options, "maxRetryDelayMs"),
                    "signal": _option(options, "signal"),
                },
            )
            onResponse = _option(options, "onResponse")
            if onResponse:
                await maybe_await(
                    onResponse({"status": response.status_code, "headers": headersToRecord(response.headers)}, model)
                )
            stream.push(StartEvent(partial=output))

            textBlock: TextContent | None = None
            thinkingBlock: ThinkingContent | None = None
            hasFinishReason = False
            toolCallBlocksByIndex: dict[Any, ToolCall] = {}
            toolCallBlocksById: dict[str, ToolCall] = {}
            # MISAKA fork: the streaming tool calls' scratch fields, keyed by the block's identity.
            streamingToolCalls: dict[int, _StreamingToolCallScratch] = {}
            blocks = output.content

            def getContentIndex(block: Any) -> int:
                """``blocks.indexOf(block)``: by identity, as a JS array searches."""
                return next((index for index, candidate in enumerate(blocks) if candidate is block), -1)

            def scratchOf(block: ToolCall) -> _StreamingToolCallScratch:
                return streamingToolCalls.setdefault(id(block), _StreamingToolCallScratch())

            def getCustomToolCallInput(block: ToolCall) -> str:
                customInput = scratchOf(block).customInput
                property = None if customInput is None else customInput.property
                if property is None:
                    return ""
                value = block.arguments.get(property)
                return value if isinstance(value, str) else ""

            def appendCustomToolCallInput(block: ToolCall, nextInput: str, close: bool) -> str | None:
                customInput = scratchOf(block).customInput
                if not customInput:
                    return None
                delta = appendGrammarToolInputJsonDelta(
                    customInput.jsonBuffer,
                    customInput.property,
                    nextInput,
                    close,
                )
                block.arguments = {customInput.property: nextInput}
                return delta

            def finishBlock(block: Any) -> None:
                contentIndex = getContentIndex(block)
                if contentIndex == -1:
                    return
                if block.type == "text":
                    stream.push(
                        TextEndEvent(
                            contentIndex=contentIndex,
                            content=block.text,
                            partial=output,
                        )
                    )
                elif block.type == "thinking":
                    applyStreamedReasoningDetails(block)
                    stream.push(
                        ThinkingEndEvent(
                            contentIndex=contentIndex,
                            content=block.thinking,
                            partial=output,
                        )
                    )
                elif block.type == "toolCall":
                    scratch = scratchOf(block)
                    if scratch.customInput:
                        delta = appendCustomToolCallInput(block, getCustomToolCallInput(block), True)
                        if delta is not None:
                            stream.push(
                                ToolCallDeltaEvent(
                                    contentIndex=contentIndex,
                                    delta=delta,
                                    partial=output,
                                )
                            )
                    else:
                        # `Object.assign(block, parseToolCallArguments(block.partialArgs))`.
                        (scratch.partialArgs if scratch.partialArgs is not None else StreamingArgs()).finish_into(block)
                    # Finalize in-place and strip the scratch buffers so replay only
                    # carries parsed arguments.
                    streamingToolCalls.pop(id(block), None)
                    stream.push(
                        ToolCallEndEvent(
                            contentIndex=contentIndex,
                            toolCall=block,
                            partial=output,
                        )
                    )

            def ensureTextBlock() -> TextContent:
                nonlocal textBlock
                if textBlock is None:
                    textBlock = TextContent(text="")
                    blocks.append(textBlock)
                    stream.push(TextStartEvent(contentIndex=getContentIndex(textBlock), partial=output))
                return textBlock

            def ensureThinkingBlock(thinkingSignature: str) -> ThinkingContent:
                nonlocal thinkingBlock
                if thinkingBlock is None:
                    thinkingBlock = ThinkingContent(
                        thinking="",
                        thinkingSignature=thinkingSignature,
                    )
                    blocks.append(thinkingBlock)
                    stream.push(ThinkingStartEvent(contentIndex=getContentIndex(thinkingBlock), partial=output))
                return thinkingBlock

            def ensureToolCallBlock(toolCall: Mapping[str, Any]) -> ToolCall:
                streamIndex = toolCall.get("index") if isNumber(toolCall.get("index")) else None
                function = toolCall.get("function")
                custom = toolCall.get("custom")
                name = nullishCoalesce(_field(function, "name"), nullishCoalesce(_field(custom, "name"), ""))
                block = toolCallBlocksByIndex.get(streamIndex) if streamIndex is not None else None
                if block is None and toolCall.get("id"):
                    block = toolCallBlocksById.get(toolCall["id"])
                if block is None:
                    # Note: the "input" fallback here should/must not be taken.  in case the LLM makes up
                    # a tool we don't knwo about, we at least have a place to stash our stuff.
                    customInputProperty = (
                        nullishCoalesce(grammarToolInputProperties.get(name), "input")
                        if custom is not None and function is None
                        else None
                    )
                    hasCustomInput = customInputProperty is not None
                    block = ToolCall(
                        id=toolCall.get("id") or "",
                        name=name,
                        arguments={customInputProperty: ""} if hasCustomInput else {},
                    )
                    streamingToolCalls[id(block)] = _StreamingToolCallScratch(
                        partialArgs=None if hasCustomInput else StreamingArgs(""),
                        customInput=(
                            _CustomToolCallInput(
                                property=customInputProperty,
                                jsonBuffer=GrammarToolInputJsonBuffer(input="", started=False, closed=False),
                            )
                            if hasCustomInput
                            else None
                        ),
                        streamIndex=streamIndex,
                    )
                    if streamIndex is not None:
                        toolCallBlocksByIndex[streamIndex] = block
                    if toolCall.get("id"):
                        toolCallBlocksById[toolCall["id"]] = block
                    blocks.append(block)
                    stream.push(
                        ToolCallStartEvent(
                            contentIndex=getContentIndex(block),
                            partial=output,
                        )
                    )
                scratch = scratchOf(block)
                if streamIndex is not None and scratch.streamIndex is None:
                    scratch.streamIndex = streamIndex
                    toolCallBlocksByIndex[streamIndex] = block
                if toolCall.get("id"):
                    toolCallBlocksById[toolCall["id"]] = block
                if not block.name and name:
                    block.name = name
                if custom is not None and function is None and not scratch.customInput:
                    customInputProperty = nullishCoalesce(grammarToolInputProperties.get(block.name), "input")
                    block.arguments = {customInputProperty: ""}
                    scratch.customInput = _CustomToolCallInput(
                        property=customInputProperty,
                        jsonBuffer=GrammarToolInputJsonBuffer(input="", started=False, closed=False),
                    )
                    scratch.partialArgs = None
                return block

            onProviderStreamEvent = _option(options, "onProviderStreamEvent")
            # MISAKA fork: the chunk iterator is closed with the reply (GitHub issue #6).
            async with contextlib.aclosing(_openai_sdk._iterate_stream(openaiStream, _option(options, "signal"))) as chunks:
                async for chunk in chunks:
                    if onProviderStreamEvent:
                        await maybe_await(onProviderStreamEvent(chunk, model))
                    if not chunk or not isinstance(chunk, Mapping):
                        continue

                    # OpenAI documents ChatCompletionChunk.id as the unique chat completion identifier,
                    # and each chunk in a streamed completion carries the same id.
                    output.responseId = output.responseId or chunk.get("id")
                    if isinstance(chunk.get("model"), str) and len(chunk["model"]) > 0 and chunk["model"] != model.id:
                        output.responseModel = output.responseModel or chunk["model"]
                    if chunk.get("usage") is not None:
                        output.usage = _parseChunkUsage(chunk["usage"], model)

                    choices = chunk.get("choices")
                    choice = (choices[0] if choices else None) if isinstance(choices, list) else None
                    if not choice or not isinstance(choice, Mapping):
                        continue

                    # Fallback: some providers (e.g., Moonshot) return usage
                    # in choice.usage instead of the standard chunk.usage
                    if chunk.get("usage") is None and choice.get("usage") is not None:
                        output.usage = _parseChunkUsage(choice["usage"], model)

                    if choice.get("finish_reason"):
                        output.rawStopReason = choice["finish_reason"]
                        finishReasonResult = _mapStopReason(choice["finish_reason"])
                        output.stopReason = finishReasonResult["stopReason"]
                        if finishReasonResult.get("errorMessage"):
                            output.errorMessage = finishReasonResult["errorMessage"]
                        hasFinishReason = True

                    if isinstance(choice.get("delta"), Mapping):
                        # Handle reasoning before content: some proxies (e.g. LiteLLM) aggregate reasoning and
                        # answer into one delta, and the thinking block must precede the text block.
                        # Some endpoints return reasoning in reasoning_content (llama.cpp),
                        # or reasoning (other openai compatible endpoints)
                        # Use the first non-empty reasoning field to avoid duplication
                        # (e.g., chutes.ai returns both reasoning_content and reasoning with same content)
                        reasoningFields = ["reasoning_content", "reasoning", "reasoning_text"]
                        deltaFields = choice["delta"]
                        foundReasoningField: str | None = None
                        for field in reasoningFields:
                            value = deltaFields.get(field)
                            if isinstance(value, str) and len(value) > 0:
                                foundReasoningField = field
                                break

                        if foundReasoningField:
                            delta = deltaFields[foundReasoningField]
                            if isinstance(delta, str) and len(delta) > 0:
                                thinkingSignature = (
                                    "reasoning_content"
                                    if model.provider == "opencode-go" and foundReasoningField == "reasoning"
                                    else foundReasoningField
                                )
                                block = ensureThinkingBlock(thinkingSignature)
                                block.thinking += delta
                                stream.push(
                                    ThinkingDeltaEvent(
                                        contentIndex=getContentIndex(block),
                                        delta=delta,
                                        partial=output,
                                    )
                                )

                        reasoningDetails = choice["delta"].get("reasoning_details")
                        if isinstance(reasoningDetails, list):
                            for detail in reasoningDetails:
                                if not _isOpenAIReasoningDetail(detail):
                                    continue
                                ensureThinkingBlock("")
                                if streamedReasoningDetails is None:
                                    streamedReasoningDetails = []
                                # Keep provider replay data in the existing signature slot. OpenRouter streams
                                # reasoning_details as deltas: consecutive text/summary deltas are merged into
                                # logical entries, while encrypted entries remain opaque and discrete.
                                _appendOpenAIReasoningDetail(streamedReasoningDetails, detail)

                        if choice["delta"].get("content") is not None and len(choice["delta"]["content"]) > 0:
                            block = ensureTextBlock()
                            block.text += choice["delta"]["content"]
                            stream.push(
                                TextDeltaEvent(
                                    contentIndex=getContentIndex(block),
                                    delta=choice["delta"]["content"],
                                    partial=output,
                                )
                            )

                        if choice["delta"].get("tool_calls"):
                            for toolCall in choice["delta"]["tool_calls"]:
                                if not isinstance(toolCall, Mapping):
                                    continue
                                block = ensureToolCallBlock(toolCall)
                                if not block.id and toolCall.get("id"):
                                    block.id = toolCall["id"]
                                    toolCallBlocksById[toolCall["id"]] = block
                                name = nullishCoalesce(_field(toolCall.get("function"), "name"), _field(toolCall.get("custom"), "name"))
                                if not block.name and name:
                                    block.name = name

                                delta = ""
                                if _field(toolCall.get("function"), "arguments"):
                                    delta = toolCall["function"]["arguments"]
                                    scratch = scratchOf(block)
                                    if scratch.partialArgs is None:
                                        scratch.partialArgs = StreamingArgs("")
                                    scratch.partialArgs.append(toolCall["function"]["arguments"])
                                    block.arguments = scratch.partialArgs.arguments
                                elif _field(toolCall.get("custom"), "input"):
                                    nextInput = getCustomToolCallInput(block) + toolCall["custom"]["input"]
                                    delta = nullishCoalesce(appendCustomToolCallInput(block, nextInput, False), "")
                                stream.push(
                                    ToolCallDeltaEvent(
                                        contentIndex=getContentIndex(block),
                                        delta=delta,
                                        partial=output,
                                    )
                                )

            for block in blocks:
                finishBlock(block)
            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")

            if output.stopReason == "aborted":
                raise RuntimeError("Request was aborted")
            if not hasFinishReason and not compat["supportsFinishReason"]:
                output.stopReason = "toolUse" if any(block.type == "toolCall" for block in output.content) else "stop"
            if output.stopReason == "error":
                raise RuntimeError(output.errorMessage or "Provider returned an error stop reason")
            if (compat["supportsFinishReason"] and not hasFinishReason) or output.stopReason == "pending":
                raise RuntimeError("Stream ended without finish_reason")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
            stream.end()
        except Exception as error:  # noqa: BLE001 - every failure becomes the stream's error event
            for block in output.content:
                if block.type == "thinking":
                    applyStreamedReasoningDetails(block)
                # MISAKA fork: streaming scratch buffers live beside the blocks
                # (`_StreamingToolCallScratch`), so there is nothing to strip here.
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            # MISAKA fork: a connection error names the transport failure behind it.
            output.errorMessage = _openai_sdk._format_completion_error(error)
            # Some providers via OpenRouter give additional information in this field.
            # normalizeProviderError already stringifies the parsed body (error.error)
            # into errorMessage, so only append the raw metadata when it is not already
            # present to avoid double-printing it.
            # MISAKA fork: openai-python keeps what openai-node calls `error.error` on `error.body`.
            rawMetadata = read_field(read_field(getattr(error, "body", None), "metadata"), "raw")
            if rawMetadata and jsString(rawMetadata) not in output.errorMessage:
                output.errorMessage += f"\n{jsString(rawMetadata)}"
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
            stream.end()
        finally:
            # MISAKA fork: close the client this request opened while its loop still runs.
            await _openai_sdk._close_client(client)

    spawn_stream_task(run(), stream=stream)
    return stream


def streamSimple(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    _getClientApiKey(model.provider, _option(options, "apiKey"), _option(options, "headers"))

    base = {
        **dict(buildBaseOptions(model, context, options, _option(options, "apiKey"))),
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
            "thinkingBudgets": _option(options, "thinkingBudgets"),
        },
    )


def _createClient(
    model: Model,
    context: TranscriptContext,
    apiKey: str,
    optionsHeaders: ProviderHeaders | None = None,
    sessionId: str | None = None,
    compat: Mapping[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """MISAKA fork: returns the client and the per-request headers that carry every suppression
    (``_openai_sdk._sdk_client``)."""
    if compat is None:
        compat = _getCompat(model)
    headers: dict[str, Any] = {"User-Agent": getPiUserAgent(), **(model.headers or {})}
    if model.provider == "github-copilot":
        hasImages = hasCopilotVisionInput(context.messages)
        copilotHeaders = buildCopilotDynamicHeaders({
            "messages": context.messages,
            "hasImages": hasImages,
        })
        headers.update(copilotHeaders)

    if sessionId and compat["sendSessionAffinityHeaders"]:
        if compat["sessionAffinityFormat"] == "openrouter":
            headers["x-session-id"] = sessionId
        else:
            if compat["sessionAffinityFormat"] == "openai":
                headers["session_id"] = sessionId
            headers["x-client-request-id"] = sessionId
            headers["x-session-affinity"] = sessionId

    # Merge options headers last so they can override defaults
    if optionsHeaders:
        headers.update(optionsHeaders)

    return _openai_sdk._sdk_client(
        apiKey=apiKey,
        baseURL=model.baseUrl,
        defaultHeaders=headers,
    )


def _levelMap(model: Model, level: str) -> Any:
    """``model.thinkingLevelMap?.[level]``: ``UNDEFINED`` when the map or the entry is absent, so a ``=== undefined``
    test tells it from an explicit ``null``."""
    if model.thinkingLevelMap is None:
        return UNDEFINED
    return model.thinkingLevelMap.get(level, UNDEFINED)


def _dumpModel(value: Any) -> Any:
    """A compat sub-object as the JSON object pi sends."""
    return value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else value


def _buildParams(
    model: Model,
    context: TranscriptContext,
    options: OpenAICompletionsOptions | Mapping[str, Any] | None = None,
    compat: Mapping[str, Any] | None = None,
    cacheRetention: CacheRetention | None = None,
    grammarToolInputProperties: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if compat is None:
        compat = _getCompat(model)
    if cacheRetention is None:
        cacheRetention = _resolveCacheRetention(_option(options, "cacheRetention"), _option(options, "env"))
    if grammarToolInputProperties is None:
        grammarToolInputProperties = createGrammarToolInputProperties(
            getDeclaredTools(context.messages),
            compat["supportsOpenAIGrammarTools"],
        )
    transcriptTools = resolveTranscriptTools(
        context.messages,
        compat.get("supportsMidConvoSystemMessages") is True and compat.get("supportsMidConvoToolAdditions") is True,
    )
    messages = convertMessages(
        model,
        context,
        compat,
        {
            "grammarToolInputProperties": grammarToolInputProperties,
        },
    )
    cacheControl = _getCompatCacheControl(compat, cacheRetention)

    params: dict[str, Any] = {
        "model": model.id,
        "messages": messages,
        "stream": True,
        "prompt_cache_key": (
            clampOpenAIPromptCacheKey(_option(options, "sessionId"))
            if ("api.openai.com" in model.baseUrl and cacheRetention != "none")
            or (cacheRetention == "long" and compat["supportsLongCacheRetention"])
            else None
        ),
        "prompt_cache_retention": "24h" if cacheRetention == "long" and compat["supportsLongCacheRetention"] else None,
    }

    if compat["supportsUsageInStreaming"] is not False:
        params["stream_options"] = {"include_usage": True}

    if compat["supportsStore"]:
        params["store"] = False

    if _option(options, "maxTokens"):
        if compat["maxTokensField"] == "max_tokens":
            # Deprecated by OpenAI, but some OpenAI-compatible providers only accept max_tokens.
            params["max_tokens"] = _option(options, "maxTokens")
        else:
            params["max_completion_tokens"] = _option(options, "maxTokens")

    if _option(options, "temperature") is not None:
        params["temperature"] = _option(options, "temperature")

    if len(transcriptTools.requestTools) > 0:
        params["tools"] = _convertTools(transcriptTools.requestTools, compat)
        if compat["zaiToolStream"]:
            params["tool_stream"] = True
    elif _hasToolHistory(context.messages):
        # Anthropic (via LiteLLM/proxy) requires tools param when conversation has tool_calls/tool_results
        params["tools"] = []

    if cacheControl:
        _applyAnthropicCacheControl(messages, params.get("tools"), cacheControl)

    if _option(options, "toolChoice"):
        params["tool_choice"] = _option(options, "toolChoice")

    if compat.get("vllmPriority") is not None:
        params["priority"] = compat["vllmPriority"]

    thinkingTokenBudgetField = _resolveThinkingTokenBudgetField(compat)
    thinkingBudget = _resolveClampedThinkingBudget(model, options, params)
    reasoningEffort = _option(options, "reasoningEffort")

    if compat["thinkingFormat"] == "zai" and model.reasoning:
        params["thinking"] = {"type": "enabled", "clear_thinking": False} if reasoningEffort else {"type": "disabled"}
        if reasoningEffort and compat["supportsReasoningEffort"]:
            mappedEffort = _levelMap(model, reasoningEffort)
            effort = reasoningEffort if mappedEffort is UNDEFINED else mappedEffort
            if isinstance(effort, str):
                params["reasoning_effort"] = effort
    elif compat["thinkingFormat"] == "qwen" and model.reasoning:
        params["enable_thinking"] = bool(reasoningEffort)
        if reasoningEffort and compat["supportsReasoningEffort"]:
            effort = nullishCoalesce(_levelMap(model, reasoningEffort), reasoningEffort)
            if isinstance(effort, str):
                params["reasoning_effort"] = effort
    elif compat["thinkingFormat"] == "qwen-chat-template" and model.reasoning:
        params["chat_template_kwargs"] = {
            "enable_thinking": bool(reasoningEffort),
            "preserve_thinking": True,
        }
    elif compat["thinkingFormat"] == "chat-template" and model.reasoning:
        chatTemplateKwargs = _buildChatTemplateValues(model, options, compat["chatTemplateKwargs"], thinkingBudget)
        if chatTemplateKwargs:
            params["chat_template_kwargs"] = chatTemplateKwargs
    elif compat["thinkingFormat"] == "baseten" and model.reasoning:
        chatTemplateArgs = _buildChatTemplateValues(model, options, compat["chatTemplateArgs"], thinkingBudget)
        if chatTemplateArgs:
            params["chat_template_args"] = chatTemplateArgs
        if compat["supportsReasoningEffort"]:
            requestedEffort = reasoningEffort
            mappedEffort = _levelMap(model, requestedEffort) if requestedEffort else _levelMap(model, "off")
            effort = requestedEffort if mappedEffort is UNDEFINED else mappedEffort
            if isinstance(effort, str):
                params["reasoning_effort"] = effort
    elif compat["thinkingFormat"] == "deepseek" and model.reasoning:
        if reasoningEffort:
            params["thinking"] = {"type": "enabled"}
        elif _levelMap(model, "off") is not None:
            params["thinking"] = {"type": "disabled"}
        if reasoningEffort and compat["supportsReasoningEffort"]:
            params["reasoning_effort"] = nullishCoalesce(_levelMap(model, reasoningEffort), reasoningEffort)
    elif compat["thinkingFormat"] == "openrouter" and model.reasoning:
        # OpenRouter normalizes reasoning across providers via a nested reasoning object.
        if reasoningEffort:
            params["reasoning"] = {
                "effort": nullishCoalesce(_levelMap(model, reasoningEffort), reasoningEffort),
            }
        elif _levelMap(model, "off") is not None:
            params["reasoning"] = {"effort": nullishCoalesce(_levelMap(model, "off"), "none")}
    elif compat["thinkingFormat"] == "ant-ling" and model.reasoning and reasoningEffort:
        effort = _levelMap(model, reasoningEffort)
        if isinstance(effort, str):
            params["reasoning"] = {"effort": effort}
    elif compat["thinkingFormat"] == "together" and model.reasoning:
        params["reasoning"] = {"enabled": bool(reasoningEffort)}
        if reasoningEffort and compat["supportsReasoningEffort"]:
            params["reasoning_effort"] = nullishCoalesce(_levelMap(model, reasoningEffort), reasoningEffort)
    elif compat["thinkingFormat"] == "string-thinking" and model.reasoning:
        if reasoningEffort:
            params["thinking"] = nullishCoalesce(_levelMap(model, reasoningEffort), reasoningEffort)
        elif _levelMap(model, "off") is not None:
            params["thinking"] = nullishCoalesce(_levelMap(model, "off"), "none")
    elif reasoningEffort and model.reasoning and compat["supportsReasoningEffort"]:
        # OpenAI-style reasoning_effort
        params["reasoning_effort"] = nullishCoalesce(_levelMap(model, reasoningEffort), reasoningEffort)
    elif not reasoningEffort and model.reasoning and compat["supportsReasoningEffort"]:
        offValue = _levelMap(model, "off")
        if isinstance(offValue, str):
            params["reasoning_effort"] = offValue

    # Cap reasoning with a top-level budget field. Independent of thinkingFormat: the
    # same server can serve zai, qwen or chat-template models. Reasoning and the answer
    # share max_tokens here, so an uncapped reasoning phase can consume the whole
    # response and leave no answer and no tool call.
    if thinkingTokenBudgetField and thinkingBudget is not None:
        params[thinkingTokenBudgetField] = thinkingBudget

    # OpenRouter provider routing preferences
    if _compat_value(model, "openRouterRouting") is not None:
        params["provider"] = _dumpModel(_compat_value(model, "openRouterRouting"))

    # Vercel AI Gateway provider routing preferences
    if _compat_value(model, "vercelGatewayRouting") is not None:
        routing = _dumpModel(_compat_value(model, "vercelGatewayRouting"))
        # `is not None`: an empty list is truthy in JS and is sent as given.
        if routing.get("only") is not None or routing.get("order") is not None:
            gatewayOptions: dict[str, list[str]] = {}
            if routing.get("only") is not None:
                gatewayOptions["only"] = routing["only"]
            if routing.get("order") is not None:
                gatewayOptions["order"] = routing["order"]
            params["providerOptions"] = {"gateway": gatewayOptions}

    # Last so model and request sampling parameters override named request fields.
    samplingParams = resolveSamplingParams(model, nullishCoalesce(reasoningEffort, "off"), _option(options, "samplingParams"))
    if samplingParams:
        params.update(samplingParams)

    return params


def _resolveThinkingTokenBudgetField(compat: Mapping[str, Any]) -> str | None:
    if compat.get("thinkingTokenBudgetField"):
        return compat["thinkingTokenBudgetField"]
    if compat.get("supportsThinkingTokenBudget"):
        return "thinking_token_budget"
    return None


def _resolveClampedThinkingBudget(
    model: Model, options: OpenAICompletionsOptions | Mapping[str, Any] | None, params: Mapping[str, Any]
) -> int | None:
    if not _option(options, "reasoningEffort") or not model.reasoning:
        return None
    ceiling = nullishCoalesce(params.get("max_tokens"), nullishCoalesce(params.get("max_completion_tokens"), model.maxTokens))
    budget = clampThinkingBudgetToAnswerRoom(
        thinkingBudgetForLevel(_option(options, "reasoningEffort"), _option(options, "thinkingBudgets")),
        ceiling,
    )
    return budget if budget > 0 else None


def _buildChatTemplateValues(
    model: Model,
    options: OpenAICompletionsOptions | Mapping[str, Any] | None,
    values: Mapping[str, Any],
    thinkingBudget: int | None = None,
) -> dict[str, Any] | None:
    resolvedValues: dict[str, Any] = {}

    for key, value in values.items():
        resolved = _resolveChatTemplateKwargValue(model, options, value, thinkingBudget)
        if resolved is not UNDEFINED:
            resolvedValues[key] = resolved

    return resolvedValues if len(resolvedValues) > 0 else None


def _resolveChatTemplateKwargValue(
    model: Model,
    options: OpenAICompletionsOptions | Mapping[str, Any] | None,
    value: Any,
    thinkingBudget: int | None = None,
) -> Any:
    """``UNDEFINED`` for "do not send this key", distinct from ``None``, which a template may want."""
    if not isinstance(value, Mapping):
        return value

    reasoningEffort = _option(options, "reasoningEffort")
    if not reasoningEffort and value.get("omitWhenOff"):
        return UNDEFINED
    if value.get("$var") == "thinking.enabled":
        return bool(reasoningEffort)
    if value.get("$var") == "thinking.budget":
        return UNDEFINED if thinkingBudget is None else thinkingBudget

    mappedValue = _levelMap(model, reasoningEffort) if reasoningEffort else _levelMap(model, "off")
    if mappedValue is UNDEFINED:
        return UNDEFINED if reasoningEffort is None else reasoningEffort
    return mappedValue if isinstance(mappedValue, str) else UNDEFINED


def _getCompatCacheControl(compat: Mapping[str, Any], cacheRetention: CacheRetention) -> dict[str, Any] | None:
    if compat.get("cacheControlFormat") != "anthropic" or cacheRetention == "none":
        return None

    ttl = "1h" if cacheRetention == "long" and compat["supportsLongCacheRetention"] else None
    return {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}


def _applyAnthropicCacheControl(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    cacheControl: dict[str, Any],
) -> None:
    _addCacheControlToSystemPrompt(messages, cacheControl)
    _addCacheControlToLastTool(tools, cacheControl)
    _addCacheControlToLastConversationMessage(messages, cacheControl)


def _addCacheControlToSystemPrompt(
    messages: list[dict[str, Any]],
    cacheControl: dict[str, Any],
) -> None:
    for message in messages:
        if message.get("role") == "system" or message.get("role") == "developer":
            _addCacheControlToInstructionMessage(message, cacheControl)
            return


def _addCacheControlToLastConversationMessage(
    messages: list[dict[str, Any]],
    cacheControl: dict[str, Any],
) -> None:
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if (
            message.get("role") == "user" or message.get("role") == "assistant" or message.get("role") == "tool"
        ) and _addCacheControlToMessage(message, cacheControl):
            return


def _addCacheControlToLastTool(
    tools: list[dict[str, Any]] | None,
    cacheControl: dict[str, Any],
) -> None:
    if not tools or len(tools) == 0:
        return

    lastTool = tools[-1]
    lastTool["cache_control"] = cacheControl


def _addCacheControlToInstructionMessage(
    message: dict[str, Any],
    cacheControl: dict[str, Any],
) -> bool:
    return _addCacheControlToTextContent(message, cacheControl)


def _addCacheControlToMessage(
    message: dict[str, Any],
    cacheControl: dict[str, Any],
) -> bool:
    if message.get("role") == "user" or message.get("role") == "assistant" or message.get("role") == "tool":
        return _addCacheControlToTextContent(message, cacheControl)
    return False


def _addCacheControlToTextContent(
    message: dict[str, Any],
    cacheControl: dict[str, Any],
) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        if len(content) == 0:
            return False
        message["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": cacheControl,
            },
        ]
        return True

    if not isinstance(content, list):
        return False

    for i in range(len(content) - 1, -1, -1):
        part = content[i]
        if isinstance(part, dict) and part.get("type") == "text":
            textPart = part
            textPart["cache_control"] = cacheControl
            return True

    return False


def convertMessages(
    model: Model,
    context: TranscriptContext,
    compat: Mapping[str, Any],
    options: ConvertCompletionsMessagesOptions | None = None,
) -> list[dict[str, Any]]:
    normalizedContext = resolveTranscript(context, compat.get("supportsMidConvoSystemMessages"))
    params: list[dict[str, Any]] = []
    grammarToolInputProperties = None if options is None else options.get("grammarToolInputProperties")

    def normalizeToolCallId(id: str) -> str:
        # Handle pipe-separated IDs from OpenAI Responses API
        # Format: {call_id}|{id} where {id} can be 400+ chars with special chars (+, /, =)
        # These come from providers like github-copilot, openai-codex, opencode
        # Extract just the call_id part and normalize it
        # Multiple tool calls in the same turn can share call_id but differ by item_id.
        # Preserve item-level uniqueness when replaying into Chat Completions, which
        # requires distinct tool call ids.
        if "|" in id:
            # Sanitize to allowed chars and truncate to 40 chars (OpenAI limit)
            separatorIndex = id.index("|")
            callId = re.sub(r"[^a-zA-Z0-9_-]", "_", id[:separatorIndex])
            itemId = re.sub(r"[^a-zA-Z0-9_-]", "_", id[separatorIndex + 1 :])
            combinedId = f"{callId}_{itemId}" if len(itemId) > 0 else callId
            if len(combinedId) <= 40:
                return combinedId
            hash = shortHash(id)[:8]
            prefix = callId[: max(1, 40 - len(hash) - 1)]
            return f"{prefix}_{hash}"

        if model.provider == "openai":
            return id[:40] if len(id) > 40 else id
        return id

    transformedMessages = transformMessages(normalizedContext.messages, model, lambda id, *_: normalizeToolCallId(id))
    transcriptTools = resolveTranscriptTools(
        normalizedContext.messages,
        compat.get("supportsMidConvoSystemMessages") is True and compat.get("supportsMidConvoToolAdditions") is True,
    )
    instructionRole = "developer" if model.reasoning and compat["supportsDeveloperRole"] else "system"

    lastRole: str | None = None

    # `for (let i = 0; i < n; i++)` with the increment first, so `continue` and `i = j - 1` work as in the for loop.
    i = -1
    while i + 1 < len(transformedMessages):
        i += 1
        msg = transformedMessages[i]
        # Some providers don't allow user messages directly after tool results
        # Insert a synthetic assistant message to bridge the gap
        if compat["requiresAssistantAfterToolResult"] and lastRole == "toolResult" and msg.role == "user":
            params.append({
                "role": "assistant",
                "content": "I have processed the tool results.",
            })

        if msg.role == "system":
            addedTools = nullishCoalesce(msg.toolsAdded, []) if i > 0 and transcriptTools.anchorsAdditions else []
            if len(addedTools) > 0:
                kimiToolMessage = {
                    "role": "system",
                    "tools": _convertTools(addedTools, compat),
                }
                params.append(kimiToolMessage)
            text = getSystemMessageText(msg) if i == 0 else renderSystemMessageUpdate(msg)
            if len(text) > 0:
                params.append({"role": instructionRole, "content": sanitizeSurrogates(text)})
        elif msg.role == "user":
            if isinstance(msg.content, str):
                params.append({
                    "role": "user",
                    "content": sanitizeSurrogates(msg.content),
                })
            else:
                content: list[dict[str, Any]] = [
                    {
                        "type": "text",
                        "text": sanitizeSurrogates(item.text),
                    }
                    if item.type == "text"
                    else {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{item.mimeType};base64,{item.data}",
                        },
                    }
                    for item in msg.content
                    if item.type != "text" or len(item.text) > 0
                ]
                if len(content) == 0:
                    continue
                params.append({
                    "role": "user",
                    "content": content,
                })
        elif msg.role == "assistant":
            # Some providers don't accept null content, use empty string instead
            assistantMsg: dict[str, Any] = {
                "role": "assistant",
                "content": "" if compat["requiresAssistantAfterToolResult"] else None,
            }

            assistantTextParts = [
                {
                    "type": "text",
                    "text": sanitizeSurrogates(block.text),
                }
                for block in msg.content
                if _isTextContentBlock(block) and len(jsTrim(block.text)) > 0
            ]
            assistantText = "".join(part["text"] for part in assistantTextParts)

            thinkingBlocks = [block for block in msg.content if _isThinkingContentBlock(block)]
            toolCalls = [block for block in msg.content if _isToolCallBlock(block)]
            signedReasoningDetails = next(
                (
                    details
                    for details in (_parseOpenAIReasoningDetails(block.thinkingSignature) for block in thinkingBlocks)
                    if details is not None
                ),
                None,
            )
            legacyReasoningDetails = [
                detail
                for detail in (_parseLegacyEncryptedReasoningDetail(toolCall.thoughtSignature) for toolCall in toolCalls)
                if detail is not None
            ]
            preservedReasoningDetails = nullishCoalesce(
                signedReasoningDetails, legacyReasoningDetails if len(legacyReasoningDetails) > 0 else None
            )

            nonEmptyThinkingBlocks = [block for block in thinkingBlocks if len(jsTrim(block.thinking)) > 0]
            if len(nonEmptyThinkingBlocks) > 0:
                if compat["requiresThinkingAsText"]:
                    # Convert thinking blocks to plain text (no tags to avoid model mimicking them)
                    thinkingText = "\n\n".join(sanitizeSurrogates(block.thinking) for block in nonEmptyThinkingBlocks)
                    assistantMsg["content"] = [{"type": "text", "text": thinkingText}, *assistantTextParts]
                else:
                    # Always send assistant content as a plain string (OpenAI Chat Completions
                    # API standard format). Sending as an array of {type:"text", text:"..."}
                    # objects is non-standard and causes some models (e.g. DeepSeek V3.2 via
                    # NVIDIA NIM) to mirror the content-block structure literally in their
                    # output, producing recursive nesting like [{'type':'text','text':'[{...}]'}].
                    if len(assistantText) > 0:
                        assistantMsg["content"] = assistantText

                    # reasoning_details is the structured alternative to a raw reasoning field.
                    if not preservedReasoningDetails:
                        # Use the signature from the first thinking block if available (for llama.cpp server + gpt-oss)
                        signature = nonEmptyThinkingBlocks[0].thinkingSignature
                        if model.provider == "opencode-go" and signature == "reasoning":
                            signature = "reasoning_content"
                        if signature and _isOpenAICompletionsReasoningField(signature):
                            assistantMsg[signature] = "\n".join(block.thinking for block in nonEmptyThinkingBlocks)
            elif len(assistantText) > 0:
                # Always send assistant content as a plain string (OpenAI Chat Completions
                # API standard format). Sending as an array of {type:"text", text:"..."}
                # objects is non-standard and causes some models (e.g. DeepSeek V3.2 via
                # NVIDIA NIM) to mirror the content-block structure literally in their
                # output, producing recursive nesting like [{'type':'text','text':'[{...}]'}].
                assistantMsg["content"] = assistantText

            if len(toolCalls) > 0:

                def convertToolCall(tc: ToolCall) -> dict[str, Any]:
                    customInputProperty = (
                        None if grammarToolInputProperties is None else grammarToolInputProperties.get(tc.name)
                    )
                    if customInputProperty is not None:
                        return {
                            "id": tc.id,
                            "type": "custom",
                            "custom": {
                                "name": tc.name,
                                "input": sanitizeSurrogates(getGrammarToolInput(tc.name, tc.arguments, customInputProperty)),
                            },
                        }
                    return {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": jsonStringify(tc.arguments),
                        },
                    }

                assistantMsg["tool_calls"] = [convertToolCall(tc) for tc in toolCalls]
            if preservedReasoningDetails:
                assistantMsg["reasoning_details"] = preservedReasoningDetails
            if (
                compat["requiresReasoningContentOnAssistantMessages"]
                and model.reasoning
                and "reasoning_content" not in assistantMsg
            ):
                assistantMsg["reasoning_content"] = ""
            # Skip assistant messages that have no content and no tool calls.
            # Some providers require "either content or tool_calls, but not none".
            # Other providers also don't accept empty assistant messages.
            # This handles aborted assistant responses that got no content.
            content = assistantMsg["content"]
            hasContent = content is not None and len(content) > 0
            if not hasContent and "tool_calls" not in assistantMsg:
                continue
            params.append(assistantMsg)
        elif msg.role == "toolResult":
            imageBlocks: list[dict[str, Any]] = []
            j = i

            while j < len(transformedMessages) and transformedMessages[j].role == "toolResult":
                toolMsg: ToolResultMessage = transformedMessages[j]

                # Extract text and image content
                textResult = "\n".join(block.text for block in toolMsg.content if _isTextContentBlock(block))
                hasImages = any(c.type == "image" for c in toolMsg.content)

                # Always send tool result with text (or placeholder if only images)
                hasText = len(textResult) > 0
                toolResultText = textResult if hasText else "(see attached image)" if hasImages else "(no tool output)"
                # Some providers require the 'name' field in tool results
                toolResultMsg: dict[str, Any] = {
                    "role": "tool",
                    "content": sanitizeSurrogates(toolResultText),
                    "tool_call_id": toolMsg.toolCallId,
                }
                if compat["requiresToolResultName"] and toolMsg.toolName:
                    toolResultMsg["name"] = toolMsg.toolName
                params.append(toolResultMsg)

                if hasImages and "image" in model.input:
                    for block in toolMsg.content:
                        if _isImageContentBlock(block):
                            imageBlocks.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{block.mimeType};base64,{block.data}",
                                },
                            })
                j += 1

            i = j - 1

            if len(imageBlocks) > 0:
                if compat["requiresAssistantAfterToolResult"]:
                    params.append({
                        "role": "assistant",
                        "content": "I have processed the tool results.",
                    })

                params.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Attached image(s) from tool result:",
                        },
                        *imageBlocks,
                    ],
                })
                lastRole = "user"
            else:
                lastRole = "toolResult"

            continue

        lastRole = msg.role

    return params


def _convertTools(
    tools: Sequence[Tool],
    compat: Mapping[str, Any],
) -> list[dict[str, Any]]:
    def convert(tool: Tool) -> dict[str, Any]:
        grammar = resolveGrammarConstrainedSampling(tool, compat["supportsOpenAIGrammarTools"])
        if grammar:
            return {
                "type": "custom",
                "custom": {
                    "name": tool.name,
                    "description": tool.description,
                    "format": {
                        "type": "grammar",
                        "grammar": {
                            "syntax": grammar.format,
                            "definition": grammar.definition,
                        },
                    },
                },
            }

        strict = resolveJsonSchemaStrictSampling(tool, compat["supportsStrictMode"] is not False)
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": getJsonSchemaToolParameters(tool, strict),
                # Only include strict if provider supports it. Some reject unknown fields.
                **({"strict": nullishCoalesce(strict, False)} if compat["supportsStrictMode"] is not False else {}),
            },
        }

    return [convert(tool) for tool in tools]


def _parseChunkUsage(rawUsage: Mapping[str, Any], model: Model) -> Usage:
    promptTokens = rawUsage.get("prompt_tokens") or 0
    cacheReadTokens = nullishCoalesce(
        _field(rawUsage.get("prompt_tokens_details"), "cached_tokens"),
        nullishCoalesce(rawUsage.get("prompt_cache_hit_tokens"), nullishCoalesce(rawUsage.get("cached_tokens"), 0)),
    )
    cacheWriteTokens = _field(rawUsage.get("prompt_tokens_details"), "cache_write_tokens") or 0

    # Follow documented OpenAI/OpenRouter semantics: cached_tokens is cache-read
    # tokens (hits). Providers disagree on placement: OpenAI/OpenRouter use
    # prompt_tokens_details.cached_tokens, DeepSeek uses prompt_cache_hit_tokens,
    # and Kimi documents top-level usage.cached_tokens on the final usage chunk.
    # OpenAI does not document or emit cache_write_tokens, but
    # OpenRouter-compatible providers can include it as a separate write count.
    # OpenRouter's own provider/tests affirm the separate mapping:
    # https://github.com/OpenRouterTeam/ai-sdk-provider/pull/409
    # Do not subtract writes from cached_tokens, otherwise spec-compliant
    # providers are under-reported. DS4 mirrors this contract too:
    # https://github.com/antirez/ds4/pull/29
    input = max(0, promptTokens - cacheReadTokens - cacheWriteTokens)
    # OpenAI completion_tokens already includes reasoning_tokens.
    outputTokens = rawUsage.get("completion_tokens") or 0
    usage = Usage(
        input=input,
        output=outputTokens,
        cacheRead=cacheReadTokens,
        cacheWrite=cacheWriteTokens,
        reasoning=_field(rawUsage.get("completion_tokens_details"), "reasoning_tokens") or 0,
        totalTokens=input + outputTokens + cacheReadTokens + cacheWriteTokens,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )
    calculateCost(model, usage)
    return usage


def _mapStopReason(reason: str | None) -> dict[str, str]:
    if reason is None:
        return {"stopReason": "stop"}
    match reason:
        case "stop" | "end":
            return {"stopReason": "stop"}
        case "length":
            return {"stopReason": "length"}
        case "function_call" | "tool_calls":
            return {"stopReason": "toolUse"}
        case "content_filter":
            return {"stopReason": "error", "errorMessage": "Provider finish_reason: content_filter"}
        case "network_error":
            return {"stopReason": "error", "errorMessage": "Provider finish_reason: network_error"}
        case _:
            return {
                "stopReason": "error",
                "errorMessage": f"Provider finish_reason: {reason}",
            }


def _detectCompat(model: Model) -> dict[str, Any]:
    """Auto-detect compatibility settings from provider name and baseUrl.
    Used as the base when model.compat is not set; explicit model.compat
    entries override these detected values.
    """
    provider = model.provider
    baseUrl = model.baseUrl

    isZai = provider == "zai" or provider == "zai-coding-cn" or "api.z.ai" in baseUrl or "open.bigmodel.cn" in baseUrl
    isTogether = provider == "together" or "api.together.ai" in baseUrl or "api.together.xyz" in baseUrl
    isMoonshot = provider == "moonshotai" or provider == "moonshotai-cn" or "api.moonshot." in baseUrl
    isOpenRouter = provider == "openrouter" or "openrouter.ai" in baseUrl
    isCloudflareWorkersAI = provider == "cloudflare-workers-ai" or "api.cloudflare.com" in baseUrl
    isCloudflareAiGateway = provider == "cloudflare-ai-gateway" or "gateway.ai.cloudflare.com" in baseUrl
    isNvidia = provider == "nvidia" or "integrate.api.nvidia.com" in baseUrl
    isAntLing = provider == "ant-ling" or "api.ant-ling.com" in baseUrl
    isCerebras = provider == "cerebras" or "cerebras.ai" in baseUrl
    isDeepSeek = provider == "deepseek" or "deepseek.com" in baseUrl.lower()

    isNonStandard = (
        isNvidia
        or isCerebras
        or provider == "xai"
        or "api.x.ai" in baseUrl
        or isTogether
        or "chutes.ai" in baseUrl
        or isDeepSeek
        or isZai
        or isMoonshot
        or provider == "opencode"
        or "opencode.ai" in baseUrl
        or isCloudflareWorkersAI
        or isCloudflareAiGateway
        or isAntLing
    )

    useMaxTokens = (
        "chutes.ai" in baseUrl
        or isDeepSeek
        or isMoonshot
        or isCloudflareAiGateway
        or isTogether
        or isNvidia
        or isAntLing
        or isZai
    )

    isGrok = provider == "xai" or "api.x.ai" in baseUrl
    isOpenRouterDeveloperRoleModel = isOpenRouter and (model.id.startswith("anthropic/") or model.id.startswith("openai/"))
    cacheControlFormat = "anthropic" if provider == "openrouter" and model.id.startswith("anthropic/") else None

    return {
        "supportsStore": not isNonStandard,
        "supportsDeveloperRole": isOpenRouterDeveloperRoleModel or (not isNonStandard and not isOpenRouter),
        "supportsReasoningEffort": (
            not isGrok and not isZai and not isMoonshot and not isTogether and not isCloudflareAiGateway and not isNvidia and not isAntLing
        ),
        "supportsUsageInStreaming": True,
        "supportsFinishReason": True,
        "maxTokensField": "max_tokens" if useMaxTokens else "max_completion_tokens",
        "requiresToolResultName": False,
        "requiresAssistantAfterToolResult": False,
        "requiresThinkingAsText": False,
        "requiresReasoningContentOnAssistantMessages": isDeepSeek,
        "thinkingFormat": (
            "deepseek"
            if isDeepSeek
            else "zai"
            if isZai
            else "together"
            if isTogether
            else "ant-ling"
            if isAntLing
            else "openrouter"
            if isOpenRouter
            else "openai"
        ),
        "openRouterRouting": {},
        "vercelGatewayRouting": {},
        "chatTemplateKwargs": {},
        "chatTemplateArgs": {},
        "zaiToolStream": False,
        "supportsThinkingTokenBudget": False,
        "thinkingTokenBudgetField": None,
        # OpenAI compatibility alone does not imply strict JSON-schema tool support.
        "supportsStrictMode": False,
        "supportsOpenAIGrammarTools": False,
        "supportsMidConvoSystemMessages": False,
        "supportsMidConvoToolAdditions": False,
        "cacheControlFormat": cacheControlFormat,
        "sendSessionAffinityHeaders": isOpenRouter,
        "sessionAffinityFormat": "openrouter" if isOpenRouter else "openai",
        "supportsLongCacheRetention": not (
            isTogether or isCloudflareWorkersAI or isCloudflareAiGateway or isNvidia or isAntLing
        ),
    }


def _getCompat(model: Model) -> dict[str, Any]:
    """Get resolved compatibility settings for a model.
    Auto-detects from provider/URL then overrides with explicit model.compat.
    """
    detected = _detectCompat(model)
    if not model.compat:
        return detected

    def compat(name: str) -> Any:
        return _compat_value(model, name)

    return {
        "supportsStore": nullishCoalesce(compat("supportsStore"), detected["supportsStore"]),
        "supportsDeveloperRole": nullishCoalesce(compat("supportsDeveloperRole"), detected["supportsDeveloperRole"]),
        "supportsReasoningEffort": nullishCoalesce(compat("supportsReasoningEffort"), detected["supportsReasoningEffort"]),
        "supportsUsageInStreaming": nullishCoalesce(compat("supportsUsageInStreaming"), detected["supportsUsageInStreaming"]),
        "supportsFinishReason": nullishCoalesce(compat("supportsFinishReason"), detected["supportsFinishReason"]),
        "maxTokensField": nullishCoalesce(compat("maxTokensField"), detected["maxTokensField"]),
        "requiresToolResultName": nullishCoalesce(compat("requiresToolResultName"), detected["requiresToolResultName"]),
        "requiresAssistantAfterToolResult": nullishCoalesce(
            compat("requiresAssistantAfterToolResult"), detected["requiresAssistantAfterToolResult"]
        ),
        "requiresThinkingAsText": nullishCoalesce(compat("requiresThinkingAsText"), detected["requiresThinkingAsText"]),
        "requiresReasoningContentOnAssistantMessages": nullishCoalesce(
            compat("requiresReasoningContentOnAssistantMessages"),
            detected["requiresReasoningContentOnAssistantMessages"],
        ),
        "thinkingFormat": nullishCoalesce(compat("thinkingFormat"), detected["thinkingFormat"]),
        "openRouterRouting": nullishCoalesce(compat("openRouterRouting"), {}),
        "vercelGatewayRouting": nullishCoalesce(compat("vercelGatewayRouting"), detected["vercelGatewayRouting"]),
        "chatTemplateKwargs": nullishCoalesce(compat("chatTemplateKwargs"), detected["chatTemplateKwargs"]),
        "chatTemplateArgs": nullishCoalesce(compat("chatTemplateArgs"), detected["chatTemplateArgs"]),
        "zaiToolStream": nullishCoalesce(compat("zaiToolStream"), detected["zaiToolStream"]),
        "supportsThinkingTokenBudget": nullishCoalesce(
            compat("supportsThinkingTokenBudget"), detected["supportsThinkingTokenBudget"]
        ),
        "thinkingTokenBudgetField": nullishCoalesce(compat("thinkingTokenBudgetField"), detected["thinkingTokenBudgetField"]),
        "supportsStrictMode": nullishCoalesce(compat("supportsStrictMode"), detected["supportsStrictMode"]),
        "supportsOpenAIGrammarTools": nullishCoalesce(compat("supportsOpenAIGrammarTools"), detected["supportsOpenAIGrammarTools"]),
        "supportsMidConvoSystemMessages": nullishCoalesce(
            compat("supportsMidConvoSystemMessages"), detected["supportsMidConvoSystemMessages"]
        ),
        "supportsMidConvoToolAdditions": nullishCoalesce(
            compat("supportsMidConvoToolAdditions"), detected["supportsMidConvoToolAdditions"]
        ),
        "cacheControlFormat": nullishCoalesce(compat("cacheControlFormat"), detected["cacheControlFormat"]),
        "sendSessionAffinityHeaders": nullishCoalesce(compat("sendSessionAffinityHeaders"), detected["sendSessionAffinityHeaders"]),
        "sessionAffinityFormat": nullishCoalesce(compat("sessionAffinityFormat"), detected["sessionAffinityFormat"]),
        "supportsLongCacheRetention": nullishCoalesce(compat("supportsLongCacheRetention"), detected["supportsLongCacheRetention"]),
        "vllmPriority": compat("vllmPriority"),
    }


__all__ = ["ConvertCompletionsMessagesOptions", "OpenAICompletionsOptions", "convertMessages", "stream", "streamSimple"]
