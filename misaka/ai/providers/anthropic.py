"""Anthropic Messages provider adapter."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from collections.abc import Set as AbstractSet
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
    _iterate_async_iterable,
    _option,
    resolve_cache_retention,
)
from misaka.ai.providers.cloudflare import resolve_cloudflare_base_url
from misaka.ai.providers.constrained_sampling import (
    get_json_schema_tool_parameters,
    resolve_json_schema_strict_sampling,
)
from misaka.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from misaka.ai.providers.sdk import require
from misaka.ai.providers.simple_options import (
    adjust_max_tokens_for_thinking,
    build_base_options,
    clamp_thinking_budget_to_answer_room,
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
from misaka.ai.utils.deferred_tools import split_deferred_tools
from misaka.ai.utils.diagnostics import (
    append_assistant_message_diagnostic,
    create_assistant_message_diagnostic,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headers_to_record
from misaka.ai.utils.json_parse import StreamingArgs, parse_json_with_repair
from misaka.ai.utils.provider_retry import retry_provider_request
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates
from misaka.ai.utils.user_agent import get_misaka_user_agent
from misaka.utils.values import maybe_await, signal_aborted

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
# Managed-effort models (`compat.supportsMidConvoEffort`): the request carries the effort of
# every past turn as an effort-only system message, and binds thinking blocks so a prefix
# that no longer matches is dropped instead of returning 400.
MID_CONVERSATION_OUTPUT_CONFIG_BETA = "mid-conversation-output-config-2026-07-01"
THINKING_BINDING_CONTROLS_BETA = "thinking-binding-controls-2026-08-01"
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
    """Later sources win, matching header names case-insensitively.

    Upstream merges these with `Object.assign`, which is case-sensitive, and relies on the
    `Headers` object downstream to collapse `User-Agent` and `user-agent` into one. A dict
    handed to httpx collapses nothing: both spellings go out as separate headers, and the
    OAuth path -- which deliberately overrides the client string with `claude-cli/...` --
    would send its own identity *and* misaka's. Replacing case-insensitively here, the way
    `models_runtime._mergeHeaders` already does, produces what upstream puts on the wire.
    """
    merged: dict[str, Any] = {}
    for headers in header_sources:
        if not headers:
            continue
        for key, value in headers.items():
            if value is None:
                continue
            name = str(key)
            lowered = name.lower()
            for existing in [k for k in merged if k.lower() == lowered]:
                del merged[existing]
            merged[name] = value
    return merged


def supports_mid_convo_effort(model: Model) -> bool:
    """Whether this model's transport takes effort-only system messages and binding controls.

    Set in the catalog on Anthropic's managed-effort models. Everything the flag turns on is
    request shape, so reading it in one place keeps the six call sites from drifting apart.
    """
    return getattr(model.compat, "supportsMidConvoEffort", None) is True


def _is_anthropic_effort(value: Any) -> bool:
    return value in ("low", "medium", "high", "xhigh", "max")


def _force_adaptive_thinking(model: Model) -> bool | None:
    compat = getattr(model, "compat", None)
    return getattr(compat, "forceAdaptiveThinking", None)


def _default_supports_tool_references(model: Model) -> bool:
    """First-party Anthropic models from 4.5 on, Haiku excepted.

    Haiku rejects client-side ``tool_reference`` blocks, and models older than tool search
    do not understand them. The minor group is only a minor version when it is short: an
    id like ``claude-opus-4-20250514`` puts a date where a point release would go, and
    upstream tells them apart by length rather than by shape.
    """
    if model.provider != "anthropic" or "haiku" in model.id:
        return False
    version = re.match(r"^claude-(?:opus|sonnet|fable)-(\d+)(?:-(\d+))?(?:-|$)", model.id)
    if not version:
        return False
    major = int(version.group(1))
    minor_group = version.group(2)
    minor = int(minor_group) if minor_group and len(minor_group) < 8 else 0
    return major > 4 or (major == 4 and minor >= 5)


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
        "supportsStrictTools": bool(getattr(compat, "supportsStrictTools", None)),
        "supportsToolReferences": (
            getattr(compat, "supportsToolReferences", None)
            if getattr(compat, "supportsToolReferences", None) is not None
            else _default_supports_tool_references(model)
        ),
    }


def get_cache_control(
    model: Model, cache_retention: CacheRetention | None = None, env: Any = None
) -> dict[str, Any]:
    retention = resolve_cache_retention(cache_retention, env)
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


def is_oauth_token(api_key: str | None) -> bool:
    # `None` is a real case, not a caller error: upstream's `createClient` takes
    # `apiKey: string | undefined`, and a provider whose auth resolves to headers only
    # (Kimi's OAuth returns `Authorization: Bearer ...` and no key) arrives that way.
    if not api_key:
        return False
    return "sk-ant-oat" in api_key


def create_client(
    model: Model,
    api_key: str | None,
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
    if supports_mid_convo_effort(model):
        beta_features.extend((MID_CONVERSATION_OUTPUT_CONFIG_BETA, THINKING_BINDING_CONTROLS_BETA))

    if model.provider == "cloudflare-ai-gateway":
        client = require(AsyncAnthropic, "anthropic")(
            # The real credential is `cf-aig-authorization` in default_headers. The Python
            # SDK, unlike the TS one pi builds on, refuses to send a request with neither
            # api_key nor auth_token -- so a placeholder satisfies that check, and the
            # `X-Api-Key: omit` below guarantees it never reaches the wire.
            api_key=api_key or "header-auth-placeholder",
            auth_token=None,
            base_url=resolve_cloudflare_base_url(model),
            default_headers=_merge_headers(
                {
                    "User-Agent": get_misaka_user_agent(),
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
                    "User-Agent": get_misaka_user_agent(),
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
                    "User-Agent": get_misaka_user_agent(),
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
    # A header-only configuration (ANTHROPIC_AUTH_TOKEN, Kimi's bearer token) arrives with
    # no api key at all: the credential rides in options_headers. The TS SDK accepts
    # `apiKey: null` and simply sends what defaultHeaders carries; the Python SDK raises
    # "Could not resolve authentication method" at request time. The placeholder passes
    # that check and `X-Api-Key: omit` strips the header the SDK would mint from it, so
    # the only auth on the wire is the one the caller supplied.
    header_only_auth = api_key is None
    client = require(AsyncAnthropic, "anthropic")(
        api_key=api_key if api_key is not None else "header-auth-placeholder",
        auth_token=None,
        base_url=model.baseUrl,
        default_headers=_merge_headers(
            {
                "User-Agent": get_misaka_user_agent(),
                "accept": "application/json",
                "anthropic-dangerous-direct-browser-access": "true",
                **({"X-Api-Key": omit} if header_only_auth else {}),
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
    cache_state = get_cache_control(model, _option(options, "cacheRetention"), _option(options, "env"))
    cache_control = cache_state.get("cacheControl")
    compat = get_anthropic_compat(model)

    # Tools whose definitions the transcript will deliver are declared with
    # `defer_loading` and their bodies arrive later, as `tool_reference` blocks in the
    # result of the call that made them available. The split has to happen against the
    # *transformed* messages, because that is where tool call ids and names are settled.
    normalize_tool_name = to_claude_code_name if is_oauth else (lambda name: name)
    placement = split_deferred_tools(
        context.model_copy(update={"messages": transform_messages(context.messages, model, normalize_tool_call_id)}),
        bool(compat["supportsToolReferences"]),
        normalize_tool_name,
    )
    immediate_tools = list(placement.immediate)
    deferred_tools = list(placement.deferred.values())
    if not immediate_tools and deferred_tools:
        # Nothing to send now would leave the request with no tools at all; upstream
        # promotes the whole deferred set rather than send an empty list.
        immediate_tools, deferred_tools = deferred_tools, []
    deferred_tool_names = {normalize_tool_name(tool.name) for tool in deferred_tools}

    managed_effort = supports_mid_convo_effort(model)
    assistant_levels: dict[int, str] = {}
    converted = convert_messages(
        context.messages, model, is_oauth, cache_control, deferred_tool_names,
        model.provider if managed_effort else None,
        assistant_levels if managed_effort else None,
    )
    active_effort = _option(options, "effort") or "high"
    params: dict[str, Any] = {
        "model": model.id,
        "messages": (_insert_thinking_level_messages(converted, assistant_levels, active_effort)
                     if managed_effort else converted),
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

    # Temperature is incompatible with extended thinking, and a managed-effort model always
    # thinks, so it is never sent for one however the caller asked. Upstream also gates this
    # on `compat.supportsTemperature`; this port's compat table has never carried that key,
    # and adding it here would change what every other Anthropic model sends.
    if (_option(options, "temperature") is not None and not _option(options, "thinkingEnabled")
            and not managed_effort):
        params["temperature"] = _option(options, "temperature")

    if immediate_tools or deferred_tools:
        params["tools"] = [
            *convert_tools(
                immediate_tools,
                is_oauth,
                bool(compat["supportsEagerToolInputStreaming"]),
                bool(compat["supportsStrictTools"]),
                cache_control if compat["supportsCacheControlOnTools"] else None,
            ),
            *convert_tools(
                deferred_tools,
                is_oauth,
                bool(compat["supportsEagerToolInputStreaming"]),
                bool(compat["supportsStrictTools"]),
                None,
                defer_loading=True,
            ),
        ]

    if managed_effort:
        # Always adaptive: the binding control lets the server drop a thinking block whose
        # prefix no longer matches, instead of failing the whole request with a 400 that
        # would repeat on every retry.
        params["thinking"] = {
            "type": "adaptive",
            "display": _option(options, "thinkingDisplay", "summarized"),
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        }
        params["output_config"] = {"effort": "high"}
    elif model.reasoning:
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


def normalize_tool_call_id(
    tool_call_id: str, _target_model: Model | None = None, _source: Any = None
) -> str:
    """Anthropic ids must match ``^[a-zA-Z0-9_-]+$`` and stay under 65 characters.

    ``transform_messages`` calls this with three arguments, which is upstream's declared
    callback shape; upstream's own implementation takes one and JavaScript discards the
    rest. Python does not -- passing this a three-argument call raised ``TypeError``, and
    only on a cross-provider handoff, which is the one path no test exercised. The extra
    parameters are accepted and ignored, as upstream's are.
    """
    normalized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in tool_call_id)
    return normalized[:64]


def _convert_tool_result(
    message: ToolResultMessage,
    is_oauth: bool,
    deferred_tool_names: AbstractSet[str],
    loaded_tool_names: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One tool result, plus whatever its own content was displaced by tool references.

    A deferred tool's definition is delivered by naming it in the result of the call that
    made it available. Anthropic rejects a result that mixes references with ordinary
    content, so the references take the result body and the real content is returned
    separately, to be re-attached after every ``tool_result`` in the same user turn.
    """
    references: list[dict[str, Any]] = []
    for name in message.addedToolNames or []:
        normalized = to_claude_code_name(name) if is_oauth else name
        if normalized not in deferred_tool_names or normalized in loaded_tool_names:
            continue
        loaded_tool_names.add(normalized)
        references.append({"type": "tool_reference", "tool_name": normalized})
    converted_content = convert_content_blocks(message.content)
    tool_result = {
        "type": "tool_result",
        "tool_use_id": message.toolCallId,
        "content": references if references else converted_content,
        "is_error": message.isError,
    }
    if not references:
        return tool_result, []
    if isinstance(converted_content, str):
        return tool_result, [{"type": "text", "text": converted_content}]
    return tool_result, list(converted_content)


def convert_messages(
    messages: list[Any],
    model: Model,
    is_oauth: bool,
    cache_control: dict[str, Any] | None = None,
    deferred_tool_names: AbstractSet[str] | None = None,
    managed_provider: str | None = None,
    assistant_levels: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Anthropic wire messages. With ``managed_provider`` set, ``assistant_levels`` is filled
    with the effort each converted assistant turn ran at, keyed by its index in the result --
    what ``_insert_thinking_level_messages`` needs to replay the timeline. Only turns this
    same provider produced count: an effort level from another provider's transcript means
    nothing to this one."""
    params: list[dict[str, Any]] = []
    transformed_messages = transform_messages(messages, model, normalize_tool_call_id)
    deferred_names: AbstractSet[str] = deferred_tool_names or frozenset()
    loaded_tool_names: set[str] = set()

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
                if (managed_provider is not None and assistant_levels is not None
                        and getattr(message, "api", None) == "anthropic-messages"
                        and getattr(message, "provider", None) == managed_provider
                        and _is_anthropic_effort(getattr(message, "providerThinkingLevel", None))):
                    assistant_levels[len(params)] = message.providerThinkingLevel
                params.append({"role": "assistant", "content": blocks})
            index += 1
            continue

        if message.role == "toolResult":
            # Consecutive tool results are collected into one user turn, which the z.ai
            # Anthropic endpoint requires.
            tool_results: list[dict[str, Any]] = []
            sibling_content: list[dict[str, Any]] = []
            lookahead = index
            while lookahead < len(transformed_messages) and transformed_messages[lookahead].role == "toolResult":
                next_message = transformed_messages[lookahead]
                if not isinstance(next_message, ToolResultMessage):
                    break
                converted, siblings = _convert_tool_result(
                    next_message, is_oauth, deferred_names, loaded_tool_names
                )
                tool_results.append(converted)
                sibling_content.extend(siblings)
                lookahead += 1

            # Displaced content must follow every tool_result block, not sit between them.
            params.append({"role": "user", "content": [*tool_results, *sibling_content]})
            index = lookahead
            continue

        index += 1

    if cache_control and params:
        from misaka.utils.prompt_cache_wire import _apply_cache_marker

        last_message = params[-1]
        if last_message.get("role") == "user":
            content = last_message.get("content")
            if isinstance(content, list) and content:
                last_block = content[-1]
                if len(content) == 1 and last_block.get("type") == "text":
                    text_message = {"role": "user", "content": last_block["text"]}
                    _apply_cache_marker(text_message, cache_control, native_anthropic=True)
                    last_message["content"] = text_message["content"]
                elif isinstance(last_block, dict) and last_block.get("type") in {"text", "image", "tool_result"}:
                    last_block["cache_control"] = cache_control
            elif isinstance(content, str):
                _apply_cache_marker(last_message, cache_control, native_anthropic=True)

    return params


def _insert_thinking_level_messages(
    messages: list[dict[str, Any]],
    assistant_levels: dict[int, str],
    active_effort: str,
) -> list[dict[str, Any]]:
    """Put each past turn's effort back in front of it, and the current one at the end.

    A managed-effort model is told what it was thinking at when it produced each earlier
    answer, because the effort is part of what those answers mean; the trailing message is
    the level for the turn about to be generated. The carrier is a system message with no
    content, which is what the mid-conversation-output-config beta defines.
    """
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        historical = assistant_levels.get(index)
        if historical is not None:
            out.append({"role": "system", "content": [], "output_config": {"effort": historical}})
        out.append(message)
    out.append({"role": "system", "content": [], "output_config": {"effort": active_effort}})
    return out


def should_use_fine_grained_tool_streaming_beta(model: Model, context: Context) -> bool:
    return bool(context.tools) and not bool(get_anthropic_compat(model)["supportsEagerToolInputStreaming"])


def convert_tools(
    tools: list[Tool],
    is_oauth: bool,
    supports_eager_tool_input_streaming: bool,
    supports_strict_tools: bool = False,
    cache_control: dict[str, Any] | None = None,
    defer_loading: bool = False,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    root_combinators = {"oneOf", "allOf", "anyOf"}
    for index, tool in enumerate(tools):
        parameters = get_json_schema_tool_parameters(tool, None)
        # Anthropic rejects root combinators even without strict sampling. A projected
        # schema must not promise strict enforcement of constraints it no longer sends.
        strict = resolve_json_schema_strict_sampling(
            tool, supports_strict_tools and not root_combinators.intersection(parameters)
        )
        if strict is True:
            parameters = get_json_schema_tool_parameters(tool, strict)
        # Keep $defs and nested schemas; the untouched original still validates calls.
        # ponytail: current tools declare fields at the root; branch-only field
        # definitions would need schema lowering rather than this wire-only projection.
        input_schema = {
            **{key: value for key, value in parameters.items() if key not in root_combinators},
            "type": "object",
            "properties": parameters.get("properties", {}),
            "required": parameters.get("required", []),
        }
        converted_tool = {
            "name": to_claude_code_name(tool.name) if is_oauth else tool.name,
            "description": tool.description,
            **({"eager_input_streaming": True} if supports_eager_tool_input_streaming else {}),
            **({"strict": True} if strict is True else {}),
            "input_schema": input_schema,
            **({"defer_loading": True} if defer_loading else {}),
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
            if signal_aborted(signal):
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
            if signal_aborted(signal):
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
        if signal_aborted(signal):
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
            # TRY004 waived below: the sole caller (the stream loop in this module) catches
            # every exception and turns it into an error event, so a malformed payload stays
            # a RuntimeError like the parse failure just above instead of a TypeError.
            raise RuntimeError(f"Could not parse Anthropic SSE event {event_name}: parsed payload was not an object")  # noqa: TRY004

        if event.get("type") == "message_start":
            saw_message_start = True
        elif event.get("type") == "message_stop":
            saw_message_stop = True
        yield event

    if saw_message_start and not saw_message_stop:
        raise RuntimeError("Anthropic stream ended before message_stop")


async def _create_raw_response(
    client: Any, params: dict[str, Any], options: Any = None, auth_extra_headers: dict[str, Any] | None = None
) -> Any:
    signal = _option(options, "signal")
    request_client = client
    request_client_options: dict[str, Any] = {}
    request_call_options: dict[str, Any] = {}
    if _option(options, "timeoutMs") is not None:
        request_client_options["timeout"] = _option(options, "timeoutMs") / 1000
    if _option(options, "maxRetries") is not None:
        # Upstream disables the SDK's own retry and retries the request itself
        # (`maxRetries: 0` in its requestOptions), so the policy that decides *what* is
        # worth retrying is `utils/provider_retry`, not whichever heuristic the SDK ships.
        request_client_options["max_retries"] = 0
    if request_client_options and hasattr(client, "with_options"):
        request_client = client.with_options(**request_client_options)
    else:
        request_call_options = request_client_options
    if auth_extra_headers:
        # The Python SDK's auth check only honours an `Omit` in the *per-request*
        # extra_headers -- one placed in the client's default_headers is folded away before
        # the check runs. Injected after the branch above, because the else-arm reassigns
        # request_call_options wholesale and silently dropped an earlier injection.
        request_call_options = {
            **request_call_options,
            "extra_headers": {
                **(request_call_options.get("extra_headers") or {}),
                **auth_extra_headers,
            },
        }

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
            # Recorded only for managed-effort models, and only because the next request
            # replays it: an unmanaged response has no provider-native level to state.
            providerThinkingLevel=((_option(options, "effort") or "high")
                                   if supports_mid_convo_effort(model) else None),
        )
        raw_response: Any = None
        owned_client: Any = None       # a client made here is closed here

        try:
            auth_extra_headers: dict[str, Any] | None = None
            client = _option(options, "client")
            if client is None:
                api_key = _option(options, "apiKey") or get_env_api_key(model.provider) or ""
                if model.provider == "cloudflare-ai-gateway" or not api_key:
                    # Gateway requests authenticate with `cf-aig-authorization`; header-only
                    # configurations (ANTHROPIC_AUTH_TOKEN, Kimi's bearer token) carry theirs
                    # in options headers. Either way the SDK's own `X-Api-Key` must not go
                    # out, and the SDK only accepts that declaration per request.
                    auth_extra_headers = {"X-Api-Key": omit}
                copilot_dynamic_headers: dict[str, str] | None = None
                if model.provider == "github-copilot":
                    copilot_dynamic_headers = build_copilot_dynamic_headers(
                        messages=context.messages,
                        hasImages=has_copilot_vision_input(context.messages),
                    )
                cache_retention = resolve_cache_retention(_option(options, "cacheRetention"), _option(options, "env"))
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
                owned_client = client
            else:
                is_oauth = False

            params = build_params(model, context, is_oauth, options)
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_params = await maybe_await(on_payload(params, model))
                if next_params is not None:
                    params = next_params

            raw_response = await retry_provider_request(
                lambda: _create_raw_response(client, params, options, auth_extra_headers),
                max_retries=_option(options, "maxRetries") or 0,
                max_retry_delay_ms=_option(options, "maxRetryDelayMs"),
                signal=_option(options, "signal"),
            )
            await _emit_response_metadata(raw_response, options, model)
            stream.push(StartEvent(partial=output))
            provider_indexes: dict[int, int] = {}
            tool_partial_json: dict[int, StreamingArgs] = {}
            # Deltas whose block index was never started: a compatible endpoint's framing
            # fault. Counted so a tool call that ends empty can say where its input went.
            dropped_deltas: dict[str, int] = {}

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
                    # dropping it would lose content (pi #7358).
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
                        tool_partial_json[provider_index] = StreamingArgs()
                        stream.push(ToolCallStartEvent(contentIndex=len(output.content) - 1, partial=output))
                    continue

                if event_type == "content_block_delta":
                    provider_index = event.get("index")
                    delta = event.get("delta")
                    if not isinstance(provider_index, int) or not isinstance(delta, Mapping):
                        continue
                    content_index = provider_indexes.get(provider_index)
                    if content_index is None:
                        kind = str(delta.get("type") or "?")
                        dropped_deltas[kind] = dropped_deltas.get(kind, 0) + 1
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
                        accumulated = tool_partial_json.setdefault(provider_index, StreamingArgs())
                        accumulated.append(partial_delta)
                        block.arguments = accumulated.arguments
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
                        # Only the deltas may overwrite what content_block_start inlined:
                        # compatible endpoints (proxies, gateways) put the whole input in the
                        # start block and send no input_json_delta, and parsing the empty
                        # buffer would hand the tool {} instead. Same guard as the
                        # openai-completions and openai-responses adapters.
                        accumulated = tool_partial_json.get(provider_index)
                        if accumulated is not None and accumulated.raw:
                            block.arguments = accumulated.finish()
                        _note_empty_tool_arguments(output, block, accumulated, dropped_deltas)
                        stream.push(ToolCallEndEvent(contentIndex=content_index, toolCall=block, partial=output))
                    continue

                if event_type == "message_delta":
                    delta = event.get("delta")
                    if isinstance(delta, Mapping) and isinstance(delta.get("stop_reason"), str):
                        output.stopReason = map_stop_reason(delta["stop_reason"])
                    usage = event.get("usage")
                    if isinstance(usage, Mapping):
                        _update_usage_from_anthropic_usage(output, usage, model)

            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")
            if output.stopReason in {"aborted", "error"}:
                raise RuntimeError("An unknown error occurred")
            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001 - every failure becomes an error event on the stream
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            output.errorMessage = _format_anthropic_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
        finally:
            await _close_stream(raw_response)
            if owned_client is not None:
                # Closed while the loop that made it is still running. Left to the garbage
                # collector, httpx closes the pool from its finaliser: in a one-shot helper
                # loop (`platform.session.run_coro`) that loop is gone by then, and every
                # call printed "Event loop is closed" through asyncio's default handler --
                # a full traceback per request in every research node pane.
                try:
                    await owned_client.close()
                except Exception:  # noqa: BLE001, S110 - closing is best-effort; the stream has already ended
                    pass
            stream.end()

    spawn_stream_task(run(), stream=stream)
    return stream


def _note_empty_tool_arguments(output: AssistantMessage, block: ToolCall, accumulated: StreamingArgs | None,
                               dropped_deltas: dict[str, int]) -> None:
    """A tool call that ends with ``{}`` although its input was streamed is a silent failure
    with two possible causes -- the streamed buffer did not parse, or its deltas were framed
    under a block that never started -- and the tool's validation error ("received {}") shows
    neither. The message keeps a diagnostic with what actually arrived, so the transcript can
    say which it was. A tool call that genuinely takes no arguments records nothing."""
    if block.arguments != {}:
        return
    raw = accumulated.raw if accumulated is not None else ""
    if raw.strip() not in ("", "{}"):
        kind, why = "tool_arguments_unparsed", f"{len(raw)} streamed characters did not parse as a JSON object"
    elif dropped_deltas.get("input_json_delta"):
        kind, why = "tool_arguments_missing", "input_json_delta events arrived for a block that was never started"
    else:
        return
    append_assistant_message_diagnostic(output, create_assistant_message_diagnostic(
        kind, RuntimeError(f"tool {block.name}: {why}"),
        {"tool": block.name, "toolCallId": block.id, "rawLength": len(raw),
         "rawHead": raw[:200], "rawTail": raw[-200:] if len(raw) > 200 else "",
         "droppedDeltas": dict(dropped_deltas)}))


def map_thinking_level_to_effort(model: Model, level: str | None) -> AnthropicEffort:
    mapped = model.thinkingLevelMap.get(level) if level and model.thinkingLevelMap else None
    if isinstance(mapped, str):
        return mapped  # type: ignore[return-value]
    if level in {"minimal", "low"}:
        return "low"
    if level == "medium":
        return "medium"
    return "high"


def _has_request_auth_header(headers) -> bool:
    if not headers:
        return False
    names = {str(name).lower() for name, value in headers.items() if value is not None}
    return bool(names & {"authorization", "x-api-key", "cf-aig-authorization"})


def stream_simple_anthropic(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key and not _has_request_auth_header(_option(options, "headers")):
        # Mirrors upstream's assertRequestAuth (anthropic-messages.ts:297-307): a request
        # is authenticated by an api key *or* by a header the resolved auth supplied --
        # `Authorization` (ANTHROPIC_AUTH_TOKEN, Kimi's bearer token), `x-api-key`, or the
        # gateway's `cf-aig-authorization`. Requiring the key alone rejected both
        # header-only configurations this repo actually ships.
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, context, options, api_key)
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
            # Thinking and the answer share max_tokens: always leave the answer its room.
            "thinkingBudgetTokens": clamp_thinking_budget_to_answer_room(
                adjusted.thinkingBudget, adjusted.maxTokens
            ),
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
