"""Translated from pi's ``packages/codemode/src/declarations.ts``."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypedDict, TypeGuard
from urllib.parse import unquote

from misaka.utils.js import (
    UNDEFINED,
    jsLength,
    jsonStringify,
    jsString,
    jsTrim,
    nullishCoalesce,
)
from misaka.utils.values import read_field

from .identifier import toCodemodeIdentifier
from .types import CodemodeJsonSchema

# `\Z`, not `$`: Python's `$` also matches before a final newline.
IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*\Z")
INDENT = "  "
# Largest rendered input type, in characters, before it becomes `unknown`.
DEFAULT_INPUT_SCHEMA_MAX_CHARS = 16_000
# Local `$ref` expansions per rendered schema, so shared definitions cannot blow up the output.
MAX_REF_EXPANSIONS = 32

# JSON Schema keywords with a number or string value shown as `@keyword value` property tags.
TAG_KEYWORDS = (
    "minimum",
    "exclusiveMinimum",
    "maximum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
)
# Tag keywords whose values all hold at once, so only the largest (lower) or smallest (upper) matters.
LOWER_BOUNDS: frozenset[str] = frozenset({"minimum", "exclusiveMinimum", "minLength", "minItems"})
UPPER_BOUNDS: frozenset[str] = frozenset({"maximum", "exclusiveMaximum", "maxLength", "maxItems"})
# Longest property tag; longer ones (large defaults, long patterns) are left out.
MAX_TAG_CHARS = 80
# Characters that end a `//` comment.
LINE_TERMINATORS = re.compile("[\r\n\u2028\u2029]")


class RenderDeclarationsOptions(TypedDict, total=False):
    # `CodemodeTool`s, or anything carrying their fields.
    tools: Sequence[Any]
    globals: Sequence[Any]


def renderDeclarations(options: RenderDeclarationsOptions) -> str:
    """Render TypeScript declarations for the script-visible API. Tools become members of
    ``declare const tools``, globals become ``declare function`` statements, and ``ns.member`` globals
    members of ``declare const ns``. Descriptions become doc comments; schemas become types."""
    sections: list[str] = []
    tools = options.get("tools") or []
    if len(tools) > 0:
        members = [
            f"{docComment(read_field(tool, 'description'), INDENT)}{INDENT}{renderToolSignature(tool)}" for tool in tools
        ]
        sections.append("declare const tools: {\n" + "\n".join(members) + "\n};")
    namespaces: dict[str, list[str]] = {}
    for global_ in options.get("globals") or []:
        name: str = read_field(global_, "name")
        dot = name.find(".")
        if dot == -1:
            sections.append(renderGlobal(f"declare function {name}", global_, ""))
            continue
        namespace = name[:dot]
        members = namespaces.setdefault(namespace, [])
        members.append(renderGlobal(name[dot + 1 :], global_, INDENT))
    for namespace, members in namespaces.items():
        sections.append(f"declare const {namespace}: {{\n" + "\n".join(members) + "\n};")
    return "\n\n".join(sections)


def renderToolSignature(tool: Any, options: Mapping[str, int] | None = None) -> str:
    """One tool as a member of the ``tools`` object: ``name(args: T): Promise<R>;`` with the name as
    the identifier scripts use. Input types longer than ``inputMaxChars`` render as ``unknown``."""
    inputSchema = read_field(tool, "inputSchema")
    input = (
        "unknown"
        if inputSchema is None
        else schemaToType(
            inputSchema,
            {"maxChars": nullishCoalesce((options or {}).get("inputMaxChars"), DEFAULT_INPUT_SCHEMA_MAX_CHARS)},
        )
    )
    output = renderToolOutputType(read_field(tool, "outputSchema"))
    return f"{toCodemodeIdentifier(read_field(tool, 'name'))}(args: {input}): Promise<{output}>;"


def renderToolSample(tool: Any, options: Mapping[str, int] | None = None) -> str:
    """A tool's sample: the description followed by the tool's declaration. Used for tool listings
    and ``ALL_TOOLS`` entries."""
    declaration = f"declare const tools: {{ {renderToolSignature(tool, options)} }};"
    description = read_field(tool, "description")
    return f"{jsTrim(description) if description is not None else ''}\n\ncodemode tool declaration:\n```ts\n{declaration}\n```"


def renderToolOutputType(schema: CodemodeJsonSchema | None) -> str:
    """The type a tool call resolves to: the output schema's type, and ``unknown`` without a schema."""
    return "unknown" if schema is None else schemaToType(schema)


def renderGlobal(head: str, global_: Any, indent: str) -> str:
    signature = read_field(global_, "signature")
    description = read_field(global_, "description")
    if signature is not None:
        return f"{docComment(description, indent)}{indent}{head}{signature};"
    inputSchema, outputSchema = read_field(global_, "inputSchema"), read_field(global_, "outputSchema")
    input = "unknown" if inputSchema is None else schemaToType(inputSchema)
    output = "unknown" if outputSchema is None else schemaToType(outputSchema)
    return f"{docComment(description, indent)}{indent}{head}(args: {input}): Promise<{output}>;"


def docComment(description: str | None, indent: str) -> str:
    text = jsTrim(description) if description is not None else ""
    if not text:
        return ""
    lines = re.split(r"\r?\n", text.replace("*/", "*\\/"))
    if len(lines) == 1:
        return f"{indent}/** {lines[0]} */\n"
    body = "\n".join(f"{indent} *{f' {line}' if line else ''}" for line in lines)
    return f"{indent}/**\n{body}\n{indent} */\n"


def propertyKey(name: str) -> str:
    return name if IDENTIFIER.match(name) else jsonStringify(name)


def isObject(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def union(types: Sequence[str]) -> str:
    unique = list(dict.fromkeys(types))
    if "unknown" in unique:
        return "unknown"
    return "never" if len(unique) == 0 else " | ".join(unique)


def schemaToType(schema: CodemodeJsonSchema, options: Mapping[str, int] | None = None) -> str:
    """Convert a JSON Schema to a TypeScript type expression: objects on one line
    (``{ a: string; b?: number; }``) with properties sorted by name, or one property per line with
    ``//`` comments when a property has a description or constraints the type cannot express
    (``// @integer @minimum 1 @default 20``); ``Array<T>`` for arrays. Local references
    (``#/$defs/...``, ``#/definitions/...``) resolve against ``schema``; recursive and remote
    references render as ``unknown``. A result longer than ``maxChars`` is rendered again without the
    constraint tags, and renders as ``unknown`` if it is still too long."""

    def render(tags: bool) -> str:
        return toType(schema, SchemaContext(root=schema, tags=tags))

    type = render(True)
    maxChars = (options or {}).get("maxChars")
    if maxChars is None or jsLength(type) <= maxChars:
        return type
    untagged = render(False)
    return untagged if jsLength(untagged) <= maxChars else "unknown"


@dataclass(slots=True, kw_only=True)
class SchemaContext:
    root: CodemodeJsonSchema
    # References being expanded on the current path, to stop at recursive types.
    resolving: set[str] = field(default_factory=set)
    expansions: int = 0
    # Whether property comments include constraint tags.
    tags: bool


_MALFORMED_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _decodeURIComponent(text: str) -> str:
    """``decodeURIComponent``: a malformed escape or invalid UTF-8 raises, where ``unquote`` leaves the
    escape as it is or substitutes U+FFFD."""
    if _MALFORMED_ESCAPE.search(text):
        raise ValueError("URI malformed")
    try:
        return unquote(text, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("URI malformed") from None


def resolveRef(ref: str, root: CodemodeJsonSchema) -> CodemodeJsonSchema | None:
    if ref != "#" and not ref.startswith("#/"):
        return None
    current: Any = root
    for segment in filter(None, ref[2:].split("/")):
        key = _decodeURIComponent(segment).replace("~1", "/").replace("~0", "~")
        if not isObject(current) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, bool) or isObject(current) else None


def toType(schema: Any, context: SchemaContext) -> str:
    if schema is True:
        return "unknown"
    if schema is False:
        return "never"
    if not isObject(schema):
        return "unknown"
    if isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref in context.resolving or context.expansions >= MAX_REF_EXPANSIONS:
            return "unknown"
        target = resolveRef(ref, context.root)
        if target is None:
            return "unknown"
        context.expansions += 1
        context.resolving.add(ref)
        try:
            return toType(target, context)
        finally:
            context.resolving.discard(ref)

    if "const" in schema:
        return _stringifyOrUnknown(schema["const"])
    if isinstance(schema.get("enum"), list):
        return union([_stringifyOrUnknown(value) for value in schema["enum"]])

    anyOf, oneOf = schema.get("anyOf"), schema.get("oneOf")
    variants = anyOf if isinstance(anyOf, list) else oneOf if isinstance(oneOf, list) else None
    if variants is not None:
        return union([toType(variant, context) for variant in variants])
    if isinstance(schema.get("allOf"), list):
        parts = [part for part in (toType(part, context) for part in schema["allOf"]) if part != "unknown"]
        return "unknown" if len(parts) == 0 else " & ".join(f"({part})" if " | " in part else part for part in parts)

    type = schema.get("type", UNDEFINED)
    if isinstance(type, list):
        return union([toType({**schema, "type": entry}, context) for entry in type])
    match type:
        case "string":
            return "string"
        case "number" | "integer":
            return "number"
        case "boolean":
            return "boolean"
        case "null":
            return "null"
        case "array":
            return arrayType(schema, context)
        case "object":
            return objectType(schema, context)
        case _ if type is UNDEFINED:
            if "properties" in schema or "additionalProperties" in schema or "required" in schema:
                return objectType(schema, context)
            if "items" in schema or "prefixItems" in schema:
                return arrayType(schema, context)
            return "unknown"
        case _:
            return "unknown"


def _stringifyOrUnknown(value: Any) -> str:
    """``JSON.stringify(value) ?? "unknown"``: undefined and functions have no JSON; a value that cannot be
    serialized (circular) throws."""
    if value is UNDEFINED or callable(value):
        return "unknown"
    return jsonStringify(value)


def arrayType(schema: dict[str, Any], context: SchemaContext) -> str:
    items = schema.get("items", UNDEFINED)
    if items is not UNDEFINED and not isinstance(items, list):
        return f"Array<{toType(items, context)}>"
    prefixItems = schema.get("prefixItems")
    tuple_ = prefixItems if isinstance(prefixItems, list) else items if isinstance(items, list) else []
    if len(tuple_) > 0:
        return "[" + ", ".join(toType(item, context) for item in tuple_) + "]"
    return "unknown[]"


def descriptionOf(property: Any) -> str:
    return jsTrim(property["description"]) if isObject(property) and isinstance(property.get("description"), str) else ""


def _utf16Order(name: str) -> bytes:
    """``Array.prototype.sort()`` compares UTF-16 code units."""
    return name.encode("utf-16-be", "surrogatepass")


def _isNumber(value: Any) -> bool:
    """``typeof value === "number"``."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _escapeLineTerminator(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group(0)):04x}"


def constraintTags(schema: Any) -> str:
    """Constraints of a property that its TypeScript type cannot express, as JSDoc-style tags:
    ``@integer @minimum 1 @maximum 100 @default 20``. Empty when there are none."""
    if not isObject(schema):
        return ""
    property = schema
    # Optional fields such as Pydantic's `int | None` keep their constraints in the non-null branch.
    # The branch's constraints apply together with the property's, so both are read.
    variants = property.get("anyOf") if isinstance(property.get("anyOf"), list) else property.get("oneOf")
    nonNull = (
        [variant for variant in variants if not (isObject(variant) and variant.get("type", UNDEFINED) == "null")]
        if isinstance(variants, list)
        else []
    )
    branch = nonNull[0] if len(nonNull) == 1 else UNDEFINED
    branches = [property, branch] if isObject(branch) else [property]
    # Draft-04 and OpenAPI 3.0 make `minimum`/`maximum` exclusive with a boolean `exclusiveMinimum`/`exclusiveMaximum`.
    schemas: list[dict[str, Any]] = []
    for entry in branches:
        adjusted = dict(entry)
        if entry.get("exclusiveMinimum") is True:
            adjusted["minimum"] = UNDEFINED
            adjusted["exclusiveMinimum"] = entry.get("minimum", UNDEFINED)
        if entry.get("exclusiveMaximum") is True:
            adjusted["maximum"] = UNDEFINED
            adjusted["exclusiveMaximum"] = entry.get("maximum", UNDEFINED)
        schemas.append(adjusted)

    tags: list[str] = []

    def isInteger(type: Any) -> bool:
        return type == "integer" or (isinstance(type, list) and "integer" in type)

    if any(isInteger(entry.get("type")) for entry in schemas):
        tags.append("@integer")
    for keyword in TAG_KEYWORDS:
        values = [entry.get(keyword, UNDEFINED) for entry in schemas]
        if keyword in LOWER_BOUNDS or keyword in UPPER_BOUNDS:
            numbers = [value for value in values if _isNumber(value)]
            if len(numbers) == 0:
                continue
            tags.append(f"@{keyword} {jsString(max(numbers) if keyword in LOWER_BOUNDS else min(numbers))}")
            continue
        # `new Set(values)`: each distinct value once, in order; a number and a string never match.
        seen: set[tuple[str, Any]] = set()
        for value in values:
            if not (_isNumber(value) or isinstance(value, str)):
                continue
            key = ("number" if _isNumber(value) else "string", value)
            if key in seen:
                continue
            seen.add(key)
            tags.append(f"@{keyword} {jsString(value)}")
    if any(entry.get("uniqueItems") is True for entry in schemas):
        tags.append("@uniqueItems")
    # Annotations, unlike constraints, come from the property itself.
    default = property.get("default", UNDEFINED)
    if default is not UNDEFINED and default is not None:
        tags.append(f"@default {jsonStringify(default)}")
    if property.get("deprecated") is True:
        tags.append("@deprecated")
    return " ".join(
        LINE_TERMINATORS.sub(_escapeLineTerminator, tag) for tag in tags if jsLength(tag) <= MAX_TAG_CHARS
    )


def objectType(schema: dict[str, Any], context: SchemaContext) -> str:
    properties: dict[str, Any] = schema["properties"] if isObject(schema.get("properties")) else {}
    required_ = schema.get("required")
    required = {entry for entry in required_ if isinstance(entry, str)} if isinstance(required_, list) else set()
    names = sorted(properties, key=_utf16Order)
    members = [f"{propertyKey(name)}{'' if name in required else '?'}: {toType(properties[name], context)};" for name in names]
    additional = schema.get("additionalProperties", UNDEFINED)
    if additional is not UNDEFINED and additional is not False:
        type = "unknown" if additional is True else toType(additional, context)
        members.append(f"[key: string]: {type};")
    elif additional is UNDEFINED and len(names) == 0:
        members.append("[key: string]: unknown;")
    if len(members) == 0:
        return "{}"
    comments: list[list[str]] = []
    for name in names:
        lines_ = [jsTrim(line) for line in LINE_TERMINATORS.split(descriptionOf(properties[name]))]
        lines_ = [line for line in lines_ if line]
        tags = constraintTags(properties[name]) if context.tags else ""
        comments.append([*lines_, tags] if tags else lines_)
    if not any(len(comment) > 0 for comment in comments):
        return "{ " + " ".join(members) + " }"

    lines = ["{"]
    for index, comment in enumerate(comments):
        for line in comment:
            lines.append(f"{INDENT}// {line}")
        lines.append(f"{INDENT}{members[index].replace(chr(10), chr(10) + INDENT)}")
    for member in members[len(names) :]:
        lines.append(f"{INDENT}{member}")
    lines.append("}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_INPUT_SCHEMA_MAX_CHARS",
    "RenderDeclarationsOptions",
    "renderDeclarations",
    "renderToolOutputType",
    "renderToolSample",
    "renderToolSignature",
    "schemaToType",
    "toCodemodeIdentifier",
]
