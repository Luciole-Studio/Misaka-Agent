"""Translated from pi's ``packages/ai/src/api/google-generative-ai.ts`` (v1.1.0).

MISAKA fork, kept to what the Python SDK forces (each is marked where it happens; the SDK glue is ``_google_sdk``):

- The SDK sends the request when the stream is first read and takes no abort signal: the stream is opened inside the
  retried call, the wait for it races the signal, and so does each read.
- Response objects are read through ``coalesce_attr``; a thought signature arrives as bytes and a finish reason as
  an enum.
- The client is closed once the stream has ended, and an error event carries its live exception (``cause=``).
- ``fetch`` is not ported, so there is no custom-fetch check; the blocks carry no ``index`` to strip on error.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

try:
    from google.genai import Client as GoogleGenAI
except ImportError:  # optional extra: misaka[google]
    GoogleGenAI = None

from misaka.ai.api._common import _await_with_signal, _option
from misaka.ai.api._google_sdk import (
    close_client,
    coalesce_attr,
    coerce_thought_signature,
    format_google_error,
    iterate_google_stream,
    open_google_stream,
    raw_finish_reason,
)
from misaka.ai.api._sdk import require
from misaka.ai.api.google_shared import (
    GoogleApiThinkingLevel,
    ResolvedGoogleThinkingLevel,
    convertMessages,
    convertTools,
    getDisabledGoogleThinkingConfig,
    isThinkingPart,
    mapStopReason,
    resolveGoogleFunctionCallingMode,
    resolveGoogleThinkingLevel,
    retainThoughtSignature,
    retryGoogleRequest,
    supportsGoogleStrictToolSampling,
    toGoogleSdkThinkingLevel,
    toGoogleThinkingLevel,
    usesGoogleThinkingLevel,
)
from misaka.ai.api.simple_options import buildBaseOptions
from misaka.ai.models import calculateCost, clampThinkingLevel
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    DoneEvent,
    ErrorEvent,
    Model,
    ProviderEnv,
    ProviderHeaders,
    SimpleStreamOptions,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import providerHeadersToRecord
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.ai.utils.text import getSystemMessageText
from misaka.ai.utils.transcript import (
    collapseSystemMessages,
    getCurrentTools,
    getInitialSystemMessage,
)
from misaka.utils.js import UNDEFINED, jsonStringify, jsString, nullishCoalesce
from misaka.utils.values import maybe_await, signal_aborted


class _GoogleThinkingOptions(TypedDict, total=False):
    enabled: bool
    budgetTokens: int  # -1 for dynamic, 0 to disable
    level: GoogleApiThinkingLevel


class GoogleOptions(TypedDict, total=False):
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
    toolChoice: Literal["auto", "none", "any"]
    thinking: _GoogleThinkingOptions


# Counter for generating unique tool call IDs
_toolCallCounter = 0


def stream(
    model: Model,
    context: TranscriptContext,
    options: GoogleOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    normalizedContext = collapseSystemMessages(context)

    async def run() -> None:
        global _toolCallCounter
        output = AssistantMessage(
            role="assistant",
            content=[],
            api="google-generative-ai",
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
        try:
            apiKey = _option(options, "apiKey")
            if not apiKey:
                raise RuntimeError(f"No API key for provider: {model.provider}")
            client = _createClient(model, apiKey, _option(options, "headers"))
            params = _buildParams(model, normalizedContext, options)
            onPayload = _option(options, "onPayload")
            nextParams = await maybe_await(onPayload(params, model)) if onPayload else None
            if nextParams is not None:
                params = nextParams
            # MISAKA fork: the SDK sends the request on the first read and takes no abort signal, so the stream is
            # opened inside the retried call and the wait for it races the signal.
            googleStream = await _await_with_signal(
                retryGoogleRequest(lambda: open_google_stream(client, params), options), _option(options, "signal")
            )

            stream.push(StartEvent(partial=output))
            currentBlock: TextContent | ThinkingContent | None = None
            blocks = output.content

            def blockIndex() -> int:
                return len(blocks) - 1

            onProviderStreamEvent = _option(options, "onProviderStreamEvent")
            # MISAKA fork: `iterate_google_stream` is the `for await`, minding the signal; the SDK's models are read
            # through `coalesce_attr`.
            async with contextlib.aclosing(iterate_google_stream(googleStream, _option(options, "signal"))) as chunks:
                async for chunk in chunks:
                    if onProviderStreamEvent:
                        await maybe_await(onProviderStreamEvent(chunk, model))
                    # @google/genai documents GenerateContentResponse.responseId as an output-only field
                    # used to identify each response. Keep the first non-empty one from the stream.
                    output.responseId = output.responseId or coalesce_attr(chunk, "response_id", "responseId")
                    candidates = coalesce_attr(chunk, "candidates")
                    candidate = candidates[0] if candidates else None
                    parts = coalesce_attr(coalesce_attr(candidate, "content"), "parts")
                    if parts:
                        for part in parts:
                            text = coalesce_attr(part, "text")
                            # MISAKA fork: the SDK types the signature as bytes.
                            thoughtSignature = coerce_thought_signature(
                                coalesce_attr(part, "thought_signature", "thoughtSignature")
                            )
                            if text is not None:
                                isThinking = isThinkingPart(part)
                                if (
                                    not currentBlock
                                    or (isThinking and currentBlock.type != "thinking")
                                    or (not isThinking and currentBlock.type != "text")
                                ):
                                    if currentBlock:
                                        if currentBlock.type == "text":
                                            stream.push(
                                                TextEndEvent(
                                                    contentIndex=len(blocks) - 1,
                                                    content=currentBlock.text,
                                                    partial=output,
                                                )
                                            )
                                        else:
                                            stream.push(
                                                ThinkingEndEvent(
                                                    contentIndex=blockIndex(),
                                                    content=currentBlock.thinking,
                                                    partial=output,
                                                )
                                            )
                                    if isThinking:
                                        currentBlock = ThinkingContent(thinking="", thinkingSignature=None)
                                        output.content.append(currentBlock)
                                        stream.push(ThinkingStartEvent(contentIndex=blockIndex(), partial=output))
                                    else:
                                        currentBlock = TextContent(text="")
                                        output.content.append(currentBlock)
                                        stream.push(TextStartEvent(contentIndex=blockIndex(), partial=output))
                                if currentBlock.type == "thinking":
                                    currentBlock.thinking += text
                                    currentBlock.thinkingSignature = retainThoughtSignature(
                                        currentBlock.thinkingSignature,
                                        thoughtSignature,
                                    )
                                    stream.push(
                                        ThinkingDeltaEvent(
                                            contentIndex=blockIndex(),
                                            delta=text,
                                            partial=output,
                                        )
                                    )
                                else:
                                    currentBlock.text += text
                                    currentBlock.textSignature = retainThoughtSignature(
                                        currentBlock.textSignature,
                                        thoughtSignature,
                                    )
                                    stream.push(
                                        TextDeltaEvent(
                                            contentIndex=blockIndex(),
                                            delta=text,
                                            partial=output,
                                        )
                                    )

                            functionCall = coalesce_attr(part, "function_call", "functionCall")
                            if functionCall is not None:
                                if currentBlock:
                                    if currentBlock.type == "text":
                                        stream.push(
                                            TextEndEvent(
                                                contentIndex=blockIndex(),
                                                content=currentBlock.text,
                                                partial=output,
                                            )
                                        )
                                    else:
                                        stream.push(
                                            ThinkingEndEvent(
                                                contentIndex=blockIndex(),
                                                content=currentBlock.thinking,
                                                partial=output,
                                            )
                                        )
                                    currentBlock = None

                                # Generate unique ID if not provided or if it's a duplicate
                                providedId = coalesce_attr(functionCall, "id")
                                needsNewId = not providedId or any(
                                    b.type == "toolCall" and b.id == providedId for b in output.content
                                )
                                if needsNewId:
                                    _toolCallCounter += 1
                                toolCallId = (
                                    f"{jsString(nullishCoalesce(coalesce_attr(functionCall, 'name'), UNDEFINED))}_{time.time_ns() // 1_000_000}_{_toolCallCounter}"
                                    if needsNewId
                                    else providedId
                                )

                                toolCall = ToolCall(
                                    type="toolCall",
                                    id=toolCallId,
                                    name=coalesce_attr(functionCall, "name") or "",
                                    arguments=nullishCoalesce(coalesce_attr(functionCall, "args"), {}),
                                    **({"thoughtSignature": thoughtSignature} if thoughtSignature else {}),
                                )

                                output.content.append(toolCall)
                                stream.push(ToolCallStartEvent(contentIndex=blockIndex(), partial=output))
                                stream.push(
                                    ToolCallDeltaEvent(
                                        contentIndex=blockIndex(),
                                        delta=jsonStringify(toolCall.arguments),
                                        partial=output,
                                    )
                                )
                                stream.push(
                                    ToolCallEndEvent(contentIndex=blockIndex(), toolCall=toolCall, partial=output)
                                )

                    finishReason = coalesce_attr(candidate, "finish_reason", "finishReason")
                    if finishReason:
                        # MISAKA fork: the SDK hands back its FinishReason enum; pi records the wire string.
                        output.rawStopReason = raw_finish_reason(finishReason)
                        output.stopReason = mapStopReason(finishReason)
                        if any(b.type == "toolCall" for b in output.content) and output.stopReason == "stop":
                            output.stopReason = "toolUse"

                    usageMetadata = coalesce_attr(chunk, "usage_metadata", "usageMetadata")
                    if usageMetadata is not None:
                        output.usage = Usage(
                            input=(coalesce_attr(usageMetadata, "prompt_token_count", "promptTokenCount") or 0)
                            - (coalesce_attr(usageMetadata, "cached_content_token_count", "cachedContentTokenCount") or 0),
                            output=(coalesce_attr(usageMetadata, "candidates_token_count", "candidatesTokenCount") or 0)
                            + (coalesce_attr(usageMetadata, "thoughts_token_count", "thoughtsTokenCount") or 0),
                            cacheRead=coalesce_attr(usageMetadata, "cached_content_token_count", "cachedContentTokenCount")
                            or 0,
                            cacheWrite=0,
                            reasoning=coalesce_attr(usageMetadata, "thoughts_token_count", "thoughtsTokenCount") or 0,
                            totalTokens=coalesce_attr(usageMetadata, "total_token_count", "totalTokenCount") or 0,
                            cost=UsageCost(
                                input=0,
                                output=0,
                                cacheRead=0,
                                cacheWrite=0,
                                total=0,
                            ),
                        )
                        calculateCost(model, output.usage)

            if currentBlock:
                if currentBlock.type == "text":
                    stream.push(
                        TextEndEvent(
                            contentIndex=blockIndex(),
                            content=currentBlock.text,
                            partial=output,
                        )
                    )
                else:
                    stream.push(
                        ThinkingEndEvent(
                            contentIndex=blockIndex(),
                            content=currentBlock.thinking,
                            partial=output,
                        )
                    )

            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")

            if output.stopReason == "pending":
                raise RuntimeError("Google stream ended without a finish reason")
            if output.stopReason == "aborted" or output.stopReason == "error":
                errorMessage = (
                    f"Provider stopped with: {output.rawStopReason}" if output.rawStopReason else "An unknown error occurred"
                )
                raise RuntimeError(errorMessage)

            stream.push(DoneEvent(reason=output.stopReason, message=output))
            stream.end()
        except Exception as error:  # noqa: BLE001 - every failure becomes the stream's error event
            # MISAKA fork: the blocks carry no `index`, so there is nothing to strip here.
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            output.errorMessage = format_google_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
            stream.end()
        finally:
            # MISAKA fork: the SDK client owns an httpx client; close it while this loop still runs.
            await close_client(client)

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
    if not _option(options, "reasoning"):
        return stream(model, context, {**base, "thinking": {"enabled": False}})

    clampedReasoning = clampThinkingLevel(model, _option(options, "reasoning"))
    if clampedReasoning == "off":
        return stream(model, context, {**base, "thinking": {"enabled": False}})
    resolvedLevel = resolveGoogleThinkingLevel(model, clampedReasoning)

    if usesGoogleThinkingLevel(model):
        return stream(
            model,
            context,
            {
                **base,
                "thinking": {
                    "enabled": True,
                    "level": toGoogleThinkingLevel(resolvedLevel),
                },
            },
        )

    return stream(
        model,
        context,
        {
            **base,
            "thinking": {
                "enabled": True,
                "budgetTokens": _getGoogleBudget(model, resolvedLevel, _option(options, "thinkingBudgets")),
            },
        },
    )


def _createClient(
    model: Model,
    apiKey: str | None = None,
    optionsHeaders: ProviderHeaders | None = None,
) -> Any:
    httpOptions: dict[str, Any] = {}
    if model.baseUrl:
        httpOptions["baseUrl"] = model.baseUrl
        httpOptions["apiVersion"] = ""  # baseUrl already includes version path, don't append
    headers = providerHeadersToRecord({"User-Agent": getPiUserAgent(), **(model.headers or {}), **(optionsHeaders or {})})
    if headers:
        httpOptions["headers"] = headers

    return require(GoogleGenAI, "google-genai")(
        api_key=apiKey,
        http_options=httpOptions if len(httpOptions) > 0 else None,
    )


def _buildParams(
    model: Model,
    context: TranscriptContext,
    options: GoogleOptions | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contents = convertMessages(model, context)
    initialSystemMessage = getInitialSystemMessage(context.messages)
    currentTools = getCurrentTools(context.messages)

    generationConfig: dict[str, Any] = {}
    if _option(options, "temperature") is not None:
        generationConfig["temperature"] = _option(options, "temperature")
    if _option(options, "maxTokens") is not None:
        generationConfig["maxOutputTokens"] = _option(options, "maxTokens")

    supportsStrictMode = supportsGoogleStrictToolSampling(model.id)
    functionCallingMode = (
        resolveGoogleFunctionCallingMode(currentTools, _option(options, "toolChoice"), supportsStrictMode)
        if len(currentTools) > 0
        else None
    )
    systemInstruction = getSystemMessageText(initialSystemMessage) if initialSystemMessage else ""
    config: dict[str, Any] = {
        **generationConfig,
        **({"systemInstruction": sanitizeSurrogates(systemInstruction)} if systemInstruction else {}),
        **(
            {
                "tools": convertTools(currentTools, False, supportsStrictMode),
            }
            if len(currentTools) > 0
            else {}
        ),
        **(
            {
                "toolConfig": {"functionCallingConfig": {"mode": functionCallingMode}},
            }
            if functionCallingMode is not None
            else {}
        ),
    }

    thinking = _option(options, "thinking")
    if _option(thinking, "enabled") and model.reasoning:
        thinkingConfig: dict[str, Any] = {"includeThoughts": True}
        if _option(thinking, "level") is not None:
            thinkingConfig["thinkingLevel"] = toGoogleSdkThinkingLevel(_option(thinking, "level"))
        elif _option(thinking, "budgetTokens") is not None:
            thinkingConfig["thinkingBudget"] = _option(thinking, "budgetTokens")
        config["thinkingConfig"] = thinkingConfig
    elif model.reasoning and thinking is not None and not _option(thinking, "enabled"):
        config["thinkingConfig"] = getDisabledGoogleThinkingConfig(model)

    if _option(options, "signal") is not None:
        if signal_aborted(_option(options, "signal")):
            raise RuntimeError("Request aborted")
        config["abortSignal"] = _option(options, "signal")

    params: dict[str, Any] = {
        "model": model.id,
        "contents": contents,
        "config": config,
    }

    return params


def _getGoogleBudget(
    model: Model,
    level: ResolvedGoogleThinkingLevel,
    customBudgets: ThinkingBudgets | Mapping[str, int | None] | None = None,
) -> int:
    if _option(customBudgets, level) is not None:
        return _option(customBudgets, level)

    if "2.5-pro" in model.id:
        budgets: dict[ResolvedGoogleThinkingLevel, int] = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 32768,
        }
        return budgets[level]

    if "2.5-flash-lite" in model.id:
        budgets = {
            "minimal": 512,
            "low": 2048,
            "medium": 8192,
            "high": 24576,
        }
        return budgets[level]

    if "2.5-flash" in model.id:
        budgets = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 24576,
        }
        return budgets[level]

    return -1


__all__ = ["GoogleOptions", "stream", "streamSimple"]
