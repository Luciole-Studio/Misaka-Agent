"""Shared OpenAI Responses message conversion and stream processing."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterable, Iterable, Mapping
from typing import Any, TypedDict

from misaka.ai.models import calculate_cost
from misaka.ai.providers._common import _empty_usage
from misaka.ai.providers.constrained_sampling import (
    GrammarToolInputJsonBuffer,
    append_grammar_tool_input_json_delta,
    get_grammar_tool_input,
    get_json_schema_tool_parameters,
    resolve_grammar_constrained_sampling,
    resolve_json_schema_strict_sampling,
)
from misaka.ai.providers.transform_messages import transform_messages
from misaka.ai.types import (
    AssistantMessage,
    Model,
    StopReason,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextSignatureV1,
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
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.hash import short_hash
from misaka.ai.utils.json_parse import StreamingArgs
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates
from misaka.ai.utils.text import get_system_message_text, render_system_message_update
from misaka.ai.utils.transcript import resolve_transcript, resolve_transcript_tools

_TOOL_CALL_ID_PART_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")


class OpenAIResponsesStreamOptions(TypedDict, total=False):
    serviceTier: Any
    resolveServiceTier: Any
    applyServiceTierPricing: Any
    # Tool name -> the property a grammar tool's free-text output is encoded into. Used to
    # rebuild `custom_tool_call` items into ordinary tool calls with one string argument.
    grammarToolInputProperties: Mapping[str, str]


class ConvertResponsesMessagesOptions(TypedDict, total=False):
    includeSystemPrompt: bool
    # Tool name -> the property its grammar output is encoded into. A tool listed here is
    # a grammar tool: it is replayed as a `custom_tool_call` carrying free text rather
    # than a `function_call` carrying JSON arguments.
    grammarToolInputProperties: Mapping[str, str]
    supportsMidConvoSystemMessages: bool
    # How this endpoint takes tools a later system message adds: a developer
    # `additional_tools` item, or a pair of `tool_search_call` / `tool_search_output` items.
    supportsAdditionalTools: bool
    supportsToolSearch: bool
    toolOptions: ConvertResponsesToolsOptions


class ConvertResponsesToolsOptions(TypedDict, total=False):
    strict: bool | None
    supportsStrictMode: bool
    supportsOpenAIGrammarTools: bool
    toolSearchResult: bool


def encode_text_signature_v1(text_id: str, phase: TextSignatureV1 | str | None = None) -> str:
    payload: dict[str, Any] = {"v": 1, "id": text_id}
    if phase in {"commentary", "final_answer"}:
        payload["phase"] = phase
    return json.dumps(payload, separators=(",", ":"))


def parse_text_signature(signature: str | None) -> dict[str, str] | None:
    if not signature:
        return None
    if signature.startswith("{"):
        try:
            parsed = json.loads(signature)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("v") == 1 and isinstance(parsed.get("id"), str):
            phase = parsed.get("phase")
            if phase in {"commentary", "final_answer"}:
                return {"id": parsed["id"], "phase": phase}
            return {"id": parsed["id"]}
    return {"id": signature}


def _normalize_id_part(part: str) -> str:
    sanitized = _TOOL_CALL_ID_PART_PATTERN.sub("_", part)
    normalized = sanitized[:64] if len(sanitized) > 64 else sanitized
    return normalized.rstrip("_")


def _build_foreign_responses_item_id(item_id: str) -> str:
    normalized = f"fc_{short_hash(item_id)}"
    return normalized[:64] if len(normalized) > 64 else normalized


def _create_normalize_tool_call_id(model: Model, allowed_tool_call_providers: set[str] | frozenset[str]):
    def normalize_tool_call_id(tool_call_id: str, _target_model: Model, source: AssistantMessage) -> str:
        if model.provider not in allowed_tool_call_providers:
            return _normalize_id_part(tool_call_id)
        if "|" not in tool_call_id:
            return _normalize_id_part(tool_call_id)

        parts = tool_call_id.split("|")
        call_id = parts[0]
        item_id = parts[1] if len(parts) > 1 else ""
        normalized_call_id = _normalize_id_part(call_id)
        is_foreign_tool_call = source.provider != model.provider or source.api != model.api
        normalized_item_id = (
            _build_foreign_responses_item_id(item_id) if is_foreign_tool_call else _normalize_id_part(item_id)
        )
        if not normalized_item_id.startswith("fc_"):
            normalized_item_id = _normalize_id_part(f"fc_{normalized_item_id}")
        return f"{normalized_call_id}|{normalized_item_id}"

    return normalize_tool_call_id


def convert_responses_messages(
    model: Model,
    context: TranscriptContext,
    allowed_tool_call_providers: set[str] | frozenset[str],
    options: ConvertResponsesMessagesOptions | None = None,
) -> list[dict[str, Any]]:
    opts = options or {}
    normalized_context = resolve_transcript(context, opts.get("supportsMidConvoSystemMessages"))
    messages: list[dict[str, Any]] = []
    tool_options: ConvertResponsesToolsOptions = opts.get("toolOptions") or {}
    transformed_messages = transform_messages(
        normalized_context.messages,
        model,
        _create_normalize_tool_call_id(model, allowed_tool_call_providers),
    )
    transcript_tools = resolve_transcript_tools(
        normalized_context.messages,
        bool(opts.get("supportsAdditionalTools", False)) or bool(opts.get("supportsToolSearch", False)),
    )

    def append_system_tool_additions(message: Any, seed: str) -> None:
        tools = (message.toolsAdded or []) if transcript_tools.anchorsAdditions else []
        if len(tools) == 0:
            return
        if opts.get("supportsAdditionalTools"):
            messages.append({
                "type": "additional_tools",
                "role": "developer",
                "tools": convert_responses_tools(tools, tool_options),
            })
            return
        if not opts.get("supportsToolSearch"):
            return
        names = [tool.name for tool in tools]
        call_id = f"pi_tool_load_{short_hash(seed + ':' + ','.join(names))}"
        messages.append({
            "type": "tool_search_call",
            "call_id": call_id,
            "execution": "client",
            "status": "completed",
            "arguments": {"query": " ".join(names), "limit": len(names)},
        })
        messages.append({
            "type": "tool_search_output",
            "call_id": call_id,
            "execution": "client",
            "status": "completed",
            "tools": convert_responses_tools(tools, {**tool_options, "toolSearchResult": True}),
        })

    include_initial_system_message = True if options is None else options.get("includeSystemPrompt", True)
    grammar_tool_input_properties: Mapping[str, str] = (
        {} if options is None else options.get("grammarToolInputProperties") or {}
    )
    compat = model.compat
    instruction_role = (
        "developer" if model.reasoning and getattr(compat, "supportsDeveloperRole", None) is not False else "system"
    )

    msg_index = 0
    for source_index, message in enumerate(transformed_messages):
        is_leading_system_message = source_index == 0 and message.role == "system"
        if message.role == "system":
            if not is_leading_system_message:
                append_system_tool_additions(message, f"system:{msg_index}")
            if not is_leading_system_message or include_initial_system_message:
                text = get_system_message_text(message) if is_leading_system_message else render_system_message_update(message)
                if len(text) > 0:
                    messages.append({"role": instruction_role, "content": sanitize_surrogates(text)})
        elif message.role == "user":
            if isinstance(message.content, str):
                messages.append(
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": sanitize_surrogates(message.content)}],
                    }
                )
            else:
                content: list[dict[str, Any]] = []
                for item in message.content:
                    if item.type == "text":
                        content.append({"type": "input_text", "text": sanitize_surrogates(item.text)})
                    else:
                        content.append(
                            {
                                "type": "input_image",
                                "detail": "auto",
                                "image_url": f"data:{item.mimeType};base64,{item.data}",
                            }
                        )
                if content:
                    messages.append({"role": "user", "content": content})
        elif message.role == "assistant":
            output: list[dict[str, Any]] = []
            assistant_message = message
            is_different_model = (
                assistant_message.model != model.id
                and assistant_message.provider == model.provider
                and assistant_message.api == model.api
            )

            for block in assistant_message.content:
                if block.type == "thinking":
                    if block.thinkingSignature:
                        output.append(json.loads(block.thinkingSignature))
                elif block.type == "text":
                    parsed_signature = parse_text_signature(block.textSignature)
                    msg_id = parsed_signature["id"] if parsed_signature else f"msg_{msg_index}"
                    if len(msg_id) > 64:
                        msg_id = f"msg_{short_hash(msg_id)}"
                    message_item: dict[str, Any] = {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": sanitize_surrogates(block.text), "annotations": []}],
                        "status": "completed",
                        "id": msg_id,
                    }
                    phase = parsed_signature.get("phase") if parsed_signature else None
                    if phase:
                        message_item["phase"] = phase
                    output.append(message_item)
                elif block.type == "toolCall":
                    parts = block.id.split("|")
                    call_id = parts[0]
                    item_id_raw = parts[1] if len(parts) > 1 else ""
                    item_id: str | None = item_id_raw or None
                    custom_input_property = grammar_tool_input_properties.get(block.name)
                    # A `function_call` item id must be an `fc_` one. Replaying a
                    # custom-tool call as a function call carries a `ctc_` id, which the
                    # endpoint rejects, so it is dropped along with the cross-model case.
                    if (is_different_model and item_id and item_id.startswith("fc_")) or (
                        custom_input_property is None and not (item_id or "").startswith("fc_")
                    ):
                        item_id = None
                    namespace = (
                        {"namespace": block.namespace}
                        if not is_different_model and block.namespace is not None
                        else {}
                    )
                    if custom_input_property is not None:
                        output.append(
                            {
                                "type": "custom_tool_call",
                                "id": item_id,
                                "call_id": call_id,
                                "name": block.name,
                                "input": sanitize_surrogates(
                                    get_grammar_tool_input(
                                        block.name, block.arguments, custom_input_property
                                    )
                                ),
                                **namespace,
                            }
                        )
                    else:
                        output.append(
                            {
                                "type": "function_call",
                                "id": item_id,
                                "call_id": call_id,
                                "name": block.name,
                                "arguments": json.dumps(block.arguments),
                                **namespace,
                            }
                        )
            if output:
                messages.extend(output)
        elif message.role == "toolResult":
            text_result = "\n".join(block.text for block in message.content if block.type == "text")
            has_images = any(block.type == "image" for block in message.content)
            has_text = len(text_result) > 0
            call_id = message.toolCallId.split("|", 1)[0]

            if has_images and "image" in model.input:
                output_parts: list[dict[str, Any]] = []
                if has_text:
                    output_parts.append({"type": "input_text", "text": sanitize_surrogates(text_result)})
                for block in message.content:
                    if block.type == "image":
                        output_parts.append(
                            {
                                "type": "input_image",
                                "detail": "auto",
                                "image_url": f"data:{block.mimeType};base64,{block.data}",
                            }
                        )
                output_value: str | list[dict[str, Any]] = output_parts
            else:
                output_value = sanitize_surrogates(text_result if has_text else "(see attached image)")

            # A grammar tool's result goes back on the custom-tool channel it came from.
            messages.append(
                {
                    "type": (
                        "custom_tool_call_output"
                        if message.toolName in grammar_tool_input_properties
                        else "function_call_output"
                    ),
                    "call_id": call_id,
                    "output": output_value,
                }
            )
        if not is_leading_system_message:
            msg_index += 1

    return messages


def convert_responses_tools(
    tools: Iterable[Tool],
    options: ConvertResponsesToolsOptions | None = None,
) -> list[dict[str, Any]]:
    options = options or {}
    default_strict = options.get("strict", False)
    supports_strict_mode = options.get("supportsStrictMode", True)
    supports_grammar_tools = options.get("supportsOpenAIGrammarTools", False)
    tool_search_result = bool(options.get("toolSearchResult"))

    converted: list[dict[str, Any]] = []
    for tool in tools:
        grammar = resolve_grammar_constrained_sampling(tool, bool(supports_grammar_tools))
        if grammar:
            # A grammar tool is not a function tool: the model emits free text that the
            # grammar constrains, and it comes back as a `custom_tool_call`.
            converted.append(
                {
                    "type": "custom",
                    "name": tool.name,
                    "description": tool.description,
                    "format": {
                        "type": "grammar",
                        "syntax": grammar.format,
                        "definition": grammar.definition,
                    },
                    **({"defer_loading": True} if tool_search_result else {}),
                }
            )
            continue

        constrained = resolve_json_schema_strict_sampling(tool, bool(supports_strict_mode))
        strict = default_strict if constrained is None else constrained
        function_tool: dict[str, Any] = {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": get_json_schema_tool_parameters(tool, strict is True),
            **({"defer_loading": True} if tool_search_result else {}),
        }
        if supports_strict_mode:
            function_tool["strict"] = strict
        converted.append(function_tool)
    return converted


def _map_stop_reason(status: str | None) -> StopReason:
    if not status:
        return "stop"
    if status == "completed":
        return "stop"
    if status == "incomplete":
        return "length"
    if status in {"failed", "cancelled"}:
        return "error"
    if status in {"in_progress", "queued"}:
        return "stop"
    raise RuntimeError(f"Unhandled stop reason: {status}")


def _custom_tool_call_input(block: ToolCall, property_name: str) -> str:
    value = block.arguments.get(property_name)
    return value if isinstance(value, str) else ""


def _append_custom_tool_call_input(
    block: ToolCall,
    property_name: str,
    buffer: GrammarToolInputJsonBuffer,
    next_input: str,
    close: bool,
) -> str | None:
    """Advance a grammar tool's text and report the JSON delta a consumer would see.

    Consumers of ``toolcall_delta`` are fed JSON, because that is what a function tool
    streams. A grammar tool streams raw text, so the buffer re-encodes it as the growing
    JSON of ``{"<property>": "<text>"}`` and hands back only the newly added slice.
    """
    delta = append_grammar_tool_input_json_delta(buffer, property_name, next_input, close)
    block.arguments = {property_name: next_input}
    return delta


async def process_responses_stream(
    openai_stream: AsyncIterable[dict[str, Any]],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: Model,
    options: OpenAIResponsesStreamOptions | None = None,
) -> None:
    current_item: dict[str, Any] | None = None
    current_block: ThinkingContent | TextContent | ToolCall | None = None
    current_tool_args = StreamingArgs()
    blocks = output.content
    saw_terminal = False

    def block_index() -> int:
        return len(blocks) - 1

    custom_input_property = "input"
    custom_input_buffer: GrammarToolInputJsonBuffer | None = None
    grammar_tool_input_properties: Mapping[str, str] = (
        {} if options is None else options.get("grammarToolInputProperties") or {}
    )

    async for event in openai_stream:
        event_type = event.get("type")
        if event_type == "response.created":
            response = event.get("response")
            if isinstance(response, dict) and isinstance(response.get("id"), str):
                output.responseId = response["id"]
        elif event_type == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "reasoning":
                current_item = item
                current_tool_args = StreamingArgs()
                current_block = ThinkingContent(thinking="")
                blocks.append(current_block)
                stream.push(ThinkingStartEvent(contentIndex=block_index(), partial=output))
            elif item_type == "message":
                current_item = item
                current_tool_args = StreamingArgs()
                current_block = TextContent(text="")
                blocks.append(current_block)
                stream.push(TextStartEvent(contentIndex=block_index(), partial=output))
            elif item_type == "function_call":
                current_item = item
                # An item that inlines its arguments seeds the buffer; the block itself
                # stays ``{}`` until the item is done, exactly as before.
                current_tool_args = StreamingArgs(item.get("arguments") or "")
                current_block = ToolCall(
                    id=f"{item.get('call_id', '')}|{item.get('id', '')}",
                    name=item.get("name", ""),
                    arguments={},
                )
                blocks.append(current_block)
                stream.push(ToolCallStartEvent(contentIndex=block_index(), partial=output))
            elif item_type == "custom_tool_call":
                # A grammar tool answers with free text, not JSON. It is surfaced as an
                # ordinary tool call whose single argument is that text, so nothing
                # downstream needs to know the difference.
                current_item = item
                current_tool_args = StreamingArgs()
                custom_input_property = grammar_tool_input_properties.get(item.get("name", ""), "input")
                custom_input_buffer = GrammarToolInputJsonBuffer()
                current_block = ToolCall(
                    id=f"{item.get('call_id', '')}|{item.get('id', '')}",
                    name=item.get("name", ""),
                    arguments={custom_input_property: item.get("input") or ""},
                )
                blocks.append(current_block)
                stream.push(ToolCallStartEvent(contentIndex=block_index(), partial=output))
        elif event_type == "response.reasoning_summary_part.added":
            if isinstance(current_item, dict) and current_item.get("type") == "reasoning":
                current_item.setdefault("summary", []).append(event.get("part"))
        elif event_type == "response.reasoning_summary_text.delta":
            if (
                isinstance(current_item, dict)
                and current_item.get("type") == "reasoning"
                and isinstance(current_block, ThinkingContent)
            ):
                summary = current_item.setdefault("summary", [])
                if summary:
                    last_part = summary[-1]
                    if isinstance(last_part, dict):
                        delta = event.get("delta", "")
                        last_part["text"] = f"{last_part.get('text', '')}{event.get('delta', '')}"
                        current_block.thinking += delta
                        stream.push(ThinkingDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
        elif event_type == "response.reasoning_summary_part.done":
            if (
                isinstance(current_item, dict)
                and current_item.get("type") == "reasoning"
                and isinstance(current_block, ThinkingContent)
            ):
                summary = current_item.setdefault("summary", [])
                if summary:
                    last_part = summary[-1]
                    if isinstance(last_part, dict):
                        last_part["text"] = f"{last_part.get('text', '')}\n\n"
                        current_block.thinking += "\n\n"
                        stream.push(ThinkingDeltaEvent(contentIndex=block_index(), delta="\n\n", partial=output))
        elif event_type == "response.reasoning_text.delta":
            if (
                isinstance(current_item, dict)
                and current_item.get("type") == "reasoning"
                and isinstance(current_block, ThinkingContent)
            ):
                delta = event.get("delta", "")
                current_block.thinking += delta
                stream.push(ThinkingDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
        elif event_type == "response.content_part.added":
            if isinstance(current_item, dict) and current_item.get("type") == "message":
                part = event.get("part")
                if isinstance(part, dict) and part.get("type") in {"output_text", "refusal"}:
                    current_item.setdefault("content", []).append(part)
        elif event_type == "response.output_text.delta":
            if isinstance(current_item, dict) and current_item.get("type") == "message" and isinstance(current_block, TextContent):
                content = current_item.get("content") or []
                if isinstance(content, list) and content:
                    last_part = content[-1]
                    if isinstance(last_part, dict) and last_part.get("type") == "output_text":
                        delta = event.get("delta", "")
                        last_part["text"] = f"{last_part.get('text', '')}{delta}"
                        current_block.text += delta
                        stream.push(TextDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
        elif event_type == "response.refusal.delta":
            if isinstance(current_item, dict) and current_item.get("type") == "message" and isinstance(current_block, TextContent):
                content = current_item.get("content") or []
                if isinstance(content, list) and content:
                    last_part = content[-1]
                    if isinstance(last_part, dict) and last_part.get("type") == "refusal":
                        delta = event.get("delta", "")
                        last_part["refusal"] = f"{last_part.get('refusal', '')}{delta}"
                        current_block.text += delta
                        stream.push(TextDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
        elif event_type == "response.function_call_arguments.delta":
            if isinstance(current_item, dict) and current_item.get("type") == "function_call" and isinstance(current_block, ToolCall):
                delta = event.get("delta", "")
                current_tool_args.append(delta)
                current_block.arguments = current_tool_args.arguments
                stream.push(ToolCallDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
        elif event_type == "response.function_call_arguments.done":
            if isinstance(current_item, dict) and current_item.get("type") == "function_call" and isinstance(current_block, ToolCall):
                previous_partial_json = current_tool_args.raw
                # The arguments are complete here: replace the buffer and parse it whole.
                current_tool_args = StreamingArgs(event.get("arguments", ""))
                current_tool_args.finish_into(current_block)
                if current_tool_args.raw.startswith(previous_partial_json):
                    delta = current_tool_args.raw[len(previous_partial_json) :]
                    if delta:
                        stream.push(ToolCallDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
        elif event_type == "response.custom_tool_call_input.delta":
            if (
                isinstance(current_item, dict)
                and current_item.get("type") == "custom_tool_call"
                and isinstance(current_block, ToolCall)
                and custom_input_buffer is not None
            ):
                next_input = _custom_tool_call_input(current_block, custom_input_property) + event.get("delta", "")
                delta = _append_custom_tool_call_input(
                    current_block, custom_input_property, custom_input_buffer, next_input, False
                )
                if delta:
                    stream.push(ToolCallDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
        elif event_type == "response.custom_tool_call_input.done":
            if (
                isinstance(current_item, dict)
                and current_item.get("type") == "custom_tool_call"
                and isinstance(current_block, ToolCall)
                and custom_input_buffer is not None
            ):
                delta = _append_custom_tool_call_input(
                    current_block, custom_input_property, custom_input_buffer, event.get("input", ""), True
                )
                if delta:
                    stream.push(ToolCallDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "reasoning" and isinstance(current_block, ThinkingContent):
                summary = item.get("summary") if isinstance(item.get("summary"), list) else []
                content = item.get("content") if isinstance(item.get("content"), list) else []
                summary_text = "\n\n".join(part.get("text", "") for part in summary if isinstance(part, dict))
                content_text = "\n\n".join(part.get("text", "") for part in content if isinstance(part, dict))
                current_block.thinking = summary_text or content_text or current_block.thinking
                current_block.thinkingSignature = json.dumps(item, separators=(",", ":"))
                stream.push(ThinkingEndEvent(contentIndex=block_index(), content=current_block.thinking, partial=output))
                current_block = None
            elif item_type == "message" and isinstance(current_block, TextContent):
                content = item.get("content") if isinstance(item.get("content"), list) else []
                text = "".join(
                    part.get("text", "") if part.get("type") == "output_text" else part.get("refusal", "")
                    for part in content
                    if isinstance(part, dict)
                )
                current_block.text = text
                current_block.textSignature = encode_text_signature_v1(item.get("id", ""), item.get("phase"))
                stream.push(TextEndEvent(contentIndex=block_index(), content=current_block.text, partial=output))
                current_block = None
            elif item_type == "function_call":
                call_id = f"{item.get('call_id', '')}|{item.get('id', '')}"
                tool_call = next((block for block in output.content
                                  if isinstance(block, ToolCall) and block.id == call_id), None)
                if tool_call is None:
                    tool_call = ToolCall(id=call_id, name=item.get("name", ""), arguments={})
                    output.content.append(tool_call)
                final_args = (current_tool_args if tool_call is current_block and current_tool_args.raw
                              else StreamingArgs(item.get("arguments") or "{}"))
                final_args.finish_into(tool_call)
                if tool_call is current_block:
                    current_tool_args = StreamingArgs()
                    current_block = None
                stream.push(ToolCallEndEvent(contentIndex=output.content.index(tool_call),
                                            toolCall=tool_call, partial=output))
            elif item_type == "custom_tool_call" and isinstance(current_block, ToolCall):
                final_input = item.get("input")
                if final_input is None:
                    final_input = _custom_tool_call_input(current_block, custom_input_property)
                if custom_input_buffer is not None:
                    delta = _append_custom_tool_call_input(
                        current_block, custom_input_property, custom_input_buffer, final_input, True
                    )
                    if delta:
                        stream.push(ToolCallDeltaEvent(contentIndex=block_index(), delta=delta, partial=output))
                tool_call = current_block
                custom_input_buffer = None
                current_tool_args = StreamingArgs()
                current_block = None
                stream.push(ToolCallEndEvent(contentIndex=block_index(), toolCall=tool_call, partial=output))
        elif event_type == "response.completed":
            saw_terminal = True
            response = event.get("response")
            if isinstance(response, dict):
                if isinstance(response.get("id"), str):
                    output.responseId = response["id"]
                usage_data = response.get("usage")
                if isinstance(usage_data, dict):
                    cached_tokens = 0
                    input_details = usage_data.get("input_tokens_details")
                    if isinstance(input_details, dict):
                        cached_tokens = int(input_details.get("cached_tokens") or 0)
                    input_tokens = int(usage_data.get("input_tokens") or 0)
                    output_tokens = int(usage_data.get("output_tokens") or 0)
                    total_tokens = int(usage_data.get("total_tokens") or (input_tokens + output_tokens))
                    output.usage = Usage(
                        input=max(0, input_tokens - cached_tokens),
                        output=output_tokens,
                        cacheRead=cached_tokens,
                        cacheWrite=0,
                        totalTokens=total_tokens,
                        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
                    )
                else:
                    output.usage = _empty_usage()
                calculate_cost(model, output.usage)
                if options and options.get("applyServiceTierPricing"):
                    resolve_service_tier = options.get("resolveServiceTier")
                    request_service_tier = options.get("serviceTier")
                    response_service_tier = response.get("service_tier")
                    service_tier = (
                        resolve_service_tier(response_service_tier, request_service_tier)
                        if callable(resolve_service_tier)
                        else response_service_tier or request_service_tier
                    )
                    options["applyServiceTierPricing"](output.usage, service_tier)
                output.stopReason = _map_stop_reason(response.get("status"))
                if any(block.type == "toolCall" for block in output.content) and output.stopReason == "stop":
                    output.stopReason = "toolUse"
        elif event_type == "error":
            code = event.get("code")
            raise RuntimeError(f"Error Code {code}: {event.get('message')}")
        elif event_type == "response.failed":
            response = event.get("response") if isinstance(event.get("response"), dict) else {}
            error = response.get("error") if isinstance(response, dict) else None
            incomplete_details = response.get("incomplete_details") if isinstance(response, dict) else None
            if isinstance(error, dict):
                raise RuntimeError(f"{error.get('code', 'unknown')}: {error.get('message', 'no message')}")
            if isinstance(incomplete_details, dict) and incomplete_details.get("reason"):
                raise RuntimeError(f"incomplete: {incomplete_details['reason']}")
            raise RuntimeError("Unknown error (no error details in response)")

    if not saw_terminal:
        # A proxy or an idle timeout can close the SSE connection cleanly mid-response.
        # Without this the caller sees a normal return and reports the constructor's
        # default stopReason="stop": half a message delivered as a finished turn, with
        # zero usage and no TextEndEvent for the block still open. Every other family
        # guards this (anthropic on message_stop, completions on finish_reason).
        # An abort never reaches here -- it surfaces as an exception out of the event
        # iterable, so it stays distinguishable from a truncated stream.
        raise RuntimeError("Responses stream ended before response.completed")


__all__ = [
    "ConvertResponsesMessagesOptions",
    "ConvertResponsesToolsOptions",
    "OpenAIResponsesStreamOptions",
    "convert_responses_messages",
    "convert_responses_tools",
    "encode_text_signature_v1",
    "parse_text_signature",
    "process_responses_stream",
]
