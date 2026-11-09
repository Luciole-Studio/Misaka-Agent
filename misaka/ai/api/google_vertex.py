"""Translated from pi's ``packages/ai/src/api/google-vertex.ts`` (v1.1.0).

MISAKA fork, kept to what the Python SDK forces (each is marked where it happens; the SDK glue is ``_google_sdk``):

- The SDK sends the request when the stream is first read and takes no abort signal: the stream is opened inside the
  retried call, the wait for it races the signal, and so does each read.
- Response objects are read through ``coalesce_attr``; a thought signature arrives as bytes and a finish reason as
  an enum.
- The client is closed once the stream has ended, and an error event carries its live exception (``cause=``).
- The client takes loaded credentials and reads its API version from the HTTP options; a custom base URL without a
  version gets one, which @google/genai would have appended.
- ``fetch`` is not ported, so there is no custom-fetch check; the blocks carry no ``index`` to strip on error.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Mapping
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

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
    load_google_credentials,
    open_google_stream,
    raw_finish_reason,
    with_api_version,
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
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.ai.utils.text import getSystemMessageText
from misaka.ai.utils.transcript import (
    collapseSystemMessages,
    getCurrentTools,
    getInitialSystemMessage,
)
from misaka.utils.js import UNDEFINED, jsonStringify, jsString, jsTrim, nullishCoalesce
from misaka.utils.values import maybe_await, signal_aborted


class _GoogleVertexThinkingOptions(TypedDict, total=False):
    enabled: bool
    budgetTokens: int  # -1 for dynamic, 0 to disable
    level: GoogleApiThinkingLevel


class GoogleVertexOptions(TypedDict, total=False):
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
    thinking: _GoogleVertexThinkingOptions
    project: str
    location: str


_API_VERSION = "v1"
_GCP_VERTEX_CREDENTIALS_MARKER = "gcp-vertex-credentials"

# Counter for generating unique tool call IDs
_toolCallCounter = 0


def stream(
    model: Model,
    context: TranscriptContext,
    options: GoogleVertexOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    normalizedContext = collapseSystemMessages(context)

    async def run() -> None:
        global _toolCallCounter
        output = AssistantMessage(
            role="assistant",
            content=[],
            api="google-vertex",
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
            apiKey = _resolveApiKey(options)
            # Create the client using either a Vertex API key, if provided, or ADC with project and location
            client = (
                _createClientWithApiKey(model, apiKey, _option(options, "headers"))
                if apiKey
                else _createClient(
                    model,
                    _resolveProject(options),
                    _resolveLocation(options),
                    _option(options, "headers"),
                    _option(options, "env"),
                )
            )
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
                    # Vertex uses the same @google/genai GenerateContentResponse type as Gemini.
                    # responseId is documented there as an output-only identifier for each response.
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
                raise RuntimeError("Google Vertex stream ended without a finish reason")
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
    base = {
        **dict(buildBaseOptions(model, context, options, None)),
        "toolChoice": _option(options, "toolChoice"),
    }
    if not _option(options, "reasoning"):
        return stream(
            model,
            context,
            {
                **base,
                "thinking": {"enabled": False},
            },
        )

    clampedReasoning = clampThinkingLevel(model, _option(options, "reasoning"))
    if clampedReasoning == "off":
        return stream(
            model,
            context,
            {
                **base,
                "thinking": {"enabled": False},
            },
        )
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
    project: str,
    location: str,
    optionsHeaders: ProviderHeaders | None = None,
    env: ProviderEnv | None = None,
) -> Any:
    googleAuthOptions = _buildGoogleAuthOptions(env)
    # MISAKA fork: the SDK takes loaded credentials and reads the API version from the HTTP options alone.
    return require(GoogleGenAI, "google-genai")(
        vertexai=True,
        project=project,
        location=location,
        **({"credentials": load_google_credentials(googleAuthOptions)} if googleAuthOptions else {}),
        http_options=with_api_version(_buildHttpOptions(model, optionsHeaders), _API_VERSION),
    )


def _createClientWithApiKey(
    model: Model,
    apiKey: str,
    optionsHeaders: ProviderHeaders | None = None,
) -> Any:
    # MISAKA fork: the SDK reads the API version from the HTTP options alone.
    return require(GoogleGenAI, "google-genai")(
        vertexai=True,
        api_key=apiKey,
        http_options=with_api_version(_buildHttpOptions(model, optionsHeaders), _API_VERSION),
    )


def _buildHttpOptions(model: Model, optionsHeaders: ProviderHeaders | None = None) -> dict[str, Any] | None:
    httpOptions: dict[str, Any] = {}
    baseUrl = _resolveCustomBaseUrl(model.baseUrl)
    if baseUrl:
        httpOptions["baseUrl"] = baseUrl
        # MISAKA fork: the ResourceScope member's wire value; the SDK is an optional extra.
        httpOptions["baseUrlResourceScope"] = "COLLECTION"
        if _baseUrlIncludesApiVersion(baseUrl):
            httpOptions["apiVersion"] = ""
        else:
            # MISAKA fork: @google/genai still appends the API version to a COLLECTION-scoped base URL; the Python
            # SDK drops it, so a base URL without one gets it here.
            httpOptions["baseUrl"] = f"{baseUrl.rstrip('/')}/{_API_VERSION}"
            httpOptions["apiVersion"] = ""

    headers = providerHeadersToRecord({"User-Agent": getPiUserAgent(), **(model.headers or {}), **(optionsHeaders or {})})
    if headers:
        httpOptions["headers"] = headers

    return httpOptions if len(httpOptions) > 0 else None


def _resolveCustomBaseUrl(baseUrl: str) -> str | None:
    trimmed = jsTrim(baseUrl)
    if not trimmed or "{location}" in trimmed:
        return None
    return trimmed


def _baseUrlIncludesApiVersion(baseUrl: str) -> bool:
    url = urlsplit(baseUrl)
    # MISAKA fork: `urlsplit` does not throw where `new URL()` does; a string without a scheme and host is what
    # `new URL()` rejects, so it takes the fallback.
    if url.scheme and url.netloc:
        return any(re.fullmatch(r"v\d+(?:beta\d*)?", part) for part in url.path.split("/"))
    return re.search(r"(?:^|/)v\d+(?:beta\d*)?(?:/|$)", baseUrl) is not None


def _buildGoogleAuthOptions(env: ProviderEnv | None = None) -> dict[str, str] | None:
    keyFilename = getProviderEnvValue("GOOGLE_APPLICATION_CREDENTIALS", env)
    return {"keyFilename": keyFilename} if keyFilename else None


def _resolveApiKey(options: GoogleVertexOptions | Mapping[str, Any] | None = None) -> str | None:
    apiKey = _option(options, "apiKey")
    apiKey = jsTrim(apiKey) if apiKey is not None else None
    if not apiKey or apiKey == _GCP_VERTEX_CREDENTIALS_MARKER or _isPlaceholderApiKey(apiKey):
        return None
    return apiKey


def _isPlaceholderApiKey(apiKey: str) -> bool:
    return re.fullmatch(r"<[^>]+>", apiKey) is not None


def _resolveProject(options: GoogleVertexOptions | Mapping[str, Any] | None = None) -> str:
    project = (
        _option(options, "project")
        or getProviderEnvValue("GOOGLE_CLOUD_PROJECT", _option(options, "env"))
        or getProviderEnvValue("GCLOUD_PROJECT", _option(options, "env"))
    )
    if not project:
        raise RuntimeError(
            "Vertex AI requires a project ID. Set GOOGLE_CLOUD_PROJECT/GCLOUD_PROJECT or pass project in options."
        )
    return project


def _resolveLocation(options: GoogleVertexOptions | Mapping[str, Any] | None = None) -> str:
    location = _option(options, "location") or getProviderEnvValue("GOOGLE_CLOUD_LOCATION", _option(options, "env"))
    if not location:
        raise RuntimeError("Vertex AI requires a location. Set GOOGLE_CLOUD_LOCATION or pass location in options.")
    return location


def _buildParams(
    model: Model,
    context: TranscriptContext,
    options: GoogleVertexOptions | Mapping[str, Any] | None = None,
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

    if "2.5-flash" in model.id:
        budgets = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 24576,
        }
        return budgets[level]

    return -1


__all__ = ["GoogleVertexOptions", "stream", "streamSimple"]
