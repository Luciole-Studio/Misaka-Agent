"""Translated from pi's ``packages/ai/src/api/azure-openai-responses.ts`` (v1.1.0).

MISAKA fork, kept to what the Python SDK and platform force (each is marked where it happens):

- The SDK glue lives in ``misaka.ai.api._openai_sdk`` (``omit`` for suppressed headers, ``extra_body`` for undeclared
  fields, the request and every stream read raced against the signal).
- There is no ``fetch`` option: requests go through the SDK's own transport.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from typing import Any, Literal

from misaka.ai.api import _openai_sdk
from misaka.ai.api._common import _compat_value, _option
from misaka.ai.api.azure_openai_config import (
    AzureEndpointOptions,
    resolveAzureConfig,
    resolveDeploymentName,
)
from misaka.ai.api.constrained_sampling import createGrammarToolInputProperties
from misaka.ai.api.openai_prompt_cache import clampOpenAIPromptCacheKey
from misaka.ai.api.openai_responses_shared import (
    convertResponsesMessages,
    convertResponsesTools,
    processResponsesStream,
)
from misaka.ai.api.simple_options import buildBaseOptions, resolveSamplingParams
from misaka.ai.models import clampThinkingLevel
from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils.error_body import formatProviderError, normalizeProviderError
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headersToRecord
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.ai.utils.transcript import (
    getDeclaredTools,
    resolveTranscript,
    resolveTranscriptTools,
)
from misaka.utils.js import UNDEFINED, nullishCoalesce
from misaka.utils.values import maybe_await, signal_aborted

_AZURE_TOOL_CALL_PROVIDERS = {"openai", "openai-codex", "opencode", "azure"}
# OpenAI Responses rejects max_output_tokens below 16: https://github.com/earendil-works/pi/issues/6265
_OPENAI_RESPONSES_MIN_OUTPUT_TOKENS = 16


def _formatAzureOpenAIError(error: Any) -> str:
    return formatProviderError(normalizeProviderError(error), "Azure OpenAI API error")


# Azure OpenAI Responses-specific options
class AzureOpenAIResponsesOptions(AzureEndpointOptions, total=False):
    reasoningEffort: Literal["minimal", "low", "medium", "high", "xhigh", "max"]
    toolChoice: Any
    reasoningSummary: Literal["auto", "detailed", "concise"] | None


def stream(
    model: Model,
    context: TranscriptContext,
    options: AzureOpenAIResponsesOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    """Generate function for Azure OpenAI Responses API"""
    stream = AssistantMessageEventStream()
    normalizedContext = resolveTranscript(context, _compat_value(model, "supportsMidConvoSystemMessages"))

    # Start async processing
    async def run() -> None:
        deploymentName = resolveDeploymentName(model, options)

        output = AssistantMessage(
            content=[],
            api="azure-openai-responses",
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

        try:
            # Create Azure OpenAI client
            apiKey = _option(options, "apiKey")
            if not apiKey:
                raise RuntimeError(f"No API key for provider: {model.provider}")
            client, requestHeaders = _createClient(model, apiKey, options)
            grammarToolInputProperties = createGrammarToolInputProperties(
                getDeclaredTools(normalizedContext.messages),
                nullishCoalesce(_compat_value(model, "supportsOpenAIGrammarTools"), False),
            )
            params = _buildParams(model, normalizedContext, options, deploymentName, grammarToolInputProperties)
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
                lambda: _openai_sdk._create_with_response(client, "responses", params, requestOptions, requestHeaders),
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

            # MISAKA fork: the event iterator is closed with the reply (GitHub issue #6).
            async with contextlib.aclosing(_openai_sdk._iterate_stream(openaiStream, _option(options, "signal"))) as events:
                await processResponsesStream(
                    events,
                    output,
                    stream,
                    model,
                    {
                        "onProviderStreamEvent": _option(options, "onProviderStreamEvent"),
                        "grammarToolInputProperties": grammarToolInputProperties,
                    },
                )

            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")

            if output.stopReason == "pending":
                raise RuntimeError("Azure OpenAI Responses stream ended without a stop reason")
            if output.stopReason in ("aborted", "error"):
                raise RuntimeError(output.errorMessage or "An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
            stream.end()
        except Exception as error:  # noqa: BLE001 - every failure becomes the stream's error event
            # MISAKA fork: streaming scratch buffers live beside the blocks
            # (`openai_responses_shared._StreamingToolCallScratch`), so there is nothing to strip here.
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            output.errorMessage = _formatAzureOpenAIError(error)
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


def _createClient(
    model: Model, apiKey: str, options: AzureOpenAIResponsesOptions | Mapping[str, Any] | None = None
) -> tuple[Any, dict[str, Any]]:
    """MISAKA fork: returns the client and the per-request headers that carry every suppression
    (``_openai_sdk._sdk_client``)."""
    headers: dict[str, Any] = {"User-Agent": getPiUserAgent(), **(model.headers or {})}

    if _option(options, "headers"):
        headers.update(_option(options, "headers"))

    config = resolveAzureConfig(model, options)

    return _openai_sdk._sdk_client(
        apiKey=apiKey,
        apiVersion=config["apiVersion"],
        defaultHeaders=headers,
        baseURL=config["baseUrl"],
    )


def _buildParams(
    model: Model,
    context: TranscriptContext,
    options: AzureOpenAIResponsesOptions | Mapping[str, Any] | None,
    deploymentName: str,
    grammarToolInputProperties: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if grammarToolInputProperties is None:
        grammarToolInputProperties = createGrammarToolInputProperties(
            getDeclaredTools(context.messages),
            nullishCoalesce(_compat_value(model, "supportsOpenAIGrammarTools"), False),
        )
    supportsAdditionalTools = nullishCoalesce(_compat_value(model, "supportsAdditionalTools"), False)
    supportsToolSearch = nullishCoalesce(_compat_value(model, "supportsToolSearch"), False)
    transcriptTools = resolveTranscriptTools(context.messages, supportsAdditionalTools or supportsToolSearch)
    messages = convertResponsesMessages(
        model,
        context,
        _AZURE_TOOL_CALL_PROVIDERS,
        {
            "grammarToolInputProperties": grammarToolInputProperties,
            "supportsMidConvoSystemMessages": nullishCoalesce(_compat_value(model, "supportsMidConvoSystemMessages"), False),
            "supportsAdditionalTools": supportsAdditionalTools,
            "supportsToolSearch": supportsToolSearch,
            "toolOptions": {
                "supportsStrictMode": nullishCoalesce(_compat_value(model, "supportsStrictMode"), True),
                "supportsOpenAIGrammarTools": nullishCoalesce(_compat_value(model, "supportsOpenAIGrammarTools"), False),
            },
        },
    )

    params: dict[str, Any] = {
        "model": deploymentName,
        "input": messages,
        "stream": True,
        "prompt_cache_key": clampOpenAIPromptCacheKey(_option(options, "sessionId")),
        "store": False,
    }

    if _option(options, "maxTokens"):
        params["max_output_tokens"] = max(_option(options, "maxTokens"), _OPENAI_RESPONSES_MIN_OUTPUT_TOKENS)

    if _option(options, "temperature") is not None:
        params["temperature"] = _option(options, "temperature")

    if len(transcriptTools.requestTools) > 0:
        params["tools"] = convertResponsesTools(
            transcriptTools.requestTools,
            {
                "supportsStrictMode": nullishCoalesce(_compat_value(model, "supportsStrictMode"), True),
                "supportsOpenAIGrammarTools": nullishCoalesce(_compat_value(model, "supportsOpenAIGrammarTools"), False),
            },
        )
    if _option(options, "toolChoice") is not None:
        params["tool_choice"] = _option(options, "toolChoice")

    reasoningEffort = nullishCoalesce(
        _option(options, "reasoningEffort"), "medium" if _option(options, "reasoningSummary") else None
    )
    if model.reasoning:
        thinkingLevelMap = model.thinkingLevelMap
        if reasoningEffort:
            effort = (
                nullishCoalesce((thinkingLevelMap or {}).get(_option(options, "reasoningEffort")), _option(options, "reasoningEffort"))
                if _option(options, "reasoningEffort")
                else reasoningEffort
            )
            params["reasoning"] = {
                "effort": effort,
                "summary": _option(options, "reasoningSummary") or "auto",
            }
            params["include"] = ["reasoning.encrypted_content"]
        elif thinkingLevelMap is None or thinkingLevelMap.get("off", UNDEFINED) is not None:
            params["reasoning"] = {
                "effort": nullishCoalesce(None if thinkingLevelMap is None else thinkingLevelMap.get("off"), "none"),
            }

    # Last so model and request sampling parameters override named request fields.
    samplingParams = resolveSamplingParams(model, nullishCoalesce(reasoningEffort, "off"), _option(options, "samplingParams"))
    if samplingParams:
        params.update(samplingParams)

    return params


__all__ = ["AzureOpenAIResponsesOptions", "stream", "streamSimple"]
