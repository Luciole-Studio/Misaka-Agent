"""Translated from pi's ``packages/ai/src/api/openai-responses.ts`` (v1.3.0).

MISAKA fork, kept to what the Python SDK and platform force (each is marked where it happens):

- The SDK glue lives in ``misaka.ai.api._openai_sdk`` (``omit`` for suppressed headers, ``extra_body`` for undeclared
  fields, the request and every stream read raced against the signal).
- There is no ``fetch`` option: requests go through the SDK's own transport.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

from misaka.ai.api import _openai_sdk
from misaka.ai.api._common import _compat_value, _option
from misaka.ai.api.constrained_sampling import createGrammarToolInputProperties
from misaka.ai.api.github_copilot_headers import (
    buildCopilotDynamicHeaders,
    hasCopilotVisionInput,
)
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
    CacheRetention,
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
from misaka.ai.utils.error_body import formatProviderError, normalizeProviderError
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headersToRecord
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.ai.utils.transcript import (
    getDeclaredTools,
    resolveTranscript,
    resolveTranscriptTools,
)
from misaka.utils.js import UNDEFINED, nullishCoalesce
from misaka.utils.values import maybe_await, signal_aborted

_OPENAI_TOOL_CALL_PROVIDERS = {"openai", "openai-codex", "opencode"}
# OpenAI Responses rejects max_output_tokens below 16: https://github.com/earendil-works/pi/issues/6265
_OPENAI_RESPONSES_MIN_OUTPUT_TOKENS = 16
_CHATGPT_USAGE_URL = "https://chatgpt.com/settings/usage"
# Fixed reasoning.effort while configuration_update items carry the thinking level.
_CONFIGURATION_UPDATE_REQUEST_EFFORT = "medium"


# OpenAI API keys start with `sk-`; a different credential sent directly to OpenAI
# is a Sign in with ChatGPT access token.
def _isChatGPTSignIn(model: Model, apiKey: str | None) -> bool:
    return (
        model.provider == "openai"
        and model.baseUrl == "https://api.openai.com/v1"
        and apiKey is not None
        and not apiKey.startswith("sk-")
    )


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


def _detectSessionAffinityFormat(model: Model) -> str:
    return "openrouter" if model.provider == "openrouter" or "openrouter.ai" in model.baseUrl else "openai"


def _resolveCacheRetention(cacheRetention: CacheRetention | None = None, env: ProviderEnv | None = None) -> CacheRetention:
    """Resolve cache retention preference.
    Defaults to "short" and uses PI_CACHE_RETENTION for backward compatibility.
    """
    if cacheRetention:
        return cacheRetention
    if getProviderEnvValue("PI_CACHE_RETENTION", env) == "long":
        return "long"
    return "short"


def _getCompat(model: Model) -> dict[str, Any]:
    return {
        "supportsDeveloperRole": nullishCoalesce(_compat_value(model, "supportsDeveloperRole"), True),
        "supportsMidConvoSystemMessages": nullishCoalesce(_compat_value(model, "supportsMidConvoSystemMessages"), False),
        "sessionAffinityFormat": nullishCoalesce(
            _compat_value(model, "sessionAffinityFormat"), _detectSessionAffinityFormat(model)
        ),
        "supportsLongCacheRetention": nullishCoalesce(_compat_value(model, "supportsLongCacheRetention"), True),
        "supportsStrictMode": nullishCoalesce(_compat_value(model, "supportsStrictMode"), False),
        "supportsOpenAIGrammarTools": nullishCoalesce(_compat_value(model, "supportsOpenAIGrammarTools"), False),
        "supportsAdditionalTools": nullishCoalesce(_compat_value(model, "supportsAdditionalTools"), False),
        "supportsToolSearch": nullishCoalesce(_compat_value(model, "supportsToolSearch"), False),
        "supportsExplicitPromptCacheMode": nullishCoalesce(_compat_value(model, "supportsExplicitPromptCacheMode"), False),
        "supportsMaxOutputTokens": nullishCoalesce(_compat_value(model, "supportsMaxOutputTokens"), True),
        "supportsMidConvoEffort": nullishCoalesce(_compat_value(model, "supportsMidConvoEffort"), False),
    }


def _getConfigurationUpdateEffort(
    model: Model,
    options: OpenAIResponsesOptions | Mapping[str, Any] | None,
    compat: Mapping[str, Any],
) -> str | None:
    """Effort to send in configuration_update items, or None to send it as reasoning.effort."""
    if (
        not compat["supportsMidConvoEffort"]
        or not _option(options, "reasoningEffort")
        # Sign in with ChatGPT rejects configuration_update items.
        or _isChatGPTSignIn(model, _option(options, "apiKey"))
    ):
        return None
    return nullishCoalesce(
        (model.thinkingLevelMap or {}).get(_option(options, "reasoningEffort")), _option(options, "reasoningEffort")
    )


def _getPromptCacheRetention(compat: Mapping[str, Any], cacheRetention: CacheRetention) -> str | None:
    return (
        "24h"
        if cacheRetention == "long" and compat["supportsLongCacheRetention"] and not compat["supportsExplicitPromptCacheMode"]
        else None
    )


def _getPromptCacheOptions(compat: Mapping[str, Any], cacheRetention: CacheRetention) -> dict[str, str] | None:
    if not compat["supportsExplicitPromptCacheMode"]:
        return None
    if cacheRetention == "none":
        return {"mode": "explicit"}
    if cacheRetention == "long" and compat["supportsLongCacheRetention"]:
        return {"ttl": "30m"}
    return None


# OpenAI Responses-specific options
class OpenAIResponsesOptions(TypedDict, total=False):
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
    reasoningEffort: Literal["minimal", "low", "medium", "high", "xhigh", "max"]
    reasoningSummary: Literal["auto", "detailed", "concise"] | None
    serviceTier: Literal["auto", "default", "flex", "scale", "priority", "fast"] | None
    toolChoice: Any


def stream(
    model: Model,
    context: TranscriptContext,
    options: OpenAIResponsesOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    """Generate function for OpenAI Responses API"""
    stream = AssistantMessageEventStream()
    normalizedContext = resolveTranscript(context, _getCompat(model)["supportsMidConvoSystemMessages"])

    # Start async processing
    async def run() -> None:
        compat = _getCompat(model)
        configurationUpdateEffort = _getConfigurationUpdateEffort(model, options, compat)
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            **({} if configurationUpdateEffort is None else {"providerThinkingLevel": configurationUpdateEffort}),
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
            # Create OpenAI client
            apiKey = _getClientApiKey(model.provider, _option(options, "apiKey"), _option(options, "headers"))
            cacheRetention = _resolveCacheRetention(_option(options, "cacheRetention"), _option(options, "env"))
            cacheSessionId = None if cacheRetention == "none" else _option(options, "sessionId")
            grammarToolInputProperties = createGrammarToolInputProperties(
                getDeclaredTools(normalizedContext.messages),
                compat["supportsOpenAIGrammarTools"],
            )
            client, requestHeaders = _createClient(
                model,
                normalizedContext,
                apiKey,
                _option(options, "headers"),
                cacheSessionId,
            )
            params = _buildParams(
                model,
                normalizedContext,
                options,
                compat,
                grammarToolInputProperties,
                configurationUpdateEffort,
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
                        "serviceTier": _option(options, "serviceTier"),
                        "grammarToolInputProperties": grammarToolInputProperties,
                        "applyServiceTierPricing": lambda usage, serviceTier: _applyServiceTierPricing(
                            usage, serviceTier, model
                        ),
                    },
                )

            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")

            if output.stopReason == "pending":
                raise RuntimeError("OpenAI Responses stream ended without a stop reason")
            if output.stopReason in ("aborted", "error"):
                raise RuntimeError(output.errorMessage or "An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
            stream.end()
        except Exception as error:  # noqa: BLE001 - every failure becomes the stream's error event
            # MISAKA fork: streaming scratch buffers live beside the blocks
            # (`openai_responses_shared._StreamingToolCallScratch`), so there is nothing to strip here.
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            errorMessage = formatProviderError(
                normalizeProviderError(error),
                f"{'OpenAI' if model.provider == 'openai' else model.provider} API error",
            )
            # Sign in with ChatGPT shares the subscription's usage limit with other apps.
            output.errorMessage = (
                f"{errorMessage}\nCheck your ChatGPT usage: {_CHATGPT_USAGE_URL}"
                if "subscription_sharing_usage_limit_exceeded" in errorMessage
                else errorMessage
            )
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
        },
    )


def _createClient(
    model: Model,
    context: TranscriptContext,
    apiKey: str,
    optionsHeaders: ProviderHeaders | None = None,
    sessionId: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """MISAKA fork: returns the client and the per-request headers that carry every suppression
    (``_openai_sdk._sdk_client``)."""
    compat = _getCompat(model)
    headers: dict[str, Any] = {"User-Agent": getPiUserAgent(), **(model.headers or {})}
    if model.provider == "github-copilot":
        hasImages = hasCopilotVisionInput(context.messages)
        copilotHeaders = buildCopilotDynamicHeaders({
            "messages": context.messages,
            "hasImages": hasImages,
        })
        headers.update(copilotHeaders)

    if sessionId:
        if compat["sessionAffinityFormat"] == "openrouter":
            headers["x-session-id"] = sessionId
        else:
            if compat["sessionAffinityFormat"] == "openai":
                headers["session_id"] = sessionId
            headers["x-client-request-id"] = sessionId

    # Merge options headers last so they can override defaults
    if optionsHeaders:
        headers.update(optionsHeaders)

    return _openai_sdk._sdk_client(
        apiKey=apiKey,
        baseURL=model.baseUrl,
        defaultHeaders=headers,
    )


def _buildParams(
    model: Model,
    context: TranscriptContext,
    options: OpenAIResponsesOptions | Mapping[str, Any] | None,
    compat: Mapping[str, Any],
    grammarToolInputProperties: Mapping[str, str],
    configurationUpdateEffort: str | None,
) -> dict[str, Any]:
    transcriptTools = resolveTranscriptTools(
        context.messages,
        compat["supportsAdditionalTools"] or compat["supportsToolSearch"],
    )
    messages = convertResponsesMessages(
        model,
        context,
        _OPENAI_TOOL_CALL_PROVIDERS,
        {
            "grammarToolInputProperties": grammarToolInputProperties,
            "supportsMidConvoSystemMessages": compat["supportsMidConvoSystemMessages"],
            "supportsAdditionalTools": compat["supportsAdditionalTools"],
            "supportsToolSearch": compat["supportsToolSearch"],
            "toolOptions": {
                "supportsStrictMode": compat["supportsStrictMode"],
                "supportsOpenAIGrammarTools": compat["supportsOpenAIGrammarTools"],
            },
            **({} if configurationUpdateEffort is None else {"configurationUpdateEffort": configurationUpdateEffort}),
        },
    )

    cacheRetention = _resolveCacheRetention(_option(options, "cacheRetention"), _option(options, "env"))
    # Sign in with ChatGPT rejects these request fields.
    omitUnsupportedFields = _isChatGPTSignIn(model, _option(options, "apiKey"))
    params: dict[str, Any] = {
        "model": model.id,
        "input": messages,
        "stream": True,
        "prompt_cache_key": None if cacheRetention == "none" else clampOpenAIPromptCacheKey(_option(options, "sessionId")),
        "prompt_cache_retention": None if omitUnsupportedFields else _getPromptCacheRetention(compat, cacheRetention),
        "prompt_cache_options": None if omitUnsupportedFields else _getPromptCacheOptions(compat, cacheRetention),
        "store": False,
    }

    if _option(options, "maxTokens") and compat["supportsMaxOutputTokens"] and not omitUnsupportedFields:
        params["max_output_tokens"] = max(_option(options, "maxTokens"), _OPENAI_RESPONSES_MIN_OUTPUT_TOKENS)

    if _option(options, "temperature") is not None and not omitUnsupportedFields:
        params["temperature"] = _option(options, "temperature")

    if _option(options, "serviceTier") is not None:
        params["service_tier"] = _option(options, "serviceTier")

    if len(transcriptTools.requestTools) > 0:
        params["tools"] = convertResponsesTools(
            transcriptTools.requestTools,
            {
                "supportsStrictMode": compat["supportsStrictMode"],
                "supportsOpenAIGrammarTools": compat["supportsOpenAIGrammarTools"],
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
            requestedEffort = (
                nullishCoalesce((thinkingLevelMap or {}).get(_option(options, "reasoningEffort")), _option(options, "reasoningEffort"))
                if _option(options, "reasoningEffort")
                else reasoningEffort
            )
            effort = requestedEffort if configurationUpdateEffort is None else _CONFIGURATION_UPDATE_REQUEST_EFFORT
            params["reasoning"] = {
                "effort": effort,
                "summary": _option(options, "reasoningSummary") or "auto",
            }
            params["include"] = ["reasoning.encrypted_content"]
        elif model.provider != "github-copilot" and (
            thinkingLevelMap is None or thinkingLevelMap.get("off", UNDEFINED) is not None
        ):
            params["reasoning"] = {
                "effort": nullishCoalesce(None if thinkingLevelMap is None else thinkingLevelMap.get("off"), "none"),
            }
        if model.provider == "xai":
            params["include"] = ["reasoning.encrypted_content"]

    # Last so model and request sampling parameters override named request fields.
    samplingParams = resolveSamplingParams(model, nullishCoalesce(reasoningEffort, "off"), _option(options, "samplingParams"))
    if samplingParams:
        params.update(samplingParams)

    return params


def _getServiceTierCostMultiplier(model: Model, serviceTier: str | None) -> float:
    match serviceTier:
        case "flex":
            return 0.5
        case "priority" | "fast":
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


__all__ = ["OpenAIResponsesOptions", "stream", "streamSimple"]
