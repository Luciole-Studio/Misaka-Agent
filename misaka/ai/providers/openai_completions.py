"""OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal, TypedDict

try:
    from openai import AsyncOpenAI
except ImportError:  # optional extra: misaka[openai]
    AsyncOpenAI = None

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import calculate_cost, clamp_thinking_level
from misaka.ai.providers._common import (
    _await_maybe_with_signal,
    _await_with_signal,
    _close_stream,
    _compat_value,
    _empty_usage,
    _is_aborted,
    _maybe_await,
    _option,
    resolve_cache_retention,
)
from misaka.ai.providers.cloudflare import (
    is_cloudflare_provider,
    resolve_cloudflare_base_url,
)
from misaka.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from misaka.ai.providers.openai_prompt_cache import clamp_openai_prompt_cache_key
from misaka.ai.providers.sdk import require
from misaka.ai.providers.simple_options import build_base_options
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
    Usage,
    UsageCost,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headers_to_record
from misaka.ai.utils.json_parse import parse_streaming_json
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates


def _set_extra(params: dict[str, Any], key: str, value: Any) -> None:
    """Route a non-standard param through extra_body.

    Python equivalent of TypeScript's (params as any).key = value,
    which passes unknown properties through to the API request body.
    """
    if "extra_body" not in params:
        params["extra_body"] = {}
    params["extra_body"][key] = value


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return value


class OpenAICompletionsOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    toolChoice: Literal["auto", "none", "required"] | OpenAICompletionsToolChoiceObject
    reasoningEffort: Literal["minimal", "low", "medium", "high", "xhigh"]


class OpenAICompletionsToolChoiceFunction(TypedDict):
    name: str


class OpenAICompletionsToolChoiceObject(TypedDict):
    type: Literal["function"]
    function: OpenAICompletionsToolChoiceFunction


def has_tool_history(messages: list[Any]) -> bool:
    for message in messages:
        if message.role == "toolResult":
            return True
        if message.role == "assistant" and any(block.type == "toolCall" for block in message.content):
            return True
    return False


def stream_openai_completions(
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

        try:
            compat = get_compat(model)
            client = _option(options, "client")
            if client is None:
                api_key = _option(options, "apiKey") or get_env_api_key(model.provider) or ""
                cache_retention = resolve_cache_retention(_option(options, "cacheRetention"))
                cache_session_id = None if cache_retention == "none" else _option(options, "sessionId")
                client = create_client(
                    model,
                    context,
                    api_key,
                    _option(options, "headers"),
                    cache_session_id,
                    compat,
                )

            params = build_params(model, context, options, compat)
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_params = await _maybe_await(on_payload(params, model))
                if next_params is not None:
                    params = next_params

            openai_stream = await _create_completion_stream(client, params, options, model)
            stream.push(StartEvent(partial=output))

            text_block: TextContent | None = None
            thinking_block: ThinkingContent | None = None
            has_finish_reason = False
            tool_call_index_by_stream_index: dict[int, int] = {}
            tool_call_index_by_id: dict[str, int] = {}
            tool_call_partial_args: dict[int, str] = {}

            def content_index_for(block: TextContent | ThinkingContent | ToolCall) -> int:
                return output.content.index(block)

            def finish_block(block: TextContent | ThinkingContent | ToolCall) -> None:
                index = content_index_for(block)
                if isinstance(block, TextContent):
                    stream.push(TextEndEvent(contentIndex=index, content=block.text, partial=output))
                elif isinstance(block, ThinkingContent):
                    stream.push(ThinkingEndEvent(contentIndex=index, content=block.thinking, partial=output))
                else:
                    partial_args = tool_call_partial_args.get(index, "")
                    if partial_args:
                        block.arguments = parse_streaming_json(partial_args)
                    stream.push(ToolCallEndEvent(contentIndex=index, toolCall=block, partial=output))

            def ensure_text_block() -> TextContent:
                nonlocal text_block
                if text_block is None:
                    text_block = TextContent(text="")
                    output.content.append(text_block)
                    stream.push(TextStartEvent(contentIndex=content_index_for(text_block), partial=output))
                return text_block

            def ensure_thinking_block(thinking_signature: str) -> ThinkingContent:
                nonlocal thinking_block
                if thinking_block is None:
                    thinking_block = ThinkingContent(thinking="", thinkingSignature=thinking_signature)
                    output.content.append(thinking_block)
                    stream.push(ThinkingStartEvent(contentIndex=content_index_for(thinking_block), partial=output))
                return thinking_block

            def ensure_tool_call_block(tool_call: Mapping[str, Any]) -> tuple[int, ToolCall]:
                stream_index = tool_call.get("index")
                existing_index: int | None = None
                if isinstance(stream_index, int):
                    existing_index = tool_call_index_by_stream_index.get(stream_index)
                if existing_index is None and isinstance(tool_call.get("id"), str):
                    existing_index = tool_call_index_by_id.get(tool_call["id"])

                if existing_index is None:
                    block = ToolCall(
                        id=str(tool_call.get("id") or ""),
                        name=str((tool_call.get("function") or {}).get("name") or ""),
                        arguments={},
                    )
                    output.content.append(block)
                    existing_index = len(output.content) - 1
                    stream.push(ToolCallStartEvent(contentIndex=existing_index, partial=output))
                else:
                    block = output.content[existing_index]
                    if not isinstance(block, ToolCall):
                        raise RuntimeError("Tool call stream index collided with non-tool block")

                if isinstance(stream_index, int):
                    tool_call_index_by_stream_index[stream_index] = existing_index
                if isinstance(tool_call.get("id"), str):
                    tool_call_index_by_id[tool_call["id"]] = existing_index
                return existing_index, block

            async for raw_chunk in _iterate_stream(openai_stream, _option(options, "signal")):
                if not isinstance(raw_chunk, Mapping):
                    continue

                chunk_id = raw_chunk.get("id")
                if isinstance(chunk_id, str) and not output.responseId:
                    output.responseId = chunk_id
                chunk_model = raw_chunk.get("model")
                if isinstance(chunk_model, str) and chunk_model and chunk_model != model.id:
                    output.responseModel = chunk_model

                usage = raw_chunk.get("usage")
                if isinstance(usage, Mapping):
                    output.usage = parse_chunk_usage(usage, model)

                choices = raw_chunk.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, Mapping):
                    continue

                if usage is None and isinstance(choice.get("usage"), Mapping):
                    output.usage = parse_chunk_usage(choice["usage"], model)

                finish_reason = choice.get("finish_reason")
                if finish_reason is not None:
                    finish_reason_result = map_stop_reason(str(finish_reason))
                    output.stopReason = finish_reason_result["stopReason"]
                    if finish_reason_result.get("errorMessage"):
                        output.errorMessage = finish_reason_result["errorMessage"]
                    has_finish_reason = True

                delta = choice.get("delta")
                if not isinstance(delta, Mapping):
                    continue

                content_delta = delta.get("content")
                if isinstance(content_delta, str) and content_delta:
                    block = ensure_text_block()
                    block.text += content_delta
                    stream.push(TextDeltaEvent(contentIndex=content_index_for(block), delta=content_delta, partial=output))

                reasoning_fields = ("reasoning_content", "reasoning", "reasoning_text")
                found_reasoning_field: str | None = None
                for field in reasoning_fields:
                    value = delta.get(field)
                    if isinstance(value, str) and value:
                        found_reasoning_field = field
                        break

                if found_reasoning_field:
                    reasoning_delta = delta.get(found_reasoning_field)
                    if isinstance(reasoning_delta, str) and reasoning_delta:
                        thinking_signature = (
                            "reasoning_content"
                            if model.provider == "opencode-go" and found_reasoning_field == "reasoning"
                            else found_reasoning_field
                        )
                        block = ensure_thinking_block(thinking_signature)
                        block.thinking += reasoning_delta
                        stream.push(
                            ThinkingDeltaEvent(
                                contentIndex=content_index_for(block),
                                delta=reasoning_delta,
                                partial=output,
                            )
                        )

                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        if not isinstance(tool_call, Mapping):
                            continue
                        content_index, block = ensure_tool_call_block(tool_call)
                        tool_id = tool_call.get("id")
                        function = tool_call.get("function")
                        if not block.id and isinstance(tool_id, str):
                            block.id = tool_id
                            tool_call_index_by_id[tool_id] = content_index
                        if not block.name and isinstance(function, Mapping) and isinstance(function.get("name"), str):
                            block.name = function["name"]

                        tool_delta = ""
                        if isinstance(function, Mapping) and isinstance(function.get("arguments"), str):
                            tool_delta = function["arguments"]
                            tool_call_partial_args[content_index] = tool_call_partial_args.get(content_index, "") + tool_delta
                            block.arguments = parse_streaming_json(tool_call_partial_args[content_index])
                        stream.push(ToolCallDeltaEvent(contentIndex=content_index, delta=tool_delta, partial=output))

                reasoning_details = delta.get("reasoning_details")
                if isinstance(reasoning_details, list):
                    for detail in reasoning_details:
                        if not isinstance(detail, Mapping):
                            continue
                        if detail.get("type") == "reasoning.encrypted" and isinstance(detail.get("id"), str) and detail.get("data"):
                            tool_index = tool_call_index_by_id.get(detail["id"])
                            if tool_index is None:
                                continue
                            block = output.content[tool_index]
                            if isinstance(block, ToolCall):
                                block.thoughtSignature = json.dumps(detail, separators=(",", ":"))

            for block in list(output.content):
                finish_block(block)

            if _is_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")
            if output.stopReason == "aborted":
                raise RuntimeError("Request was aborted")
            if output.stopReason == "error":
                raise RuntimeError(output.errorMessage or "Provider returned an error stop reason")
            if not has_finish_reason:
                raise RuntimeError("Stream ended without finish_reason")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            output.stopReason = "aborted" if _is_aborted(_option(options, "signal")) else "error"
            output.errorMessage = _format_completion_error(error)
            error_payload = getattr(error, "error", None)
            raw_metadata = (
                error_payload.get("metadata")
                if isinstance(error_payload, Mapping)
                else getattr(error_payload, "metadata", None)
            )
            if isinstance(raw_metadata, Mapping) and raw_metadata.get("raw"):
                output.errorMessage = f"{output.errorMessage}\n{raw_metadata['raw']}"
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
        finally:
            stream.end()

    spawn_stream_task(run())
    return stream


def stream_simple_openai_completions(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    clamped_reasoning = clamp_thinking_level(model, options.reasoning) if options and options.reasoning else None
    reasoning_effort = None if clamped_reasoning == "off" else clamped_reasoning
    tool_choice = _option(options, "toolChoice")
    return stream_openai_completions(
        model,
        context,
        {**base.model_dump(), "reasoningEffort": reasoning_effort, "toolChoice": tool_choice},
    )


def create_client(
    model: Model,
    context: Context,
    api_key: str | None = None,
    options_headers: Mapping[str, str] | None = None,
    session_id: str | None = None,
    compat: Mapping[str, Any] | None = None,
) -> AsyncOpenAI:
    if not api_key:
        env_key = os.environ.get("OPENAI_API_KEY")
        if not env_key:
            raise RuntimeError(
                "OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it as an argument."
            )
        api_key = env_key

    compat = compat or get_compat(model)
    headers = dict(model.headers or {})
    if model.provider == "github-copilot":
        headers.update(
            build_copilot_dynamic_headers(
                messages=context.messages,
                hasImages=has_copilot_vision_input(context.messages),
            )
        )

    if session_id and compat.get("sendSessionAffinityHeaders"):
        headers["session_id"] = session_id
        headers["x-client-request-id"] = session_id
        headers["x-session-affinity"] = session_id

    if options_headers:
        headers.update(dict(options_headers))

    default_headers: dict[str, Any]
    if model.provider == "cloudflare-ai-gateway":
        default_headers = {
            **headers,
            "Authorization": headers.get("Authorization"),
            "cf-aig-authorization": f"Bearer {api_key}",
        }
    else:
        default_headers = headers

    return require(AsyncOpenAI, "openai")(
        api_key=api_key,
        base_url=resolve_cloudflare_base_url(model) if is_cloudflare_provider(model.provider) else model.baseUrl,
        default_headers=default_headers,
    )


def build_params(
    model: Model,
    context: Context,
    options: Any = None,
    compat: Mapping[str, Any] | None = None,
    cache_retention: CacheRetention | None = None,
) -> dict[str, Any]:
    compat = compat or get_compat(model)
    resolved_cache_retention = resolve_cache_retention(_option(options, "cacheRetention") if cache_retention is None else cache_retention)
    messages = convert_messages(model, context, compat)
    cache_control = get_compat_cache_control(compat, resolved_cache_retention)

    params: dict[str, Any] = {
        "model": model.id,
        "messages": messages,
        "stream": True,
        "prompt_cache_key": (
            clamp_openai_prompt_cache_key(_option(options, "sessionId"))
            if (
                ("api.openai.com" in model.baseUrl and resolved_cache_retention != "none")
                or (resolved_cache_retention == "long" and compat.get("supportsLongCacheRetention"))
            )
            else None
        ),
    }

    if resolved_cache_retention == "long" and compat.get("supportsLongCacheRetention"):
        _set_extra(params, "prompt_cache_retention", "24h")

    if compat.get("supportsUsageInStreaming") is not False:
        params["stream_options"] = {"include_usage": True}
    if compat.get("supportsStore"):
        params["store"] = False

    max_tokens = _option(options, "maxTokens")
    if max_tokens:
        if compat.get("maxTokensField") == "max_tokens":
            params["max_tokens"] = max_tokens
        else:
            params["max_completion_tokens"] = max_tokens

    if _option(options, "temperature") is not None:
        params["temperature"] = _option(options, "temperature")

    # Sampling passthrough (pi #7568): model-level defaults, request-level overrides.
    # Keys already set explicitly above (temperature, max_tokens, ...) win.
    sampling: dict[str, Any] = {}
    model_sampling = getattr(context.model, "samplingParams", None)
    if isinstance(model_sampling, dict):
        sampling.update(model_sampling)
    request_sampling = _option(options, "samplingParams")
    if isinstance(request_sampling, dict):
        sampling.update(request_sampling)
    for key, value in sampling.items():
        if value is not None and key not in params:
            params[key] = value

    if context.tools:
        params["tools"] = convert_tools(context.tools, compat)
        if compat.get("zaiToolStream"):
            _set_extra(params, "tool_stream", True)
    elif has_tool_history(context.messages):
        params["tools"] = []

    if cache_control:
        apply_anthropic_cache_control(messages, params.get("tools"), cache_control)

    tool_choice = _option(options, "toolChoice")
    if tool_choice:
        params["tool_choice"] = tool_choice

    reasoning_effort = _option(options, "reasoningEffort")
    if compat.get("thinkingFormat") in {"zai", "qwen"} and model.reasoning:
        _set_extra(params, "enable_thinking", bool(reasoning_effort))
    elif compat.get("thinkingFormat") == "qwen-chat-template" and model.reasoning:
        _set_extra(params, "chat_template_kwargs", {"enable_thinking": bool(reasoning_effort), "preserve_thinking": True})
    elif compat.get("thinkingFormat") == "deepseek" and model.reasoning:
        _set_extra(params, "thinking", {"type": "enabled" if reasoning_effort else "disabled"})
        if reasoning_effort:
            params["reasoning_effort"] = (
                model.thinkingLevelMap.get(reasoning_effort, reasoning_effort)
                if model.thinkingLevelMap
                else reasoning_effort
            )
    elif compat.get("thinkingFormat") == "openrouter" and model.reasoning:
        if reasoning_effort:
            _set_extra(params, "reasoning", {
                "effort": model.thinkingLevelMap.get(reasoning_effort, reasoning_effort)
                if model.thinkingLevelMap
                else reasoning_effort
            })
        else:
            has_off_override = False
            off_value: Any = None
            if model.thinkingLevelMap is not None:
                has_off_override = "off" in model.thinkingLevelMap
                off_value = model.thinkingLevelMap.get("off")
            if model.thinkingLevelMap is None or not has_off_override or off_value is not None:
                _set_extra(params, "reasoning", {"effort": "none" if off_value is None else off_value})
    elif compat.get("thinkingFormat") == "together" and model.reasoning:
        _set_extra(params, "reasoning", {"enabled": bool(reasoning_effort)})
        if reasoning_effort and compat.get("supportsReasoningEffort"):
            params["reasoning_effort"] = (
                model.thinkingLevelMap.get(reasoning_effort, reasoning_effort)
                if model.thinkingLevelMap
                else reasoning_effort
            )
    elif reasoning_effort and model.reasoning and compat.get("supportsReasoningEffort"):
        params["reasoning_effort"] = (
            model.thinkingLevelMap.get(reasoning_effort, reasoning_effort)
            if model.thinkingLevelMap
            else reasoning_effort
        )
    elif not reasoning_effort and model.reasoning and compat.get("supportsReasoningEffort"):
        off_value = model.thinkingLevelMap.get("off") if model.thinkingLevelMap else None
        if isinstance(off_value, str):
            params["reasoning_effort"] = off_value

    if "openrouter.ai" in model.baseUrl and _compat_value(model.compat, "openRouterRouting"):
        _set_extra(params, "provider", _dump_model(_compat_value(model.compat, "openRouterRouting")))
    if "ai-gateway.vercel.sh" in model.baseUrl and _compat_value(model.compat, "vercelGatewayRouting"):
        routing = _dump_model(_compat_value(model.compat, "vercelGatewayRouting"))
        gateway_options: dict[str, list[str]] = {}
        if routing.get("only"):
            gateway_options["only"] = routing["only"]
        if routing.get("order"):
            gateway_options["order"] = routing["order"]
        if gateway_options:
            _set_extra(params, "providerOptions", {"gateway": gateway_options})

    return {key: value for key, value in params.items() if value is not None}


def get_compat_cache_control(
    compat: Mapping[str, Any],
    cache_retention: CacheRetention,
) -> dict[str, Any] | None:
    if compat.get("cacheControlFormat") != "anthropic" or cache_retention == "none":
        return None
    ttl = "1h" if cache_retention == "long" and compat.get("supportsLongCacheRetention") else None
    cache_control: dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        cache_control["ttl"] = ttl
    return cache_control


def apply_anthropic_cache_control(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    cache_control: dict[str, Any],
) -> None:
    add_cache_control_to_system_prompt(messages, cache_control)
    add_cache_control_to_last_tool(tools, cache_control)
    add_cache_control_to_last_conversation_message(messages, cache_control)


def add_cache_control_to_system_prompt(messages: list[dict[str, Any]], cache_control: dict[str, Any]) -> None:
    for message in messages:
        if message.get("role") in {"system", "developer"}:
            add_cache_control_to_text_content(message, cache_control)
            return


def add_cache_control_to_last_conversation_message(messages: list[dict[str, Any]], cache_control: dict[str, Any]) -> None:
    for message in reversed(messages):
        if message.get("role") in {"user", "assistant"} and add_cache_control_to_text_content(message, cache_control):
            return


def add_cache_control_to_last_tool(tools: list[dict[str, Any]] | None, cache_control: dict[str, Any]) -> None:
    if tools:
        tools[-1]["cache_control"] = cache_control


def add_cache_control_to_text_content(message: dict[str, Any], cache_control: dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return False
        message["content"] = [{"type": "text", "text": content, "cache_control": cache_control}]
        return True
    if not isinstance(content, list):
        return False
    for part in reversed(content):
        if isinstance(part, dict) and part.get("type") == "text":
            part["cache_control"] = cache_control
            return True
    return False


def convert_messages(
    model: Model,
    context: Context,
    compat: Mapping[str, Any],
) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []

    def normalize_tool_call_id(tool_call_id: str, _target_model: Model, _source: AssistantMessage) -> str:
        if "|" in tool_call_id:
            call_id, _, _ = tool_call_id.partition("|")
            return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in call_id)[:40]
        if model.provider == "openai":
            return tool_call_id[:40]
        return tool_call_id

    transformed_messages = transform_messages(context.messages, model, normalize_tool_call_id)

    if context.systemPrompt:
        role = "developer" if model.reasoning and compat.get("supportsDeveloperRole") else "system"
        params.append({"role": role, "content": sanitize_surrogates(context.systemPrompt)})

    last_role: str | None = None
    index = 0
    while index < len(transformed_messages):
        message = transformed_messages[index]
        if compat.get("requiresAssistantAfterToolResult") and last_role == "toolResult" and message.role == "user":
            params.append({"role": "assistant", "content": "I have processed the tool results."})

        if message.role == "user":
            if isinstance(message.content, str):
                params.append({"role": "user", "content": sanitize_surrogates(message.content)})
            else:
                content: list[dict[str, Any]] = []
                for item in message.content:
                    if item.type == "text":
                        content.append({"type": "text", "text": sanitize_surrogates(item.text)})
                    else:
                        content.append(
                            {"type": "image_url", "image_url": {"url": f"data:{item.mimeType};base64,{item.data}"}}
                        )
                if content:
                    params.append({"role": "user", "content": content})
            last_role = message.role
            index += 1
            continue

        if message.role == "assistant":
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": "" if compat.get("requiresAssistantAfterToolResult") else None,
            }

            assistant_text_parts = [
                {"type": "text", "text": sanitize_surrogates(block.text)}
                for block in message.content
                if block.type == "text" and block.text.strip()
            ]
            assistant_text = "".join(part["text"] for part in assistant_text_parts)

            non_empty_thinking_blocks = [
                block for block in message.content if block.type == "thinking" and block.thinking.strip()
            ]
            if non_empty_thinking_blocks:
                if compat.get("requiresThinkingAsText"):
                    thinking_text = "\n\n".join(sanitize_surrogates(block.thinking) for block in non_empty_thinking_blocks)
                    assistant_message["content"] = [{"type": "text", "text": thinking_text}, *assistant_text_parts]
                else:
                    if assistant_text:
                        assistant_message["content"] = assistant_text
                    signature = non_empty_thinking_blocks[0].thinkingSignature
                    if model.provider == "opencode-go" and signature == "reasoning":
                        signature = "reasoning_content"
                    if signature:
                        assistant_message[signature] = "\n".join(block.thinking for block in non_empty_thinking_blocks)
            elif assistant_text:
                assistant_message["content"] = assistant_text

            tool_calls = [block for block in message.content if block.type == "toolCall"]
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.arguments)},
                    }
                    for tool_call in tool_calls
                ]
                reasoning_details = []
                for tool_call in tool_calls:
                    if not tool_call.thoughtSignature:
                        continue
                    try:
                        reasoning_details.append(json.loads(tool_call.thoughtSignature))
                    except ValueError:
                        continue
                if reasoning_details:
                    assistant_message["reasoning_details"] = reasoning_details

            if (
                compat.get("requiresReasoningContentOnAssistantMessages")
                and model.reasoning
                and "reasoning_content" not in assistant_message
            ):
                assistant_message["reasoning_content"] = ""

            content = assistant_message.get("content")
            has_content = content is not None and (len(content) > 0 if isinstance(content, (str, list)) else True)
            if not has_content and "tool_calls" not in assistant_message:
                index += 1
                continue
            params.append(assistant_message)
            last_role = message.role
            index += 1
            continue

        if message.role == "toolResult":
            image_blocks: list[dict[str, Any]] = []
            lookahead = index
            while lookahead < len(transformed_messages) and transformed_messages[lookahead].role == "toolResult":
                tool_message = transformed_messages[lookahead]
                if not isinstance(tool_message, ToolResultMessage):
                    break

                text_result = "\n".join(block.text for block in tool_message.content if block.type == "text")
                has_images = any(block.type == "image" for block in tool_message.content)
                tool_result_message: dict[str, Any] = {
                    "role": "tool",
                    "content": sanitize_surrogates(text_result if text_result else "(see attached image)"),
                    "tool_call_id": tool_message.toolCallId,
                }
                if compat.get("requiresToolResultName") and tool_message.toolName:
                    tool_result_message["name"] = tool_message.toolName
                params.append(tool_result_message)

                if has_images and "image" in model.input:
                    for block in tool_message.content:
                        if block.type == "image":
                            image_blocks.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{block.mimeType};base64,{block.data}"},
                                }
                            )
                lookahead += 1

            if image_blocks:
                if compat.get("requiresAssistantAfterToolResult"):
                    params.append({"role": "assistant", "content": "I have processed the tool results."})
                params.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Attached image(s) from tool result:"}, *image_blocks],
                    }
                )
                last_role = "user"
            else:
                last_role = "toolResult"
            index = lookahead
            continue

        index += 1

    return params


def convert_tools(tools: list[Tool], compat: Mapping[str, Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function_spec: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters_json_schema(),
        }
        if compat.get("supportsStrictMode") is not False:
            function_spec["strict"] = False
        converted.append({"type": "function", "function": function_spec})
    return converted


def parse_chunk_usage(raw_usage: Mapping[str, Any], model: Model) -> Usage:
    prompt_tokens = int(raw_usage.get("prompt_tokens") or 0)
    prompt_details = raw_usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, Mapping) else {}
    cache_read_tokens = int(prompt_details.get("cached_tokens") or raw_usage.get("prompt_cache_hit_tokens") or 0)
    cache_write_tokens = int(prompt_details.get("cache_write_tokens") or 0)
    input_tokens = max(0, prompt_tokens - cache_read_tokens - cache_write_tokens)
    output_tokens = int(raw_usage.get("completion_tokens") or 0)

    usage = Usage(
        input=input_tokens,
        output=output_tokens,
        cacheRead=cache_read_tokens,
        cacheWrite=cache_write_tokens,
        totalTokens=input_tokens + output_tokens + cache_read_tokens + cache_write_tokens,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )
    calculate_cost(model, usage)
    return usage


def map_stop_reason(reason: str | None) -> dict[str, str]:
    if reason is None:
        return {"stopReason": "stop"}
    if reason in {"stop", "end"}:
        return {"stopReason": "stop"}
    if reason == "length":
        return {"stopReason": "length"}
    if reason in {"function_call", "tool_calls"}:
        return {"stopReason": "toolUse"}
    if reason == "content_filter":
        return {"stopReason": "error", "errorMessage": "Provider finish_reason: content_filter"}
    if reason == "network_error":
        return {"stopReason": "error", "errorMessage": "Provider finish_reason: network_error"}
    return {"stopReason": "error", "errorMessage": f"Provider finish_reason: {reason}"}


def detect_compat(model: Model) -> dict[str, Any]:
    provider = model.provider
    base_url = model.baseUrl

    is_zai = provider == "zai" or "api.z.ai" in base_url
    is_together = provider == "together" or "api.together.ai" in base_url or "api.together.xyz" in base_url
    is_moonshot = provider in {"moonshotai", "moonshotai-cn"} or "api.moonshot." in base_url
    is_cloudflare_workers_ai = provider == "cloudflare-workers-ai" or "api.cloudflare.com" in base_url
    is_cloudflare_ai_gateway = provider == "cloudflare-ai-gateway" or "gateway.ai.cloudflare.com" in base_url
    is_non_standard = (
        provider == "cerebras"
        or "cerebras.ai" in base_url
        or provider == "xai"
        or "api.x.ai" in base_url
        or is_together
        or "chutes.ai" in base_url
        or "deepseek.com" in base_url
        or is_zai
        or is_moonshot
        or provider == "opencode"
        or "opencode.ai" in base_url
        or is_cloudflare_workers_ai
        or is_cloudflare_ai_gateway
    )
    use_max_tokens = "chutes.ai" in base_url or is_moonshot or is_cloudflare_ai_gateway or is_together
    is_grok = provider == "xai" or "api.x.ai" in base_url
    is_deepseek = provider == "deepseek" or "deepseek.com" in base_url
    cache_control_format = "anthropic" if provider == "openrouter" and model.id.startswith("anthropic/") else None

    return {
        "supportsStore": not is_non_standard,
        "supportsDeveloperRole": not is_non_standard,
        "supportsReasoningEffort": not (is_grok or is_zai or is_moonshot or is_together or is_cloudflare_ai_gateway),
        "supportsUsageInStreaming": True,
        "maxTokensField": "max_tokens" if use_max_tokens else "max_completion_tokens",
        "requiresToolResultName": False,
        "requiresAssistantAfterToolResult": False,
        "requiresThinkingAsText": False,
        "requiresReasoningContentOnAssistantMessages": is_deepseek,
        "thinkingFormat": (
            "deepseek"
            if is_deepseek
            else "zai"
            if is_zai
            else "together"
            if is_together
            else "openrouter"
            if provider == "openrouter" or "openrouter.ai" in base_url
            else "openai"
        ),
        "openRouterRouting": {},
        "vercelGatewayRouting": {},
        "zaiToolStream": False,
        "supportsStrictMode": not (is_moonshot or is_together or is_cloudflare_ai_gateway),
        "cacheControlFormat": cache_control_format,
        "sendSessionAffinityHeaders": False,
        "supportsLongCacheRetention": not (is_together or is_cloudflare_workers_ai or is_cloudflare_ai_gateway),
    }


def get_compat(model: Model) -> dict[str, Any]:
    detected = detect_compat(model)
    compat = model.compat
    if compat is None:
        return detected

    return {
        "supportsStore": _compat_value(compat, "supportsStore", detected["supportsStore"]),
        "supportsDeveloperRole": _compat_value(compat, "supportsDeveloperRole", detected["supportsDeveloperRole"]),
        "supportsReasoningEffort": _compat_value(compat, "supportsReasoningEffort", detected["supportsReasoningEffort"]),
        "supportsUsageInStreaming": _compat_value(compat, "supportsUsageInStreaming", detected["supportsUsageInStreaming"]),
        "maxTokensField": _compat_value(compat, "maxTokensField", detected["maxTokensField"]),
        "requiresToolResultName": _compat_value(compat, "requiresToolResultName", detected["requiresToolResultName"]),
        "requiresAssistantAfterToolResult": _compat_value(
            compat, "requiresAssistantAfterToolResult", detected["requiresAssistantAfterToolResult"]
        ),
        "requiresThinkingAsText": _compat_value(compat, "requiresThinkingAsText", detected["requiresThinkingAsText"]),
        "requiresReasoningContentOnAssistantMessages": _compat_value(
            compat,
            "requiresReasoningContentOnAssistantMessages",
            detected["requiresReasoningContentOnAssistantMessages"],
        ),
        "thinkingFormat": _compat_value(compat, "thinkingFormat", detected["thinkingFormat"]),
        "openRouterRouting": _compat_value(compat, "openRouterRouting", {}),
        "vercelGatewayRouting": _compat_value(compat, "vercelGatewayRouting", detected["vercelGatewayRouting"]),
        "zaiToolStream": _compat_value(compat, "zaiToolStream", detected["zaiToolStream"]),
        "supportsStrictMode": _compat_value(compat, "supportsStrictMode", detected["supportsStrictMode"]),
        "cacheControlFormat": _compat_value(compat, "cacheControlFormat", detected["cacheControlFormat"]),
        "sendSessionAffinityHeaders": _compat_value(
            compat, "sendSessionAffinityHeaders", detected["sendSessionAffinityHeaders"]
        ),
        "supportsLongCacheRetention": _compat_value(
            compat, "supportsLongCacheRetention", detected["supportsLongCacheRetention"]
        ),
    }


async def _create_completion_stream(client: Any, params: dict[str, Any], options: Any, model: Model) -> Any:
    signal = _option(options, "signal")
    request_client = client
    request_client_kwargs: dict[str, Any] = {}
    timeout_ms = _option(options, "timeoutMs")
    if timeout_ms is not None:
        request_client_kwargs["timeout"] = timeout_ms / 1000
    max_retries = _option(options, "maxRetries")
    if max_retries is not None:
        request_client_kwargs["max_retries"] = max_retries
    if request_client_kwargs and hasattr(client, "with_options"):
        request_client = client.with_options(**request_client_kwargs)

    if hasattr(getattr(getattr(request_client, "chat", None), "completions", None), "with_raw_response"):
        raw_response = await _await_maybe_with_signal(
            request_client.chat.completions.with_raw_response.create(**params),
            signal,
        )
        on_response = _option(options, "onResponse")
        if callable(on_response):
            await _maybe_await(
                on_response(
                    {
                        "status": raw_response.http_response.status_code,
                        "headers": headers_to_record(raw_response.http_response.headers),
                    },
                    model,
                )
            )
        return await _await_maybe_with_signal(raw_response.parse(), signal)

    created = await _await_maybe_with_signal(request_client.chat.completions.create(**params), signal)
    if hasattr(created, "withResponse"):
        wrapped = await _await_maybe_with_signal(created.withResponse(), signal)
        on_response = _option(options, "onResponse")
        if callable(on_response):
            await _maybe_await(
                on_response(
                    {"status": wrapped["response"].status, "headers": headers_to_record(wrapped["response"].headers)},
                    model,
                )
            )
        return wrapped["data"]
    return created


async def _iterate_stream(stream_obj: Any, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    iterator = stream_obj.__aiter__()
    while True:
        try:
            chunk = await _await_with_signal(iterator.__anext__(), signal, on_abort=lambda: _close_stream(stream_obj))
        except StopAsyncIteration:
            return

        if hasattr(chunk, "model_dump"):
            dumped = chunk.model_dump()
            if isinstance(dumped, dict):
                yield dumped
                continue
        if isinstance(chunk, dict):
            yield chunk
            continue
        yield json.loads(json.dumps(chunk, default=lambda value: value.__dict__))


def _format_completion_error(error: Any) -> str:
    return str(error) if isinstance(error, Exception) else json.dumps(error, default=str)


streamOpenAICompletions = stream_openai_completions
streamSimpleOpenAICompletions = stream_simple_openai_completions
__all__ = [
    "OpenAICompletionsOptions",
    "apply_anthropic_cache_control",
    "build_params",
    "convert_messages",
    "convert_tools",
    "create_client",
    "detect_compat",
    "get_compat",
    "get_compat_cache_control",
    "has_tool_history",
    "map_stop_reason",
    "parse_chunk_usage",
    "resolve_cache_retention",
    "streamOpenAICompletions",
    "streamSimpleOpenAICompletions",
    "stream_openai_completions",
    "stream_simple_openai_completions",
]
