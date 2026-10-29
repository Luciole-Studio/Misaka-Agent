"""Translated from pi's ``packages/ai/src/utils/validation.ts`` (v1.3.0).

MISAKA fork: TypeBox's ``Compile`` is a jsonschema Draft 2020-12 validator, and a TypeBox schema
(``Object.getOwnPropertySymbols(...).includes(TYPEBOX_KIND)``) is a pydantic model class, validated with
``model_validate``. ``Value.Convert``, which upstream runs on every tool's arguments first, is ported in
``typebox_convert``: a pydantic tool's arguments go through it, as a TypeBox tool's do upstream. A plain JSON schema
has no ``~kind``, so upstream's ``Value.Convert`` leaves its arguments as they are and only ``coerceWithJsonSchema``
changes them; here too. A pydantic tool's converted arguments pass both pydantic and the Draft 2020-12 check of its
schema, as upstream's pass ``Check``. Error texts come from jsonschema and pydantic, not TypeBox.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from referencing.exceptions import Unresolvable

from misaka.ai.types import Tool, ToolCall
from misaka.ai.utils.typebox_convert import Convert
from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.utils.js import jsIsInteger, jsNumber, jsonStringify, jsString, jsTrim

# MISAKA fork: keyed by content and bounded, not a WeakMap: schemas are dicts (not weak-referenceable) and the union-arm
# coercion builds fresh copies, so an address-keyed cache grew on every tool call and could hand back another schema's
# validator once CPython reused a freed address.
_VALIDATOR_CACHE_LIMIT = 512
_validatorCache: dict[str, Draft202012Validator] = {}

_JsonSchemaObject = dict[str, Any]


def _getSchemaTypes(schema: _JsonSchemaObject) -> list[str]:
    schemaType = schema.get("type")
    if isinstance(schemaType, str):
        return [schemaType]
    if isinstance(schemaType, list):
        return [type for type in schemaType if isinstance(type, str)]
    return []


def _matchesJsonType(value: Any, type: str) -> bool:
    if type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if type == "integer":
        return isinstance(value, int | float) and not isinstance(value, bool) and jsIsInteger(value)
    if type == "boolean":
        return isinstance(value, bool)
    if type == "string":
        return isinstance(value, str)
    if type == "null":
        return value is None
    if type == "array":
        return isinstance(value, list)
    if type == "object":
        return isinstance(value, Mapping)
    return False


class _SubSchemaValidator:
    """MISAKA fork: jsonschema fails a ``$ref`` into the root's ``$defs`` when the sub-schema is checked, not when its
    validator is built; ``Check`` then answers ``None``, as ``getSubSchemaValidator(...)?.Check(...)`` is ``undefined``
    upstream when compiling the sub-schema throws."""

    def __init__(self, validator: Draft202012Validator) -> None:
        self._validator = validator

    def Check(self, value: Any) -> bool | None:
        try:
            self._validator.validate(value)
        except JsonSchemaValidationError:
            return False
        except Unresolvable:
            return None
        return True


def _getSubSchemaValidator(schema: _JsonSchemaObject) -> _SubSchemaValidator | None:
    try:
        return _SubSchemaValidator(_getValidator(schema))
    except Exception:  # noqa: BLE001 - an invalid sub-schema has no validator, as upstream's catch says
        return None


def _coercePrimitiveByType(value: Any, type: str) -> Any:
    if type == "number":
        if value is None:
            return 0
        if isinstance(value, str) and jsTrim(value) != "":
            parsed = jsNumber(value)
            if math.isfinite(parsed):
                return parsed
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if type == "integer":
        if value is None:
            return 0
        if isinstance(value, str) and jsTrim(value) != "":
            parsed = jsNumber(value)
            if jsIsInteger(parsed):
                return int(parsed)
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if type == "boolean":
        if value is None:
            return False
        if isinstance(value, str):
            if value == "true":
                return True
            if value == "false":
                return False
        if isinstance(value, int | float) and not isinstance(value, bool):
            if value == 1:
                return True
            if value == 0:
                return False
        return value
    if type == "string":
        if value is None:
            return ""
        if isinstance(value, int | float):
            return jsString(value)
        return value
    if type == "null":
        if value == "" or (value == 0 and not isinstance(value, bool)) or value is False:
            return None
        return value
    return value


def _applySchemaObjectCoercion(value: dict[str, Any], schema: _JsonSchemaObject) -> None:
    properties = schema.get("properties")
    definedKeys = set(properties.keys()) if isinstance(properties, Mapping) else set()

    if isinstance(properties, Mapping):
        for key, propertySchema in properties.items():
            if key not in value or not isinstance(propertySchema, Mapping):
                continue
            value[key] = _coerceWithJsonSchema(value[key], dict(propertySchema))

    additionalProperties = schema.get("additionalProperties")
    if isinstance(additionalProperties, Mapping):
        for key, propertyValue in list(value.items()):
            if key in definedKeys:
                continue
            value[key] = _coerceWithJsonSchema(propertyValue, dict(additionalProperties))


def _applySchemaArrayCoercion(value: list[Any], schema: _JsonSchemaObject) -> None:
    items = schema.get("items")
    if isinstance(items, list):
        for index in range(len(value)):
            itemSchema = items[index] if index < len(items) else None
            if not isinstance(itemSchema, Mapping):
                continue
            value[index] = _coerceWithJsonSchema(value[index], dict(itemSchema))
        return

    if isinstance(items, Mapping):
        for index in range(len(value)):
            value[index] = _coerceWithJsonSchema(value[index], dict(items))


def _coerceWithUnionSchema(value: Any, schemas: list[_JsonSchemaObject]) -> Any:
    for schema in schemas:
        validator = _getSubSchemaValidator(schema)
        if validator is not None and validator.Check(value):
            return value

    for schema in schemas:
        candidate = deepcopy(value)
        coerced = _coerceWithJsonSchema(candidate, schema)
        validator = _getSubSchemaValidator(schema)
        if validator is not None and validator.Check(coerced):
            return coerced
    return value


def _coerceWithJsonSchema(value: Any, schema: _JsonSchemaObject) -> Any:
    nextValue = value

    allOf = schema.get("allOf")
    if isinstance(allOf, list):
        for nested in allOf:
            if isinstance(nested, Mapping):
                nextValue = _coerceWithJsonSchema(nextValue, dict(nested))

    anyOf = schema.get("anyOf")
    if isinstance(anyOf, list):
        nextValue = _coerceWithUnionSchema(nextValue, [dict(item) for item in anyOf if isinstance(item, Mapping)])

    oneOf = schema.get("oneOf")
    if isinstance(oneOf, list):
        nextValue = _coerceWithUnionSchema(nextValue, [dict(item) for item in oneOf if isinstance(item, Mapping)])

    schemaTypes = _getSchemaTypes(schema)
    matchesUnionMember = len(schemaTypes) > 1 and any(_matchesJsonType(nextValue, type) for type in schemaTypes)
    if len(schemaTypes) > 0 and not matchesUnionMember:
        for schemaType in schemaTypes:
            candidate = _coercePrimitiveByType(nextValue, schemaType)
            if candidate is not nextValue:
                nextValue = candidate
                break

    # MISAKA fork: stands in for TypeBox's `Value.Convert`, which upstream runs first and which rescues the same value
    # written in another type ("1" for 1) for literal and enum schemas. Nothing else is rewritten: a model asking for an
    # operation that does not exist gets the validation error back rather than a real one run in its place.
    if "enum" in schema and isinstance(schema["enum"], list) and nextValue not in schema["enum"]:
        for candidate in schema["enum"]:
            if jsString(candidate) == jsString(nextValue):
                nextValue = candidate
                break

    if "const" in schema and nextValue != schema["const"] and jsString(nextValue) == jsString(schema["const"]):
        nextValue = schema["const"]

    if "object" in schemaTypes and isinstance(nextValue, dict):
        _applySchemaObjectCoercion(nextValue, schema)

    if "array" in schemaTypes and isinstance(nextValue, list):
        _applySchemaArrayCoercion(nextValue, schema)

    return nextValue


def _normalizeOptionalNulls(value: Any, schema: Any) -> None:
    if isinstance(value, list):
        items = schema.get("items") if isinstance(schema, Mapping) else None
        if isinstance(items, list):
            for index in range(len(value)):
                itemSchema = items[index] if index < len(items) else None
                if isinstance(itemSchema, Mapping):
                    _normalizeOptionalNulls(value[index], itemSchema)
        elif isinstance(items, Mapping):
            for item in value:
                _normalizeOptionalNulls(item, items)
        return
    if not isinstance(value, dict) or not isinstance(schema, Mapping) or not isinstance(schema.get("properties"), Mapping):
        return

    # MISAKA fork: `object` (upstream's name) is a builtin here.
    record = value
    required = set(schema.get("required") or [])
    for key, propertySchema in schema["properties"].items():
        if key not in record:
            continue
        validator = _getSubSchemaValidator(dict(propertySchema)) if isinstance(propertySchema, Mapping) else None
        if (
            record[key] is None
            and key not in required
            and not (isinstance(propertySchema, Mapping) and isinstance(propertySchema.get("$ref"), str))
            and validator is not None
            and validator.Check(None) is False
        ):
            del record[key]
        else:
            _normalizeOptionalNulls(record[key], propertySchema)


def _getValidator(schema: _JsonSchemaObject) -> Draft202012Validator:
    try:
        key = json.dumps(schema, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return Draft202012Validator(schema)  # not serializable: build it, cache nothing
    cached = _validatorCache.get(key)
    if cached is not None:
        return cached
    if len(_validatorCache) >= _VALIDATOR_CACHE_LIMIT:
        _validatorCache.clear()
    validator = Draft202012Validator(schema)
    _validatorCache[key] = validator
    return validator


def _formatValidationPath(error: JsonSchemaValidationError) -> str:
    """MISAKA fork: reads a jsonschema error (``validator``, ``absolute_path``) instead of TypeBox's ``keyword`` and
    ``instancePath``."""
    if error.validator == "required":
        requiredProperty = error.message.split("'")[1] if "'" in error.message else None
        if requiredProperty:
            basePath = ".".join(str(part) for part in error.absolute_path)
            return f"{basePath}.{requiredProperty}" if basePath else requiredProperty
    path = ".".join(str(part) for part in error.absolute_path)
    return path or "root"


def validateToolCall(tools: list[Tool], toolCall: ToolCall) -> Any:
    """Finds a tool by name and validates the tool call arguments against its TypeBox schema.

    Returns the validated arguments; raises if the tool is not found or validation fails.
    """
    tool = next((t for t in tools if t.name == toolCall.name), None)
    if tool is None:
        raise RuntimeError(f'Tool "{toolCall.name}" not found')
    return validateToolArguments(tool, toolCall)


def validateToolArguments(tool: Tool, toolCall: ToolCall) -> Any:
    """Validates tool call arguments against the tool's TypeBox schema.

    Returns the validated (and potentially coerced) arguments; raises with a formatted message if validation fails.
    """
    if toolCall.argumentsError:
        raise ValueError(f'Invalid arguments for tool "{toolCall.name}": {toolCall.argumentsError}')
    # MISAKA fork: a pydantic model class is the TypeBox schema of a MISAKA tool.
    if isinstance(tool.parameters, type) and issubclass(tool.parameters, BaseModel):
        return _validatePydanticToolArguments(tool, toolCall)
    if not isinstance(tool.parameters, Mapping):
        raise TypeError("Tool.parameters must be a Pydantic model class or JSON schema mapping")

    args = deepcopy(toolCall.arguments)
    _normalizeOptionalNulls(args, tool.parameters)

    validator = _getValidator(dict(tool.parameters))
    coerced = _coerceWithJsonSchema(args, dict(tool.parameters))
    if coerced is not args:
        if isinstance(args, dict) and isinstance(coerced, dict):
            args.clear()
            args.update(coerced)
        else:
            return coerced if not any(True for _ in validator.iter_errors(coerced)) else args

    errorList = sorted(validator.iter_errors(args), key=lambda error: list(error.absolute_path))
    if not errorList:
        return args

    errors = "\n".join(f"  - {_formatValidationPath(error)}: {error.message}" for error in errorList) or "Unknown validation error"

    errorMessage = f'Validation failed for tool "{toolCall.name}":\n{errors}\n\nReceived arguments:\n{_receivedArguments(toolCall.arguments)}'

    raise ValueError(errorMessage)


def _receivedArguments(arguments: Any) -> str:
    """MISAKA fork: ``JSON.stringify(toolCall.arguments, null, 2)``; a value that is not JSON is shown as Python
    shows it."""
    try:
        return jsonStringify(arguments, 2)
    except Exception:  # noqa: BLE001 - not JSON: show it anyway
        return repr(arguments)


def _validatePydanticToolArguments(tool: Tool, toolCall: ToolCall) -> Any:
    """MISAKA fork: the TypeBox branch, for a pydantic model class."""
    args = deepcopy(toolCall.arguments)
    schema = typeboxSchema(tool.parameters)
    _normalizeOptionalNulls(args, schema)
    Convert(schema, args)        # upstream: `Value.Convert(tool.parameters, args)`, which converts `args` in place
    try:
        validated = tool.parameters.model_validate(args)
    except PydanticValidationError as error:
        errors = (
            "\n".join(
                f"  - {'.'.join(str(part) for part in item['loc']) or 'root'}: {item['msg']}" for item in error.errors()
            )
            or "Unknown validation error"
        )
        raise ValueError(
            f'Validation failed for tool "{toolCall.name}":\n{errors}\n\nReceived arguments:\n{_receivedArguments(toolCall.arguments)}'
        ) from error
    # Upstream accepts only what `validator.Check(args)` accepts. Pydantic's lax mode also takes values TypeBox's Check
    # refuses ("yes" for a boolean, which `Value.Convert` leaves as it is), so the converted arguments are checked
    # against the schema too, with the plain JSON schema branch's error texts.
    errorList = sorted(_getValidator(schema).iter_errors(args), key=lambda error: list(error.absolute_path))
    if errorList:
        errors = "\n".join(f"  - {_formatValidationPath(error)}: {error.message}" for error in errorList)
        raise ValueError(
            f'Validation failed for tool "{toolCall.name}":\n{errors}\n\nReceived arguments:\n{_receivedArguments(toolCall.arguments)}'
        )
    return validated.model_dump(mode="python")


__all__ = ["validateToolArguments", "validateToolCall"]
