"""pi codemode's ``declarations.test.ts``."""

from __future__ import annotations

from misaka.codemode import (
    renderDeclarations,
    renderToolSample,
    renderToolSignature,
    schemaToType,
)


def execute(_args, _context):
    return None


def joinLines(*lines: str) -> str:
    return "\n".join(lines)


# -- schemaToType ------------------------------------------------------------------------------


def test_renders_primitives_literals_and_unions():
    assert schemaToType({"type": "string"}) == "string"
    assert schemaToType({"type": "integer"}) == "number"
    assert schemaToType({"type": ["string", "null"]}) == "string | null"
    assert schemaToType({"const": "a"}) == '"a"'
    assert schemaToType({"enum": ["a", 1, None]}) == '"a" | 1 | null'
    assert schemaToType({"anyOf": [{"type": "string"}, {"type": "number"}]}) == "string | number"
    assert schemaToType({"anyOf": [{"type": "string"}, {}]}) == "unknown"
    assert schemaToType({"allOf": [{"anyOf": [{"type": "string"}, {"type": "number"}]}, {"const": 1}]}) == "(string | number) & 1"
    assert schemaToType({"$ref": "#/defs/x"}) == "unknown"
    assert schemaToType(True) == "unknown"
    assert schemaToType(False) == "never"


def test_renders_objects_on_one_line_with_sorted_properties():
    assert schemaToType({
        "type": "object",
        "properties": {"city": {"type": "string"}, "max-lines": {"type": "number"}},
        "required": ["city"],
        "additionalProperties": False,
    }) == '{ city: string; "max-lines"?: number; }'
    assert schemaToType({"type": "object", "additionalProperties": {"type": "number"}}) == "{ [key: string]: number; }"
    assert schemaToType({"type": "object"}) == "{ [key: string]: unknown; }"
    assert schemaToType({"type": "object", "properties": {}, "additionalProperties": False}) == "{}"


def test_puts_property_descriptions_on_comment_lines():
    assert schemaToType({
        "type": "object",
        "properties": {
            "weather": {
                "type": "array",
                "description": "look up weather for a given list of locations",
                "items": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]},
            },
        },
        "required": ["weather"],
    }) == "{\n  // look up weather for a given list of locations\n  weather: Array<{ location: string; }>;\n}"
    assert schemaToType({
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "description": "Outer",
                "properties": {"inner": {"type": "string", "description": "Inner"}},
            },
        },
    }) == "{\n  // Outer\n  outer?: {\n    // Inner\n    inner?: string;\n  };\n}"


# https://github.com/earendil-works/pi/issues/10707
def test_puts_constraints_the_type_cannot_express_on_property_comment_lines():
    assert schemaToType({
        "type": "object",
        "properties": {
            "timeoutMs": {
                "type": "integer",
                "minimum": 0,
                "maximum": 30000,
                "default": 0,
                "description": "Wait in milliseconds.",
            },
            "id": {"type": "string", "pattern": "^[a-z]+$", "maxLength": 8},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5, "uniqueItems": True, "deprecated": True},
            "name": {"type": "string"},
        },
        "required": ["id"],
    }) == joinLines(
        "{",
        "  // @maxLength 8 @pattern ^[a-z]+$",
        "  id: string;",
        "  name?: string;",
        "  // @maxItems 5 @uniqueItems @deprecated",
        "  tags?: Array<string>;",
        "  // Wait in milliseconds.",
        "  // @integer @minimum 0 @maximum 30000 @default 0",
        "  timeoutMs?: number;",
        "}",
    )


def test_reads_constraints_of_optional_fields_from_the_non_null_branch():
    # Pydantic's `limit: int | None = Field(None, ge=1, le=100)`.
    assert schemaToType({
        "type": "object",
        "properties": {
            "limit": {
                "anyOf": [{"type": "integer", "minimum": 1, "maximum": 100}, {"type": "null"}],
                "default": None,
                "title": "Limit",
            },
        },
    }) == "{\n  // @integer @minimum 1 @maximum 100\n  limit?: number | null;\n}"
    # Constraints of the property and the branch both apply.
    assert schemaToType({
        "type": "object",
        "properties": {
            "n": {
                "anyOf": [{"type": "integer", "minimum": 10, "maximum": 50, "format": "int32"}, {"type": "null"}],
                "minimum": 0,
                "maximum": 20,
                "format": "port",
            },
        },
    }) == "{\n  // @integer @minimum 10 @maximum 20 @format port @format int32\n  n?: number | null;\n}"


def test_reads_draft_04_boolean_exclusive_bounds():
    assert schemaToType({
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 0, "exclusiveMinimum": True, "maximum": 10, "exclusiveMaximum": False},
            "m": {"anyOf": [{"type": "number", "maximum": 5, "exclusiveMaximum": True}, {"type": "null"}], "maximum": 8},
        },
    }) == joinLines(
        "{",
        "  // @maximum 8 @exclusiveMaximum 5",
        "  m?: number | null;",
        "  // @integer @exclusiveMinimum 0 @maximum 10",
        "  n?: number;",
        "}",
    )


def test_leaves_out_long_tags_and_escapes_line_terminators():
    assert schemaToType({
        "type": "object",
        "properties": {"config": {"type": "object", "default": {"text": "x" * 100}}},
    }) == "{ config?: { [key: string]: unknown; }; }"
    assert schemaToType({
        "type": "object",
        "properties": {
            "sep": {"type": "string", "pattern": "^[\r\n\u2028\u2029]$", "default": "\u2028", "description": "a\u2028b"},
        },
    }) == joinLines(
        "{",
        "  // a",
        "  // b",
        '  // @pattern ^[\\u000d\\u000a\\u2028\\u2029]$ @default "\\u2028"',
        "  sep?: string;",
        "}",
    )


def test_drops_constraint_tags_before_rendering_a_type_over_the_budget_as_unknown():
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}
    assert schemaToType(schema) == "{\n  // @integer @minimum 1\n  n?: number;\n}"
    assert schemaToType(schema, {"maxChars": 20}) == "{ n?: number; }"
    assert schemaToType(schema, {"maxChars": 10}) == "unknown"


# -- misaka adaptations: JavaScript number formatting and equality in tags ---------------------


def test_formats_tag_numbers_as_javascript_prints_them():
    # `${n}`: a whole float prints without a fraction, and 1 and 1.0 are one value for `new Set`.
    assert schemaToType({
        "type": "object",
        "properties": {
            "n": {"anyOf": [{"type": "number", "multipleOf": 1.0}, {"type": "null"}], "multipleOf": 1, "maximum": 1e21},
            "s": {"type": "string", "format": "1", "anyOf": [{"type": "string", "format": 1}, {"type": "null"}]},
            "b": {"type": "boolean", "default": False},
        },
    }) == joinLines(
        "{",
        "  // @default false",
        "  b?: boolean;",
        "  // @maximum 1e+21 @multipleOf 1",
        "  n?: number | null;",
        "  // @format 1 @format 1",
        "  s?: string | null;",
        "}",
    )


def test_resolves_local_references_and_stops_at_recursive_ones():
    schema = {
        "type": "object",
        "properties": {
            "item": {"$ref": "#/$defs/Item"},
            "legacy": {"$ref": "#/definitions/Legacy"},
            "remote": {"$ref": "https://example.com/schema.json"},
        },
        "required": ["item"],
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "parent": {"$ref": "#/$defs/Item"}},
                "required": ["id"],
            },
        },
        "definitions": {"Legacy": {"enum": ["a", "b"]}},
    }
    assert schemaToType(schema) == '{ item: { id: string; parent?: unknown; }; legacy?: "a" | "b"; remote?: unknown; }'


def test_renders_arrays_and_tuples():
    assert schemaToType({"type": "array", "items": {"type": "string"}}) == "Array<string>"
    assert schemaToType({"type": "array", "prefixItems": [{"type": "string"}, {"type": "number"}]}) == "[string, number]"
    assert schemaToType({"type": "array"}) == "unknown[]"


def test_renders_types_over_the_budget_as_unknown():
    properties = {f"field{i}": {"type": "string"} for i in range(50)}
    schema = {"type": "object", "properties": properties}
    assert schemaToType(schema, {"maxChars": 100}) == "unknown"
    assert "field49?: string;" in schemaToType(schema)


# -- tool declarations -------------------------------------------------------------------------


def test_renders_signatures_with_normalized_identifiers():
    assert renderToolSignature({
        "name": "hidden-dynamic-tool",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    }) == "hidden_dynamic_tool(args: { city: string; }): Promise<{ ok: boolean; }>;"
    assert renderToolSignature({"name": "free"}) == "free(args: unknown): Promise<unknown>;"


def test_renders_output_schemas_shaped_like_an_mcp_call_tool_result_as_plain_types():
    outputSchema = {
        "type": "object",
        "properties": {
            "content": {"type": "array", "items": {"type": "object"}},
            "isError": {"type": "boolean"},
            "_meta": {"type": "object"},
        },
        "required": ["content"],
    }
    assert renderToolSignature({"name": "plain", "inputSchema": {"type": "object", "properties": {}}, "outputSchema": outputSchema}) == (
        "plain(args: { [key: string]: unknown; }): Promise<{ _meta?: { [key: string]: unknown; }; content: Array<{ [key: string]: unknown; }>; isError?: boolean; }>;"
    )


def test_renders_the_per_tool_sample():
    assert renderToolSample({"name": "foo", "description": "bar", "inputSchema": {"type": "string"}}) == (
        "bar\n\ncodemode tool declaration:\n```ts\ndeclare const tools: { foo(args: string): Promise<unknown>; };\n```"
    )


# -- renderDeclarations ------------------------------------------------------------------------


def test_renders_tools_and_globals():
    text = renderDeclarations({
        "tools": [
            {
                "name": "read",
                "description": "Read a file.\nSecond line.",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                "outputSchema": {"type": "string"},
                "execute": execute,
            },
            {"name": "remote-api", "execute": execute},
        ],
        "globals": [{"name": "attach", "description": "Attach it.", "inputSchema": {"type": "string"}, "execute": execute}],
    })
    assert text == "declare const tools: {\n  /**\n   * Read a file.\n   * Second line.\n   */\n  read(args: { path: string; }): Promise<string>;\n  remote_api(args: unknown): Promise<unknown>;\n};\n\n/** Attach it. */\ndeclare function attach(args: string): Promise<unknown>;"


def test_renders_namespaced_globals_and_explicit_signatures():
    text = renderDeclarations({
        "globals": [
            {"name": "models.list", "description": "List models.", "signature": "(type: string): Promise<string[]>", "execute": execute},
            {"name": "models.get", "inputSchema": {"type": "string"}, "execute": execute},
            {"name": "plain", "signature": "(): void", "execute": execute},
        ],
    })
    assert text == "declare function plain(): void;\n\ndeclare const models: {\n  /** List models. */\n  list(type: string): Promise<string[]>;\n  get(args: string): Promise<unknown>;\n};"


def test_escapes_comment_terminators_in_descriptions():
    text = renderDeclarations({"tools": [{"name": "x", "description": "a */ b", "execute": execute}]})
    assert "/** a *\\/ b */" in text
