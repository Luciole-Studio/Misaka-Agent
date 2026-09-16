"""OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Iterable, Mapping
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
    _empty_usage,
    _option,
    resolve_cache_retention,
)
from misaka.ai.providers.cloudflare import (
    is_cloudflare_provider,
    resolve_cloudflare_base_url,
)
from misaka.ai.providers.constrained_sampling import (
    GrammarToolInputJsonBuffer,
    append_grammar_tool_input_json_delta,
    create_grammar_tool_input_properties,
    get_grammar_tool_input,
    get_json_schema_tool_parameters,
    resolve_grammar_constrained_sampling,
    resolve_json_schema_strict_sampling,
)
from misaka.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from misaka.ai.providers.openai_prompt_cache import clamp_openai_prompt_cache_key
from misaka.ai.providers.sdk import require
from misaka.ai.providers.simple_options import (
    build_base_options,
    clamp_thinking_budget_to_answer_room,
    thinking_budget_for_level,
)
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
from misaka.ai.utils.headers import (
    apply_provider_headers,
    headers_to_record,
    provider_headers_to_record,
)
from misaka.ai.utils.json_parse import StreamingArgs
from misaka.ai.utils.provider_retry import retry_provider_request
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates
from misaka.ai.utils.user_agent import get_misaka_user_agent
from misaka.utils.values import maybe_await, read_field, signal_aborted


def _set_extra(params: dict[str, Any], key: str, value: Any) -> None:
    """Route a non-standard param through extra_body.

    Python equivalent of TypeScript's (params as any).key = value,
    which passes unknown properties through to the API request body.
    """
    if "extra_body" not in params:
        params["extra_body"] = {}
    params["extra_body"][key] = value


def _resolve_thinking_token_budget_field(compat: Mapping[str, Any]) -> str | None:
    """Which top-level field carries the thinking budget, if any."""
    field = compat.get("thinkingTokenBudgetField")
    if field:
        return field
    return "thinking_token_budget" if compat.get("supportsThinkingTokenBudget") else None


def _resolve_clamped_thinking_budget(model: Model, options: Any, params: Mapping[str, Any]) -> int | None:
    """The budget this request may spend on thinking, clamped to leave room for an answer."""
    reasoning_effort = _option(options, "reasoningEffort")
    if not reasoning_effort or not model.reasoning:
        return None
    ceiling = params.get("max_tokens")
    if ceiling is None:
        ceiling = params.get("max_completion_tokens")
    if ceiling is None:
        ceiling = model.maxTokens
    budget = clamp_thinking_budget_to_answer_room(
        thinking_budget_for_level(reasoning_effort, _option(options, "thinkingBudgets")), ceiling
    )
    return budget if budget > 0 else None


_OMIT = object()  # "do not send this key", which `None` cannot express


def _resolve_chat_template_value(model: Model, reasoning_effort: Any, value: Any, thinking_budget: Any) -> Any:
    """One ``chat_template_kwargs`` entry, with ``{"$var": ...}`` placeholders resolved.

    A literal passes through. An object is a placeholder: ``thinking.enabled`` becomes the
    boolean, ``thinking.budget`` the token budget, anything else the mapped thinking level.
    ``omitWhenOff`` drops the entry entirely when thinking is off, which is how a template
    that has no "off" spelling stays silent instead of sending a wrong one.

    Returns ``_OMIT`` for "do not send this key" -- distinct from ``None``, which is a
    value a template may legitimately want.
    """
    if not isinstance(value, Mapping):
        return value
    if not reasoning_effort and value.get("omitWhenOff"):
        return _OMIT
    variable = value.get("$var")
    if variable == "thinking.enabled":
        return bool(reasoning_effort)
    if variable == "thinking.budget":
        return thinking_budget
    mapped = _mapped_thinking_effort(model, reasoning_effort) if reasoning_effort else (
        model.thinkingLevelMap.get("off") if model.thinkingLevelMap else None
    )
    if reasoning_effort:
        # `mapped === undefined ? reasoningEffort : typeof mapped === "string" ? mapped : undefined`
        return mapped if isinstance(mapped, str) else (reasoning_effort if mapped is reasoning_effort else _OMIT)
    return mapped if isinstance(mapped, str) else _OMIT


def _build_chat_template_values(
    model: Model, reasoning_effort: Any, values: Any, thinking_budget: Any
) -> dict[str, Any] | None:
    """Every entry resolved; ``None`` when nothing survives, so the key is not sent at all."""
    if not isinstance(values, Mapping):
        return None
    resolved = {
        key: value
        for key, value in (
            (key, _resolve_chat_template_value(model, reasoning_effort, raw, thinking_budget))
            for key, raw in values.items()
        )
        if value is not _OMIT
    }
    return resolved or None


def _mapped_thinking_effort(model: Model, level: str) -> Any:
    """pi's ``mapped === undefined ? level : mapped``: an explicit null suppresses the field."""
    if model.thinkingLevelMap is None or level not in model.thinkingLevelMap:
        return level
    return model.thinkingLevelMap[level]


def _coalesced_thinking_effort(model: Model, level: str) -> Any:
    """pi's ``mapped ?? level``: a null mapping falls back to the requested level."""
    mapped = model.thinkingLevelMap.get(level) if model.thinkingLevelMap else None
    return level if mapped is None else mapped


def _thinking_off_is_suppressed(model: Model) -> bool:
    """pi's ``model.thinkingLevelMap?.off !== null`` guard: an explicit null means "send nothing"."""
    return model.thinkingLevelMap is not None and model.thinkingLevelMap.get("off", "") is None


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
    reasoningEffort: Literal["minimal", "low", "medium", "high", "xhigh", "max"]


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
                cache_retention = resolve_cache_retention(_option(options, "cacheRetention"), _option(options, "env"))
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
                next_params = await maybe_await(on_payload(params, model))
                if next_params is not None:
                    params = next_params

            openai_stream = await retry_provider_request(
                lambda: _create_completion_stream(client, params, options, model),
                max_retries=_option(options, "maxRetries") or 0,
                max_retry_delay_ms=_option(options, "maxRetryDelayMs"),
                signal=_option(options, "signal"),
            )
            stream.push(StartEvent(partial=output))

            text_block: TextContent | None = None
            thinking_block: ThinkingContent | None = None
            has_finish_reason = False
            tool_call_index_by_stream_index: dict[int, int] = {}
            tool_call_index_by_id: dict[str, int] = {}
            tool_call_partial_args: dict[int, StreamingArgs] = {}
            # One JSON re-encoder per grammar tool call in flight.
            tool_call_custom_buffers: dict[int, GrammarToolInputJsonBuffer] = {}
            grammar_tool_input_properties = create_grammar_tool_input_properties(
                context.tools, bool(get_compat(model).get("supportsOpenAIGrammarTools"))
            )

            def content_index_for(block: TextContent | ThinkingContent | ToolCall) -> int:
                return output.content.index(block)

            def finish_block(block: TextContent | ThinkingContent | ToolCall) -> None:
                index = content_index_for(block)
                if isinstance(block, TextContent):
                    stream.push(TextEndEvent(contentIndex=index, content=block.text, partial=output))
                elif isinstance(block, ThinkingContent):
                    stream.push(ThinkingEndEvent(contentIndex=index, content=block.thinking, partial=output))
                else:
                    partial_args = tool_call_partial_args.get(index)
                    if partial_args is not None and partial_args.raw:
                        partial_args.finish_into(block)
                    stream.push(ToolCallEndEvent(contentIndex=index, toolCall=block, partial=output))

            def ensure_text_block() -> TextContent:
                nonlocal text_block
                if text_block is None:
                    text_block = TextContent(text="")
                    output.content.append(text_block)
                    stream.push(TextStartEvent(contentIndex=content_index_for(text_block), partial=output))
                return text_block

            # Every `reasoning_details` entry this stream carried, merged as upstream merges
            # them, and stashed on the thinking block's signature when the stream ends.
            streamed_reasoning_details: list[dict[str, Any]] = []

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
                        custom = tool_call.get("custom")
                        is_custom = isinstance(custom, Mapping) and not isinstance(function, Mapping)
                        if not block.name:
                            for source in (function, custom):
                                if isinstance(source, Mapping) and isinstance(source.get("name"), str):
                                    block.name = source["name"]
                                    break

                        tool_delta = ""
                        if is_custom:
                            # A grammar tool streams free text, not JSON. It is surfaced as an
                            # ordinary tool call whose single argument is that text, and the
                            # delta is re-encoded as the growing JSON of that one property --
                            # consumers of `toolcall_delta` are fed JSON either way.
                            property_name = grammar_tool_input_properties.get(block.name) or "input"
                            buffer = tool_call_custom_buffers.get(content_index)
                            if buffer is None:
                                buffer = GrammarToolInputJsonBuffer()
                                tool_call_custom_buffers[content_index] = buffer
                                block.arguments = {property_name: ""}
                            existing = block.arguments.get(property_name)
                            next_input = (existing if isinstance(existing, str) else "") + str(
                                custom.get("input") or ""
                            )
                            tool_delta = (
                                append_grammar_tool_input_json_delta(
                                    buffer, property_name, next_input, False
                                )
                                or ""
                            )
                            block.arguments = {property_name: next_input}
                        elif isinstance(function, Mapping) and isinstance(function.get("arguments"), str):
                            tool_delta = function["arguments"]
                            accumulated = tool_call_partial_args.setdefault(content_index, StreamingArgs())
                            accumulated.append(tool_delta)
                            block.arguments = accumulated.arguments
                        stream.push(ToolCallDeltaEvent(contentIndex=content_index, delta=tool_delta, partial=output))

                reasoning_details = delta.get("reasoning_details")
                if isinstance(reasoning_details, list):
                    for detail in reasoning_details:
                        if not _is_openai_reasoning_detail(detail):
                            continue
                        # Provider replay data lives in the thinking block's signature slot.
                        # These arrive as deltas, so they are merged rather than appended --
                        # see `append_openai_reasoning_detail`.
                        ensure_thinking_block("")
                        append_openai_reasoning_detail(streamed_reasoning_details, detail)
                        if (
                            detail.get("type") == "reasoning.encrypted"
                            and isinstance(detail.get("id"), str)
                            and detail.get("data")
                        ):
                            tool_index = tool_call_index_by_id.get(detail["id"])
                            if tool_index is not None:
                                block = output.content[tool_index]
                                if isinstance(block, ToolCall):
                                    block.thoughtSignature = json.dumps(detail, separators=(",", ":"))

            if streamed_reasoning_details and thinking_block is not None:
                thinking_block.thinkingSignature = json.dumps(
                    streamed_reasoning_details, separators=(",", ":")
                )

            for block in list(output.content):
                finish_block(block)

            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")
            if output.stopReason == "aborted":
                raise RuntimeError("Request was aborted")
            if not has_finish_reason and not compat["supportsFinishReason"]:
                output.stopReason = (
                    "toolUse"
                    if any(isinstance(block, ToolCall) for block in output.content)
                    else "stop"
                )
            if output.stopReason == "error":
                raise RuntimeError(output.errorMessage or "Provider returned an error stop reason")
            if compat["supportsFinishReason"] and not has_finish_reason:
                raise RuntimeError("Stream ended without finish_reason")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            output.errorMessage = _format_completion_error(error)
            error_payload = getattr(error, "error", None)
            raw_metadata = (
                error_payload.get("metadata")
                if isinstance(error_payload, Mapping)
                else getattr(error_payload, "metadata", None)
            )
            if isinstance(raw_metadata, Mapping) and raw_metadata.get("raw"):
                output.errorMessage = f"{output.errorMessage}\n{raw_metadata['raw']}"
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
        finally:
            stream.end()

    spawn_stream_task(run(), stream=stream)
    return stream


def stream_simple_openai_completions(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, context, options, api_key)
    clamped_reasoning = clamp_thinking_level(model, options.reasoning) if options and options.reasoning else None
    reasoning_effort = None if clamped_reasoning == "off" else clamped_reasoning
    tool_choice = _option(options, "toolChoice")
    return stream_openai_completions(
        model,
        context,
        {**base.model_dump(), "reasoningEffort": reasoning_effort, "toolChoice": tool_choice},
    )


def build_request_headers(
    model: Model,
    compat: Mapping[str, Any],
    context: Context | None = None,
    options_headers: Mapping[str, str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """The headers one request carries, in upstream's precedence order.

    Its own function, as upstream's ``buildRequestHeaders`` is: session affinity has three
    shapes and no test could reach any of them while this was inlined in client
    construction behind an SDK call.
    """
    # Upstream seeds the same base and lets `model.headers` spread over it
    # (openai-completions.ts:751), so a catalog entry can still replace it.
    headers: dict[str, Any] = {"User-Agent": get_misaka_user_agent()}
    apply_provider_headers(headers, model.headers)
    if model.provider == "github-copilot" and context is not None:
        headers.update(
            build_copilot_dynamic_headers(
                messages=context.messages,
                hasImages=has_copilot_vision_input(context.messages),
            )
        )

    if session_id and compat.get("sendSessionAffinityHeaders"):
        # Three shapes, not one: OpenRouter threads a conversation with its own header and
        # accepts none of the others, and an endpoint that is neither still wants the
        # request/affinity pair without OpenAI's `session_id`.
        if compat.get("sessionAffinityFormat") == "openrouter":
            headers["x-session-id"] = session_id
        else:
            if compat.get("sessionAffinityFormat") == "openai":
                headers["session_id"] = session_id
            headers["x-client-request-id"] = session_id
            headers["x-session-affinity"] = session_id

    # Caller headers last, so they override anything above -- and a `None` among them
    # removes the header rather than becoming its value.
    apply_provider_headers(headers, options_headers)
    return headers


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
    headers = build_request_headers(model, compat, context, options_headers, session_id)

    default_headers: dict[str, Any]
    if model.provider == "cloudflare-ai-gateway":
        # The gateway's own header replaces the provider's, and resolved auth marks the
        # displaced ones with `None`. httpx refuses a `None` header value outright, so the
        # merge is filtered rather than passed through -- upstream's
        # `providerHeadersToRecord` drops them for the same reason.
        default_headers = provider_headers_to_record(
            {
                **headers,
                "Authorization": headers.get("Authorization"),
                "cf-aig-authorization": f"Bearer {api_key}",
            }
        ) or {}
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
    resolved_cache_retention = resolve_cache_retention(
        _option(options, "cacheRetention") if cache_retention is None else cache_retention,
        _option(options, "env"),
    )
    grammar_tool_input_properties = create_grammar_tool_input_properties(
        context.tools, bool(compat.get("supportsOpenAIGrammarTools"))
    )
    messages = convert_messages(model, context, compat, grammar_tool_input_properties)
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

    # Sampling passthrough: model-level defaults, request-level overrides.
    # Keys already set explicitly above (temperature, max_tokens, ...) win here -- upstream
    # goes the other way (`Object.assign` last, "so custom keys override the named request
    # fields", api/openai-completions.ts:980-983).
    sampling: dict[str, Any] = {}
    model_sampling = getattr(model, "samplingParams", None)
    if isinstance(model_sampling, dict):
        sampling.update(model_sampling)
    request_sampling = _option(options, "samplingParams")
    if isinstance(request_sampling, dict):
        sampling.update(request_sampling)
    for key, value in sampling.items():
        if value is not None and key not in params:
            params[key] = value

    # Kimi delivers a deferred tool by declaring it in a system message next to the result
    # that made it available, so it must not also appear in the request's own tool list.
    deferred_tool_names = (
        _deferred_tool_names(context.messages)
        if compat.get("deferredToolsMode") == "kimi"
        else set()
    )
    active_tools = [tool for tool in (context.tools or []) if tool.name not in deferred_tool_names]
    if active_tools:
        params["tools"] = convert_tools(active_tools, compat)
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
    thinking_token_budget_field = _resolve_thinking_token_budget_field(compat)
    thinking_budget = _resolve_clamped_thinking_budget(model, options, params)

    if compat.get("thinkingFormat") == "zai" and model.reasoning:
        # z.ai speaks `thinking: {type, clear_thinking}`; `enable_thinking` is qwen's field.
        _set_extra(
            params,
            "thinking",
            {"type": "enabled", "clear_thinking": False} if reasoning_effort else {"type": "disabled"},
        )
        if reasoning_effort and compat.get("supportsReasoningEffort"):
            effort = _mapped_thinking_effort(model, reasoning_effort)
            if isinstance(effort, str):
                params["reasoning_effort"] = effort
    elif compat.get("thinkingFormat") == "qwen" and model.reasoning:
        _set_extra(params, "enable_thinking", bool(reasoning_effort))
        if reasoning_effort and compat.get("supportsReasoningEffort"):
            effort = _coalesced_thinking_effort(model, reasoning_effort)
            if isinstance(effort, str):
                params["reasoning_effort"] = effort
    elif compat.get("thinkingFormat") == "qwen-chat-template" and model.reasoning:
        _set_extra(params, "chat_template_kwargs", {"enable_thinking": bool(reasoning_effort), "preserve_thinking": True})
    elif compat.get("thinkingFormat") == "chat-template" and model.reasoning:
        chat_template_kwargs = _build_chat_template_values(
            model, reasoning_effort, compat.get("chatTemplateKwargs"), thinking_budget
        )
        if chat_template_kwargs:
            _set_extra(params, "chat_template_kwargs", chat_template_kwargs)
    elif compat.get("thinkingFormat") == "baseten" and model.reasoning:
        chat_template_args = _build_chat_template_values(
            model, reasoning_effort, compat.get("chatTemplateArgs"), thinking_budget
        )
        if chat_template_args:
            _set_extra(params, "chat_template_args", chat_template_args)
        if compat.get("supportsReasoningEffort"):
            effort = (
                _mapped_thinking_effort(model, reasoning_effort)
                if reasoning_effort
                else (model.thinkingLevelMap.get("off") if model.thinkingLevelMap else None)
            )
            if effort is None and reasoning_effort and not model.thinkingLevelMap:
                effort = reasoning_effort
            if isinstance(effort, str):
                params["reasoning_effort"] = effort
    elif compat.get("thinkingFormat") == "ant-ling" and model.reasoning and reasoning_effort:
        effort = model.thinkingLevelMap.get(reasoning_effort) if model.thinkingLevelMap else None
        if isinstance(effort, str):
            _set_extra(params, "reasoning", {"effort": effort})
    elif compat.get("thinkingFormat") == "string-thinking" and model.reasoning:
        if reasoning_effort:
            _set_extra(params, "thinking", _coalesced_thinking_effort(model, reasoning_effort))
        elif not _thinking_off_is_suppressed(model):
            off_value = model.thinkingLevelMap.get("off") if model.thinkingLevelMap else None
            _set_extra(params, "thinking", "none" if off_value is None else off_value)
    elif compat.get("thinkingFormat") == "deepseek" and model.reasoning:
        if reasoning_effort:
            _set_extra(params, "thinking", {"type": "enabled"})
        elif not _thinking_off_is_suppressed(model):
            _set_extra(params, "thinking", {"type": "disabled"})
        if reasoning_effort and compat.get("supportsReasoningEffort"):
            params["reasoning_effort"] = _coalesced_thinking_effort(model, reasoning_effort)
    elif compat.get("thinkingFormat") == "openrouter" and model.reasoning:
        if reasoning_effort:
            _set_extra(params, "reasoning", {"effort": _coalesced_thinking_effort(model, reasoning_effort)})
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
            params["reasoning_effort"] = _coalesced_thinking_effort(model, reasoning_effort)
    elif reasoning_effort and model.reasoning and compat.get("supportsReasoningEffort"):
        params["reasoning_effort"] = _coalesced_thinking_effort(model, reasoning_effort)
    elif not reasoning_effort and model.reasoning and compat.get("supportsReasoningEffort"):
        off_value = model.thinkingLevelMap.get("off") if model.thinkingLevelMap else None
        if isinstance(off_value, str):
            params["reasoning_effort"] = off_value

    # A server that caps reasoning by token count wants it as a top-level field, whichever
    # name it spells it with.
    if thinking_token_budget_field and thinking_budget is not None:
        _set_extra(params, thinking_token_budget_field, thinking_budget)

    if "openrouter.ai" in model.baseUrl and read_field(model.compat, "openRouterRouting"):
        _set_extra(params, "provider", _dump_model(read_field(model.compat, "openRouterRouting")))
    if "ai-gateway.vercel.sh" in model.baseUrl and read_field(model.compat, "vercelGatewayRouting"):
        routing = _dump_model(read_field(model.compat, "vercelGatewayRouting"))
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


OPENAI_COMPLETIONS_REASONING_FIELDS = ("reasoning", "reasoning_content", "reasoning_text")


def is_openai_completions_reasoning_field(field: str | None) -> bool:
    """``True`` only for a name that is a reasoning field on an assistant message.

    ``thinkingSignature`` is a slot with several tenants: on llama.cpp and gpt-oss it
    names the field the reasoning came out of, but everywhere else it holds a provider's
    opaque signature. Without this check that signature becomes a top-level key on the
    request, which is a member the endpoint never declared.
    """
    return field in OPENAI_COMPLETIONS_REASONING_FIELDS


def _has_valid_common_reasoning_detail_fields(candidate: Mapping[str, Any]) -> bool:
    return (
        candidate.get("id") is None or isinstance(candidate.get("id"), str)
    ) and (
        "format" not in candidate or isinstance(candidate.get("format"), str)
    ) and (
        "index" not in candidate or isinstance(candidate.get("index"), (int, float))
    )


def _is_openai_reasoning_detail(detail: Any) -> bool:
    """One entry of OpenRouter's ``reasoning_details``, in any of its three shapes."""
    if not isinstance(detail, Mapping) or not _has_valid_common_reasoning_detail_fields(detail):
        return False
    kind = detail.get("type")
    if kind == "reasoning.summary":
        return isinstance(detail.get("summary"), str)
    if kind == "reasoning.encrypted":
        return isinstance(detail.get("data"), str)
    if kind == "reasoning.text":
        return isinstance(detail.get("text"), str) and (
            detail.get("signature") is None or isinstance(detail.get("signature"), str)
        )
    return False


def _fill_missing_common_reasoning_detail_fields(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    if target.get("id") is None and source.get("id") is not None:
        target["id"] = source["id"]
    if not target.get("format") and source.get("format"):
        target["format"] = source["format"]
    if target.get("index") is None and source.get("index") is not None:
        target["index"] = source["index"]


def append_openai_reasoning_detail(details: list[dict[str, Any]], detail: Mapping[str, Any]) -> None:
    """Merge a streamed detail into the run, the way upstream's replay expects to read it.

    OpenRouter streams these as deltas: consecutive text or summary entries are one logical
    entry arriving in pieces, while an encrypted entry is opaque and stays discrete. Pushing
    every delta as its own entry would replay the reasoning as hundreds of fragments.
    """
    last = details[-1] if details else None
    kind = detail.get("type")
    if kind == "reasoning.text" and last is not None and last.get("type") == "reasoning.text":
        last["text"] = last.get("text", "") + detail.get("text", "")
        if not last.get("signature") and detail.get("signature"):
            last["signature"] = detail["signature"]
        _fill_missing_common_reasoning_detail_fields(last, detail)
        return
    if kind == "reasoning.summary" and last is not None and last.get("type") == "reasoning.summary":
        last["summary"] = last.get("summary", "") + detail.get("summary", "")
        _fill_missing_common_reasoning_detail_fields(last, detail)
        return
    details.append(dict(detail))


def parse_openai_reasoning_details(signature: str | None) -> list[dict[str, Any]] | None:
    """A whole run of details, as stashed on a thinking block's signature."""
    if not signature:
        return None
    try:
        parsed = json.loads(signature)
    except ValueError:
        return None
    if isinstance(parsed, list) and parsed and all(_is_openai_reasoning_detail(d) for d in parsed):
        return parsed
    return None


def parse_legacy_encrypted_reasoning_detail(signature: str | None) -> dict[str, Any] | None:
    """The older single-encrypted-entry form, stashed on a tool call instead."""
    if not signature:
        return None
    try:
        parsed = json.loads(signature)
    except ValueError:
        return None
    if (
        _is_openai_reasoning_detail(parsed)
        and parsed.get("type") == "reasoning.encrypted"
        and isinstance(parsed.get("id"), str)
        and parsed["id"]
        and parsed.get("data")
    ):
        return parsed
    return None


def _deferred_tool_names(messages: list[Any]) -> set[str]:
    """Tool names the transcript says became available part-way through."""
    names: set[str] = set()
    for message in messages:
        if getattr(message, "role", None) == "toolResult":
            names.update(message.addedToolNames or [])
    return names


def _tools_by_name(tools: list[Tool] | None, names: Iterable[str]) -> list[Tool]:
    by_name = {tool.name: tool for tool in tools or []}
    return [by_name[name] for name in names if name in by_name]


def _replay_tool_call(tool_call: ToolCall, grammar_properties: Mapping[str, str]) -> dict[str, Any]:
    """One prior tool call, on whichever channel it came back from.

    A grammar tool's call carried free text, not JSON arguments, and has to be replayed the
    same way -- sending it back as a `function` call with stringified arguments is not the
    message the model produced.
    """
    custom_input_property = grammar_properties.get(tool_call.name)
    if custom_input_property is not None:
        return {
            "id": tool_call.id,
            "type": "custom",
            "custom": {
                "name": tool_call.name,
                "input": sanitize_surrogates(
                    get_grammar_tool_input(tool_call.name, tool_call.arguments, custom_input_property)
                ),
            },
        }
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.arguments)},
    }


def convert_messages(
    model: Model,
    context: Context,
    compat: Mapping[str, Any],
    grammar_tool_input_properties: Mapping[str, str] | None = None,
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

            thinking_blocks = [block for block in message.content if block.type == "thinking"]
            tool_calls = [block for block in message.content if block.type == "toolCall"]
            # A whole run stashed on a thinking block wins over the older per-tool-call
            # single encrypted entry, which is what upstream falls back to.
            preserved_reasoning_details: list[dict[str, Any]] | None = next(
                (
                    parsed
                    for parsed in (
                        parse_openai_reasoning_details(block.thinkingSignature)
                        for block in thinking_blocks
                    )
                    if parsed is not None
                ),
                None,
            )
            if preserved_reasoning_details is None:
                legacy = [
                    detail
                    for detail in (
                        parse_legacy_encrypted_reasoning_detail(tool_call.thoughtSignature)
                        for tool_call in tool_calls
                    )
                    if detail is not None
                ]
                preserved_reasoning_details = legacy or None

            non_empty_thinking_blocks = [block for block in thinking_blocks if block.thinking.strip()]
            if non_empty_thinking_blocks:
                if compat.get("requiresThinkingAsText"):
                    thinking_text = "\n\n".join(sanitize_surrogates(block.thinking) for block in non_empty_thinking_blocks)
                    assistant_message["content"] = [{"type": "text", "text": thinking_text}, *assistant_text_parts]
                else:
                    if assistant_text:
                        assistant_message["content"] = assistant_text
                    # The structured `reasoning_details` is the alternative to a raw
                    # reasoning field, not a companion to it: sending both replays the
                    # same reasoning twice, once in a shape the endpoint cannot match up.
                    if not preserved_reasoning_details:
                        signature = non_empty_thinking_blocks[0].thinkingSignature
                        if model.provider == "opencode-go" and signature == "reasoning":
                            signature = "reasoning_content"
                        if is_openai_completions_reasoning_field(signature):
                            assistant_message[signature] = "\n".join(
                                block.thinking for block in non_empty_thinking_blocks
                            )
            elif assistant_text:
                assistant_message["content"] = assistant_text

            if tool_calls:
                assistant_message["tool_calls"] = [
                    _replay_tool_call(tool_call, grammar_tool_input_properties or {})
                    for tool_call in tool_calls
                ]

            # Outside the tool-call branch, as upstream is: a turn that only thought and
            # answered still has reasoning to replay.
            if preserved_reasoning_details:
                assistant_message["reasoning_details"] = preserved_reasoning_details

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
            turn_deferred_tool_names: set[str] = set()
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

                if compat.get("deferredToolsMode") == "kimi":
                    turn_deferred_tool_names.update(tool_message.addedToolNames or [])

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

            if turn_deferred_tool_names:
                deferred_tools = _tools_by_name(context.tools, turn_deferred_tool_names)
                if deferred_tools:
                    # Kimi takes a system message carrying tools and no content field --
                    # that is how a tool that became available mid-conversation is declared.
                    params.append({"role": "system", "tools": convert_tools(deferred_tools, compat)})
            index = lookahead
            continue

        index += 1

    return params


def convert_tools(tools: list[Tool], compat: Mapping[str, Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        grammar = resolve_grammar_constrained_sampling(
            tool, bool(compat.get("supportsOpenAIGrammarTools"))
        )
        if grammar:
            # A grammar tool is not a function tool: the model emits free text the grammar
            # constrains, and it comes back as a `custom` tool call rather than JSON
            # arguments. Note the extra nesting -- completions wraps the grammar one level
            # deeper than the responses API does.
            converted.append(
                {
                    "type": "custom",
                    "custom": {
                        "name": tool.name,
                        "description": tool.description,
                        "format": {
                            "type": "grammar",
                            "grammar": {"syntax": grammar.format, "definition": grammar.definition},
                        },
                    },
                }
            )
            continue

        strict = resolve_json_schema_strict_sampling(tool, compat.get("supportsStrictMode") is not False)
        function_spec: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": get_json_schema_tool_parameters(tool, strict),
        }
        # Only sent where the provider understands it; some reject unknown fields.
        if compat.get("supportsStrictMode") is not False:
            function_spec["strict"] = strict if strict is not None else False
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

    is_zai = provider in {"zai", "zai-coding-cn"} or "api.z.ai" in base_url or "open.bigmodel.cn" in base_url
    is_together = provider == "together" or "api.together.ai" in base_url or "api.together.xyz" in base_url
    is_moonshot = provider in {"moonshotai", "moonshotai-cn"} or "api.moonshot." in base_url
    is_openrouter = provider == "openrouter" or "openrouter.ai" in base_url
    is_cloudflare_workers_ai = provider == "cloudflare-workers-ai" or "api.cloudflare.com" in base_url
    is_cloudflare_ai_gateway = provider == "cloudflare-ai-gateway" or "gateway.ai.cloudflare.com" in base_url
    is_nvidia = provider == "nvidia" or "integrate.api.nvidia.com" in base_url
    is_deepseek = provider == "deepseek" or "deepseek.com" in base_url.lower()
    is_non_standard = (
        is_nvidia
        or provider == "cerebras"
        or "cerebras.ai" in base_url
        or provider == "xai"
        or "api.x.ai" in base_url
        or is_together
        or "chutes.ai" in base_url
        or is_deepseek
        or is_zai
        or is_moonshot
        or provider == "opencode"
        or "opencode.ai" in base_url
        or is_cloudflare_workers_ai
        or is_cloudflare_ai_gateway
    )
    use_max_tokens = (
        "chutes.ai" in base_url
        or is_deepseek
        or is_moonshot
        or is_cloudflare_ai_gateway
        or is_together
        or is_nvidia
        or is_zai
    )
    is_grok = provider == "xai" or "api.x.ai" in base_url
    is_openrouter_developer_role_model = is_openrouter and model.id.startswith(("anthropic/", "openai/"))
    cache_control_format = "anthropic" if provider == "openrouter" and model.id.startswith("anthropic/") else None

    return {
        "supportsStore": not is_non_standard,
        "supportsDeveloperRole": is_openrouter_developer_role_model or (not is_non_standard and not is_openrouter),
        "supportsReasoningEffort": not (
            is_grok or is_zai or is_moonshot or is_together or is_cloudflare_ai_gateway or is_nvidia
        ),
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
            if is_openrouter
            else "openai"
        ),
        "openRouterRouting": {},
        "vercelGatewayRouting": {},
        "zaiToolStream": False,
        "supportsStrictMode": not (is_moonshot or is_together or is_cloudflare_ai_gateway or is_nvidia),
        "cacheControlFormat": cache_control_format,
        "sendSessionAffinityHeaders": False,
        "sessionAffinityFormat": "openrouter" if is_openrouter else "openai",
        "chatTemplateKwargs": {},
        "chatTemplateArgs": {},
        "supportsThinkingTokenBudget": False,
        "thinkingTokenBudgetField": None,
        "supportsOpenAIGrammarTools": False,
        "deferredToolsMode": None,
        "supportsFinishReason": True,
        "supportsLongCacheRetention": not (
            is_together or is_cloudflare_workers_ai or is_cloudflare_ai_gateway or is_nvidia
        ),
    }


def get_compat(model: Model) -> dict[str, Any]:
    detected = detect_compat(model)
    compat = model.compat
    if compat is None:
        return detected

    return {
        "supportsStore": read_field(compat, "supportsStore", detected["supportsStore"]),
        "supportsDeveloperRole": read_field(compat, "supportsDeveloperRole", detected["supportsDeveloperRole"]),
        "supportsReasoningEffort": read_field(compat, "supportsReasoningEffort", detected["supportsReasoningEffort"]),
        "supportsUsageInStreaming": read_field(compat, "supportsUsageInStreaming", detected["supportsUsageInStreaming"]),
        "maxTokensField": read_field(compat, "maxTokensField", detected["maxTokensField"]),
        "requiresToolResultName": read_field(compat, "requiresToolResultName", detected["requiresToolResultName"]),
        "requiresAssistantAfterToolResult": read_field(
            compat, "requiresAssistantAfterToolResult", detected["requiresAssistantAfterToolResult"]
        ),
        "requiresThinkingAsText": read_field(compat, "requiresThinkingAsText", detected["requiresThinkingAsText"]),
        "requiresReasoningContentOnAssistantMessages": read_field(
            compat,
            "requiresReasoningContentOnAssistantMessages",
            detected["requiresReasoningContentOnAssistantMessages"],
        ),
        "thinkingFormat": read_field(compat, "thinkingFormat", detected["thinkingFormat"]),
        "openRouterRouting": read_field(compat, "openRouterRouting", {}),
        "vercelGatewayRouting": read_field(compat, "vercelGatewayRouting", detected["vercelGatewayRouting"]),
        "zaiToolStream": read_field(compat, "zaiToolStream", detected["zaiToolStream"]),
        "supportsStrictMode": read_field(compat, "supportsStrictMode", detected["supportsStrictMode"]),
        "cacheControlFormat": read_field(compat, "cacheControlFormat", detected["cacheControlFormat"]),
        "sessionAffinityFormat": read_field(
            compat, "sessionAffinityFormat", detected["sessionAffinityFormat"]
        ),
        "chatTemplateKwargs": read_field(compat, "chatTemplateKwargs", detected["chatTemplateKwargs"]),
        "chatTemplateArgs": read_field(compat, "chatTemplateArgs", detected["chatTemplateArgs"]),
        "supportsThinkingTokenBudget": read_field(
            compat, "supportsThinkingTokenBudget", detected["supportsThinkingTokenBudget"]
        ),
        "thinkingTokenBudgetField": read_field(
            compat, "thinkingTokenBudgetField", detected["thinkingTokenBudgetField"]
        ),
        "supportsOpenAIGrammarTools": read_field(
            compat, "supportsOpenAIGrammarTools", detected["supportsOpenAIGrammarTools"]
        ),
        "deferredToolsMode": read_field(compat, "deferredToolsMode", detected["deferredToolsMode"]),
        "supportsFinishReason": read_field(compat, "supportsFinishReason", detected["supportsFinishReason"]),
        "sendSessionAffinityHeaders": read_field(
            compat, "sendSessionAffinityHeaders", detected["sendSessionAffinityHeaders"]
        ),
        "supportsLongCacheRetention": read_field(
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
    # Upstream disables the SDK's own retry and retries the request itself
    # (`maxRetries: 0` in its requestOptions), so the policy that decides *what* is
    # worth retrying is `utils/provider_retry`, not whichever heuristic the SDK ships.
    request_client_kwargs["max_retries"] = 0
    if request_client_kwargs and hasattr(client, "with_options"):
        request_client = client.with_options(**request_client_kwargs)

    if hasattr(getattr(getattr(request_client, "chat", None), "completions", None), "with_raw_response"):
        raw_response = await _await_maybe_with_signal(
            request_client.chat.completions.with_raw_response.create(**params),
            signal,
        )
        on_response = _option(options, "onResponse")
        if callable(on_response):
            await maybe_await(
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
            await maybe_await(
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
