"""OpenRouter image-generation provider."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

try:
    from openai import AsyncOpenAI
except ImportError:  # optional extra: misaka[openai]
    AsyncOpenAI = None

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.providers.sdk import require
from misaka.ai.types import (
    AssistantImages,
    ImageContent,
    ImagesContext,
    ImagesModel,
    ImagesOptions,
    TextContent,
    Usage,
)
from misaka.ai.utils.headers import headers_to_record
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates


async def generate_images_openrouter(
    model: ImagesModel,
    context: ImagesContext,
    options: ImagesOptions | None = None,
) -> AssistantImages:
    output = AssistantImages(
        api=model.api,
        provider=model.provider,
        model=model.id,
        output=[],
        stopReason="stop",
        timestamp=0,
    )
    output.timestamp = __import__("time").time_ns() // 1_000_000

    try:
        api_key = options.apiKey if options and options.apiKey else get_env_api_key(model.provider)
        if not api_key:
            raise ValueError(f"No API key available for provider: {model.provider}")

        client = _create_client(
            model,
            api_key,
            options.headers if options else None,
            options.maxRetries if options else None,
        )
        params = _build_params(model, context)
        if options and options.onPayload is not None:
            next_params = await _maybe_await(options.onPayload(params, model))
            if next_params is not None:
                params = next_params

        raw_response = await _await_with_signal(
            lambda: client.chat.completions.with_raw_response.create(
                **params,
                timeout=(options.timeoutMs / 1000) if options and options.timeoutMs is not None else None,
            ),
            options.signal if options else None,
        )
        response = raw_response.parse()
        if options and options.onResponse is not None:
            await _maybe_await(
                options.onResponse(
                    {
                        "status": raw_response.http_response.status_code,
                        "headers": headers_to_record(raw_response.http_response.headers),
                    },
                    model,
                )
            )

        output.responseId = getattr(response, "id", None)
        if getattr(response, "usage", None) is not None:
            output.usage = _parse_usage(response.usage, model)

        choices = getattr(response, "choices", []) or []
        if choices:
            choice = choices[0]
            content = getattr(choice.message, "content", None)
            if isinstance(content, str) and content:
                output.output.append(TextContent(text=content))

            for image in getattr(choice.message, "images", []) or []:
                image_url_value = getattr(image, "image_url", None)
                image_url = image_url_value if isinstance(image_url_value, str) else getattr(image_url_value, "url", None)
                if not image_url or not image_url.startswith("data:"):
                    continue
                matches = re.match(r"^data:([^;]+);base64,(.+)$", image_url)
                if matches is None:
                    continue
                output.output.append(ImageContent(mimeType=matches[1], data=matches[2]))

        return output
    except Exception as error:  # noqa: BLE001
        output.stopReason = "aborted" if _signal_aborted(options.signal if options else None) else "error"
        output.errorMessage = _format_openrouter_error(error)
        return output


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _await_with_signal(request_factory: Callable[[], Any], signal: Any) -> Any:
    if _signal_aborted(signal):
        raise RuntimeError("Request aborted")

    request = request_factory()
    wait = getattr(signal, "wait", None)
    if not callable(wait):
        return await request

    abort_waiter = wait()
    if not hasattr(abort_waiter, "__await__"):
        return await request

    request_task = asyncio.create_task(request)
    abort_task = asyncio.create_task(abort_waiter)

    try:
        done, pending = await asyncio.wait({request_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
        if request_task in done:
            return await request_task

        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)
        raise RuntimeError("Request aborted")
    finally:
        abort_task.cancel()
        await asyncio.gather(abort_task, return_exceptions=True)


def _create_client(
    model: ImagesModel,
    api_key: str,
    option_headers: dict[str, str] | None = None,
    max_retries: int | None = None,
) -> AsyncOpenAI:
    headers = {}
    if model.headers:
        headers.update(model.headers)
    if option_headers:
        headers.update(option_headers)
    client_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": model.baseUrl,
        "default_headers": headers,
    }
    if max_retries is not None:
        client_kwargs["max_retries"] = max_retries
    return require(AsyncOpenAI, "openai")(**client_kwargs)


def _build_params(model: ImagesModel, context: ImagesContext) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for item in context.input:
        if item.type == "text":
            content.append({"type": "text", "text": sanitize_surrogates(item.text)})
        else:
            content.append({"type": "image_url", "image_url": {"url": f"data:{item.mimeType};base64,{item.data}"}})

    return {
        "model": model.id,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "modalities": ["image", "text"] if "text" in model.output else ["image"],
    }


def _parse_usage(raw_usage: Any, model: ImagesModel) -> Usage:
    prompt_tokens = getattr(raw_usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(raw_usage, "completion_tokens", 0) or 0
    details = getattr(raw_usage, "prompt_tokens_details", None)
    reported_cached_tokens = getattr(details, "cached_tokens", 0) if details is not None else 0
    cache_write_tokens = getattr(details, "cache_write_tokens", 0) if details is not None else 0
    cache_read_tokens = max(0, reported_cached_tokens - cache_write_tokens) if cache_write_tokens > 0 else reported_cached_tokens
    input_tokens = max(0, prompt_tokens - cache_read_tokens - cache_write_tokens)

    usage = Usage(
        input=input_tokens,
        output=completion_tokens,
        cacheRead=cache_read_tokens,
        cacheWrite=cache_write_tokens,
        totalTokens=input_tokens + completion_tokens + cache_read_tokens + cache_write_tokens,
        cost={
            "input": (model.cost.input / 1_000_000) * input_tokens,
            "output": (model.cost.output / 1_000_000) * completion_tokens,
            "cacheRead": (model.cost.cacheRead / 1_000_000) * cache_read_tokens,
            "cacheWrite": (model.cost.cacheWrite / 1_000_000) * cache_write_tokens,
            "total": 0,
        },
    )
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite
    return usage


def _signal_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    if hasattr(signal, "aborted"):
        return bool(signal.aborted)
    if hasattr(signal, "is_set"):
        return bool(signal.is_set())
    return False


def _format_openrouter_error(error: Any) -> str:
    return str(error) if isinstance(error, Exception) else json.dumps(error, default=str)


generateImagesOpenRouter = generate_images_openrouter

__all__ = ["generateImagesOpenRouter"]
