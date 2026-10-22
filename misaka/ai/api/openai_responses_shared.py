"""Translated from pi's ``packages/ai/src/api/openai-responses-shared.ts`` (v1.3.0).

MISAKA fork, kept to what the Python SDK and platform force (each is marked where it happens):

- Stream events and output items are the parsed JSON dicts, so their shape is checked before use.
- A streaming tool call's ``partialJson`` / ``customInput`` scratch fields ride beside the block, keyed by
  content index, because the content blocks are strict models.
- A tool call's streamed arguments are parsed sparingly (``StreamingArgs``) and finalized through
  ``finish_into`` (upstream's ``parseToolCallArguments``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from misaka.ai.api.constrained_sampling import (
    GrammarToolInputJsonBuffer,
    appendGrammarToolInputJsonDelta,
    getGrammarToolInput,
    getJsonSchemaToolParameters,
    resolveGrammarConstrainedSampling,
    resolveJsonSchemaStrictSampling,
)
from misaka.ai.api.transform_messages import transformMessages
from misaka.ai.models import calculateCost
from misaka.ai.types import (
    AssistantMessage,
    ImageContent,
    Model,
    SystemMessage,
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
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.hash import shortHash
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.ai.utils.text import getSystemMessageText, renderSystemMessageUpdate
from misaka.ai.utils.transcript import resolveTranscript, resolveTranscriptTools
from misaka.utils.js import (
    UNDEFINED,
    jsonParse,
    jsonStringify,
    jsString,
    nullishCoalesce,
)
from misaka.utils.values import maybe_await

# =============================================================================
# Utilities
# =============================================================================


def _encodeTextSignatureV1(id: str, phase: Any = None) -> str:
    payload: dict[str, Any] = {"v": 1, "id": id}
    if phase:
        payload["phase"] = phase
    return jsonStringify(payload)


def _parseTextSignature(signature: str | None) -> dict[str, Any] | None:
    if not signature:
        return None
    if signature.startswith("{"):
        try:
            parsed = jsonParse(signature)
            if isinstance(parsed, dict) and parsed.get("v") == 1 and isinstance(parsed.get("id"), str):
                if parsed.get("phase") in ("commentary", "final_answer"):
                    return {"id": parsed["id"], "phase": parsed["phase"]}
                return {"id": parsed["id"]}
        except ValueError:
            # Fall through to legacy plain-string handling.
            pass
    return {"id": signature}


def _convertToolResultOutput(
    model: Model,
    content: Sequence[TextContent | ImageContent],
) -> str | list[dict[str, Any]]:
    textResult = "\n".join(c.text for c in content if c.type == "text")
    images = [c for c in content if c.type == "image"]
    hasText = len(textResult) > 0

    if len(images) == 0 or "image" not in model.input:
        return sanitizeSurrogates(textResult if hasText else "(see attached image)" if len(images) > 0 else "(no tool output)")

    output: list[dict[str, Any]] = []
    if hasText:
        output.append({"type": "input_text", "text": sanitizeSurrogates(textResult)})
    for image in images:
        output.append({
            "type": "input_image",
            "detail": "auto",
            "image_url": f"data:{image.mimeType};base64,{image.data}",
        })
    return output


class OpenAIResponsesStreamOptions(TypedDict, total=False):
    onProviderStreamEvent: Any
    serviceTier: Any
    grammarToolInputProperties: Mapping[str, str]
    resolveServiceTier: Any
    applyServiceTierPricing: Any


class ConvertResponsesMessagesOptions(TypedDict, total=False):
    includeSystemPrompt: bool
    grammarToolInputProperties: Mapping[str, str]
    supportsMidConvoSystemMessages: bool
    """Whether later system messages are sent in place; otherwise they are folded into the leading prompt."""
    supportsAdditionalTools: bool
    supportsToolSearch: bool
    toolOptions: ConvertResponsesToolsOptions
    configurationUpdateEffort: str
    """Effort for the reply being generated. When set, the thinking level is sent as
    configuration_update items instead of a changing reasoning.effort, which would invalidate the
    prompt cache. Each item sets the effort from that point on: one goes before every earlier
    reply with the level recorded on it (`providerThinkingLevel`), and one with this effort goes
    at the end. Earlier input therefore never changes. OpenAI documents the item before user
    messages only. After tool results it was verified on gpt-6-luna (October 2026) to be accepted
    and to apply to the next reply."""


class ConvertResponsesToolsOptions(TypedDict, total=False):
    strict: bool | None
    supportsStrictMode: bool
    supportsOpenAIGrammarTools: bool
    toolSearchResult: bool


# =============================================================================
# Message conversion
# =============================================================================


def convertResponsesMessages(
    model: Model,
    context: TranscriptContext,
    allowedToolCallProviders: set[str] | frozenset[str],
    options: ConvertResponsesMessagesOptions | None = None,
) -> list[dict[str, Any]]:
    normalizedContext = resolveTranscript(context, None if options is None else options.get("supportsMidConvoSystemMessages"))
    messages: list[dict[str, Any]] = []

    def normalizeIdPart(part: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", part)
        normalized = sanitized[:64] if len(sanitized) > 64 else sanitized
        return re.sub(r"_+$", "", normalized)

    def buildForeignResponsesItemId(itemId: str) -> str:
        normalized = f"fc_{shortHash(itemId)}"
        return normalized[:64] if len(normalized) > 64 else normalized

    def normalizeToolCallId(id: str, _targetModel: Model, source: AssistantMessage) -> str:
        if model.provider not in allowedToolCallProviders:
            return normalizeIdPart(id)
        if "|" not in id:
            return normalizeIdPart(id)
        callId, itemId = id.split("|")[:2]
        normalizedCallId = normalizeIdPart(callId)
        isForeignToolCall = source.provider != model.provider or source.api != model.api
        normalizedItemId = buildForeignResponsesItemId(itemId) if isForeignToolCall else normalizeIdPart(itemId)
        # OpenAI Responses API requires item id to start with "fc"
        if not normalizedItemId.startswith("fc_"):
            normalizedItemId = normalizeIdPart(f"fc_{normalizedItemId}")
        return f"{normalizedCallId}|{normalizedItemId}"

    transformedMessages = transformMessages(normalizedContext.messages, model, normalizeToolCallId)
    transcriptTools = resolveTranscriptTools(
        normalizedContext.messages,
        nullishCoalesce(None if options is None else options.get("supportsAdditionalTools"), False)
        or nullishCoalesce(None if options is None else options.get("supportsToolSearch"), False),
    )

    def appendSystemToolAdditions(message: SystemMessage, seed: str) -> None:
        tools = nullishCoalesce(message.toolsAdded, []) if transcriptTools.anchorsAdditions else []
        if len(tools) == 0:
            return
        if options is not None and options.get("supportsAdditionalTools"):
            messages.append({
                "type": "additional_tools",
                "role": "developer",
                "tools": convertResponsesTools(tools, options.get("toolOptions")),
            })
            return
        if options is None or not options.get("supportsToolSearch"):
            return
        names = [tool.name for tool in tools]
        callId = f"pi_tool_load_{shortHash(f'{seed}:{",".join(names)}')}"
        messages.append({
            "type": "tool_search_call",
            "call_id": callId,
            "execution": "client",
            "status": "completed",
            "arguments": {"query": " ".join(names), "limit": len(names)},
        })
        messages.append({
            "type": "tool_search_output",
            "call_id": callId,
            "execution": "client",
            "status": "completed",
            "tools": convertResponsesTools(tools, {**(options.get("toolOptions") or {}), "toolSearchResult": True}),
        })

    includeInitialSystemMessage = nullishCoalesce(None if options is None else options.get("includeSystemPrompt"), True)

    def pushConfigurationUpdate(effort: str) -> None:
        messages.append({"type": "configuration_update", "reasoning": {"effort": effort}})

    configurationUpdateEffort = None if options is None else options.get("configurationUpdateEffort")
    compat = model.compat
    instructionRole = (
        "developer" if model.reasoning and getattr(compat, "supportsDeveloperRole", None) is not False else "system"
    )
    grammarToolInputProperties = None if options is None else options.get("grammarToolInputProperties")

    msgIndex = 0
    for sourceIndex, msg in enumerate(transformedMessages):
        isLeadingSystemMessage = sourceIndex == 0 and msg.role == "system"
        if msg.role == "system":
            if not isLeadingSystemMessage:
                appendSystemToolAdditions(msg, f"system:{msgIndex}")
            if not isLeadingSystemMessage or includeInitialSystemMessage:
                text = getSystemMessageText(msg) if isLeadingSystemMessage else renderSystemMessageUpdate(msg)
                if len(text) > 0:
                    messages.append({"role": instructionRole, "content": sanitizeSurrogates(text)})
        elif msg.role == "user":
            if isinstance(msg.content, str):
                messages.append({
                    "role": "user",
                    "content": [{"type": "input_text", "text": sanitizeSurrogates(msg.content)}],
                })
            else:
                content: list[dict[str, Any]] = [
                    {
                        "type": "input_text",
                        "text": sanitizeSurrogates(item.text),
                    }
                    if item.type == "text"
                    else {
                        "type": "input_image",
                        "detail": "auto",
                        "image_url": f"data:{item.mimeType};base64,{item.data}",
                    }
                    for item in msg.content
                ]
                if len(content) == 0:
                    continue
                messages.append({
                    "role": "user",
                    "content": content,
                })
        elif msg.role == "assistant":
            output: list[dict[str, Any]] = []
            assistantMsg = msg
            isSameProviderAndApi = assistantMsg.provider == model.provider and assistantMsg.api == model.api
            isSameModel = isSameProviderAndApi and assistantMsg.model == model.id
            isDifferentModel = isSameProviderAndApi and assistantMsg.model != model.id
            textBlockIndex = 0

            for block in msg.content:
                if block.type == "thinking":
                    if block.thinkingSignature:
                        reasoningItem = jsonParse(block.thinkingSignature)
                        output.append(reasoningItem)
                elif block.type == "text":
                    textBlock = block
                    parsedSignature = _parseTextSignature(textBlock.textSignature)
                    fallbackMessageId = f"msg_pi_{msgIndex}" if textBlockIndex == 0 else f"msg_pi_{msgIndex}_{textBlockIndex}"
                    textBlockIndex += 1
                    # OpenAI requires id to be max 64 characters
                    msgId = None if parsedSignature is None else parsedSignature["id"]
                    if not msgId:
                        msgId = fallbackMessageId
                    elif len(msgId) > 64:
                        msgId = f"msg_{shortHash(msgId)}"
                    # MISAKA fork: pi's `phase: undefined` leaves the key out of the JSON; a Python
                    # `None` would be sent as `null`.
                    phase = None if parsedSignature is None else parsedSignature.get("phase")
                    output.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": sanitizeSurrogates(textBlock.text), "annotations": []}],
                        "status": "completed",
                        "id": msgId,
                        **({} if phase is None else {"phase": phase}),
                    })
                elif block.type == "toolCall":
                    toolCall = block
                    callId, itemIdRaw = (toolCall.id.split("|") + [None])[:2]
                    customInputProperty = (
                        None if grammarToolInputProperties is None else grammarToolInputProperties.get(toolCall.name)
                    )
                    itemId: str | None = itemIdRaw

                    # For different-model messages, set id to undefined to avoid pairing validation.
                    # OpenAI tracks which item IDs were paired with rs_xxx reasoning items.
                    # By omitting the id, we avoid triggering that validation (like cross-provider does).
                    # Also drop ids that do not match the replayed item type: function_call ids must be fc_*
                    # and custom_tool_call ids must be ctc_*. Foreign tool call ids are normalized to fc_*, and
                    # a call can switch between the two types when grammar tool support differs.
                    itemIdPrefix = "fc_" if customInputProperty is None else "ctc_"
                    if isDifferentModel or itemId is None or not itemId.startswith(itemIdPrefix):
                        itemId = None

                    # MISAKA fork: pi's `id: undefined` leaves the key out of the JSON; a Python
                    # `None` would be sent as `null`.
                    itemIdField = {} if itemId is None else {"id": itemId}
                    if customInputProperty is not None:
                        output.append({
                            "type": "custom_tool_call",
                            **itemIdField,
                            "call_id": callId,
                            "name": toolCall.name,
                            "input": sanitizeSurrogates(
                                getGrammarToolInput(toolCall.name, toolCall.arguments, customInputProperty),
                            ),
                            **({"namespace": toolCall.namespace} if isSameModel and toolCall.namespace is not None else {}),
                        })
                    else:
                        output.append({
                            "type": "function_call",
                            **itemIdField,
                            "call_id": callId,
                            "name": toolCall.name,
                            "arguments": jsonStringify(toolCall.arguments),
                            **({"namespace": toolCall.namespace} if isSameModel and toolCall.namespace is not None else {}),
                        })
            if len(output) == 0:
                continue
            if configurationUpdateEffort is not None and isSameModel and assistantMsg.providerThinkingLevel is not None:
                pushConfigurationUpdate(assistantMsg.providerThinkingLevel)
            messages.extend(output)
        elif msg.role == "toolResult":
            callId = msg.toolCallId.split("|")[0]
            toolOutput = _convertToolResultOutput(model, msg.content)

            if grammarToolInputProperties is not None and msg.toolName in grammarToolInputProperties:
                messages.append({
                    "type": "custom_tool_call_output",
                    "call_id": callId,
                    "output": toolOutput,
                })
            else:
                messages.append({
                    "type": "function_call_output",
                    "call_id": callId,
                    "output": toolOutput,
                })
        if not isLeadingSystemMessage:
            msgIndex += 1
    if configurationUpdateEffort is not None:
        pushConfigurationUpdate(configurationUpdateEffort)

    return messages


# =============================================================================
# Tool conversion
# =============================================================================


def convertResponsesTools(tools: Sequence[Tool], options: ConvertResponsesToolsOptions | None = None) -> list[dict[str, Any]]:
    defaultStrict = False if options is None or "strict" not in options else options["strict"]
    supportsStrictMode = nullishCoalesce(None if options is None else options.get("supportsStrictMode"), True)
    supportsOpenAIGrammarTools = nullishCoalesce(None if options is None else options.get("supportsOpenAIGrammarTools"), False)
    toolSearchResult = options is not None and options.get("toolSearchResult")

    def convert(tool: Tool) -> dict[str, Any]:
        grammar = resolveGrammarConstrainedSampling(tool, supportsOpenAIGrammarTools)
        if grammar:
            return {
                "type": "custom",
                "name": tool.name,
                "description": tool.description,
                "format": {
                    "type": "grammar",
                    "syntax": grammar.format,
                    "definition": grammar.definition,
                },
                **({"defer_loading": True} if toolSearchResult else {}),
            }

        constrainedStrict = resolveJsonSchemaStrictSampling(tool, supportsStrictMode)
        strict = nullishCoalesce(constrainedStrict, defaultStrict)
        functionTool: dict[str, Any] = {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": getJsonSchemaToolParameters(tool, strict is True),
            **({"defer_loading": True} if toolSearchResult else {}),
        }
        if supportsStrictMode:
            functionTool["strict"] = strict
        return functionTool

    return [convert(tool) for tool in tools]


# =============================================================================
# Stream processing
# =============================================================================


@dataclass
class _CustomToolCallInput:
    property: str
    jsonBuffer: GrammarToolInputJsonBuffer


@dataclass
class _StreamingToolCallScratch:
    """MISAKA fork: upstream's ``StreamingToolCall`` widens the block itself with the
    scratch fields ``partialJson`` and ``customInput`` and deletes them once the call is
    finished. Pydantic blocks forbid extra fields, so the scratch rides beside the block,
    keyed by content index; ``None`` stands for a deleted field. ``partialJson`` is a
    ``StreamingArgs`` so the live view does not re-parse the whole buffer on every delta.
    """

    partialJson: StreamingArgs | None = None
    customInput: _CustomToolCallInput | None = None


# MISAKA fork (both helpers): the scratch rides beside the block, so it is passed in.
def _getCustomToolCallInput(block: ToolCall, scratch: _StreamingToolCallScratch) -> str:
    property = None if scratch.customInput is None else scratch.customInput.property
    if property is None:
        return ""
    value = block.arguments.get(property)
    return value if isinstance(value, str) else ""


def _appendCustomToolCallInput(
    block: ToolCall, scratch: _StreamingToolCallScratch, nextInput: str, close: bool
) -> str | None:
    customInput = scratch.customInput
    if not customInput:
        return None
    delta = appendGrammarToolInputJsonDelta(customInput.jsonBuffer, customInput.property, nextInput, close)
    block.arguments = {customInput.property: nextInput}
    return delta


@dataclass
class _ResponsesOutputSlot:
    type: Literal["thinking", "text", "toolCall"]
    block: Any
    contentIndex: int


async def processResponsesStream(
    openaiStream: AsyncIterable[dict[str, Any]],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: Model,
    options: OpenAIResponsesStreamOptions | None = None,
) -> None:
    sawTerminalResponseEvent = False
    outputSlots: dict[Any, _ResponsesOutputSlot] = {}
    reasoningBlocksById: dict[Any, ThinkingContent] = {}
    # MISAKA fork: the scratch fields of the streaming tool calls, keyed by content index.
    streamingToolCalls: dict[int, _StreamingToolCallScratch] = {}
    grammarToolInputProperties = None if options is None else options.get("grammarToolInputProperties")

    def applyMessagePhaseStopReason(item: dict[str, Any]) -> None:
        if item.get("type") == "message" and item.get("phase") == "final_answer":
            output.stopReason = "stop"

    def getSlot(outputIndex: Any, type: str) -> _ResponsesOutputSlot | None:
        slot = outputSlots.get(outputIndex)
        return slot if slot is not None and slot.type == type else None

    def pushToolCallDelta(slot: _ResponsesOutputSlot, delta: str | None) -> None:
        if delta is None:
            return
        stream.push(
            ToolCallDeltaEvent(
                contentIndex=slot.contentIndex,
                delta=delta,
                partial=output,
            )
        )

    def createSlot(outputIndex: Any, item: dict[str, Any]) -> _ResponsesOutputSlot | None:
        if item.get("type") == "reasoning":
            block = ThinkingContent(thinking="")
            output.content.append(block)
            slot = _ResponsesOutputSlot(
                type="thinking",
                block=block,
                contentIndex=len(output.content) - 1,
            )
            outputSlots[outputIndex] = slot
            stream.push(ThinkingStartEvent(contentIndex=slot.contentIndex, partial=output))
            return slot
        if item.get("type") == "message":
            applyMessagePhaseStopReason(item)
            block = TextContent(text="")
            output.content.append(block)
            slot = _ResponsesOutputSlot(type="text", block=block, contentIndex=len(output.content) - 1)
            outputSlots[outputIndex] = slot
            stream.push(TextStartEvent(contentIndex=slot.contentIndex, partial=output))
            return slot
        if item.get("type") == "function_call":
            block = ToolCall(
                id=f"{jsString(item.get('call_id', UNDEFINED))}|{jsString(item.get('id', UNDEFINED))}",
                name=item.get("name", ""),
                arguments={},
                namespace=item.get("namespace"),
            )
            output.content.append(block)
            slot = _ResponsesOutputSlot(
                type="toolCall",
                block=block,
                contentIndex=len(output.content) - 1,
            )
            streamingToolCalls[slot.contentIndex] = _StreamingToolCallScratch(
                partialJson=StreamingArgs(item.get("arguments") or "")
            )
            outputSlots[outputIndex] = slot
            stream.push(ToolCallStartEvent(contentIndex=slot.contentIndex, partial=output))
            return slot
        if item.get("type") == "custom_tool_call":
            inputProperty = nullishCoalesce(
                None if grammarToolInputProperties is None else grammarToolInputProperties.get(item.get("name")),
                "input",
            )
            input = item.get("input") or ""
            block = ToolCall(
                id=f"{jsString(item.get('call_id', UNDEFINED))}|{jsString(item.get('id', UNDEFINED))}",
                name=item.get("name", ""),
                arguments={inputProperty: input},
                namespace=item.get("namespace"),
            )
            output.content.append(block)
            slot = _ResponsesOutputSlot(
                type="toolCall",
                block=block,
                contentIndex=len(output.content) - 1,
            )
            streamingToolCalls[slot.contentIndex] = _StreamingToolCallScratch(
                customInput=_CustomToolCallInput(
                    property=inputProperty,
                    jsonBuffer=GrammarToolInputJsonBuffer(input="", started=False, closed=False),
                )
            )
            outputSlots[outputIndex] = slot
            stream.push(ToolCallStartEvent(contentIndex=slot.contentIndex, partial=output))
            return slot
        return None

    def getOrCreateSlot(outputIndex: Any, item: dict[str, Any]) -> _ResponsesOutputSlot | None:
        slot = outputSlots.get(outputIndex)
        return slot if slot is not None else createSlot(outputIndex, item)

    # Azure OpenAI can omit reasoning.encrypted_content from response.output_item.done
    # and provide it only in response.completed.response.output. Backfill the
    # persisted reasoning signature from the terminal response to keep store:false
    # multi-turn replay stateless. See https://github.com/earendil-works/pi/issues/6409.
    def backfillReasoningSignatures(responseOutput: list[Any]) -> None:
        for item in responseOutput:
            if not isinstance(item, dict) or item.get("type") != "reasoning" or not item.get("encrypted_content"):
                continue
            block = reasoningBlocksById.get(item.get("id"))
            if block is None or not block.thinkingSignature:
                continue

            storedItem = jsonParse(block.thinkingSignature)
            if storedItem.get("encrypted_content"):
                continue
            block.thinkingSignature = jsonStringify({
                **storedItem,
                "encrypted_content": item["encrypted_content"],
            })

    def finalizeResponse(response: Any) -> None:
        nonlocal sawTerminalResponseEvent
        sawTerminalResponseEvent = True
        response = response if isinstance(response, dict) else {}
        backfillReasoningSignatures(nullishCoalesce(response.get("output"), []))
        if response.get("id"):
            output.responseId = response["id"]
        if response.get("usage"):
            inputDetails = response["usage"].get("input_tokens_details") or {}
            cachedTokens = inputDetails.get("cached_tokens") or 0
            cacheWriteTokens = inputDetails.get("cache_write_tokens") or 0
            output.usage = Usage(
                # OpenAI includes cached and cache-write tokens in input_tokens, so subtract both.
                input=max(0, (response["usage"].get("input_tokens") or 0) - cachedTokens - cacheWriteTokens),
                output=response["usage"].get("output_tokens") or 0,
                cacheRead=cachedTokens,
                cacheWrite=cacheWriteTokens,
                reasoning=(response["usage"].get("output_tokens_details") or {}).get("reasoning_tokens") or 0,
                totalTokens=response["usage"].get("total_tokens") or 0,
                cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
            )
        calculateCost(model, output.usage)
        if options is not None and options.get("applyServiceTierPricing"):
            serviceTier = (
                options["resolveServiceTier"](response.get("service_tier"), options.get("serviceTier"))
                if options.get("resolveServiceTier")
                else nullishCoalesce(response.get("service_tier"), options.get("serviceTier"))
            )
            options["applyServiceTierPricing"](output.usage, serviceTier)
        # Map status to stop reason. For incomplete responses, retain the provider's
        # specific reason so max-output truncation and content filtering stay distinct.
        status = response.get("status")
        incompleteDetails = response.get("incomplete_details")
        reason = incompleteDetails.get("reason") if isinstance(incompleteDetails, dict) else None
        incompleteReason = reason if isinstance(reason, str) else None
        output.rawStopReason = f"{status}.{incompleteReason}" if incompleteReason else status
        mappedStop = _mapStopReason(status, incompleteReason)
        output.stopReason = mappedStop["stopReason"]
        output.errorMessage = mappedStop.get("errorMessage")
        if any(b.type == "toolCall" for b in output.content) and output.stopReason == "stop":
            output.stopReason = "toolUse"

    async for event in openaiStream:
        onProviderStreamEvent = None if options is None else options.get("onProviderStreamEvent")
        if onProviderStreamEvent:
            await maybe_await(onProviderStreamEvent(event, model))
        if event.get("type") == "response.created":
            response = event.get("response")
            if isinstance(response, dict):
                output.responseId = response.get("id")
        elif event.get("type") == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict):
                createSlot(event.get("output_index"), item)
        elif event.get("type") == "response.reasoning_summary_text.delta":
            slot = getSlot(event.get("output_index"), "thinking")
            if not slot:
                continue
            slot.block.thinking += event.get("delta", "")
            stream.push(
                ThinkingDeltaEvent(
                    contentIndex=slot.contentIndex,
                    delta=event.get("delta", ""),
                    partial=output,
                )
            )
        elif event.get("type") == "response.reasoning_summary_part.done":
            slot = getSlot(event.get("output_index"), "thinking")
            if not slot:
                continue
            slot.block.thinking += "\n\n"
            stream.push(
                ThinkingDeltaEvent(
                    contentIndex=slot.contentIndex,
                    delta="\n\n",
                    partial=output,
                )
            )
        elif event.get("type") == "response.reasoning_text.delta":
            slot = getSlot(event.get("output_index"), "thinking")
            if not slot:
                continue
            slot.block.thinking += event.get("delta", "")
            stream.push(
                ThinkingDeltaEvent(
                    contentIndex=slot.contentIndex,
                    delta=event.get("delta", ""),
                    partial=output,
                )
            )
        elif event.get("type") == "response.output_text.delta":  # noqa: SIM114 - upstream keeps the refusal branch apart
            slot = getSlot(event.get("output_index"), "text")
            if not slot:
                continue
            slot.block.text += event.get("delta", "")
            stream.push(
                TextDeltaEvent(
                    contentIndex=slot.contentIndex,
                    delta=event.get("delta", ""),
                    partial=output,
                )
            )
        elif event.get("type") == "response.refusal.delta":
            slot = getSlot(event.get("output_index"), "text")
            if not slot:
                continue
            slot.block.text += event.get("delta", "")
            stream.push(
                TextDeltaEvent(
                    contentIndex=slot.contentIndex,
                    delta=event.get("delta", ""),
                    partial=output,
                )
            )
        elif event.get("type") == "response.function_call_arguments.delta":
            slot = getSlot(event.get("output_index"), "toolCall")
            scratch = None if slot is None else streamingToolCalls.get(slot.contentIndex)
            if not slot or scratch is None or scratch.partialJson is None:
                continue
            scratch.partialJson.append(event.get("delta", ""))
            slot.block.arguments = scratch.partialJson.arguments
            pushToolCallDelta(slot, event.get("delta", ""))
        elif event.get("type") == "response.function_call_arguments.done":
            slot = getSlot(event.get("output_index"), "toolCall")
            scratch = None if slot is None else streamingToolCalls.get(slot.contentIndex)
            if not slot or scratch is None or scratch.partialJson is None:
                continue
            previousPartialJson = scratch.partialJson.raw
            scratch.partialJson = StreamingArgs(event.get("arguments", ""))
            slot.block.arguments = scratch.partialJson.arguments

            if event.get("arguments", "").startswith(previousPartialJson):
                delta = event.get("arguments", "")[len(previousPartialJson) :]
                if len(delta) > 0:
                    pushToolCallDelta(slot, delta)
        elif event.get("type") == "response.custom_tool_call_input.delta":
            slot = getSlot(event.get("output_index"), "toolCall")
            scratch = None if slot is None else streamingToolCalls.get(slot.contentIndex)
            if not slot or scratch is None or not scratch.customInput:
                continue
            pushToolCallDelta(
                slot,
                _appendCustomToolCallInput(
                    slot.block, scratch, _getCustomToolCallInput(slot.block, scratch) + event.get("delta", ""), False
                ),
            )
        elif event.get("type") == "response.custom_tool_call_input.done":
            slot = getSlot(event.get("output_index"), "toolCall")
            scratch = None if slot is None else streamingToolCalls.get(slot.contentIndex)
            if not slot or scratch is None or not scratch.customInput:
                continue
            pushToolCallDelta(slot, _appendCustomToolCallInput(slot.block, scratch, event.get("input", ""), True))
        elif event.get("type") == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            applyMessagePhaseStopReason(item)
            slot = getOrCreateSlot(event.get("output_index"), item)
            scratch = None if slot is None else streamingToolCalls.get(slot.contentIndex)

            if item.get("type") == "reasoning" and slot is not None and slot.type == "thinking":
                summaryText = "\n\n".join(
                    nullishCoalesce(s.get("text"), "") for s in nullishCoalesce(item.get("summary"), []) if isinstance(s, dict)
                )
                contentText = "\n\n".join(
                    nullishCoalesce(c.get("text"), "") for c in nullishCoalesce(item.get("content"), []) if isinstance(c, dict)
                )
                slot.block.thinking = summaryText or contentText or slot.block.thinking
                slot.block.thinkingSignature = jsonStringify(item)
                reasoningBlocksById[item.get("id")] = slot.block
                stream.push(
                    ThinkingEndEvent(
                        contentIndex=slot.contentIndex,
                        content=slot.block.thinking,
                        partial=output,
                    )
                )
                outputSlots.pop(event.get("output_index"), None)
            elif item.get("type") == "message" and slot is not None and slot.type == "text":
                slot.block.text = (
                    "".join(
                        nullishCoalesce(c.get("text") if c.get("type") == "output_text" else c.get("refusal"), "")
                        for c in nullishCoalesce(item.get("content"), [])
                        if isinstance(c, dict)
                    )
                    or ""
                )
                slot.block.textSignature = _encodeTextSignatureV1(item.get("id", ""), item.get("phase"))
                stream.push(
                    TextEndEvent(
                        contentIndex=slot.contentIndex,
                        content=slot.block.text,
                        partial=output,
                    )
                )
                outputSlots.pop(event.get("output_index"), None)
            elif (
                item.get("type") == "function_call"
                and slot is not None
                and slot.type == "toolCall"
                and scratch is not None
                and scratch.partialJson is not None
            ):
                # `parseToolCallArguments(item.arguments || partialJson)`.
                (StreamingArgs(item["arguments"]) if item.get("arguments") else scratch.partialJson).finish_into(slot.block)
                if item.get("namespace") is not None:
                    slot.block.namespace = item["namespace"]
                # Finalize in-place and strip the scratch buffer so replay only
                # carries parsed arguments.
                scratch.partialJson = None
                stream.push(
                    ToolCallEndEvent(
                        contentIndex=slot.contentIndex,
                        toolCall=slot.block,
                        partial=output,
                    )
                )
                outputSlots.pop(event.get("output_index"), None)
            elif (
                item.get("type") == "custom_tool_call"
                and slot is not None
                and slot.type == "toolCall"
                and scratch is not None
                and scratch.customInput
            ):
                pushToolCallDelta(
                    slot,
                    _appendCustomToolCallInput(
                        slot.block,
                        scratch,
                        nullishCoalesce(item.get("input"), _getCustomToolCallInput(slot.block, scratch)),
                        True,
                    ),
                )
                if item.get("namespace") is not None:
                    slot.block.namespace = item["namespace"]
                scratch.customInput = None
                stream.push(
                    ToolCallEndEvent(
                        contentIndex=slot.contentIndex,
                        toolCall=slot.block,
                        partial=output,
                    )
                )
                outputSlots.pop(event.get("output_index"), None)
        elif event.get("type") in ("response.completed", "response.incomplete"):
            finalizeResponse(event.get("response"))
        elif event.get("type") == "error":
            raise RuntimeError(f"Error Code {jsString(event.get('code', UNDEFINED))}: {jsString(event.get('message', UNDEFINED))}")
        elif event.get("type") == "response.failed":
            sawTerminalResponseEvent = True
            response = event.get("response") if isinstance(event.get("response"), dict) else {}
            output.rawStopReason = response.get("status")
            error = response.get("error")
            details = response.get("incomplete_details")
            msg = (
                f"{error.get('code') or 'unknown'}: {error.get('message') or 'no message'}"
                if isinstance(error, dict)
                else f"incomplete: {details['reason']}"
                if isinstance(details, dict) and details.get("reason")
                else "Unknown error (no error details in response)"
            )
            raise RuntimeError(msg)
    if not sawTerminalResponseEvent:
        raise RuntimeError("OpenAI Responses stream ended before a terminal response event")
    # The agent runs every tool call in the final message. Refuse to hand over calls whose
    # output_item.done never arrived: their arguments may be cut off or mixed up, e.g. when a
    # non-compliant server omits output_index. Finished calls have their scratch buffers removed.
    if output.stopReason == "toolUse":
        for index, block in enumerate(output.content):
            if block.type != "toolCall":
                continue
            toolCall = streamingToolCalls.get(index)
            if toolCall is not None and (toolCall.partialJson is not None or toolCall.customInput is not None):
                raise RuntimeError(
                    f"OpenAI Responses stream completed with an unfinished tool call: {block.name} ({block.id})",
                )


def _mapStopReason(status: str | None, incompleteReason: str | None = None) -> dict[str, Any]:
    if not status:
        return {"stopReason": "stop"}
    match status:
        case "completed":
            return {"stopReason": "stop"}
        case "incomplete":
            if incompleteReason == "max_output_tokens":
                return {"stopReason": "length"}
            return {
                "stopReason": "error",
                "errorMessage": f"Response incomplete: {incompleteReason}"
                if incompleteReason
                else "Response incomplete without a provider reason",
            }
        case "failed" | "cancelled":
            return {"stopReason": "error"}
        # These two are wonky ...
        case "in_progress" | "queued":
            return {"stopReason": "stop"}
        case _:
            raise RuntimeError(f"Unhandled stop reason: {status}")


__all__ = [
    "ConvertResponsesMessagesOptions",
    "ConvertResponsesToolsOptions",
    "OpenAIResponsesStreamOptions",
    "convertResponsesMessages",
    "convertResponsesTools",
    "processResponsesStream",
]
