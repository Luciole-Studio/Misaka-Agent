# ruff: noqa: UP040 - TypeAlias spelling kept in step with misaka/ai/types.py.

"""Translated from pi's ``packages/ai/src/api/google-shared.ts`` (v1.3.0).

Shared utilities for Google Generative AI and Google Vertex providers.

MISAKA fork, kept to what the Python SDK forces (each is marked where it happens): the SDK enums come through
``_google_sdk`` (stand-ins when the optional extra is absent), a part may be an SDK model or a dict, and a finish
reason may be the SDK enum or its wire string.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypeAlias

from misaka.ai.api._common import _option
from misaka.ai.api._google_sdk import FinishReason, FunctionCallingConfigMode
from misaka.ai.api._google_sdk import ThinkingLevel as GoogleSdkThinkingLevel
from misaka.ai.api.constrained_sampling import (
    getJsonSchemaToolParameters,
    resolveJsonSchemaStrictSampling,
)
from misaka.ai.api.transform_messages import transformMessages
from misaka.ai.models import clampThinkingLevel
from misaka.ai.types import Model, StopReason, ThinkingLevel, Tool, TranscriptContext
from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.ai.utils.sanitize_unicode import sanitizeSurrogates
from misaka.ai.utils.transcript import (
    collapseSystemMessages,
    withoutInitialSystemMessage,
)
from misaka.utils.js import UNDEFINED, jsLength, jsString, jsTrim, nullishCoalesce

# Thinking level for Gemini 3 models.
# Mirrors Google's ThinkingLevel enum values.
GoogleApiThinkingLevel: TypeAlias = Literal["THINKING_LEVEL_UNSPECIFIED", "MINIMAL", "LOW", "MEDIUM", "HIGH"]
ResolvedGoogleThinkingLevel: TypeAlias = Literal["minimal", "low", "medium", "high"]

_GOOGLE_SDK_THINKING_LEVEL_MAP: dict[GoogleApiThinkingLevel, GoogleSdkThinkingLevel] = {
    "THINKING_LEVEL_UNSPECIFIED": GoogleSdkThinkingLevel.THINKING_LEVEL_UNSPECIFIED,
    "MINIMAL": GoogleSdkThinkingLevel.MINIMAL,
    "LOW": GoogleSdkThinkingLevel.LOW,
    "MEDIUM": GoogleSdkThinkingLevel.MEDIUM,
    "HIGH": GoogleSdkThinkingLevel.HIGH,
}


def resolveGoogleThinkingLevel(model: Model, level: ThinkingLevel) -> ResolvedGoogleThinkingLevel:
    """Resolve a supported pi level or model-specific Google mapping to a standard Google level."""
    mapped = model.thinkingLevelMap.get(level, UNDEFINED) if model.thinkingLevelMap is not None else UNDEFINED
    resolvedLevel = mapped.lower() if isinstance(mapped, str) else level
    match resolvedLevel:
        case "minimal" | "low" | "medium" | "high":
            return resolvedLevel
        case _:
            raise RuntimeError(
                f"Unsupported Google thinking level mapping for {model.provider}/{model.id}: {level} -> {jsString(mapped)}"
            )


def usesGoogleThinkingLevel(model: Model) -> bool:
    """Whether this model uses Gemini's discrete `thinkingLevel` control instead of
    the token-based `thinkingBudget` control. Supported levels come from the
    model's `thinkingLevelMap`; this only selects the Google wire format.
    """
    id = model.id.lower()
    return (
        # Match Gemini 3 Pro/Flash IDs with or without a minor version, such as
        # gemini-3-flash-preview, gemini-3.1-pro-preview, and gemini-3.8-flash.
        re.search(r"gemini-3(?:\.\d+)?-(?:pro|flash)", id) is not None
        # Match Gemini 4 and later, such as gemini-4-argon (#10890).
        or re.search(r"gemini-(?:[4-9]|[1-9][0-9])", id) is not None
        or id == "gemini-flash-latest"
        or id == "gemini-flash-lite-latest"
        # Match both hosted Gemma 4 naming forms: gemma-4-* and gemma4-*.
        or re.search(r"gemma-?4", id) is not None
    )


def toGoogleThinkingLevel(level: ResolvedGoogleThinkingLevel) -> GoogleApiThinkingLevel:
    match level:
        case "minimal":
            return "MINIMAL"
        case "low":
            return "LOW"
        case "medium":
            return "MEDIUM"
        case "high":
            return "HIGH"


def toGoogleSdkThinkingLevel(level: GoogleApiThinkingLevel) -> GoogleSdkThinkingLevel:
    return _GOOGLE_SDK_THINKING_LEVEL_MAP.get(level)  # type: ignore[return-value]


def getDisabledGoogleThinkingConfig(model: Model) -> dict[str, Any]:
    if not usesGoogleThinkingLevel(model):
        return {"thinkingBudget": 0}

    fallback = clampThinkingLevel(model, "off")
    if fallback == "off":
        return {"thinkingBudget": 0}

    resolvedLevel = resolveGoogleThinkingLevel(model, fallback)
    apiLevel = toGoogleThinkingLevel(resolvedLevel)
    return {"thinkingLevel": toGoogleSdkThinkingLevel(apiLevel)}


def isThinkingPart(part: Any) -> bool:
    """Determines whether a streamed Gemini `Part` should be treated as "thinking".

    Protocol note (Gemini / Vertex AI thought signatures):
    - `thought: true` is the definitive marker for thinking content (thought summaries).
    - `thoughtSignature` is an encrypted representation of the model's internal thought process
      used to preserve reasoning context across multi-turn interactions.
    - `thoughtSignature` can appear on ANY part type (text, functionCall, etc.) - it does NOT
      indicate the part itself is thinking content.
    - For non-functionCall responses, the signature appears on the last part for context replay.
    - When persisting/replaying model outputs, signature-bearing parts must be preserved as-is;
      do not merge/move signatures across parts.

    See: https://ai.google.dev/gemini-api/docs/thought-signatures
    """
    # MISAKA fork: a part is an SDK `Part` model or a plain dict.
    thought = part.get("thought") if isinstance(part, Mapping) else getattr(part, "thought", None)
    return thought is True


def retainThoughtSignature(existing: str | None, incoming: str | None) -> str | None:
    """Retain thought signatures during streaming.

    Some backends only send `thoughtSignature` on the first delta for a given part/block; later deltas may omit it.
    This helper preserves the last non-empty signature for the current block.

    Note: this does NOT merge or move signatures across distinct response parts. It only prevents
    a signature from being overwritten with `undefined` within the same streamed block.
    """
    if isinstance(incoming, str) and len(incoming) > 0:
        return incoming
    return existing


# Thought signatures must be base64 for Google APIs (TYPE_BYTES).
_base64SignaturePattern = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _isValidThoughtSignature(signature: str | None) -> bool:
    if not signature:
        return False
    if jsLength(signature) % 4 != 0:
        return False
    return _base64SignaturePattern.fullmatch(signature) is not None


def _resolveThoughtSignature(isSameProviderAndModel: bool, signature: str | None) -> str | None:
    """Only keep signatures from the same provider/model and with valid base64."""
    return signature if isSameProviderAndModel and _isValidThoughtSignature(signature) else None


def requiresToolCallId(modelId: str) -> bool:
    """Models via Google APIs that require explicit tool call IDs in function calls/responses."""
    geminiMajorVersion = _getGeminiMajorVersion(modelId)
    return (
        modelId.startswith("claude-")  # noqa: PIE810 - upstream's spelling
        or modelId.startswith("gpt-oss-")
        or (geminiMajorVersion is not None and geminiMajorVersion >= 3)
    )


def _getGeminiMajorVersion(modelId: str) -> int | None:
    match = re.match(r"^gemini(?:-live)?-(\d+)", modelId.lower())
    if not match:
        return None
    return int(match.group(1))


def _supportsMultimodalFunctionResponse(modelId: str) -> bool:
    geminiMajorVersion = _getGeminiMajorVersion(modelId)
    if geminiMajorVersion is not None:
        return geminiMajorVersion >= 3
    return True


def convertMessages(model: Model, context: TranscriptContext) -> list[dict[str, Any]]:
    """Convert internal messages to Gemini Content[] format."""
    # Gemini has no mid-conversation system messages; the leading prompt is sent as systemInstruction.
    conversation = withoutInitialSystemMessage(collapseSystemMessages(context).messages)
    contents: list[dict[str, Any]] = []

    def normalizeToolCallId(id: str, *_: Any) -> str:
        if not requiresToolCallId(model.id):
            return id
        # The pattern runs over UTF-16 code units, as JavaScript's does: an astral character becomes two.
        return re.sub(r"[^a-zA-Z0-9_-]", lambda match: "_" * jsLength(match.group()), id)[:64]

    transformedMessages = transformMessages(conversation, model, normalizeToolCallId)

    for msg in transformedMessages:
        if msg.role == "user":
            if isinstance(msg.content, str):
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": sanitizeSurrogates(msg.content)}],
                    }
                )
            else:
                parts = [
                    {"text": sanitizeSurrogates(item.text)}
                    if item.type == "text"
                    else {
                        "inlineData": {
                            "mimeType": item.mimeType,
                            "data": item.data,
                        },
                    }
                    for item in msg.content
                ]
                if len(parts) == 0:
                    continue
                contents.append(
                    {
                        "role": "user",
                        "parts": parts,
                    }
                )
        elif msg.role == "assistant":
            parts: list[dict[str, Any]] = []
            # Check if message is from same provider and model - only then keep thinking blocks
            isSameProviderAndModel = msg.provider == model.provider and msg.model == model.id

            for block in msg.content:
                if block.type == "text":
                    thoughtSignature = _resolveThoughtSignature(isSameProviderAndModel, block.textSignature)
                    # Skip empty text blocks — unless they carry a thought signature. Gemini can attach
                    # the signature to a part whose visible text is empty and requires it echoed back;
                    # dropping it breaks the reasoning chain and the model intermittently ends mid-task
                    # turns with a thought-only STOP (empty completion, no tool call).
                    if (not block.text or jsTrim(block.text) == "") and not thoughtSignature:
                        continue
                    parts.append(
                        {
                            "text": sanitizeSurrogates(block.text),
                            **({"thoughtSignature": thoughtSignature} if thoughtSignature else {}),
                        }
                    )
                elif block.type == "thinking":
                    # Only keep as thinking block if same provider AND same model
                    # Otherwise convert to plain text (no tags to avoid model mimicking them)
                    if isSameProviderAndModel:
                        thoughtSignature = _resolveThoughtSignature(isSameProviderAndModel, block.thinkingSignature)
                        # Same rule as text blocks: an empty thinking block is dropped only when it
                        # carries no signature (mirrors the anthropic converter's handling).
                        if (not block.thinking or jsTrim(block.thinking) == "") and not thoughtSignature:
                            continue
                        parts.append(
                            {
                                "thought": True,
                                "text": sanitizeSurrogates(block.thinking),
                                **({"thoughtSignature": thoughtSignature} if thoughtSignature else {}),
                            }
                        )
                    else:
                        # Cross-provider/model: the signature is unusable, empty blocks stay dropped.
                        if not block.thinking or jsTrim(block.thinking) == "":
                            continue
                        parts.append(
                            {
                                "text": sanitizeSurrogates(block.thinking),
                            }
                        )
                elif block.type == "toolCall":
                    thoughtSignature = _resolveThoughtSignature(isSameProviderAndModel, block.thoughtSignature)
                    part = {
                        "functionCall": {
                            "name": block.name,
                            "args": nullishCoalesce(block.arguments, {}),
                            **({"id": block.id} if requiresToolCallId(model.id) else {}),
                        },
                        **({"thoughtSignature": thoughtSignature} if thoughtSignature else {}),
                    }
                    parts.append(part)

            if len(parts) == 0:
                continue
            contents.append(
                {
                    "role": "model",
                    "parts": parts,
                }
            )
        elif msg.role == "toolResult":
            # Extract text and image content
            textContent = [c for c in msg.content if c.type == "text"]
            textResult = "\n".join(c.text for c in textContent)
            imageContent = [c for c in msg.content if c.type == "image"] if "image" in model.input else []

            hasText = len(textResult) > 0
            hasImages = len(imageContent) > 0

            # Gemini 3+ models support multimodal function responses with images nested inside
            # functionResponse.parts. Claude and other non-Gemini models behind Cloud Code Assist /
            # Gemini < 3 still needs a separate user image turn.
            modelSupportsMultimodalFunctionResponse = _supportsMultimodalFunctionResponse(model.id)

            # Use "output" key for success, "error" key for errors as per SDK documentation
            responseValue = sanitizeSurrogates(textResult) if hasText else "(see attached image)" if hasImages else ""

            imageParts = [
                {
                    "inlineData": {
                        "mimeType": imageBlock.mimeType,
                        "data": imageBlock.data,
                    },
                }
                for imageBlock in imageContent
            ]

            includeId = requiresToolCallId(model.id)
            functionResponsePart = {
                "functionResponse": {
                    "name": msg.toolName,
                    "response": {"error": responseValue} if msg.isError else {"output": responseValue},
                    **({"parts": imageParts} if hasImages and modelSupportsMultimodalFunctionResponse else {}),
                    **({"id": msg.toolCallId} if includeId else {}),
                },
            }

            # Cloud Code Assist API requires all function responses to be in a single user turn.
            # Check if the last content is already a user turn with function responses and merge.
            lastContent = contents[-1] if contents else None
            if (
                lastContent is not None
                and lastContent.get("role") == "user"
                and any(p.get("functionResponse") for p in lastContent.get("parts") or [])
            ):
                lastContent["parts"].append(functionResponsePart)
            else:
                contents.append(
                    {
                        "role": "user",
                        "parts": [functionResponsePart],
                    }
                )

            # For Gemini < 3, add images in a separate user message
            if hasImages and not modelSupportsMultimodalFunctionResponse:
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": "Tool result image:"}, *imageParts],
                    }
                )

    return contents


_JSON_SCHEMA_META_DECLARATIONS = frozenset(
    {
        "$schema",
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$vocabulary",
        "$comment",
        "$defs",
        "definitions",  # pre-draft-2019-09 equivalent of $defs
    }
)


def _sanitizeForOpenApi(schema: Any) -> Any:
    """Strip meta-declarations from a schema obj"""
    if not isinstance(schema, Mapping):
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _JSON_SCHEMA_META_DECLARATIONS:
            continue
        result[key] = _sanitizeForOpenApi(value)
    return result


def convertTools(
    tools: list[Tool],
    useParameters: bool = False,
    supportsStrictMode: bool = True,
) -> list[dict[str, list[dict[str, Any]]]] | None:
    """Convert tools to Gemini function declarations format.

    By default uses `parametersJsonSchema` which supports full JSON Schema (including
    anyOf, oneOf, const, etc.). Set `useParameters` to true to use the legacy `parameters`
    field instead (OpenAPI 3.03 Schema). This is needed for Cloud Code Assist with Claude
    models, where the API translates `parameters` into Anthropic's `input_schema`.
    """
    if len(tools) == 0:
        return None

    def declaration(tool: Tool) -> dict[str, Any]:
        strict = resolveJsonSchemaStrictSampling(tool, supportsStrictMode)
        parameters = getJsonSchemaToolParameters(tool, strict)
        return {
            "name": tool.name,
            "description": tool.description,
            **({"parameters": _sanitizeForOpenApi(parameters)} if useParameters else {"parametersJsonSchema": parameters}),
        }

    return [
        {
            "functionDeclarations": [declaration(tool) for tool in tools],
        },
    ]


def supportsGoogleStrictToolSampling(modelId: str) -> bool:
    """Gemini 3+ enforces required function parameters in validated tool-calling modes."""
    majorVersion = _getGeminiMajorVersion(modelId)
    return majorVersion is not None and majorVersion >= 3


def mapToolChoice(choice: str) -> FunctionCallingConfigMode:
    """Map tool choice string to Gemini FunctionCallingConfigMode."""
    match choice:
        case "auto":
            return FunctionCallingConfigMode.AUTO
        case "none":
            return FunctionCallingConfigMode.NONE
        case "any":
            return FunctionCallingConfigMode.ANY
        case _:
            return FunctionCallingConfigMode.AUTO


def resolveGoogleFunctionCallingMode(
    tools: list[Tool],
    toolChoice: str | None,
    supportsStrictMode: bool,
) -> FunctionCallingConfigMode | None:
    useStrictMode = any(resolveJsonSchemaStrictSampling(tool, supportsStrictMode) is True for tool in tools)
    if toolChoice == "none" or toolChoice == "any":
        return mapToolChoice(toolChoice)
    if useStrictMode:
        return FunctionCallingConfigMode.VALIDATED
    return mapToolChoice(toolChoice) if toolChoice else None


def mapStopReason(reason: FinishReason) -> StopReason:
    """Map Gemini FinishReason to our StopReason."""
    # MISAKA fork: the SDK's FinishReason is a str enum, so its wire string matches a member too.
    match reason:
        case FinishReason.STOP:
            return "stop"
        case FinishReason.MAX_TOKENS:
            return "length"
        case (
            FinishReason.BLOCKLIST
            | FinishReason.PROHIBITED_CONTENT
            | FinishReason.SPII
            | FinishReason.SAFETY
            | FinishReason.IMAGE_SAFETY
            | FinishReason.IMAGE_PROHIBITED_CONTENT
            | FinishReason.IMAGE_RECITATION
            | FinishReason.IMAGE_OTHER
            | FinishReason.RECITATION
            | FinishReason.FINISH_REASON_UNSPECIFIED
            | FinishReason.OTHER
            | FinishReason.LANGUAGE
            | FinishReason.MALFORMED_FUNCTION_CALL
            | FinishReason.UNEXPECTED_TOOL_CALL
            | FinishReason.TOO_MANY_TOOL_CALLS
            | FinishReason.NO_IMAGE
        ):
            return "error"
        case _:
            # MISAKA fork: an enum member prints as `FinishReason.X`; the message names the wire value.
            raise RuntimeError(f"Unhandled stop reason: {getattr(reason, 'value', reason)}")


def mapStopReasonString(reason: str) -> StopReason:
    """Map string finish reason to our StopReason (for raw API responses)."""
    match reason:
        case "STOP":
            return "stop"
        case "MAX_TOKENS":
            return "length"
        case _:
            return "error"


def retryGoogleRequest[T](request: Callable[[], Awaitable[T]], options: Any = None) -> Awaitable[T]:
    """Run a Google GenAI SDK request with the shared provider retry policy
    (408/409/429/5xx with backoff, honoring retry-after), mirroring how the
    Anthropic and OpenAI adapters wrap their initial request in
    retryProviderRequest. The SDK's ApiError has a `status` property but no
    `headers` property, and retryProviderRequest only retries errors that carry
    both, so normalize the error by adding the missing `headers` before
    rethrowing.
    """

    async def attempt() -> T:
        try:
            return await request()
        except Exception as error:
            if hasattr(error, "status") and not hasattr(error, "headers"):
                error.headers = None  # type: ignore[attr-defined]
            raise

    return retryProviderRequest(
        attempt,
        {
            "maxRetries": _option(options, "maxRetries"),
            "maxRetryDelayMs": _option(options, "maxRetryDelayMs"),
            "signal": _option(options, "signal"),
        },
    )


__all__ = [
    "GoogleApiThinkingLevel",
    "ResolvedGoogleThinkingLevel",
    "convertMessages",
    "convertTools",
    "getDisabledGoogleThinkingConfig",
    "isThinkingPart",
    "mapStopReason",
    "mapStopReasonString",
    "mapToolChoice",
    "requiresToolCallId",
    "resolveGoogleFunctionCallingMode",
    "resolveGoogleThinkingLevel",
    "retainThoughtSignature",
    "retryGoogleRequest",
    "supportsGoogleStrictToolSampling",
    "toGoogleSdkThinkingLevel",
    "toGoogleThinkingLevel",
    "usesGoogleThinkingLevel",
]
