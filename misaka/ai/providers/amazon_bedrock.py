"""Amazon Bedrock ConverseStream provider adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

try:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # optional extra: misaka[bedrock]
    boto3 = UNSIGNED = Config = None

    class ClientError(Exception):  # keeps `except ClientError` valid; never raised without boto3
        pass

from misaka.ai.providers.sdk import require

from misaka.ai.models import calculate_cost
from misaka.ai.providers.simple_options import adjust_max_tokens_for_thinking, build_base_options, clamp_reasoning
from misaka.ai.providers.transform_messages import transform_messages
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
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
    Usage,
    UsageCost,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.json_parse import parse_streaming_json
from misaka.ai.utils.node_http_proxy import create_http_proxy_agents_for_target
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates

BedrockThinkingDisplay = Literal["summarized", "omitted"]


class BedrockOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    region: str
    profile: str
    toolChoice: str | dict[str, str]
    reasoning: str
    thinkingBudgets: dict[str, int]
    interleavedThinking: bool
    thinkingDisplay: BedrockThinkingDisplay
    requestMetadata: dict[str, str]
    bearerToken: str

BEDROCK_ERROR_PREFIXES: dict[str, str] = {
    "InternalServerException": "Internal server error",
    "ModelStreamErrorException": "Model stream error",
    "ValidationException": "Validation error",
    "ThrottlingException": "Throttling error",
    "ServiceUnavailableException": "Service unavailable",
}
_STANDARD_BEDROCK_ENDPOINT_PATTERN = re.compile(
    r"^bedrock-runtime(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$"
)
_MATCH_NORMALIZATION_PATTERN = re.compile(r"[\s_.:]+")
_STREAM_SENTINEL = object()


@dataclass(frozen=True, slots=True)
class BedrockClientSettings:
    profile_name: str | None
    region_name: str | None
    endpoint_url: str | None
    config_kwargs: dict[str, Any]
    default_headers: dict[str, str]
    bearer_token: str | None
    aws_access_key_id: str | None
    aws_secret_access_key: str | None


class BedrockRuntimeServiceException(RuntimeError):
    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name


def _option(options: Any, name: str, default: Any = None) -> Any:
    if options is None:
        return default
    if isinstance(options, dict):
        value = options.get(name, default)
    else:
        value = getattr(options, name, default)
    return default if value is None else value


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _coalesce_value(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
            if value is not None:
                return value
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _is_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    if getattr(signal, "aborted", False):
        return True
    return bool(getattr(signal, "is_set", lambda: False)())


def _create_abort_wait_task(signal: Any) -> asyncio.Task[None] | None:
    if signal is None or not hasattr(signal, "wait"):
        return None
    return asyncio.create_task(signal.wait())


async def _close_stream(stream_obj: Any) -> None:
    close = getattr(stream_obj, "close", None)
    if callable(close):
        try:
            await _maybe_await(close())
        except Exception:  # noqa: BLE001
            return


async def _await_with_signal(awaitable: Any, signal: Any, *, on_abort: Any = None) -> Any:
    if _is_aborted(signal):
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
        else:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
        if on_abort is not None:
            await _maybe_await(on_abort())
        raise RuntimeError("Request was aborted")

    task = asyncio.ensure_future(awaitable)
    abort_task = _create_abort_wait_task(signal)
    try:
        if abort_task is not None:
            done, _ = await asyncio.wait({task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if on_abort is not None:
                    await _maybe_await(on_abort())
                raise RuntimeError("Request was aborted")
        return await task
    finally:
        if abort_task is not None:
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)


async def _await_maybe_with_signal(value: Any, signal: Any, *, on_abort: Any = None) -> Any:
    if hasattr(value, "__await__"):
        return await _await_with_signal(value, signal, on_abort=on_abort)
    if _is_aborted(signal):
        if on_abort is not None:
            await _maybe_await(on_abort())
        raise RuntimeError("Request was aborted")
    return value


def _empty_usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def create_client(model: Model, options: StreamOptions | dict[str, Any] | None = None) -> Any:
    settings = build_client_settings(model, options)
    session = require(boto3, "boto3").Session(profile_name=settings.profile_name)
    client_kwargs: dict[str, Any] = {}
    if settings.region_name is not None:
        client_kwargs["region_name"] = settings.region_name
    if settings.endpoint_url is not None:
        client_kwargs["endpoint_url"] = settings.endpoint_url
    if settings.config_kwargs:
        client_kwargs["config"] = Config(**settings.config_kwargs)
    if settings.aws_access_key_id is not None:
        client_kwargs["aws_access_key_id"] = settings.aws_access_key_id
    if settings.aws_secret_access_key is not None:
        client_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key

    client = session.client("bedrock-runtime", **client_kwargs)
    _register_request_overrides(client, settings.default_headers, settings.bearer_token)
    return client


def build_client_settings(model: Model, options: StreamOptions | dict[str, Any] | None = None) -> BedrockClientSettings:
    configured_region = get_configured_bedrock_region(options)
    has_configured_profile = has_configured_bedrock_profile()
    endpoint_region = get_standard_bedrock_endpoint_region(model.baseUrl)
    use_explicit_endpoint = should_use_explicit_bedrock_endpoint(
        model.baseUrl,
        configured_region,
        has_configured_profile,
    )

    config_kwargs: dict[str, Any] = {}
    proxy_agents = create_http_proxy_agents_for_target(model.baseUrl)
    if proxy_agents is not None:
        config_kwargs["proxies"] = {
            "http": proxy_agents.httpAgent,
            "https": proxy_agents.httpsAgent,
        }

    bearer_token = _option(options, "bearerToken") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    skip_auth = os.environ.get("AWS_BEDROCK_SKIP_AUTH") == "1"
    if bearer_token and not skip_auth:
        config_kwargs["signature_version"] = UNSIGNED

    default_headers: dict[str, str] = {}

    region_name = configured_region
    if region_name is None and endpoint_region is not None and use_explicit_endpoint:
        region_name = endpoint_region
    if region_name is None and not has_configured_profile:
        region_name = "us-east-1"

    return BedrockClientSettings(
        profile_name=_option(options, "profile"),
        region_name=region_name,
        endpoint_url=model.baseUrl if use_explicit_endpoint else None,
        config_kwargs=config_kwargs,
        default_headers=default_headers,
        bearer_token=None if skip_auth else bearer_token,
        aws_access_key_id="dummy-access-key" if skip_auth else None,
        aws_secret_access_key="dummy-secret-key" if skip_auth else None,
    )


def _register_request_overrides(client: Any, headers: dict[str, str], bearer_token: str | None) -> None:
    if not headers and not bearer_token:
        return

    def apply(request: Any, **_kwargs: Any) -> None:
        for key, value in headers.items():
            request.headers[key] = value
        if bearer_token:
            request.headers["Authorization"] = f"Bearer {bearer_token}"

    client.meta.events.register("before-send.bedrock-runtime.ConverseStream", apply)


def stream_bedrock(
    model: Model,
    context: Context,
    options: StreamOptions | dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api="bedrock-converse-stream",
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )
        signal = _option(options, "signal")
        response_stream: Any = None

        try:
            client = _option(options, "client") or create_client(model, options)
            cache_retention = resolve_cache_retention(_option(options, "cacheRetention"))
            inference_max_tokens = _option(options, "maxTokens")
            if inference_max_tokens is None and is_anthropic_claude_model(model):
                inference_max_tokens = model.maxTokens

            command_input: dict[str, Any] = {
                "modelId": model.id,
                "messages": convert_messages(context, model, cache_retention),
                "system": build_system_prompt(context.systemPrompt, model, cache_retention),
                "inferenceConfig": {
                    **({"maxTokens": inference_max_tokens} if inference_max_tokens is not None else {}),
                    **({"temperature": _option(options, "temperature")} if _option(options, "temperature") is not None else {}),
                },
                "toolConfig": convert_tool_config(context.tools, _option(options, "toolChoice")),
                "additionalModelRequestFields": build_additional_model_request_fields(model, options),
                **({"requestMetadata": _option(options, "requestMetadata")} if _option(options, "requestMetadata") is not None else {}),
            }

            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_input = await _maybe_await(on_payload(command_input, model))
                if next_input is not None:
                    command_input = next_input

            if _is_aborted(signal):
                raise RuntimeError("Request was aborted")

            request_input = {key: value for key, value in command_input.items() if value is not None}
            response = await _await_maybe_with_signal(client.converse_stream(**request_input), signal)
            response_metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
            on_response = _option(options, "onResponse")
            if callable(on_response) and response_metadata.get("HTTPStatusCode") is not None:
                headers: dict[str, str] = {}
                if response_metadata.get("RequestId"):
                    headers["x-amzn-requestid"] = str(response_metadata["RequestId"])
                await _maybe_await(
                    on_response({"status": int(response_metadata["HTTPStatusCode"]), "headers": headers}, model)
                )

            response_stream = response.get("stream") if isinstance(response, dict) else None
            block_indices: dict[int, int] = {}
            partial_json: dict[int, str] = {}

            async for item in iterate_stream_events(response_stream, signal):
                if _is_aborted(signal):
                    raise RuntimeError("Request was aborted")

                if "messageStart" in item:
                    message_start = item["messageStart"] or {}
                    if message_start.get("role") != "assistant":
                        raise RuntimeError("Unexpected assistant message start but got user message start instead")
                    stream.push(StartEvent(partial=output))
                    continue

                if "contentBlockStart" in item:
                    handle_content_block_start(item["contentBlockStart"], block_indices, partial_json, output, stream)
                    continue

                if "contentBlockDelta" in item:
                    handle_content_block_delta(item["contentBlockDelta"], block_indices, partial_json, output, stream)
                    continue

                if "contentBlockStop" in item:
                    handle_content_block_stop(item["contentBlockStop"], block_indices, partial_json, output, stream)
                    continue

                if "messageStop" in item:
                    message_stop = item["messageStop"] or {}
                    output.stopReason = map_stop_reason(message_stop.get("stopReason"))
                    continue

                if "metadata" in item:
                    handle_metadata(item["metadata"], model, output)
                    continue

                for event_name in (
                    "internalServerException",
                    "modelStreamErrorException",
                    "validationException",
                    "throttlingException",
                    "serviceUnavailableException",
                ):
                    if event_name in item:
                        payload = item[event_name] or {}
                        exception_name = event_name[0].upper() + event_name[1:]
                        raise BedrockRuntimeServiceException(exception_name, str(payload.get("message") or ""))

            if _is_aborted(signal):
                raise RuntimeError("Request was aborted")
            if output.stopReason in {"error", "aborted"}:
                raise RuntimeError("An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            output.stopReason = "aborted" if _is_aborted(signal) else "error"
            output.errorMessage = format_bedrock_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
        finally:
            await _close_stream(response_stream)
            stream.end()

    asyncio.create_task(run())
    return stream


def stream_simple_bedrock(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    base = build_base_options(model, options, None)
    if options is None or options.reasoning is None:
        return stream_bedrock(model, context, {**base.model_dump(), "reasoning": None})

    if is_anthropic_claude_model(model):
        if supports_adaptive_thinking(model.id, model.name):
            return stream_bedrock(
                model,
                context,
                {
                    **base.model_dump(),
                    "reasoning": options.reasoning,
                    "thinkingBudgets": options.thinkingBudgets,
                },
            )

        adjusted = adjust_max_tokens_for_thinking(
            base.maxTokens,
            model.maxTokens,
            options.reasoning,
            options.thinkingBudgets,
        )
        clamped_level = clamp_reasoning(options.reasoning)
        merged_budgets = dict(options.thinkingBudgets.model_dump() if options.thinkingBudgets is not None else {})
        if clamped_level is not None:
            merged_budgets[clamped_level] = adjusted.thinkingBudget

        return stream_bedrock(
            model,
            context,
            {
                **base.model_dump(),
                "maxTokens": adjusted.maxTokens,
                "reasoning": options.reasoning,
                "thinkingBudgets": merged_budgets,
            },
        )

    return stream_bedrock(
        model,
        context,
        {
            **base.model_dump(),
            "reasoning": options.reasoning,
            "thinkingBudgets": options.thinkingBudgets,
        },
    )


async def iterate_stream_events(response_stream: Any, signal: Any = None):
    if response_stream is None:
        return
    if hasattr(response_stream, "__aiter__"):
        iterator = response_stream.__aiter__()
        while True:
            try:
                item = await _await_with_signal(
                    iterator.__anext__(),
                    signal,
                    on_abort=lambda: _close_stream(response_stream),
                )
            except StopAsyncIteration:
                return
            yield item
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def worker() -> None:
        try:
            for event in response_stream:
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as error:
            loop.call_soon_threadsafe(queue.put_nowait, error)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _STREAM_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        item = await _await_with_signal(
            queue.get(),
            signal,
            on_abort=lambda: _close_stream(response_stream),
        )
        if item is _STREAM_SENTINEL:
            return
        if isinstance(item, Exception):
            raise item
        yield item


def format_bedrock_error(error: Any) -> str:
    if isinstance(error, ClientError):
        name = error.response.get("Error", {}).get("Code", "ClientError")
        prefix = BEDROCK_ERROR_PREFIXES.get(name, name)
        return f"{prefix}: {error.response.get('Error', {}).get('Message', str(error))}"

    message = str(error) if isinstance(error, Exception) else safe_json_stringify(error)
    name = getattr(error, "name", None) or error.__class__.__name__
    prefix = BEDROCK_ERROR_PREFIXES.get(name)
    return f"{prefix}: {message}" if prefix else message


def handle_content_block_start(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, str],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    content_block_index = int(event.get("contentBlockIndex") or 0)
    start = event.get("start") or {}
    tool_use = start.get("toolUse") or {}
    if not tool_use:
        return

    block = ToolCall(
        id=str(tool_use.get("toolUseId") or ""),
        name=str(tool_use.get("name") or ""),
        arguments={},
    )
    output.content.append(block)
    content_index = len(output.content) - 1
    block_indices[content_block_index] = content_index
    partial_json[content_index] = ""
    stream.push(ToolCallStartEvent(contentIndex=content_index, partial=output))


def handle_content_block_delta(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, str],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    content_block_index = int(event.get("contentBlockIndex") or 0)
    delta = event.get("delta") or {}
    content_index = block_indices.get(content_block_index)
    block = output.content[content_index] if content_index is not None else None

    if delta.get("text") is not None:
        text_delta = str(delta.get("text") or "")
        if block is None:
            text_block = TextContent(text="")
            output.content.append(text_block)
            content_index = len(output.content) - 1
            block_indices[content_block_index] = content_index
            block = text_block
            stream.push(TextStartEvent(contentIndex=content_index, partial=output))

        if block.type == "text":
            block.text += text_delta
            stream.push(TextDeltaEvent(contentIndex=content_index, delta=text_delta, partial=output))
        return

    if delta.get("toolUse") and block is not None and block.type == "toolCall":
        tool_use = delta["toolUse"] or {}
        input_delta = str(tool_use.get("input") or "")
        partial_json[content_index] = partial_json.get(content_index, "") + input_delta
        block.arguments = parse_streaming_json(partial_json[content_index])
        stream.push(ToolCallDeltaEvent(contentIndex=content_index, delta=input_delta, partial=output))
        return

    if delta.get("reasoningContent") is not None:
        reasoning_content = delta["reasoningContent"] or {}
        if block is None:
            thinking_block = ThinkingContent(thinking="", thinkingSignature="")
            output.content.append(thinking_block)
            content_index = len(output.content) - 1
            block_indices[content_block_index] = content_index
            block = thinking_block
            stream.push(ThinkingStartEvent(contentIndex=content_index, partial=output))

        if block.type == "thinking":
            text_delta = reasoning_content.get("text")
            if text_delta:
                block.thinking += str(text_delta)
                stream.push(ThinkingDeltaEvent(contentIndex=content_index, delta=str(text_delta), partial=output))
            if reasoning_content.get("signature"):
                existing = block.thinkingSignature or ""
                block.thinkingSignature = existing + str(reasoning_content["signature"])


def handle_metadata(event: dict[str, Any], model: Model, output: AssistantMessage) -> None:
    usage = event.get("usage") or {}
    if not usage:
        return

    output.usage.input = int(usage.get("inputTokens") or 0)
    output.usage.output = int(usage.get("outputTokens") or 0)
    output.usage.cacheRead = int(usage.get("cacheReadInputTokens") or 0)
    output.usage.cacheWrite = int(usage.get("cacheWriteInputTokens") or 0)
    output.usage.totalTokens = int(usage.get("totalTokens") or (output.usage.input + output.usage.output))
    calculate_cost(model, output.usage)


def handle_content_block_stop(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, str],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    content_block_index = int(event.get("contentBlockIndex") or 0)
    content_index = block_indices.pop(content_block_index, None)
    if content_index is None:
        return

    block = output.content[content_index]
    if block.type == "text":
        stream.push(TextEndEvent(contentIndex=content_index, content=block.text, partial=output))
        return

    if block.type == "thinking":
        stream.push(ThinkingEndEvent(contentIndex=content_index, content=block.thinking, partial=output))
        return

    if block.type == "toolCall":
        block.arguments = parse_streaming_json(partial_json.get(content_index, ""))
        partial_json.pop(content_index, None)
        stream.push(ToolCallEndEvent(contentIndex=content_index, toolCall=block, partial=output))


def get_model_match_candidates(model_id: str, model_name: str | None = None) -> list[str]:
    values = [model_id, model_name] if model_name else [model_id]
    candidates: list[str] = []
    for value in values:
        if value is None:
            continue
        lowered = value.lower()
        candidates.append(lowered)
        candidates.append(_MATCH_NORMALIZATION_PATTERN.sub("-", lowered))
    return candidates


def supports_adaptive_thinking(model_id: str, model_name: str | None = None) -> bool:
    candidates = get_model_match_candidates(model_id, model_name)
    return any(
        "opus-4-6" in value or "opus-4-7" in value or "sonnet-4-6" in value
        for value in candidates
    )


def supports_native_xhigh_effort(model: Model) -> bool:
    return any("opus-4-7" in value for value in get_model_match_candidates(model.id, model.name))


def map_thinking_level_to_effort(
    model: Model,
    level: ThinkingLevel | None,
) -> Literal["low", "medium", "high", "xhigh", "max"]:
    if level == "xhigh" and supports_native_xhigh_effort(model):
        return "xhigh"

    mapped = model.thinkingLevelMap.get(level) if level and model.thinkingLevelMap is not None else None
    if isinstance(mapped, str):
        return mapped  # type: ignore[return-value]

    if level in {"minimal", "low"}:
        return "low"
    if level == "medium":
        return "medium"
    return "high"


def resolve_cache_retention(cache_retention: CacheRetention | None = None) -> CacheRetention:
    if cache_retention:
        return cache_retention
    return "long" if os.environ.get("MISAKA_CACHE_RETENTION") == "long" else "short"


def is_anthropic_claude_model(model: Model) -> bool:
    model_id = model.id.lower()
    model_name = (model.name or "").lower()
    return (
        "anthropic.claude" in model_id
        or "anthropic/claude" in model_id
        or "anthropic.claude" in model_name
        or "anthropic/claude" in model_name
        or "claude" in model_name
    )


def supports_prompt_caching(model: Model) -> bool:
    candidates = get_model_match_candidates(model.id, model.name)
    has_claude_ref = any("claude" in value for value in candidates)
    if not has_claude_ref:
        return os.environ.get("AWS_BEDROCK_FORCE_CACHE") == "1"
    if any("-4-" in value for value in candidates):
        return True
    if any("claude-3-7-sonnet" in value for value in candidates):
        return True
    if any("claude-3-5-haiku" in value for value in candidates):
        return True
    return False


def supports_thinking_signature(model: Model) -> bool:
    return is_anthropic_claude_model(model)


def build_system_prompt(
    system_prompt: str | None,
    model: Model,
    cache_retention: CacheRetention,
) -> list[dict[str, Any]] | None:
    if not system_prompt:
        return None

    blocks: list[dict[str, Any]] = [{"text": sanitize_surrogates(system_prompt)}]
    if cache_retention != "none" and supports_prompt_caching(model):
        cache_point: dict[str, Any] = {"type": "default"}
        if cache_retention == "long":
            cache_point["ttl"] = "1h"
        blocks.append({"cachePoint": cache_point})
    return blocks


def normalize_tool_call_id(tool_call_id: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_call_id)
    return sanitized[:64] if len(sanitized) > 64 else sanitized


def convert_messages(
    context: Context,
    model: Model,
    cache_retention: CacheRetention,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    transformed_messages = transform_messages(context.messages, model, lambda tool_call_id, _target_model, _source: normalize_tool_call_id(tool_call_id))
    index = 0
    while index < len(transformed_messages):
        message = transformed_messages[index]

        if message.role == "user":
            content: list[dict[str, Any]] = []
            if isinstance(message.content, str):
                content.append({"text": sanitize_surrogates(message.content)})
            else:
                for item in message.content:
                    if item.type == "text":
                        content.append({"text": sanitize_surrogates(item.text)})
                    elif item.type == "image":
                        content.append({"image": create_image_block(item.mimeType, item.data)})
            if content:
                result.append({"role": "user", "content": content})
            index += 1
            continue

        if message.role == "assistant":
            if not message.content:
                index += 1
                continue

            content_blocks: list[dict[str, Any]] = []
            for block in message.content:
                if block.type == "text":
                    if block.text.strip():
                        content_blocks.append({"text": sanitize_surrogates(block.text)})
                    continue

                if block.type == "toolCall":
                    content_blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": block.id,
                                "name": block.name,
                                "input": block.arguments,
                            }
                        }
                    )
                    continue

                if block.type == "thinking":
                    if not block.thinking.strip():
                        continue
                    if supports_thinking_signature(model):
                        if not block.thinkingSignature or not block.thinkingSignature.strip():
                            content_blocks.append({"text": sanitize_surrogates(block.thinking)})
                        else:
                            content_blocks.append(
                                {
                                    "reasoningContent": {
                                        "reasoningText": {
                                            "text": sanitize_surrogates(block.thinking),
                                            "signature": block.thinkingSignature,
                                        }
                                    }
                                }
                            )
                    else:
                        content_blocks.append(
                            {
                                "reasoningContent": {
                                    "reasoningText": {"text": sanitize_surrogates(block.thinking)}
                                }
                            }
                        )
                    continue

            if content_blocks:
                result.append({"role": "assistant", "content": content_blocks})
            index += 1
            continue

        if message.role == "toolResult":
            tool_results: list[dict[str, Any]] = []
            while index < len(transformed_messages) and transformed_messages[index].role == "toolResult":
                tool_message = transformed_messages[index]
                assert isinstance(tool_message, ToolResultMessage)
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": tool_message.toolCallId,
                            "content": [
                                {"image": create_image_block(part.mimeType, part.data)}
                                if part.type == "image"
                                else {"text": sanitize_surrogates(part.text)}
                                for part in tool_message.content
                            ],
                            "status": "error" if tool_message.isError else "success",
                        }
                    }
                )
                index += 1

            result.append({"role": "user", "content": tool_results})
            continue

        index += 1

    if cache_retention != "none" and supports_prompt_caching(model) and result:
        last_message = result[-1]
        if last_message.get("role") == "user" and isinstance(last_message.get("content"), list):
            cache_point: dict[str, Any] = {"type": "default"}
            if cache_retention == "long":
                cache_point["ttl"] = "1h"
            last_message["content"].append({"cachePoint": cache_point})

    return result


def convert_tool_config(
    tools: list[Tool] | None,
    tool_choice: str | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not tools or tool_choice == "none":
        return None

    bedrock_tools = [
        {
            "toolSpec": {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {"json": tool.parameters_json_schema()},
            }
        }
        for tool in tools
    ]

    bedrock_tool_choice: dict[str, Any] | None = None
    if tool_choice == "auto":
        bedrock_tool_choice = {"auto": {}}
    elif tool_choice == "any":
        bedrock_tool_choice = {"any": {}}
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        bedrock_tool_choice = {"tool": {"name": tool_choice.get("name")}}

    return {"tools": bedrock_tools, "toolChoice": bedrock_tool_choice}


def map_stop_reason(reason: str | None) -> StopReason:
    if reason in {"end_turn", "stop_sequence"}:
        return "stop"
    if reason in {"max_tokens", "model_context_window_exceeded"}:
        return "length"
    if reason == "tool_use":
        return "toolUse"
    return "error"


def get_configured_bedrock_region(options: StreamOptions | dict[str, Any] | None = None) -> str | None:
    return _option(options, "region") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def has_configured_bedrock_profile() -> bool:
    return bool(os.environ.get("AWS_PROFILE"))


def get_standard_bedrock_endpoint_region(base_url: str | None) -> str | None:
    if not base_url:
        return None
    try:
        hostname = urlparse(base_url).hostname or ""
    except Exception:  # noqa: BLE001
        return None
    match = _STANDARD_BEDROCK_ENDPOINT_PATTERN.match(hostname.lower())
    return match.group(1) if match else None


def should_use_explicit_bedrock_endpoint(
    base_url: str,
    configured_region: str | None,
    has_configured_profile: bool,
) -> bool:
    endpoint_region = get_standard_bedrock_endpoint_region(base_url)
    if endpoint_region is None:
        return True
    return not configured_region and not has_configured_profile


def is_govcloud_bedrock_target(model: Model, options: StreamOptions | dict[str, Any] | None = None) -> bool:
    region = get_configured_bedrock_region(options)
    if region and region.lower().startswith("us-gov-"):
        return True
    model_id = model.id.lower()
    return model_id.startswith("us-gov.") or model_id.startswith("arn:aws-us-gov:")


def build_additional_model_request_fields(
    model: Model,
    options: StreamOptions | dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    reasoning = _option(options, "reasoning")
    if not reasoning or not model.reasoning:
        return None

    if not is_anthropic_claude_model(model):
        return None

    display: BedrockThinkingDisplay | None = None if is_govcloud_bedrock_target(model, options) else _option(options, "thinkingDisplay", "summarized")

    if supports_adaptive_thinking(model.id, model.name):
        result: dict[str, Any] = {
            "thinking": {"type": "adaptive", **({"display": display} if display is not None else {})},
            "output_config": {"effort": map_thinking_level_to_effort(model, reasoning)},
        }
        return result

    default_budgets: dict[ThinkingLevel, int] = {
        "minimal": 1024,
        "low": 2048,
        "medium": 8192,
        "high": 16384,
        "xhigh": 16384,
    }
    thinking_budgets = _option(options, "thinkingBudgets")
    if isinstance(thinking_budgets, ThinkingBudgets):
        budget_map = thinking_budgets.model_dump()
    elif isinstance(thinking_budgets, dict):
        budget_map = dict(thinking_budgets)
    else:
        budget_map = {}

    budget_key = "high" if reasoning == "xhigh" else reasoning
    budget = budget_map.get(budget_key)
    if budget is None:
        budget = default_budgets[reasoning]
    result = {
        "thinking": {
            "type": "enabled",
            "budget_tokens": budget,
            **({"display": display} if display is not None else {}),
        }
    }
    if _option(options, "interleavedThinking", True):
        result["anthropic_beta"] = ["interleaved-thinking-2025-05-14"]
    return result


def create_image_block(mime_type: str, data: str) -> dict[str, Any]:
    if mime_type in {"image/jpeg", "image/jpg"}:
        image_format = "jpeg"
    elif mime_type == "image/png":
        image_format = "png"
    elif mime_type == "image/gif":
        image_format = "gif"
    elif mime_type == "image/webp":
        image_format = "webp"
    else:
        raise RuntimeError(f"Unknown image type: {mime_type}")

    return {
        "source": {"bytes": base64.b64decode(data)},
        "format": image_format,
    }


def safe_json_stringify(value: Any) -> str:
    try:
        serialized = json.dumps(value)
        return str(value) if serialized is None else serialized
    except Exception:  # noqa: BLE001
        return str(value)


streamBedrock = stream_bedrock
streamSimpleBedrock = stream_simple_bedrock
createClient = create_client
mapThinkingLevelToEffort = map_thinking_level_to_effort
resolveCacheRetention = resolve_cache_retention
buildSystemPrompt = build_system_prompt
convertMessages = convert_messages
mapStopReason = map_stop_reason

__all__ = [
    "BedrockOptions",
    "BedrockThinkingDisplay",
    "streamBedrock",
    "streamSimpleBedrock",
    "stream_bedrock",
    "stream_simple_bedrock",
]
