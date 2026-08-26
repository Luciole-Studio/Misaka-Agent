"""Shared Google Generative AI and Vertex request helpers."""

from __future__ import annotations

import base64
import copy
import re
from typing import Any, Literal, TypeAlias

from misaka.ai.providers.transform_messages import transform_messages
from misaka.ai.types import Context, ImageContent, Model, StopReason, Tool
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates

GoogleApiType: TypeAlias = Literal["google-generative-ai", "google-vertex"]
GoogleThinkingLevel: TypeAlias = Literal["THINKING_LEVEL_UNSPECIFIED", "MINIMAL", "LOW", "MEDIUM", "HIGH"]

_BASE64_SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_JSON_SCHEMA_META_DECLARATIONS = frozenset(
    {
        "$schema",
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$vocabulary",
        "$comment",
        "$defs",
        "definitions",
    }
)


def is_thinking_part(part: dict[str, Any] | Any) -> bool:
    if isinstance(part, dict):
        return part.get("thought") is True
    return getattr(part, "thought", None) is True


def coerce_thought_signature(value: object) -> str | None:
    """Coerce a thought_signature to str.

    google-genai sometimes returns it as raw bytes while ToolCall declares str;
    base64 preserves it losslessly (it is only ever echoed back, never read).
    """
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value if isinstance(value, str) else None


def retain_thought_signature(existing: str | None, incoming: object) -> str | None:
    coerced = coerce_thought_signature(incoming)
    if coerced:
        return coerced
    return existing


def _is_valid_thought_signature(signature: str | None) -> bool:
    if not signature:
        return False
    if len(signature) % 4 != 0:
        return False
    return _BASE64_SIGNATURE_PATTERN.fullmatch(signature) is not None


def _resolve_thought_signature(is_same_provider_and_model: bool, signature: str | None) -> str | None:
    return signature if is_same_provider_and_model and _is_valid_thought_signature(signature) else None


def requires_tool_call_id(model_id: str) -> bool:
    return model_id.startswith("claude-") or model_id.startswith("gpt-oss-")


def _get_gemini_major_version(model_id: str) -> int | None:
    match = re.match(r"^gemini(?:-live)?-(\d+)", model_id.lower())
    if not match:
        return None
    return int(match.group(1))


def _supports_multimodal_function_response(model_id: str) -> bool:
    major = _get_gemini_major_version(model_id)
    if major is not None:
        return major >= 3
    return True


def convert_messages(model: Model, context: Context) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []

    def normalize_tool_call_id(tool_call_id: str, _target_model: Model, _source: Any) -> str:
        if not requires_tool_call_id(model.id):
            return tool_call_id
        normalized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in tool_call_id)
        return normalized[:64]

    transformed_messages = transform_messages(context.messages, model, normalize_tool_call_id)
    for message in transformed_messages:
        if message.role == "user":
            if isinstance(message.content, str):
                contents.append({"role": "user", "parts": [{"text": sanitize_surrogates(message.content)}]})
            else:
                parts: list[dict[str, Any]] = []
                for item in message.content:
                    if item.type == "text":
                        parts.append({"text": sanitize_surrogates(item.text)})
                    else:
                        parts.append({"inlineData": {"mimeType": item.mimeType, "data": item.data}})
                if parts:
                    contents.append({"role": "user", "parts": parts})
            continue

        if message.role == "assistant":
            parts: list[dict[str, Any]] = []
            is_same_provider_and_model = message.provider == model.provider and message.model == model.id

            for block in message.content:
                if block.type == "text":
                    if not block.text or not block.text.strip():
                        continue
                    thought_signature = _resolve_thought_signature(is_same_provider_and_model, block.textSignature)
                    part: dict[str, Any] = {"text": sanitize_surrogates(block.text)}
                    if thought_signature:
                        part["thoughtSignature"] = thought_signature
                    parts.append(part)
                    continue

                if block.type == "thinking":
                    if not block.thinking or not block.thinking.strip():
                        continue
                    if is_same_provider_and_model:
                        thought_signature = _resolve_thought_signature(
                            is_same_provider_and_model, block.thinkingSignature
                        )
                        part = {"thought": True, "text": sanitize_surrogates(block.thinking)}
                        if thought_signature:
                            part["thoughtSignature"] = thought_signature
                        parts.append(part)
                    else:
                        parts.append({"text": sanitize_surrogates(block.thinking)})
                    continue

                if block.type == "toolCall":
                    thought_signature = _resolve_thought_signature(is_same_provider_and_model, block.thoughtSignature)
                    function_call: dict[str, Any] = {"name": block.name, "args": block.arguments or {}}
                    if requires_tool_call_id(model.id):
                        function_call["id"] = block.id
                    part = {"functionCall": function_call}
                    if thought_signature:
                        part["thoughtSignature"] = thought_signature
                    parts.append(part)

            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        if message.role == "toolResult":
            text_result = "\n".join(block.text for block in message.content if block.type == "text")
            image_content = (
                [block for block in message.content if isinstance(block, ImageContent) or block.type == "image"]
                if "image" in model.input
                else []
            )

            has_text = len(text_result) > 0
            has_images = len(image_content) > 0
            supports_multimodal = _supports_multimodal_function_response(model.id)
            response_value = sanitize_surrogates(text_result) if has_text else "(see attached image)" if has_images else ""
            image_parts = [
                {"inlineData": {"mimeType": image_block.mimeType, "data": image_block.data}}
                for image_block in image_content
            ]

            function_response: dict[str, Any] = {
                "name": message.toolName,
                "response": {"error": response_value} if message.isError else {"output": response_value},
            }
            if has_images and supports_multimodal:
                function_response["parts"] = image_parts
            if requires_tool_call_id(model.id):
                function_response["id"] = message.toolCallId

            function_response_part = {"functionResponse": function_response}

            last_content = contents[-1] if contents else None
            if (
                isinstance(last_content, dict)
                and last_content.get("role") == "user"
                and any(isinstance(part, dict) and part.get("functionResponse") for part in last_content.get("parts", []))
            ):
                last_content["parts"].append(function_response_part)
            else:
                contents.append({"role": "user", "parts": [function_response_part]})

            if has_images and not supports_multimodal:
                contents.append({"role": "user", "parts": [{"text": "Tool result image:"}, *image_parts]})

    return contents


def sanitize_for_openapi(schema: Any) -> Any:
    if not isinstance(schema, dict):
        if isinstance(schema, list):
            return schema
        return schema

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _JSON_SCHEMA_META_DECLARATIONS:
            continue
        result[key] = sanitize_for_openapi(value)
    return result


def convert_tools(
    tools: list[Tool],
    use_parameters: bool = False,
) -> list[dict[str, Any]] | None:
    if not tools:
        return None

    declarations: list[dict[str, Any]] = []
    for tool in tools:
        declaration: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
        }
        if use_parameters:
            declaration["parameters"] = sanitize_for_openapi(copy.deepcopy(tool.parameters_json_schema()))
        else:
            declaration["parametersJsonSchema"] = copy.deepcopy(tool.parameters_json_schema())
        declarations.append(declaration)

    return [{"functionDeclarations": declarations}]


def map_tool_choice(choice: str) -> str:
    if choice == "none":
        return "NONE"
    if choice == "any":
        return "ANY"
    return "AUTO"


def map_stop_reason(reason: Any) -> StopReason:
    name = getattr(reason, "name", None)
    if isinstance(name, str):
        normalized = name
    elif isinstance(reason, str):
        normalized = reason.split(".")[-1]
    else:
        normalized = str(reason)

    if normalized == "STOP":
        return "stop"
    if normalized == "MAX_TOKENS":
        return "length"
    if normalized in {
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "SAFETY",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "IMAGE_OTHER",
        "RECITATION",
        "FINISH_REASON_UNSPECIFIED",
        "OTHER",
        "LANGUAGE",
        "MALFORMED_FUNCTION_CALL",
        "UNEXPECTED_TOOL_CALL",
        "NO_IMAGE",
    }:
        return "error"

    raise RuntimeError(f"Unhandled stop reason: {normalized}")


def map_stop_reason_string(reason: str) -> StopReason:
    if reason == "STOP":
        return "stop"
    if reason == "MAX_TOKENS":
        return "length"
    return "error"


convertMessages = convert_messages
convertTools = convert_tools
mapToolChoice = map_tool_choice
mapStopReason = map_stop_reason

__all__ = [
    "GoogleThinkingLevel",
    "convertMessages",
    "convertTools",
    "mapStopReason",
    "mapToolChoice",
    ]
