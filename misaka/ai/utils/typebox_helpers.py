"""Translated from pi's ``packages/ai/src/utils/typebox-helpers.ts`` (v1.1.0).

MISAKA fork: ``typeboxSchema`` -- tools declare their parameters as pydantic models where pi declares TypeBox
objects, and this gives the schema the TypeBox declaration would emit, so a model sees what pi's model sees.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

from misaka.ai.utils._typebox import TSchema, Type


class _StringEnumOptions(TypedDict, total=False):
    description: str
    default: str


def StringEnum(values: Sequence[str], options: _StringEnumOptions | None = None) -> TSchema:
    """Creates a string enum schema compatible with Google's API and other providers
    that don't support anyOf/const patterns.

    Example::

        OperationSchema = StringEnum(["add", "subtract", "multiply", "divide"], {
            "description": "The operation to perform",
        })
    """
    schema: dict[str, Any] = {"type": "string", "enum": list(values)}
    if options is not None and options.get("description"):
        schema["description"] = options["description"]
    if options is not None and options.get("default"):
        schema["default"] = options["default"]
    return Type.Unsafe(schema)


# Keys whose values are data, not schemas: nothing inside them is rewritten.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})
_NULL_SCHEMA = {"type": "null"}


def typeboxSchema(model: Any) -> dict[str, Any]:
    """MISAKA fork: the JSON schema TypeBox emits for the declaration a pydantic model class stands for.

    Pydantic adds what TypeBox leaves out: a ``title`` on every schema, ``$defs`` with ``$ref``s for nested models,
    and an optional field as ``anyOf: [X, null]`` with ``default: null`` where ``Type.Optional(X)`` is just ``X``
    left out of ``required``. A field's own keys (its description, a ``type`` its ``json_schema_extra`` sets) win over
    the type it wraps.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    return _typeboxNode(schema, defs)


def _typeboxNode(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_typeboxNode(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        rest = {key: value for key, value in node.items() if key != "$ref"}
        return _typeboxNode({**defs[ref.removeprefix("#/$defs/")], **rest}, defs)
    anyOf = node.get("anyOf")
    if "default" in node and isinstance(anyOf, list) and anyOf.count(_NULL_SCHEMA) == 1:
        variants = [variant for variant in anyOf if variant != _NULL_SCHEMA]
        inner = variants[0] if len(variants) == 1 else {"anyOf": variants}
        outer = {key: value for key, value in node.items() if key != "anyOf" and not (key == "default" and value is None)}
        return _typeboxNode({**inner, **outer}, defs)
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" or (key == "default" and value is None):
            continue
        if key in _DATA_KEYWORDS:
            result[key] = value
        elif key in ("properties", "patternProperties") and isinstance(value, dict):
            result[key] = {name: _typeboxNode(child, defs) for name, child in value.items()}
        else:
            result[key] = _typeboxNode(value, defs)
    # TypeBox's key order: the type, then an object's `required` and `properties` or an array's `items`, then the
    # options the declaration passed.
    leading = [key for key in ("type", "required", "properties", "items") if key in result]
    return {**{key: result[key] for key in leading}, **result}


__all__ = ["StringEnum", "typeboxSchema"]
