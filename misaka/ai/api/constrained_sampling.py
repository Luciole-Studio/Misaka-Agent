"""Translated from pi's ``packages/ai/src/api/constrained-sampling.ts`` (v1.1.0).

MISAKA fork: a tool's ``parameters`` may be a pydantic model class, so the schema is read through
``Tool.parameters_json_schema()``; ``structuredClone`` is ``deepcopy``; ``!== undefined`` on a decoded JSON schema is
key presence; ``_isJsTruthy`` stands in for JavaScript truthiness where ``{}`` must count as present.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from misaka.ai.types import SchemaModel, Tool
from misaka.utils.js import jsLength, jsonStringify, jsSlice, jsString, jsTrim

type _JsonSchemaObject = dict[str, Any]


class _UnsupportedStrictJsonSchemaError(Exception):
    pass


# Returns true when a provider's strict mode rejects this schema keyword with this value.
type UnsupportedStrictSchemaKeywordCheck = Callable[[str, Any], bool]

_UNSUPPORTED_STRICT_SCHEMA_KEYS = (
    "$ref",
    "$defs",
    "definitions",
    "allOf",
    "oneOf",
    "patternProperties",
    "dependentSchemas",
    "dependencies",
    "unevaluatedProperties",
    "propertyNames",
    "contains",
    "prefixItems",
    "not",
    "if",
    "then",
    "else",
)


def _isJsonSchemaObject(value: Any) -> bool:
    return isinstance(value, dict)


def _isJsTruthy(value: Any) -> bool:
    """MISAKA fork: JavaScript truthiness, where ``{}`` and ``[]`` are truthy."""
    return True if isinstance(value, (dict, list)) else bool(value)


def _isStructuredSchema(schema: Any) -> bool:
    if not _isJsonSchemaObject(schema):
        return False
    schemaType = schema.get("type")
    types = [schemaType] if isinstance(schemaType, str) else schemaType if isinstance(schemaType, list) else []
    return "object" in types or "array" in types or "properties" in schema or "items" in schema


def _schemaAllowsNull(schema: Any) -> bool:
    if not _isJsonSchemaObject(schema):
        return False
    schemaType = schema.get("type")
    if schemaType == "null" or (isinstance(schemaType, list) and "null" in schemaType):
        return True
    if ("const" in schema and schema["const"] is None) or (
        isinstance(schema.get("enum"), list) and None in schema["enum"]
    ):
        return True
    anyOf = schema.get("anyOf")
    return isinstance(anyOf, list) and any(_schemaAllowsNull(variant) for variant in anyOf)


def _makeJsonSchemaNodeStrict(schema: Any, isUnsupportedKeyword: UnsupportedStrictSchemaKeywordCheck | None = None) -> None:
    if not _isJsonSchemaObject(schema):
        raise _UnsupportedStrictJsonSchemaError("boolean schemas are unsupported")
    for key in _UNSUPPORTED_STRICT_SCHEMA_KEYS:
        if key in schema:
            raise _UnsupportedStrictJsonSchemaError(f"{key} schemas are unsupported")
    if isUnsupportedKeyword:
        for key, value in schema.items():
            if isUnsupportedKeyword(key, value):
                raise _UnsupportedStrictJsonSchemaError(f"{key}: {jsonStringify(value)} is unsupported")

    if "anyOf" in schema:
        if not isinstance(schema["anyOf"], list) or len(schema["anyOf"]) == 0:
            raise _UnsupportedStrictJsonSchemaError("anyOf must contain at least one schema")
        for variant in schema["anyOf"]:
            if _isStructuredSchema(variant):
                raise _UnsupportedStrictJsonSchemaError("object and array unions are unsupported")
            _makeJsonSchemaNodeStrict(variant, isUnsupportedKeyword)

    if "items" in schema:
        if isinstance(schema["items"], list):
            raise _UnsupportedStrictJsonSchemaError("tuple schemas are unsupported")
        _makeJsonSchemaNodeStrict(schema["items"], isUnsupportedKeyword)

    isObjectSchema = schema.get("type") == "object"
    if "properties" in schema and not isObjectSchema:
        raise _UnsupportedStrictJsonSchemaError("properties require type object")
    if not isObjectSchema:
        return
    # `!== false`: `is not False`, since `0 == False` in Python.
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise _UnsupportedStrictJsonSchemaError("schema-valued or true additionalProperties is unsupported")
    if "properties" in schema and not _isJsonSchemaObject(schema["properties"]):
        raise _UnsupportedStrictJsonSchemaError("object properties must be a schema map")
    if "required" in schema and (
        not isinstance(schema["required"], list) or any(not isinstance(key, str) for key in schema["required"])
    ):
        raise _UnsupportedStrictJsonSchemaError("object required must be a string array")

    properties = schema["properties"] if schema.get("properties") is not None else {}
    propertyNames = list(properties.keys())
    required = set(schema["required"] if isinstance(schema.get("required"), list) else [])
    if any(key not in propertyNames for key in required):
        raise _UnsupportedStrictJsonSchemaError("required contains an unknown property")
    for key, property in list(properties.items()):
        _makeJsonSchemaNodeStrict(property, isUnsupportedKeyword)
        if key not in required and not _schemaAllowsNull(property):
            properties[key] = {"anyOf": [property, {"type": "null"}]}
    schema["required"] = propertyNames
    schema["additionalProperties"] = False


def makeStrictJsonSchema(
    schema: Any,
    isUnsupportedKeyword: UnsupportedStrictSchemaKeywordCheck | None = None,
) -> dict[str, Any]:
    """Convert a tool schema to the strict subset expected by provider constrained sampling."""
    cloned = deepcopy(schema)
    if not _isJsonSchemaObject(cloned):
        raise _UnsupportedStrictJsonSchemaError("root schema must have type object")
    _makeJsonSchemaNodeStrict(cloned, isUnsupportedKeyword)
    if cloned.get("type") != "object":
        raise _UnsupportedStrictJsonSchemaError("root schema must have type object")
    return cloned  # type: ignore[no-any-return]


def _portable_json_schema(parameters: dict[str, Any]) -> dict[str, Any]:
    """Give required-only object alternatives their own property definitions."""
    result = deepcopy(parameters)

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            properties = node.get("properties")
            alternatives = node.get("anyOf")
            if node.get("type") == "object" and isinstance(properties, dict) and isinstance(alternatives, list):
                for branch in alternatives:
                    if not isinstance(branch, dict) or set(branch) != {"required"}:
                        continue
                    required = branch["required"]
                    if isinstance(required, list) and required and all(isinstance(key, str) and key in properties for key in required):
                        branch["type"] = "object"
                        branch["properties"] = {key: deepcopy(properties[key]) for key in required}
            for value in node.values():
                visit(value)

    visit(result)
    return result


def getJsonSchemaToolParameters(tool: Tool, strict: bool | None) -> Any:
    parameters = tool.parameters_json_schema()
    return makeStrictJsonSchema(parameters) if strict is True else _portable_json_schema(parameters)


class GrammarConstrainedSampling(SchemaModel):
    format: Literal["lark", "regex"]
    definition: str
    inputProperty: str


@dataclass
class GrammarToolInputJsonBuffer:
    input: str = ""
    started: bool = False
    closed: bool = False


def getGrammarToolInput(toolName: str, arguments_: dict[str, Any], inputProperty: str) -> str:
    input = arguments_.get(inputProperty)
    if not isinstance(input, str):
        raise RuntimeError(f'Grammar tool call "{toolName}" requires argument "{inputProperty}" to be a string.')  # noqa: TRY004
    return input


def appendGrammarToolInputJsonDelta(
    buffer: GrammarToolInputJsonBuffer,
    inputProperty: str,
    nextInput: str,
    close: bool,
) -> str | None:
    if buffer.closed:
        if close and nextInput == buffer.input:
            return None
        raise RuntimeError(f'grammar tool input for property "{inputProperty}" changed after it was closed')
    if not nextInput.startswith(buffer.input):
        raise RuntimeError(f'grammar tool input for property "{inputProperty}" changed non-monotonically')

    inputDelta = jsSlice(nextInput, jsLength(buffer.input))
    if not close and jsLength(inputDelta) == 0:
        return None

    delta = ""
    if not buffer.started:
        delta += f'{{{jsonStringify(inputProperty)}:"'
        buffer.started = True
    delta += jsonStringify(inputDelta)[1:-1]
    buffer.input = nextInput

    if close:
        delta += '"}'
        buffer.closed = True
    return delta


def _inferGrammarInputProperty(tool: Tool) -> str:
    schema: _JsonSchemaObject = tool.parameters_json_schema()
    if schema.get("type") != "object":
        raise RuntimeError("grammar constrained sampling requires an object parameter schema")
    required = schema.get("required")
    if not isinstance(required, list) or len(required) != 1 or not isinstance(required[0], str):
        raise RuntimeError("grammar constrained sampling requires exactly one required string property")

    inputProperty = required[0]
    properties = schema.get("properties")
    entry = properties.get(inputProperty) if _isJsonSchemaObject(properties) else None
    if not _isJsTruthy(entry):
        raise RuntimeError(f"grammar constrained sampling requires a properties entry for {inputProperty}")
    if not _isJsonSchemaObject(entry) or entry.get("type") != "string":
        raise RuntimeError(f"grammar constrained sampling property {inputProperty} must have type string")
    return inputProperty


def resolveJsonSchemaStrictSampling(
    tool: Tool,
    supportsStrictMode: bool,
    isUnsupportedKeyword: UnsupportedStrictSchemaKeywordCheck | None = None,
) -> bool | None:
    """Decide whether a JSON-schema tool is sent in strict mode. `isUnsupportedKeyword` lets a provider
    reject extra keywords its strict mode does not accept, so "prefer" tools fall back to non-strict."""
    config = tool.constrainedSampling
    if not config or config.type != "json_schema":
        return None

    if supportsStrictMode:
        try:
            makeStrictJsonSchema(tool.parameters_json_schema(), isUnsupportedKeyword)
            return True
        except _UnsupportedStrictJsonSchemaError as error:
            if config.strict != "require":
                return None
            raise RuntimeError(
                f'Tool "{tool.name}" requires JSON-schema constrained sampling, but {error}.'
            ) from error
    if config.strict == "require":
        raise RuntimeError(
            f'Tool "{tool.name}" requires JSON-schema constrained sampling, but strict tools are unsupported.'
        )
    return None


def resolveGrammarConstrainedSampling(
    tool: Tool,
    supportsOpenAIGrammarTools: bool,
) -> GrammarConstrainedSampling | None:
    config = tool.constrainedSampling
    if not config or config.type != "grammar":
        return None

    if not supportsOpenAIGrammarTools:
        return None

    larkDefinition = config.variants.get("openai_lark")
    regexDefinition = config.variants.get("openai_regex")
    hasLarkDefinition = isinstance(larkDefinition, str) and jsLength(jsTrim(larkDefinition)) > 0
    hasRegexDefinition = isinstance(regexDefinition, str) and jsLength(jsTrim(regexDefinition)) > 0
    if not hasLarkDefinition and not hasRegexDefinition:
        raise RuntimeError(
            f'Tool "{tool.name}" cannot use grammar constrained sampling: no supported grammar variant was provided.'
        )

    try:
        return GrammarConstrainedSampling(
            format="lark" if hasLarkDefinition else "regex",
            definition=larkDefinition if hasLarkDefinition else regexDefinition,  # type: ignore[arg-type]
            inputProperty=_inferGrammarInputProperty(tool),
        )
    except Exception as error:
        message = str(error) if isinstance(error, Exception) else jsString(error)
        raise RuntimeError(f'Tool "{tool.name}" cannot use grammar constrained sampling: {message}.') from error


def createGrammarToolInputProperties(
    tools: Sequence[Tool] | None,
    supportsOpenAIGrammarTools: bool,
) -> dict[str, str]:
    properties: dict[str, str] = {}
    for tool in tools or []:
        grammar = resolveGrammarConstrainedSampling(tool, supportsOpenAIGrammarTools)
        if grammar:
            properties[tool.name] = grammar.inputProperty
    return properties


__all__ = [
    "GrammarConstrainedSampling",
    "GrammarToolInputJsonBuffer",
    "UnsupportedStrictSchemaKeywordCheck",
    "appendGrammarToolInputJsonDelta",
    "createGrammarToolInputProperties",
    "getGrammarToolInput",
    "getJsonSchemaToolParameters",
    "makeStrictJsonSchema",
    "resolveGrammarConstrainedSampling",
    "resolveJsonSchemaStrictSampling",
]
