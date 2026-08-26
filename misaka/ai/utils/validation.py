"""Tool-call argument validation and JSON-schema coercion helpers."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from misaka.ai.types import Tool, ToolCall

type JsonSchemaObject = dict[str, Any]

# Keyed by content, and bounded: see _get_validator.
_VALIDATOR_CACHE_LIMIT = 512
_validator_cache: dict[str, Draft202012Validator] = {}


def _is_json_schema_object(value: Any) -> bool:
    return isinstance(value, Mapping)


def _get_schema_types(schema: JsonSchemaObject) -> list[str]:
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return [schema_type]
    if isinstance(schema_type, list):
        return [value for value in schema_type if isinstance(value, str)]
    return []


def _matches_json_type(value: Any, schema_type: str) -> bool:
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return math.isfinite(value) and value.is_integer()
        return False
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "null":
        return value is None
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, Mapping)
    return False


def _get_validator(schema: JsonSchemaObject) -> Draft202012Validator:
    """Cache by content, never by ``id``.

    The union-arm coercion below hands this freshly built ``dict(item)`` copies, so an
    address-keyed cache grew on every tool call *and* could return another schema's
    validator once CPython reused a freed address.
    """
    try:
        key = json.dumps(schema, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return Draft202012Validator(schema)          # not serialisable: build it, cache nothing
    validator = _validator_cache.get(key)
    if validator is None:
        if len(_validator_cache) >= _VALIDATOR_CACHE_LIMIT:
            _validator_cache.clear()
        validator = Draft202012Validator(schema)
        _validator_cache[key] = validator
    return validator


def _schema_validates(value: Any, schema: JsonSchemaObject) -> bool:
    try:
        _get_validator(schema).validate(value)
    except JsonSchemaValidationError:
        return False
    return True


def _coerce_primitive_by_type(value: Any, schema_type: str) -> Any:
    if schema_type == "number":
        if value is None:
            return 0
        if isinstance(value, str) and value.strip():
            try:
                parsed = float(value)
            except ValueError:
                return value
            if math.isfinite(parsed):
                return parsed
            return value
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if schema_type == "integer":
        if value is None:
            return 0
        if isinstance(value, str) and value.strip():
            try:
                parsed = float(value)
            except ValueError:
                return value
            if parsed.is_integer():
                return int(parsed)
            return value
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if schema_type == "boolean":
        if value is None:
            return False
        if isinstance(value, str):
            if value == "true":
                return True
            if value == "false":
                return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value == 1:
                return True
            if value == 0:
                return False
        return value
    if schema_type == "string":
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        return value
    if schema_type == "null":
        if value in ("", 0, False):
            return None
        return value
    return value


def _apply_schema_object_coercion(value: dict[str, Any], schema: JsonSchemaObject) -> None:
    properties = schema.get("properties")
    defined_keys = set(properties.keys()) if isinstance(properties, Mapping) else set()

    if isinstance(properties, Mapping):
        for key, property_schema in properties.items():
            if key not in value or not _is_json_schema_object(property_schema):
                continue
            value[key] = _coerce_with_json_schema(value[key], dict(property_schema))

    additional_properties = schema.get("additionalProperties")
    if isinstance(additional_properties, Mapping):
        for key, property_value in list(value.items()):
            if key in defined_keys:
                continue
            value[key] = _coerce_with_json_schema(property_value, dict(additional_properties))


def _apply_schema_array_coercion(value: list[Any], schema: JsonSchemaObject) -> None:
    items = schema.get("items")
    if isinstance(items, list):
        for index, item_schema in enumerate(items):
            if index >= len(value) or not _is_json_schema_object(item_schema):
                continue
            value[index] = _coerce_with_json_schema(value[index], dict(item_schema))
        return

    if isinstance(items, Mapping):
        for index, item_value in enumerate(value):
            value[index] = _coerce_with_json_schema(item_value, dict(items))


def _coerce_with_union_schema(value: Any, schemas: Sequence[JsonSchemaObject]) -> Any:
    # Keep the value as-is if it already matches an arm; otherwise a nullable
    # union would coerce null into 0/"" (pi #7328/2e95584da).
    for schema in schemas:
        if _schema_validates(value, schema):
            return value
    for schema in schemas:
        candidate = deepcopy(value)
        coerced = _coerce_with_json_schema(candidate, schema)
        if _schema_validates(coerced, schema):
            return coerced
    return value


def _coerce_with_json_schema(value: Any, schema: JsonSchemaObject) -> Any:
    next_value = value

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for nested in all_of:
            if _is_json_schema_object(nested):
                next_value = _coerce_with_json_schema(next_value, dict(nested))

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        next_value = _coerce_with_union_schema(next_value, [dict(item) for item in any_of if _is_json_schema_object(item)])

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        next_value = _coerce_with_union_schema(next_value, [dict(item) for item in one_of if _is_json_schema_object(item)])

    schema_types = _get_schema_types(schema)
    matches_union_member = len(schema_types) > 1 and any(_matches_json_type(next_value, item) for item in schema_types)
    if schema_types and not matches_union_member:
        for schema_type in schema_types:
            candidate = _coerce_primitive_by_type(next_value, schema_type)
            if candidate is not next_value:
                next_value = candidate
                break

    if "enum" in schema and isinstance(schema["enum"], list) and next_value not in schema["enum"]:
        for candidate in schema["enum"]:
            if type(candidate) is type(next_value) or str(candidate) == str(next_value):
                next_value = candidate
                break

    if "const" in schema and next_value != schema["const"] and str(next_value) == str(schema["const"]):
        next_value = schema["const"]

    if "object" in schema_types and isinstance(next_value, dict):
        _apply_schema_object_coercion(next_value, schema)

    if "array" in schema_types and isinstance(next_value, list):
        _apply_schema_array_coercion(next_value, schema)

    return next_value


def _format_jsonschema_path(error: JsonSchemaValidationError) -> str:
    if error.validator == "required":
        required_property = error.message.split("'")[1] if "'" in error.message else None
        base_path = ".".join(str(part) for part in error.absolute_path)
        if required_property:
            return f"{base_path}.{required_property}" if base_path else required_property
    path = ".".join(str(part) for part in error.absolute_path)
    return path or "root"


def _format_pydantic_path(location: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in location) or "root"


def _format_received_arguments(arguments: Any) -> str:
    try:
        return json.dumps(arguments, indent=2, ensure_ascii=False)
    except TypeError:
        return repr(arguments)


def validate_tool_call(tools: list[Tool], tool_call: ToolCall) -> Any:
    tool = next((candidate for candidate in tools if candidate.name == tool_call.name), None)
    if tool is None:
        raise ValueError(f'Tool "{tool_call.name}" not found')
    return validate_tool_arguments(tool, tool_call)


def _validate_pydantic_tool_arguments(tool: Tool, tool_call: ToolCall) -> Any:
    assert isinstance(tool.parameters, type) and issubclass(tool.parameters, BaseModel)
    try:
        validated = tool.parameters.model_validate(deepcopy(tool_call.arguments))
    except PydanticValidationError as error:
        errors = "\n".join(
            f"  - {_format_pydantic_path(tuple(item['loc']))}: {item['msg']}"
            for item in error.errors()
        ) or "Unknown validation error"
        raise ValueError(
            f'Validation failed for tool "{tool_call.name}":\n{errors}\n\nReceived arguments:\n{_format_received_arguments(tool_call.arguments)}'
        ) from error
    return validated.model_dump(mode="python")


def _validate_json_schema_tool_arguments(tool: Tool, tool_call: ToolCall) -> Any:
    assert isinstance(tool.parameters, Mapping)
    schema = tool.parameters
    args = deepcopy(tool_call.arguments)
    coerced = _coerce_with_json_schema(args, schema)
    validator = _get_validator(schema)
    errors = sorted(validator.iter_errors(coerced), key=lambda error: list(error.absolute_path))
    if not errors:
        return coerced

    formatted_errors = "\n".join(
        f"  - {_format_jsonschema_path(error)}: {error.message}"
        for error in errors
    ) or "Unknown validation error"
    raise ValueError(
        f'Validation failed for tool "{tool_call.name}":\n{formatted_errors}\n\nReceived arguments:\n{_format_received_arguments(tool_call.arguments)}'
    )


def validate_tool_arguments(tool: Tool, tool_call: ToolCall) -> Any:
    if isinstance(tool.parameters, type) and issubclass(tool.parameters, BaseModel):
        return _validate_pydantic_tool_arguments(tool, tool_call)

    if isinstance(tool.parameters, Mapping):
        return _validate_json_schema_tool_arguments(tool, tool_call)

    raise TypeError("Tool.parameters must be a Pydantic model class or JSON schema mapping")


__all__ = [
    "validate_tool_arguments",
    "validate_tool_call",
]
