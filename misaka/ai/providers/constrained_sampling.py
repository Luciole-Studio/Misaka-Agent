
# misaka/ai/types.py. This is consistency, not a technical constraint: a PEP 695 `type` alias
# does resolve in the `TypeAdapter` below (checked against the installed pydantic 2.13.4).

"""Provider-side constrained sampling: strict JSON schemas and grammar tools.

Port of pi ``packages/ai/src/api/constrained-sampling.ts``. This is not a stream API. It is
the request-construction half of constrained sampling -- in pi the adapters that import it
(openai-completions, openai-responses and its azure/codex variants, openai-responses-shared,
anthropic-messages, bedrock-converse-stream, google-shared, mistral-conversations) call it
while they encode a ``Tool`` -- plus the incremental encoder that turns a grammar tool's raw
text back into the tool-call argument JSON deltas the rest of the runtime expects. No misaka
adapter imports this module yet; today only the port-fidelity tests do.

Two provider features live here:

* **JSON-schema constrained sampling** (``strict`` in OpenAI's vocabulary). Providers accept
  only a subset of JSON Schema for it, so :func:`make_strict_json_schema` rewrites a tool's
  parameters into that subset or refuses. A tool asking for ``strict: "prefer"`` silently
  falls back to unconstrained sampling when the rewrite is impossible; ``"require"`` fails.
* **Grammar constrained sampling**, where the model emits raw text in a Lark/regex language
  instead of JSON. The tool still has to look like a normal one-string-argument tool to the
  agent, so the text is re-encoded into ``{"<property>":"..."}`` as it streams.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from misaka.ai.types import (
    ConstrainedSamplingConfig,
    GrammarConstrainedSamplingConfig,
    GrammarFormat,
    JsonSchemaConstrainedSamplingConfig,
    SchemaModel,
    Tool,
)

_CONSTRAINED_SAMPLING_ADAPTER: TypeAdapter[ConstrainedSamplingConfig] = TypeAdapter(
    Annotated[ConstrainedSamplingConfig, Field(discriminator="type")]
)


class UnsupportedStrictJsonSchemaError(ValueError):
    """A schema cannot be expressed in the strict subset.

    Its own class because :func:`resolve_json_schema_strict_sampling` has to tell "this
    schema is too rich for strict mode, fall back" apart from a genuine bug; a ``ValueError``
    subclass because to every other caller a rejected schema is just a bad value.
    """


_UNSUPPORTED_STRICT_SCHEMA_KEYS: tuple[str, ...] = (
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


def _is_json_schema_object(value: Any) -> bool:
    """pi's ``typeof value === "object" && value !== null && !Array.isArray(value)``.

    A JSON schema reaches this module as a ``dict`` (``Tool.parameters_json_schema`` returns
    one), and the strict rewrite edits nodes in place, so the plain ``dict`` test is both the
    faithful translation and the one that guarantees the mutation below is legal.
    """
    return isinstance(value, dict)


def _is_js_truthy(value: Any) -> bool:
    """JS truthiness, which is not Python's.

    ``{}`` and ``[]`` are truthy in JS and falsy in Python, and the distinction is load
    bearing: an empty property schema must reach the "must have type string" check rather
    than be reported as a missing property entry.
    """
    if isinstance(value, (dict, list)):
        return True
    return bool(value)


def _is_structured_schema(schema: Any) -> bool:
    """Whether the schema describes an object or an array (in a union: unsupported)."""
    if not _is_json_schema_object(schema):
        return False
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        types: list[Any] = [raw_type]
    elif isinstance(raw_type, list):
        types = raw_type
    else:
        types = []
    return "object" in types or "array" in types or "properties" in schema or "items" in schema


def _schema_allows_null(schema: Any) -> bool:
    """Whether ``null`` already validates, so an optional property needs no null variant."""
    if not _is_json_schema_object(schema):
        return False
    raw_type = schema.get("type")
    if raw_type == "null" or (isinstance(raw_type, list) and "null" in raw_type):
        return True
    # `const: null` is present-and-null, not absent: JS distinguishes null from undefined,
    # and in a decoded JSON document a missing key is the only "undefined" there is.
    if ("const" in schema and schema["const"] is None) or (
        isinstance(schema.get("enum"), list) and None in schema["enum"]
    ):
        return True
    any_of = schema.get("anyOf")
    return isinstance(any_of, list) and any(_schema_allows_null(variant) for variant in any_of)


def _make_json_schema_node_strict(schema: Any) -> None:
    """Rewrite one node in place, or raise if the strict subset cannot express it."""
    if not _is_json_schema_object(schema):
        raise UnsupportedStrictJsonSchemaError("boolean schemas are unsupported")
    for key in _UNSUPPORTED_STRICT_SCHEMA_KEYS:
        # pi tests `!== undefined`; decoded JSON has no undefined, so presence is the test.
        if key in schema:
            raise UnsupportedStrictJsonSchemaError(f"{key} schemas are unsupported")

    if "anyOf" in schema:
        any_of = schema["anyOf"]
        if not isinstance(any_of, list) or len(any_of) == 0:
            raise UnsupportedStrictJsonSchemaError("anyOf must contain at least one schema")
        for variant in any_of:
            if _is_structured_schema(variant):
                raise UnsupportedStrictJsonSchemaError("object and array unions are unsupported")
            _make_json_schema_node_strict(variant)

    if "items" in schema:
        items = schema["items"]
        if isinstance(items, list):
            raise UnsupportedStrictJsonSchemaError("tuple schemas are unsupported")
        _make_json_schema_node_strict(items)

    is_object_schema = schema.get("type") == "object"
    if "properties" in schema and not is_object_schema:
        raise UnsupportedStrictJsonSchemaError("properties require type object")
    if not is_object_schema:
        return
    # `is not False` rather than `!= False`: in Python `0 == False`, and a schema-valued
    # `additionalProperties` of `0` must still be rejected.
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise UnsupportedStrictJsonSchemaError("schema-valued or true additionalProperties is unsupported")
    if "properties" in schema and not _is_json_schema_object(schema["properties"]):
        raise UnsupportedStrictJsonSchemaError("object properties must be a schema map")
    if "required" in schema and (
        not isinstance(schema["required"], list) or any(not isinstance(key, str) for key in schema["required"])
    ):
        raise UnsupportedStrictJsonSchemaError("object required must be a string array")

    properties: dict[str, Any] = schema.get("properties", {})
    property_names = list(properties.keys())
    raw_required = schema.get("required")
    required = set(raw_required) if isinstance(raw_required, list) else set()
    if any(key not in property_names for key in required):
        raise UnsupportedStrictJsonSchemaError("required contains an unknown property")
    for key, prop in list(properties.items()):
        _make_json_schema_node_strict(prop)
        if key not in required and not _schema_allows_null(prop):
            # Strict mode has no optional properties -- every key must be listed in
            # `required` -- so an omissible property becomes an explicitly nullable one.
            properties[key] = {"anyOf": [prop, {"type": "null"}]}
    schema["required"] = property_names
    schema["additionalProperties"] = False


def make_strict_json_schema(schema: Any) -> dict[str, Any]:
    """Convert a tool schema to the strict subset expected by provider constrained sampling.

    Takes the JSON schema, not the ``Tool``: misaka's ``Tool.parameters`` may be a pydantic
    model class, and ``Tool.parameters_json_schema()`` is the accessor that flattens either
    form into the object shape pi's ``Tool["parameters"]`` already was.
    """
    cloned = deepcopy(schema)  # pi: structuredClone -- the rewrite must not touch the caller's schema
    if not _is_json_schema_object(cloned):
        raise UnsupportedStrictJsonSchemaError("root schema must have type object")
    _make_json_schema_node_strict(cloned)
    if cloned.get("type") != "object":
        raise UnsupportedStrictJsonSchemaError("root schema must have type object")
    return cloned


def get_json_schema_tool_parameters(tool: Tool, strict: bool | None) -> dict[str, Any]:
    """The parameter schema to send for ``tool``, rewritten only when strict mode is on."""
    parameters = tool.parameters_json_schema()
    return make_strict_json_schema(parameters) if strict is True else parameters


def tool_constrained_sampling(tool: Tool) -> ConstrainedSamplingConfig | None:
    """Read ``Tool.constrainedSampling``, upstream's per-tool sampling opt-in.

    ``False`` is pi's explicit opt-out and reads the same as an absent config. The value
    is still fetched defensively rather than by attribute: a caller may hand this module a
    tool-shaped object that is not the model.
    """
    config = getattr(tool, "constrainedSampling", None)
    if config is None or config is False:
        return None
    if isinstance(config, (JsonSchemaConstrainedSamplingConfig, GrammarConstrainedSamplingConfig)):
        return config
    # pi discriminates before it does anything else -- `!config || config.type !== "json_schema"`
    # / `!== "grammar"` (constrained-sampling.ts:210,235) -- so a config whose `type` is not one
    # of the two literals, or that is not an object at all, is simply "no constrained sampling"
    # and never an error. Validating first would raise on shapes pi silently ignores.
    kind = config.get("type") if isinstance(config, Mapping) else None
    if kind not in ("json_schema", "grammar"):
        return None
    return _CONSTRAINED_SAMPLING_ADAPTER.validate_python(config)


def resolve_json_schema_strict_sampling(tool: Tool, supports_strict_mode: bool) -> bool | None:
    """``True`` to send the tool with strict sampling, ``None`` to send it unconstrained.

    Raises when the tool demanded strict sampling (``strict: "require"``) and it cannot be
    had, either because the provider has no strict mode or because the schema is too rich.
    """
    config = tool_constrained_sampling(tool)
    if config is None or not isinstance(config, JsonSchemaConstrainedSamplingConfig):
        return None

    if supports_strict_mode:
        try:
            make_strict_json_schema(tool.parameters_json_schema())
        except UnsupportedStrictJsonSchemaError as error:
            # Only this error is a fallback signal; anything else is a bug and propagates.
            if config.strict != "require":
                return None
            raise ValueError(
                f'Tool "{tool.name}" requires JSON-schema constrained sampling, but {error}.'
            ) from error
        return True
    if config.strict == "require":
        raise ValueError(
            f'Tool "{tool.name}" requires JSON-schema constrained sampling, but strict tools are unsupported.'
        )
    return None


class GrammarConstrainedSampling(SchemaModel):
    """The resolved grammar for one tool: which syntax, the grammar text, and the property
    whose string value the generated text becomes."""

    format: Literal["lark", "regex"]
    definition: str
    inputProperty: str


_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


@dataclass
class GrammarToolInputJsonBuffer:
    """Cursor over one grammar tool call's growing text.

    A mutable dataclass rather than a pydantic model because pi mutates this object literal in
    place across deltas and it never crosses the wire.
    """

    input: str = ""
    started: bool = False
    closed: bool = False


def _json_string(value: str) -> str:
    """``JSON.stringify`` for a string.

    Two deviations from ``json.dumps``' defaults, both about what lands in the argument
    stream byte for byte (a client re-encoding the deltas would otherwise see a different,
    still valid, JSON document):

    * ``ensure_ascii=False``: ``JSON.stringify`` emits non-ASCII characters raw and
      ``json.dumps`` escapes them. (``separators`` is not passed: it only controls the text
      *between* container items, and this function only ever serialises a ``str``.)
    * surrogate code points are escaped back to ``\\udXXX`` (lowercase, as ECMAScript's
      well-formed ``JSON.stringify`` writes them). ``ensure_ascii=False`` would leave them
      raw, and a ``str`` holding a surrogate cannot be encoded to UTF-8 at all, so the delta
      would fail at the socket rather than in this function.

      This escapes both halves of a *pair* too, which ``JSON.stringify`` does not: a Python
      ``str`` can hold ``chr(0xD83D) + chr(0xDE00)`` as two code points distinct from
      ``chr(0x1F600)``, while the same two units in a JS string already *are* that one
      character and are emitted raw. Both spellings decode to the same character, so only
      the bytes differ, and on that input only.
    """
    return _LONE_SURROGATE_RE.sub(
        lambda match: f"\\u{ord(match.group()):04x}", json.dumps(value, ensure_ascii=False)
    )


def get_grammar_tool_input(tool_name: str, arguments: Mapping[str, Any], input_property: str) -> str:
    """Pull the grammar text back out of a completed tool call's arguments."""
    value = arguments.get(input_property)
    if not isinstance(value, str):
        # Not a TypeError: a model that emitted the wrong argument shape is a request-level
        # failure the adapter reports, not a caller type bug -- pi raises a plain Error too.
        raise ValueError(  # noqa: TRY004
            f'Grammar tool call "{tool_name}" requires argument "{input_property}" to be a string.'
        )
    return value


def append_grammar_tool_input_json_delta(
    buffer: GrammarToolInputJsonBuffer,
    input_property: str,
    next_input: str,
    close: bool,
) -> str | None:
    """Encode the growth of a grammar tool's raw text as a tool-call argument JSON delta.

    Providers stream the grammar output as plain text; consumers expect JSON arguments. Each
    call takes the full text so far and returns the fragment to append to the argument stream
    (``None`` when nothing changed), so that the concatenation of every returned fragment is
    exactly ``{"<input_property>":"<text>"}``.
    """
    if buffer.closed:
        if close and next_input == buffer.input:
            return None
        raise ValueError(f'grammar tool input for property "{input_property}" changed after it was closed')
    if not next_input.startswith(buffer.input):
        raise ValueError(f'grammar tool input for property "{input_property}" changed non-monotonically')

    input_delta = next_input[len(buffer.input) :]
    if not close and len(input_delta) == 0:
        return None

    delta = ""
    if not buffer.started:
        delta += "{" + _json_string(input_property) + ':"'
        buffer.started = True
    # Slice off the quotes JSON.stringify added: only the escaped body goes into the stream.
    delta += _json_string(input_delta)[1:-1]
    buffer.input = next_input

    if close:
        delta += '"}'
        buffer.closed = True
    return delta


def _infer_grammar_input_property(tool: Tool) -> str:
    """The single required string property a grammar tool's output is written into."""
    schema = tool.parameters_json_schema()
    if schema.get("type") != "object":
        raise ValueError("grammar constrained sampling requires an object parameter schema")
    required = schema.get("required")
    if not isinstance(required, list) or len(required) != 1 or not isinstance(required[0], str):
        raise ValueError("grammar constrained sampling requires exactly one required string property")

    input_property = required[0]
    properties = schema.get("properties")
    entry = properties.get(input_property) if _is_json_schema_object(properties) else None
    if not _is_js_truthy(entry):
        raise ValueError(f"grammar constrained sampling requires a properties entry for {input_property}")
    if not _is_json_schema_object(entry) or entry.get("type") != "string":
        raise ValueError(f"grammar constrained sampling property {input_property} must have type string")
    return input_property


def _grammar_variant(config: GrammarConstrainedSamplingConfig, fmt: GrammarFormat) -> str | None:
    """One variant's definition, or ``None`` when it is unusable.

    pi's ``typeof definition === "string" && definition.trim().length > 0``
    (constrained-sampling.ts:245-246). The type test is not redundant: ``variants`` carries
    whatever the caller put there, and a non-string ``openai_lark`` makes pi fall back to the
    regex variant rather than fail.
    """
    variants = config.variants if isinstance(config.variants, dict) else {}
    definition = variants.get(fmt)
    if isinstance(definition, str) and len(definition.strip()) > 0:
        return definition
    return None


def resolve_grammar_constrained_sampling(
    tool: Tool,
    supports_openai_grammar_tools: bool,
) -> GrammarConstrainedSampling | None:
    """The grammar to send for ``tool``, or ``None`` when it is an ordinary tool.

    A provider without grammar tools falls back silently (the tool is sent as a normal JSON
    one); a tool that asked for a grammar and cannot have a usable one fails loudly.
    """
    config = tool_constrained_sampling(tool)
    if config is None or not isinstance(config, GrammarConstrainedSamplingConfig):
        return None

    if not supports_openai_grammar_tools:
        return None

    lark_definition = _grammar_variant(config, "openai_lark")
    regex_definition = _grammar_variant(config, "openai_regex")
    if lark_definition is None and regex_definition is None:
        raise ValueError(
            f'Tool "{tool.name}" cannot use grammar constrained sampling: '
            f"no supported grammar variant was provided."
        )

    # pi wraps the whole result construction; the property inference is the only part that can
    # throw, so the narrower `try` catches the same errors without a blind except.
    try:
        input_property = _infer_grammar_input_property(tool)
    except ValueError as error:
        raise ValueError(f'Tool "{tool.name}" cannot use grammar constrained sampling: {error}.') from error

    return GrammarConstrainedSampling(
        format="lark" if lark_definition is not None else "regex",
        definition=lark_definition if lark_definition is not None else regex_definition,
        inputProperty=input_property,
    )


def create_grammar_tool_input_properties(
    tools: Sequence[Tool] | None,
    supports_openai_grammar_tools: bool,
) -> dict[str, str]:
    """Tool name -> the property its grammar output is encoded into, for the tools that have one.

    pi returns a ``ReadonlyMap``; a plain ``dict`` is returned here, since Python has no
    read-only mapping type worth the wrapper.
    """
    properties: dict[str, str] = {}
    for tool in tools or []:
        grammar = resolve_grammar_constrained_sampling(tool, supports_openai_grammar_tools)
        if grammar:
            properties[tool.name] = grammar.inputProperty
    return properties
