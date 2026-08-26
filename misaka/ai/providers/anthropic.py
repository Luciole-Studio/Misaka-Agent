"""Anthropic Messages provider adapter."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any, Literal, TypedDict

try:
    from anthropic import AsyncAnthropic, omit
except ImportError:  # optional extra: misaka[anthropic]
    AsyncAnthropic = omit = None

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import calculate_cost
from misaka.ai.providers._common import (
    _await_maybe_with_signal,
    _close_stream,
    _empty_usage,
    _is_aborted,
    _iterate_async_iterable,
    _option,
    resolve_cache_retention,
)
from misaka.ai.providers.cloudflare import resolve_cloudflare_base_url
from misaka.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from misaka.ai.providers.sdk import require
from misaka.ai.providers.simple_options import (
    adjust_max_tokens_for_thinking,
    build_base_options,
)
from misaka.ai.providers.transform_messages import transform_messages
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    Context,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    Model,
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
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headers_to_record
from misaka.ai.utils.json_parse import parse_json_with_repair, parse_streaming_json
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates
from misaka.utils.values import maybe_await

AnthropicEffort = Literal["low", "medium", "high", "xhigh", "max"]
AnthropicThinkingDisplay = Literal["summarized", "omitted"]


class AnthropicOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    thinkingEnabled: bool
    thinkingBudgetTokens: int
    effort: AnthropicEffort
    thinkingDisplay: AnthropicThinkingDisplay
    interleavedThinking: bool
    toolChoice: str | dict[str, str]
    client: Any

CLAUDE_CODE_VERSION = "2.1.75"
FINE_GRAINED_TOOL_STREAMING_BETA = "fine-grained-tool-streaming-2025-05-14"
INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"
ANTHROPIC_MESSAGE_EVENTS = frozenset(
    {
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
    }
)
_CLAUDE_CODE_TOOLS = (
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
)
_CLAUDE_CODE_TOOL_LOOKUP = {name.lower(): name for name in _CLAUDE_CODE_TOOLS}


class ServerSentEvent(dict[str, Any]):
    event: str | None
    data: str
    raw: list[str]


def _merge_headers(*header_sources: Mapping[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for headers in header_sources:
        if not headers:
            continue
        for key, value in headers.items():
            if value is None:
                continue
            merged[str(key)] = value
    return merged


def _force_adaptive_thinking(model: Model) -> bool | None:
    compat = getattr(model, "compat", None)
    return getattr(compat, "forceAdaptiveThinking", None)


def get_anthropic_compat(model: Model) -> dict[str, bool]:
    compat = getattr(model, "compat", None)
    is_fireworks = model.provider == "fireworks"
    is_cloudflare_gateway_anthropic = model.provider == "cloudflare-ai-gateway" and "anthropic" in model.baseUrl
    return {
        "supportsEagerToolInputStreaming": (
            getattr(compat, "supportsEagerToolInputStreaming", None)
            if getattr(compat, "supportsEagerToolInputStreaming", None) is not None
            else not is_fireworks
        ),
        "supportsLongCacheRetention": (
            getattr(compat, "supportsLongCacheRetention", None)
            if getattr(compat, "supportsLongCacheRetention", None) is not None
            else not is_fireworks
        ),
        "sendSessionAffinityHeaders": (
            getattr(compat, "sendSessionAffinityHeaders", None)
            if getattr(compat, "sendSessionAffinityHeaders", None) is not None
            else bool(is_fireworks or is_cloudflare_gateway_anthropic)
        ),
        "supportsCacheControlOnTools": (
            getattr(compat, "supportsCacheControlOnTools", None)
            if getattr(compat, "supportsCacheControlOnTools", None) is not None
            else not is_fireworks
        ),
    }


def get_cache_control(model: Model, cache_retention: CacheRetention | None = None) -> dict[str, Any]:
    retention = resolve_cache_retention(cache_retention)
    if retention == "none":
        return {"retention": retention}

    compat = get_anthropic_compat(model)
    ttl = "1h" if retention == "long" and compat["supportsLongCacheRetention"] else None
    cache_control: dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        cache_control["ttl"] = ttl
    return {"retention": retention, "cacheControl": cache_control}


def to_claude_code_name(name: str) -> str:
    return _CLAUDE_CODE_TOOL_LOOKUP.get(name.lower(), name)


def from_claude_code_name(name: str, tools: Iterable[Tool] | None = None) -> str:
    if tools:
        lowered = name.lower()
        for tool in tools:
            if tool.name.lower() == lowered:
                return tool.name
    return name


def convert_content_blocks(
    content: list[TextContent | ImageContent],
) -> str | list[dict[str, Any]]:
    has_images = any(block.type == "image" for block in content)
    if not has_images:
        return sanitize_surrogates("\n".join(block.text for block in content if block.type == "text"))

    blocks: list[dict[str, Any]] = []
    has_text = False
    for block in content:
        if block.type == "text":
            has_text = True
            blocks.append({"type": "text", "text": sanitize_surrogates(block.text)})
            continue

        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": block.mimeType,
                    "data": block.data,
                },
            }
        )

    if not has_text:
        blocks.insert(0, {"type": "text", "text": "(see attached image)"})
    return blocks


def is_oauth_token(api_key: str) -> bool:
    return "sk-ant-oat" in api_key


def create_client(
    model: Model,
    api_key: str,
    interleaved_thinking: bool,
    use_fine_grained_tool_streaming_beta: bool,
    options_headers: Mapping[str, str] | None = None,
    dynamic_headers: Mapping[str, str] | None = None,
    session_id: str | None = None,
) -> tuple[AsyncAnthropic, bool]:
    needs_interleaved_beta = interleaved_thinking and _force_adaptive_thinking(model) is not True
    beta_features: list[str] = []
    if use_fine_grained_tool_streaming_beta:
        beta_features.append(FINE_GRAINED_TOOL_STREAMING_BETA)
    if needs_interleaved_beta:
        beta_features.append(INTERLEAVED_THINKING_BETA)

    if model.provider == "cloudflare-ai-gateway":
        client = require(AsyncAnthropic, "anthropic")(
            api_key=None,
            auth_token=None,
            base_url=resolve_cloudflare_base_url(model),
            default_headers=_merge_headers(
                {
                    "accept": "application/json",
                    "anthropic-dangerous-direct-browser-access": "true",
                    "cf-aig-authorization": f"Bearer {api_key}",
                    "X-Api-Key": omit,
                    "Authorization": omit,
                    **({"anthropic-beta": ",".join(beta_features)} if beta_features else {}),
                },
                model.headers,
                options_headers,
            ),
        )
        return client, False

    if model.provider == "github-copilot":
        client = require(AsyncAnthropic, "anthropic")(
            api_key=None,
            auth_token=api_key,
            base_url=model.baseUrl,
            default_headers=_merge_headers(
                {
                    "accept": "application/json",
                    "anthropic-dangerous-direct-browser-access": "true",
                    **({"anthropic-beta": ",".join(beta_features)} if beta_features else {}),
                },
                model.headers,
                dynamic_headers,
                options_headers,
            ),
        )
        return client, False

    if is_oauth_token(api_key):
        client = require(AsyncAnthropic, "anthropic")(
            api_key=None,
            auth_token=api_key,
            base_url=model.baseUrl,
            default_headers=_merge_headers(
                {
                    "accept": "application/json",
                    "anthropic-dangerous-direct-browser-access": "true",
                    "anthropic-beta": ",".join(("claude-code-20250219", "oauth-2025-04-20", *beta_features)),
                    "user-agent": f"claude-cli/{CLAUDE_CODE_VERSION}",
                    "x-app": "cli",
                },
                model.headers,
                options_headers,
            ),
        )
        return client, True

    session_headers = (
        {"x-session-affinity": session_id}
        if session_id and get_anthropic_compat(model)["sendSessionAffinityHeaders"]
        else None
    )
    client = require(AsyncAnthropic, "anthropic")(
        api_key=api_key,
        auth_token=None,
        base_url=model.baseUrl,
        default_headers=_merge_headers(
            {
                "accept": "application/json",
                "anthropic-dangerous-direct-browser-access": "true",
                **({"anthropic-beta": ",".join(beta_features)} if beta_features else {}),
            },
            session_headers,
            model.headers,
            options_headers,
        ),
    )
    return client, False


def build_params(
    model: Model,
    context: Context,
    is_oauth: bool,
    options: Any = None,
) -> dict[str, Any]:
    cache_state = get_cache_control(model, _option(options, "cacheRetention"))
    cache_control = cache_state.get("cacheControl")
    params: dict[str, Any] = {
        "model": model.id,
        "messages": convert_messages(context.messages, model, is_oauth, cache_control),
        "max_tokens": _option(options, "maxTokens", model.maxTokens),
        "stream": True,
    }

    if is_oauth:
        system_blocks = [
            {
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                **({"cache_control": cache_control} if cache_control else {}),
            }
        ]
        if context.systemPrompt:
            system_blocks.append(
                {
                    "type": "text",
                    "text": sanitize_surrogates(context.systemPrompt),
                    **({"cache_control": cache_control} if cache_control else {}),
                }
            )
        params["system"] = system_blocks
    elif context.systemPrompt:
        params["system"] = [
            {
                "type": "text",
                "text": sanitize_surrogates(context.systemPrompt),
                **({"cache_control": cache_control} if cache_control else {}),
            }
        ]

    if _option(options, "temperature") is not None and not _option(options, "thinkingEnabled"):
        params["temperature"] = _option(options, "temperature")

    if context.tools:
        compat = get_anthropic_compat(model)
        params["tools"] = convert_tools(
            context.tools,
            is_oauth,
            bool(compat["supportsEagerToolInputStreaming"]),
            cache_control if compat["supportsCacheControlOnTools"] else None,
        )

    if model.reasoning:
        thinking_enabled = _option(options, "thinkingEnabled")
        if thinking_enabled:
            display = _option(options, "thinkingDisplay", "summarized")
            if _force_adaptive_thinking(model) is True:
                params["thinking"] = {"type": "adaptive", "display": display}
                effort = _option(options, "effort")
                if effort:
                    params["output_config"] = {"effort": effort}
            else:
                params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": _option(options, "thinkingBudgetTokens") or 1024,
                    "display": display,
                }
        elif thinking_enabled is False:
            params["thinking"] = {"type": "disabled"}

    metadata = _option(options, "metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("user_id"), str):
        params["metadata"] = {"user_id": metadata["user_id"]}

    tool_choice = _option(options, "toolChoice")
    if tool_choice:
        params["tool_choice"] = {"type": tool_choice} if isinstance(tool_choice, str) else tool_choice

    return params


def normalize_tool_call_id(tool_call_id: str) -> str:
    normalized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in tool_call_id)
    return normalized[:64]


def convert_messages(
    messages: list[Any],
    model: Model,
    is_oauth: bool,
    cache_control: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    transformed_messages = transform_messages(messages, model, normalize_tool_call_id)

    index = 0
    while index < len(transformed_messages):
        message = transformed_messages[index]

        if message.role == "user":
            if isinstance(message.content, str):
                if message.content.strip():
                    params.append({"role": "user", "content": sanitize_surrogates(message.content)})
            else:
                blocks: list[dict[str, Any]] = []
                for item in message.content:
                    if item.type == "text":
                        blocks.append({"type": "text", "text": sanitize_surrogates(item.text)})
                    else:
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": item.mimeType,
                                    "data": item.data,
                                },
                            }
                        )

                filtered_blocks = [block for block in blocks if block["type"] != "text" or block["text"].strip()]
                if filtered_blocks:
                    params.append({"role": "user", "content": filtered_blocks})
            index += 1
            continue

        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            for block in message.content:
                if block.type == "text":
                    if not block.text.strip():
                        continue
                    blocks.append({"type": "text", "text": sanitize_surrogates(block.text)})
                    continue

                if block.type == "thinking":
                    if block.redacted:
                        blocks.append({"type": "redacted_thinking", "data": block.thinkingSignature or ""})
                        continue
                    if not block.thinking.strip():
                        continue
                    if not block.thinkingSignature or not block.thinkingSignature.strip():
                        blocks.append({"type": "text", "text": sanitize_surrogates(block.thinking)})
                    else:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": sanitize_surrogates(block.thinking),
                                "signature": block.thinkingSignature,
                            }
                        )
                    continue

                if block.type == "toolCall":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": to_claude_code_name(block.name) if is_oauth else block.name,
                            "input": block.arguments or {},
                        }
                    )

            if blocks:
                params.append({"role": "assistant", "content": blocks})
            index += 1
            continue

        if message.role == "toolResult":
            tool_results: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": message.toolCallId,
                    "content": convert_content_blocks(message.content),
                    "is_error": message.isError,
                }
            ]

            lookahead = index + 1
            while lookahead < len(transformed_messages) and transformed_messages[lookahead].role == "toolResult":
                next_message = transformed_messages[lookahead]
                if not isinstance(next_message, ToolResultMessage):
                    break
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": next_message.toolCallId,
                        "content": convert_content_blocks(next_message.content),
                        "is_error": next_message.isError,
                    }
                )
                lookahead += 1

            params.append({"role": "user", "content": tool_results})
            index = lookahead
            continue

        index += 1

    if cache_control and params:
        last_message = params[-1]
        if last_message.get("role") == "user":
            content = last_message.get("content")
            if isinstance(content, list) and content:
                last_block = content[-1]
                if isinstance(last_block, dict) and last_block.get("type") in {"text", "image", "tool_result"}:
                    last_block["cache_control"] = cache_control
            elif isinstance(content, str):
                last_message["content"] = [{"type": "text", "text": content, "cache_control": cache_control}]

    return params


def should_use_fine_grained_tool_streaming_beta(model: Model, context: Context) -> bool:
    return bool(context.tools) and not bool(get_anthropic_compat(model)["supportsEagerToolInputStreaming"])


def convert_tools(
    tools: list[Tool],
    is_oauth: bool,
    supports_eager_tool_input_streaming: bool,
    cache_control: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        schema = tool.parameters_json_schema()
        converted_tool = {
            "name": to_claude_code_name(tool.name) if is_oauth else tool.name,
            "description": tool.description,
            **({"eager_input_streaming": True} if supports_eager_tool_input_streaming else {}),
            "input_schema": {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            },
        }
        if cache_control and index == len(tools) - 1:
            converted_tool["cache_control"] = cache_control
        converted.append(converted_tool)
    return converted


def map_stop_reason(reason: str) -> StopReason:
    if reason == "end_turn":
        return "stop"
    if reason == "max_tokens":
        return "length"
    if reason == "tool_use":
        return "toolUse"
    if reason in {"refusal", "sensitive"}:
        return "error"
    if reason in {"pause_turn", "stop_sequence"}:
        return "stop"
    raise RuntimeError(f"Unhandled stop reason: {reason}")


def _flush_sse_event(state: dict[str, Any]) -> ServerSentEvent | None:
    if not state["event"] and not state["data"]:
        return None

    event = ServerSentEvent(event=state["event"], data="\n".join(state["data"]), raw=list(state["raw"]))
    state["event"] = None
    state["data"] = []
    state["raw"] = []
    return event


def _decode_sse_line(line: str, state: dict[str, Any]) -> ServerSentEvent | None:
    if line == "":
        return _flush_sse_event(state)

    state["raw"].append(line)
    if line.startswith(":"):
        return None

    delimiter_index = line.find(":")
    field_name = line if delimiter_index == -1 else line[:delimiter_index]
    value = "" if delimiter_index == -1 else line[delimiter_index + 1 :]
    value = value.removeprefix(" ")

    if field_name == "event":
        state["event"] = value
    elif field_name == "data":
        state["data"].append(value)
    return None


def _next_line_break_index(text: str) -> int:
    carriage_return = text.find("\r")
    newline = text.find("\n")
    if carriage_return == -1:
        return newline
    if newline == -1:
        return carriage_return
    return min(carriage_return, newline)


def _consume_line(text: str) -> tuple[str, str] | None:
    line_break_index = _next_line_break_index(text)
    if line_break_index == -1:
        return None

    next_index = line_break_index + 1
    if text[line_break_index] == "\r" and next_index < len(text) and text[next_index] == "\n":
        next_index += 1
    return text[:line_break_index], text[next_index:]


async def _iter_response_lines(source: Any, signal: Any = None) -> AsyncIterator[str]:
    # Unwrap SDK raw-response wrappers (e.g. anthropic LegacyAPIResponse), which expose
    # the streaming httpx.Response as `.http_response` and none of the iteration surfaces.
    # The wrapped response comes from AsyncAnthropic, so its sync iter_lines() would raise
    # ("Attempted to call a sync iterator on an async stream") — go straight to aiter_lines.
    http_response = getattr(source, "http_response", None)
    if http_response is not None and not hasattr(source, "iter_lines") and not hasattr(source, "aiter_lines"):
        if hasattr(http_response, "aiter_lines"):
            async for line in _iterate_async_iterable(
                http_response.aiter_lines(), signal, on_abort=lambda: _close_stream(http_response)
            ):
                yield line.decode("utf-8") if isinstance(line, bytes) else str(line)
            return
        source = http_response
    if hasattr(source, "iter_lines"):
        lines = source.iter_lines()
        if hasattr(lines, "__aiter__"):
            async for line in _iterate_async_iterable(lines, signal, on_abort=lambda: _close_stream(source)):
                yield line.decode("utf-8") if isinstance(line, bytes) else str(line)
            return
        for line in lines:
            if _is_aborted(signal):
                raise RuntimeError("Request was aborted")
            yield line.decode("utf-8") if isinstance(line, bytes) else str(line)
        return

    if hasattr(source, "aiter_lines"):
        async for line in _iterate_async_iterable(source.aiter_lines(), signal, on_abort=lambda: _close_stream(source)):
            yield line.decode("utf-8") if isinstance(line, bytes) else str(line)
        return

    body = getattr(source, "body", None)
    if body is None and hasattr(source, "__aiter__"):
        body = source
    if body is None:
        raise RuntimeError("Attempted to iterate over an Anthropic response with no body")

    buffer = ""
    if hasattr(body, "__aiter__"):
        async for chunk in _iterate_async_iterable(body, signal, on_abort=lambda: _close_stream(source)):
            if isinstance(chunk, bytes):
                buffer += chunk.decode("utf-8")
            else:
                buffer += str(chunk)

            consumed = _consume_line(buffer)
            while consumed is not None:
                line, buffer = consumed
                yield line
                consumed = _consume_line(buffer)
    else:
        for chunk in body:
            if _is_aborted(signal):
                raise RuntimeError("Request was aborted")
            if isinstance(chunk, bytes):
                buffer += chunk.decode("utf-8")
            else:
                buffer += str(chunk)

            consumed = _consume_line(buffer)
            while consumed is not None:
                line, buffer = consumed
                yield line
                consumed = _consume_line(buffer)

    if buffer:
        consumed = _consume_line(buffer)
        while consumed is not None:
            line, buffer = consumed
            yield line
            consumed = _consume_line(buffer)
        if buffer:
            yield buffer


async def iterate_sse_messages(source: Any, signal: Any = None) -> AsyncIterator[ServerSentEvent]:
    state: dict[str, Any] = {"event": None, "data": [], "raw": []}
    async for line in _iter_response_lines(source, signal):
        if _is_aborted(signal):
            raise RuntimeError("Request was aborted")
        event = _decode_sse_line(line, state)
        if event is not None:
            yield event

    trailing_event = _flush_sse_event(state)
    if trailing_event is not None:
        yield trailing_event


async def iterate_anthropic_events(source: Any, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    saw_message_start = False
    saw_message_stop = False

    async for sse in iterate_sse_messages(source, signal):
        event_name = sse.get("event")
        if event_name == "error":
            raise RuntimeError(sse["data"])
        if event_name not in ANTHROPIC_MESSAGE_EVENTS:
            continue

        try:
            event = parse_json_with_repair(sse["data"])
        except Exception as error:
            raw_text = "\\n".join(sse["raw"])
            raise RuntimeError(
                f"Could not parse Anthropic SSE event {event_name}: {error}; "
                f"data={sse['data']}; raw={raw_text}"
            ) from error

        if not isinstance(event, dict):
            raise RuntimeError(f"Could not parse Anthropic SSE event {event_name}: parsed payload was not an object")  # noqa: TRY004 - callers treat bad input as ValueError

        if event.get("type") == "message_start":
            saw_message_start = True
        elif event.get("type") == "message_stop":
            saw_message_stop = True
        yield event

    if saw_message_start and not saw_message_stop:
        raise RuntimeError("Anthropic stream ended before message_stop")


async def _create_raw_response(client: Any, params: dict[str, Any], options: Any = None) -> Any:
    signal = _option(options, "signal")
    request_client = client
    request_client_options: dict[str, Any] = {}
    request_call_options: dict[str, Any] = {}
    if _option(options, "timeoutMs") is not None:
        request_client_options["timeout"] = _option(options, "timeoutMs") / 1000
    if _option(options, "maxRetries") is not None:
        request_client_options["max_retries"] = _option(options, "maxRetries")
    if request_client_options and hasattr(client, "with_options"):
        request_client = client.with_options(**request_client_options)
    else:
        request_call_options = request_client_options

    if hasattr(getattr(request_client, "messages", None), "with_raw_response"):
        return await _await_maybe_with_signal(
            request_client.messages.with_raw_response.create(**params, **request_call_options),
            signal,
        )

    created = await _await_maybe_with_signal(request_client.messages.create(**params, **request_call_options), signal)
    if hasattr(created, "asResponse"):
        return await _await_maybe_with_signal(created.asResponse(), signal)
    return created


async def _emit_response_metadata(response: Any, options: Any, model: Model) -> None:
    on_response = _option(options, "onResponse")
    if not callable(on_response):
        return

    if hasattr(response, "http_response"):
        http_response = response.http_response
        await maybe_await(
            on_response(
                {"status": http_response.status_code, "headers": headers_to_record(http_response.headers)},
                model,
            )
        )
        return

    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "status", None)
    headers = getattr(response, "headers", None)
    if isinstance(status, int) and headers is not None:
        await maybe_await(on_response({"status": status, "headers": headers_to_record(headers)}, model))


async def _iter_event_objects(stream_like: Any, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    async for event in _iterate_async_iterable(stream_like, signal, on_abort=lambda: _close_stream(stream_like)):
        if hasattr(event, "model_dump"):
            dumped = event.model_dump()
            if isinstance(dumped, dict):
                yield dumped
                continue
        if isinstance(event, dict):
            yield event
            continue
        try:
            yield json.loads(json.dumps(event, default=lambda value: value.__dict__))
        except Exception as error:
            raise RuntimeError(f"Could not serialize Anthropic stream event: {error}") from error

def _update_usage_from_anthropic_usage(output: AssistantMessage, usage: Mapping[str, Any], model: Model) -> None:
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cache_read_tokens = usage.get("cache_read_input_tokens")
    cache_write_tokens = usage.get("cache_creation_input_tokens")
    if input_tokens is not None:
        output.usage.input = int(input_tokens)
    if output_tokens is not None:
        output.usage.output = int(output_tokens)
    if cache_read_tokens is not None:
        output.usage.cacheRead = int(cache_read_tokens)
    if cache_write_tokens is not None:
        output.usage.cacheWrite = int(cache_write_tokens)
    output.usage.totalTokens = output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite
    calculate_cost(model, output.usage)


def _format_anthropic_error(error: Any) -> str:
    return str(error) if isinstance(error, Exception) else json.dumps(error, default=str)


def stream_anthropic(
    model: Model,
    context: Context,
    options: StreamOptions | dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )
        raw_response: Any = None

        try:
            client = _option(options, "client")
            if client is None:
                api_key = _option(options, "apiKey") or get_env_api_key(model.provider) or ""
                copilot_dynamic_headers: dict[str, str] | None = None
                if model.provider == "github-copilot":
                    copilot_dynamic_headers = build_copilot_dynamic_headers(
                        messages=context.messages,
                        hasImages=has_copilot_vision_input(context.messages),
                    )
                cache_retention = resolve_cache_retention(_option(options, "cacheRetention"))
                cache_session_id = None if cache_retention == "none" else _option(options, "sessionId")
                client, is_oauth = create_client(
                    model,
                    api_key,
                    bool(_option(options, "interleavedThinking", True)),
                    should_use_fine_grained_tool_streaming_beta(model, context),
                    _option(options, "headers"),
                    copilot_dynamic_headers,
                    cache_session_id,
                )
            else:
                is_oauth = False

            params = build_params(model, context, is_oauth, options)
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_params = await maybe_await(on_payload(params, model))
                if next_params is not None:
                    params = next_params

            raw_response = await _create_raw_response(client, params, options)
            await _emit_response_metadata(raw_response, options, model)
            stream.push(StartEvent(partial=output))
            provider_indexes: dict[int, int] = {}
            tool_partial_json: dict[int, str] = {}

            if hasattr(raw_response, "http_response") or hasattr(raw_response, "iter_lines") or hasattr(raw_response, "aiter_lines"):
                event_iter: AsyncIterator[dict[str, Any]] = iterate_anthropic_events(raw_response, _option(options, "signal"))
            else:
                event_iter = _iter_event_objects(raw_response, _option(options, "signal"))

            async for event in event_iter:
                event_type = event.get("type")
                if event_type == "message_start":
                    message = event.get("message")
                    if isinstance(message, Mapping):
                        message_id = message.get("id")
                        if isinstance(message_id, str):
                            output.responseId = message_id
                        usage = message.get("usage")
                        if isinstance(usage, Mapping):
                            _update_usage_from_anthropic_usage(output, usage, model)
                    continue

                if event_type == "content_block_start":
                    content_block = event.get("content_block")
                    provider_index = event.get("index")
                    if not isinstance(content_block, Mapping) or not isinstance(provider_index, int):
                        continue
                    block_type = content_block.get("type")
                    # content_block_start may already carry initial text/thinking;
                    # dropping it would lose content (pi #7358/59ad3dead).
                    if block_type == "text":
                        block = TextContent(text=str(content_block.get("text") or ""))
                        output.content.append(block)
                        provider_indexes[provider_index] = len(output.content) - 1
                        stream.push(TextStartEvent(contentIndex=len(output.content) - 1, partial=output))
                    elif block_type == "thinking":
                        block = ThinkingContent(
                            thinking=str(content_block.get("thinking") or ""),
                            thinkingSignature=str(content_block.get("signature") or ""))
                        output.content.append(block)
                        provider_indexes[provider_index] = len(output.content) - 1
                        stream.push(ThinkingStartEvent(contentIndex=len(output.content) - 1, partial=output))
                    elif block_type == "redacted_thinking":
                        block = ThinkingContent(
                            thinking="[Reasoning redacted]",
                            thinkingSignature=str(content_block.get("data") or ""),
                            redacted=True,
                        )
                        output.content.append(block)
                        provider_indexes[provider_index] = len(output.content) - 1
                        stream.push(ThinkingStartEvent(contentIndex=len(output.content) - 1, partial=output))
                    elif block_type == "tool_use":
                        initial_arguments = content_block.get("input")
                        block = ToolCall(
                            id=str(content_block.get("id") or ""),
                            name=(
                                from_claude_code_name(str(content_block.get("name") or ""), context.tools)
                                if is_oauth
                                else str(content_block.get("name") or "")
                            ),
                            arguments=initial_arguments if isinstance(initial_arguments, dict) else {},
                        )
                        output.content.append(block)
                        provider_indexes[provider_index] = len(output.content) - 1
                        tool_partial_json[provider_index] = ""
                        stream.push(ToolCallStartEvent(contentIndex=len(output.content) - 1, partial=output))
                    continue

                if event_type == "content_block_delta":
                    provider_index = event.get("index")
                    delta = event.get("delta")
                    if not isinstance(provider_index, int) or not isinstance(delta, Mapping):
                        continue
                    content_index = provider_indexes.get(provider_index)
                    if content_index is None:
                        continue
                    block = output.content[content_index]
                    delta_type = delta.get("type")
                    if delta_type == "text_delta" and isinstance(block, TextContent):
                        text_delta = str(delta.get("text") or "")
                        block.text += text_delta
                        stream.push(TextDeltaEvent(contentIndex=content_index, delta=text_delta, partial=output))
                    elif delta_type == "thinking_delta" and isinstance(block, ThinkingContent):
                        thinking_delta = str(delta.get("thinking") or "")
                        block.thinking += thinking_delta
                        stream.push(ThinkingDeltaEvent(contentIndex=content_index, delta=thinking_delta, partial=output))
                    elif delta_type == "input_json_delta" and isinstance(block, ToolCall):
                        partial_delta = str(delta.get("partial_json") or "")
                        tool_partial_json[provider_index] = tool_partial_json.get(provider_index, "") + partial_delta
                        block.arguments = parse_streaming_json(tool_partial_json[provider_index])
                        stream.push(ToolCallDeltaEvent(contentIndex=content_index, delta=partial_delta, partial=output))
                    elif delta_type == "signature_delta" and isinstance(block, ThinkingContent):
                        block.thinkingSignature = (block.thinkingSignature or "") + str(delta.get("signature") or "")
                    continue

                if event_type == "content_block_stop":
                    provider_index = event.get("index")
                    if not isinstance(provider_index, int):
                        continue
                    content_index = provider_indexes.get(provider_index)
                    if content_index is None:
                        continue
                    block = output.content[content_index]
                    if isinstance(block, TextContent):
                        stream.push(TextEndEvent(contentIndex=content_index, content=block.text, partial=output))
                    elif isinstance(block, ThinkingContent):
                        stream.push(ThinkingEndEvent(contentIndex=content_index, content=block.thinking, partial=output))
                    elif isinstance(block, ToolCall):
                        block.arguments = parse_streaming_json(tool_partial_json.get(provider_index, ""))
                        stream.push(ToolCallEndEvent(contentIndex=content_index, toolCall=block, partial=output))
                    continue

                if event_type == "message_delta":
                    delta = event.get("delta")
                    if isinstance(delta, Mapping) and isinstance(delta.get("stop_reason"), str):
                        output.stopReason = map_stop_reason(delta["stop_reason"])
                    usage = event.get("usage")
                    if isinstance(usage, Mapping):
                        _update_usage_from_anthropic_usage(output, usage, model)

            if _is_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")
            if output.stopReason in {"aborted", "error"}:
                raise RuntimeError("An unknown error occurred")
            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001 - every failure becomes an error event on the stream
            output.stopReason = "aborted" if _is_aborted(_option(options, "signal")) else "error"
            output.errorMessage = _format_anthropic_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
        finally:
            await _close_stream(raw_response)
            stream.end()

    spawn_stream_task(run())
    return stream


def map_thinking_level_to_effort(model: Model, level: str | None) -> AnthropicEffort:
    mapped = model.thinkingLevelMap.get(level) if level and model.thinkingLevelMap else None
    if isinstance(mapped, str):
        return mapped  # type: ignore[return-value]
    if level in {"minimal", "low"}:
        return "low"
    if level == "medium":
        return "medium"
    return "high"


def stream_simple_anthropic(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    if not options or not options.reasoning:
        return stream_anthropic(model, context, {**base.model_dump(), "thinkingEnabled": False})

    if _force_adaptive_thinking(model) is True:
        return stream_anthropic(
            model,
            context,
            {
                **base.model_dump(),
                "thinkingEnabled": True,
                "effort": map_thinking_level_to_effort(model, options.reasoning),
            },
        )

    adjusted = adjust_max_tokens_for_thinking(
        base.maxTokens,
        model.maxTokens,
        options.reasoning,
        options.thinkingBudgets,
    )
    return stream_anthropic(
        model,
        context,
        {
            **base.model_dump(),
            "maxTokens": adjusted.maxTokens,
            "thinkingEnabled": True,
            "thinkingBudgetTokens": adjusted.thinkingBudget,
        },
    )


streamAnthropic = stream_anthropic
streamSimpleAnthropic = stream_simple_anthropic
__all__ = [
    "AnthropicEffort",
    "AnthropicOptions",
    "AnthropicThinkingDisplay",
    "streamAnthropic",
    "streamSimpleAnthropic",
]
