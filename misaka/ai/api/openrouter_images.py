"""Translated from pi's ``packages/ai/src/api/openrouter-images.ts`` (v1.1.0).

MISAKA fork, kept to what the Python SDK forces (each is marked where it happens): the client and the request go
through ``_openai_sdk`` like the other OpenAI-shaped adapters, ``options.fetch`` is not ported, and ``images`` (a field
the SDK does not declare) and the usage are read off the SDK's models.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from misaka.ai.api import _openai_sdk
from misaka.ai.api._common import _option
from misaka.ai.types import (
    AssistantImages,
    ImageContent,
    ImageModel,
    ImagesContext,
    ImagesOptions,
    ProviderHeaders,
    TextContent,
    Usage,
    UsageCost,
)
from misaka.ai.utils.error_body import formatProviderError, normalizeProviderError
from misaka.ai.utils.headers import headersToRecord, providerHeadersToRecord
from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.utils.values import maybe_await, read_field, signal_aborted


async def generateImages(
    model: ImageModel,
    context: ImagesContext,
    options: ImagesOptions | Mapping[str, Any] | None = None,
) -> AssistantImages:
    """Image generation over OpenRouter's chat completions endpoint."""
    output = AssistantImages(
        api=model.api,
        provider=model.provider,
        model=model.id,
        output=[],
        stopReason="stop",
        timestamp=time.time_ns() // 1_000_000,
    )

    try:
        apiKey = _option(options, "apiKey")
        if not apiKey:
            raise RuntimeError(f"No API key for provider: {model.provider}")
        client, requestHeaders = _createClient(model, apiKey, _option(options, "headers"))
        try:
            params = _buildParams(model, context)
            onPayload = _option(options, "onPayload")
            nextParams = await maybe_await(onPayload(params, model)) if onPayload else None
            if nextParams is not None:
                params = nextParams
            requestOptions = {
                **({"signal": _option(options, "signal")} if _option(options, "signal") else {}),
                **({"timeout": _option(options, "timeoutMs")} if _option(options, "timeoutMs") is not None else {}),
                "maxRetries": 0,
            }
            response, rawResponse = await retryProviderRequest(
                lambda: _openai_sdk._create_with_response(
                    client, "chat.completions", params, requestOptions, requestHeaders
                ),
                {
                    "maxRetries": _option(options, "maxRetries"),
                    "maxRetryDelayMs": _option(options, "maxRetryDelayMs"),
                    "signal": _option(options, "signal"),
                },
            )
        finally:
            # MISAKA fork: close the client this request opened while its loop still runs.
            await _openai_sdk._close_client(client)
        onResponse = _option(options, "onResponse")
        if onResponse:
            await maybe_await(
                onResponse({"status": rawResponse.status_code, "headers": headersToRecord(rawResponse.headers)}, model)
            )

        imageResponse = response
        output.responseId = getattr(imageResponse, "id", None)
        if getattr(imageResponse, "usage", None):
            output.usage = _parseUsage(imageResponse.usage, model)

        choices = imageResponse.choices
        choice = choices[0] if len(choices) > 0 else None
        if choice:
            content = choice.message.content
            if isinstance(content, str) and len(content) > 0:
                output.output.append(TextContent(type="text", text=content))

            # MISAKA fork: `images` is not a field the SDK declares; it keeps the response's own dicts.
            for image in read_field(choice.message, "images", None) or []:
                imageUrl = (
                    read_field(image, "image_url", None)
                    if isinstance(read_field(image, "image_url", None), str)
                    else read_field(read_field(image, "image_url", None), "url", None)
                )
                if not (isinstance(imageUrl, str) and imageUrl.startswith("data:")):
                    continue
                matches = re.match(r"^data:([^;]+);base64,(.+)\Z", imageUrl)
                if not matches:
                    continue
                output.output.append(
                    ImageContent(
                        type="image",
                        mimeType=matches[1],
                        data=matches[2],
                    )
                )

        return output
    except Exception as error:  # noqa: BLE001 - the failure is the result
        output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
        output.errorMessage = formatProviderError(normalizeProviderError(error))
        return output


def _createClient(
    model: ImageModel,
    apiKey: str,
    optionsHeaders: ProviderHeaders | None = None,
) -> tuple[Any, dict[str, Any]]:
    """MISAKA fork: returns the client and the per-request headers that carry every suppression
    (``_openai_sdk._sdk_client``)."""
    return _openai_sdk._sdk_client(
        apiKey=apiKey,
        baseURL=model.baseUrl,
        defaultHeaders=providerHeadersToRecord({**(model.headers or {}), **(optionsHeaders or {})}) or {},
    )


def _buildParams(model: ImageModel, context: ImagesContext) -> dict[str, Any]:
    content = [
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
        for item in context.input
    ]

    return {
        "model": model.id,
        "messages": [
            {
                "role": "user",
                "content": content,
            },
        ],
        "stream": False,
        "modalities": ["image", "text"] if "text" in model.output else ["image"],
    }


def _parseUsage(rawUsage: Any, model: ImageModel) -> Usage:
    # MISAKA fork: the usage is the SDK's model, read by attribute.
    details = read_field(rawUsage, "prompt_tokens_details", None)
    promptTokens = read_field(rawUsage, "prompt_tokens", None) or 0
    reportedCachedTokens = read_field(details, "cached_tokens", None) or 0
    cacheWriteTokens = read_field(details, "cache_write_tokens", None) or 0
    cacheReadTokens = max(0, reportedCachedTokens - cacheWriteTokens) if cacheWriteTokens > 0 else reportedCachedTokens
    input = max(0, promptTokens - cacheReadTokens - cacheWriteTokens)
    output = read_field(rawUsage, "completion_tokens", None) or 0
    usage = Usage(
        input=input,
        output=output,
        cacheRead=cacheReadTokens,
        cacheWrite=cacheWriteTokens,
        totalTokens=input + output + cacheReadTokens + cacheWriteTokens,
        cost=UsageCost(
            input=(model.cost.input / 1000000) * input,
            output=(model.cost.output / 1000000) * output,
            cacheRead=(model.cost.cacheRead / 1000000) * cacheReadTokens,
            cacheWrite=(model.cost.cacheWrite / 1000000) * cacheWriteTokens,
            total=0,
        ),
    )
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite
    return usage


__all__ = ["generateImages"]
