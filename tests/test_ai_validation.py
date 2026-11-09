"""Translated from pi's ``packages/ai/test/validation.test.ts`` (v1.3.0).

MISAKA fork: TypeBox schemas are written as the JSON schemas they compile to. Two upstream checks are about TypeBox's
JavaScript code generation and have no counterpart here: validation without the ``Function`` constructor (CSP), and
running the generated validator for the nullable-array case (its ``validateToolArguments`` assertion is kept).
"""

from __future__ import annotations

from typing import Any

import pytest

from misaka.ai.types import Tool, ToolCall
from misaka.ai.utils.validation import validateToolArguments


def createToolCallWithPlainSchema(schema: Any, value: Any) -> tuple[Tool, ToolCall]:
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={"type": "object", "properties": {"value": schema}, "required": ["value"]},
    )
    toolCall = ToolCall(id="tool-1", name="echo", arguments={"value": value})
    return tool, toolCall


@pytest.mark.parametrize(
    ("schema", "input", "expected"),
    [
        ({"type": "number"}, "42", 42),
        ({"type": "number"}, True, 1),
        ({"type": "number"}, None, 0),
        ({"type": "integer"}, "42", 42),
        ({"type": "boolean"}, "true", True),
        ({"type": "boolean"}, "false", False),
        ({"type": "boolean"}, 1, True),
        ({"type": "boolean"}, 0, False),
        ({"type": "string"}, None, ""),
        ({"type": "string"}, True, "true"),
        ({"type": "null"}, "", None),
        ({"type": "null"}, 0, None),
        ({"type": "null"}, False, None),
        ({"type": ["number", "string"]}, "1", "1"),
        ({"type": ["boolean", "number"]}, "1", 1),
    ],
)
def test_coerces_serialized_plain_json_schemas_with_ajv_compatible_primitive_rules(schema, input, expected):
    tool, toolCall = createToolCallWithPlainSchema(schema, input)
    result = validateToolArguments(tool, toolCall)
    assert result == {"value": expected}
    assert type(result["value"]) is type(expected)


def test_treats_null_as_omission_for_optional_non_nullable_properties():
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "number"},
                "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "metadata": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
            },
            "required": ["path", "metadata"],
        },
    )
    toolCall = ToolCall(
        id="tool-1",
        name="echo",
        arguments={"path": "file.txt", "offset": None, "nullable": None, "metadata": {"enabled": None}},
    )

    assert validateToolArguments(tool, toolCall) == {"path": "file.txt", "nullable": None, "metadata": {}}


def test_preserves_optional_nulls_whose_referenced_schema_is_nullable():
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/value"}},
            "$defs": {"value": {"anyOf": [{"type": "number"}, {"type": "null"}]}},
        },
    )
    toolCall = ToolCall(id="tool-1", name="echo", arguments={"value": None})

    assert validateToolArguments(tool, toolCall) == {"value": None}


def test_preserves_a_value_that_already_matches_a_nullable_union_arm():
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={
            "type": "object",
            "properties": {"value": {"anyOf": [{"type": "number"}, {"type": "null"}]}},
            "required": ["value"],
        },
    )
    toolCall = ToolCall(id="tool-1", name="echo", arguments={"value": None})

    assert validateToolArguments(tool, toolCall) == {"value": None}


def test_preserves_a_value_that_already_matches_a_one_of_nullable_union_arm():
    tool, toolCall = createToolCallWithPlainSchema({"oneOf": [{"type": "number"}, {"type": "null"}]}, None)

    assert validateToolArguments(tool, toolCall) == {"value": None}


def test_still_coerces_nullable_unions_when_the_original_value_does_not_match_any_arm():
    tool, toolCall = createToolCallWithPlainSchema({"anyOf": [{"type": "number"}, {"type": "null"}]}, "42")

    assert validateToolArguments(tool, toolCall) == {"value": 42}


def test_accepts_null_for_nullable_array_schemas_with_items():
    tool, toolCall = createToolCallWithPlainSchema({"type": ["array", "null"], "items": {"type": "string"}}, None)

    assert validateToolArguments(tool, toolCall) == {"value": None}


@pytest.mark.parametrize(
    ("schema", "input"),
    [
        ({"type": "boolean"}, "1"),
        ({"type": "boolean"}, "0"),
        ({"type": "null"}, "null"),
        ({"type": "integer"}, "42.1"),
    ],
)
def test_rejects_invalid_coercions_for_serialized_plain_json_schemas(schema, input):
    tool, toolCall = createToolCallWithPlainSchema(schema, input)
    with pytest.raises(Exception, match="Validation failed"):
        validateToolArguments(tool, toolCall)


# #10897
def test_reports_malformed_arguments_json_instead_of_validating_empty_arguments():
    tool = Tool(
        name="echo",
        description="Echo tool",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
    )
    toolCall = ToolCall(id="tool-1", name="echo", arguments={}, argumentsError="Unexpected token")

    with pytest.raises(Exception, match='Invalid arguments for tool "echo": Unexpected token'):
        validateToolArguments(tool, toolCall)
