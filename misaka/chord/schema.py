"""JSON Schema helpers: canonical JSON, hashing, validation, normalization and structural subtyping.

Translated from pi's ``packages/chord/src/schema.ts``. ``Value`` is ``misaka.chord._typebox`` (TypeBox's checks
over JSON-Schema dicts). ``JSON.stringify`` is ``misaka.utils.js.jsonStringify`` (JavaScript's number and string
text), ``new RegExp(pattern, "u")`` is ``UnicodeRegExp`` (a JavaScript regular expression), and ``UNDEFINED`` stands
for ``undefined``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from misaka.chord._typebox import TSchema, UnicodeRegExp, Value
from misaka.chord.errors import ChordErrorReason, invalid
from misaka.utils.js import UNDEFINED, jsonStringify, toUtf16Units

# JSON Schema as data. TypeBox schemas are plain JSON Schema objects.
type Schema = Mapping[str, Any]


def asSchema(schema: Any) -> Schema:
    if isinstance(schema, Mapping):
        return schema
    return {"not": {}} if schema is False else {}


def canonicalJson(value: Any) -> str:
    """Canonical JSON: sorted object keys, ``undefined`` members dropped."""
    if isinstance(value, list):
        return f"[{','.join(canonicalJson(item) for item in value)}]"
    if isinstance(value, Mapping):
        # JavaScript sorts keys by UTF-16 code units.
        keys = sorted((key for key in value if value[key] is not UNDEFINED), key=toUtf16Units)
        return "{" + ",".join(f"{jsonStringify(key)}:{canonicalJson(value[key])}" for key in keys) + "}"
    if value is UNDEFINED or callable(value):
        return "null"
    text = jsonStringify(value)
    return "null" if text is UNDEFINED or text is None else text


def _imul(a: int, b: int) -> int:
    """``Math.imul``: the low 32 bits of the product, as a signed integer."""
    result = (a * b) & 0xFFFFFFFF
    return result - 0x100000000 if result & 0x80000000 else result


def _signed(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def hash64(text: str) -> str:
    """Non-cryptographic 64-bit hash (two FNV-1a lanes). Contracts are trusted, so collisions are not an attack
    surface."""
    a = _signed(0x811C9DC5)
    b = _signed(0x01000193 ^ 0x5BD1E995)
    units = toUtf16Units(text)
    for index in range(len(units)):
        code = ord(units[index])
        a = _imul(_signed(a ^ code), 0x01000193)
        b = _signed(_imul(_signed(b ^ code), 0x5BD1E995) ^ ((b & 0xFFFFFFFF) >> 13))
    return f"{a & 0xFFFFFFFF:08x}{b & 0xFFFFFFFF:08x}"


def deepEqual(a: Any, b: Any) -> bool:
    return a is b or canonicalJson(a) == canonicalJson(b)


def assertValid(reason: ChordErrorReason, what: str, schema: TSchema, value: Any) -> None:
    """Throws ``invalid`` (with ``reason``) and the first schema errors when ``value`` does not match."""
    if Value.Check(schema, value):
        return
    errors = []
    for error in Value.Errors(schema, value):
        errors.append(error)
        if len(errors) == 3:
            break
    summary = "; ".join(f"{error['instancePath'] or '/'} {error['message']}" for error in errors)
    raise invalid(reason, f"{what}: {summary}", errors)


def normalize(schema: TSchema, value: Any, what: str) -> Any:
    """Applies defaults, strips unknown fields, validates. Returns a fresh value."""
    start = {} if value is None or value is UNDEFINED else value
    normalized = Value.Clean(schema, Value.Default(schema, Value.Clone(start)))
    assertValid("input", what, schema, normalized)
    return normalized


def canonicalIdentity(schema: TSchema, value: Any) -> str:
    """Canonical identity of a normalized value: fields equal to their schema default are omitted."""
    return canonicalJson(stripDefaults(asSchema(schema), value))


def stripDefaults(schema: Schema, value: Any) -> Any:
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, list):
            return [stripDefaults(asSchema(items[index] if index < len(items) else None), item) for index, item in enumerate(value)]
        return [stripDefaults(asSchema(items), item) for item in value]
    if not isinstance(value, Mapping):
        return value
    properties = asSchema(schema.get("properties"))
    result: dict[str, Any] = {}
    for key, item in value.items():
        property = asSchema(properties.get(key))
        if "default" in property and deepEqual(property["default"], item):
            continue
        result[key] = stripDefaults(property, item)
    return result


CONSTRAINTS: Final = (
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
)


def typesOf(schema: Schema) -> list[str] | None:
    kind = schema.get("type")
    if isinstance(kind, str):
        return [kind]
    if isinstance(kind, list):
        return list(kind)
    return None


# Keywords `fits` reasons about. Any other validation keyword in the provider's schema forces exact equality.
KNOWN: Final[frozenset[str]] = frozenset(
    {
        *CONSTRAINTS,
        "type",
        "properties",
        "required",
        "additionalProperties",
        "patternProperties",
        "items",
        "additionalItems",
        "const",
        "enum",
        "anyOf",
        "parameters",
        "returnType",
    }
)

# Annotations: no effect on validity.
ANNOTATIONS: Final[frozenset[str]] = frozenset(
    {
        "title",
        "description",
        "default",
        "examples",
        "$id",
        "$schema",
        "$comment",
        "readOnly",
        "writeOnly",
        "deprecated",
    }
)


def withoutAnnotations(schema: Schema) -> dict[str, Any]:
    return {key: value for key, value in schema.items() if key not in ANNOTATIONS}


def fits(subSchema: Any, supSchema: Any) -> bool:
    """Conservative structural subtyping: true only if every value valid under ``sub`` is valid under ``sup``, where
    object fields missing from ``sub`` but optional in ``sup`` count as fine (unknown fields are stripped at the
    provider, see ``ObjectImpl.call``). A provider schema using a keyword this function does not model (``not``,
    ``allOf``, ``contains``, ``$ref``, ...) or an ``additionalProperties`` schema only fits an identical consumer
    schema."""
    sub = withoutAnnotations(asSchema(subSchema))
    sup = withoutAnnotations(asSchema(supSchema))
    if len(sup) == 0:
        return True
    if deepEqual(sub, sup):
        return True
    if any(key not in KNOWN for key in sup):
        return False
    if "additionalProperties" in sup and not isinstance(sup["additionalProperties"], bool):
        return False
    if isinstance(sub.get("anyOf"), list):
        return all(fits(branch, sup) for branch in sub["anyOf"])
    if isinstance(sup.get("anyOf"), list):
        return any(fits(sub, branch) for branch in sup["anyOf"])
    if "const" in sup:
        return "const" in sub and deepEqual(sub["const"], sup["const"])
    if isinstance(sup.get("enum"), list):
        values = [sub["const"]] if "const" in sub else sub["enum"] if isinstance(sub.get("enum"), list) else None
        if values is None:
            return False
        return all(any(deepEqual(allowed, item) for allowed in sup["enum"]) for item in values)
    for key in CONSTRAINTS:
        if key in sup and not deepEqual(sub.get(key, UNDEFINED), sup[key]):
            return False
    supTypes = typesOf(sup)
    subTypes = typesOf(sub)
    if supTypes is None or subTypes is None:
        return False
    if not all(kind in supTypes or (kind == "integer" and "number" in supTypes) for kind in subTypes):
        return False
    if "const" in sub or isinstance(sub.get("enum"), list):
        return True
    if "object" in subTypes and not objectFits(sub, sup):
        return False
    if "array" in subTypes and not arrayFits(sub, sup):
        return False
    if "function" in subTypes:
        return functionFits(sub, sup)
    return True


def objectFits(sub: Schema, sup: Schema) -> bool:
    subProperties = asSchema(sub.get("properties"))
    supProperties = asSchema(sup.get("properties"))
    subRequired = set(sub.get("required") or [])
    for key in sup.get("required") or []:
        if key not in subRequired:
            return False
    for key, property in supProperties.items():
        if key in subProperties:
            if not fits(subProperties[key], property):
                return False
        elif isinstance(sub.get("additionalProperties"), Mapping) and not fits(sub["additionalProperties"], property):
            # The consumer may send this name with any value its `additionalProperties` allows. Open consumer objects
            # are fine: unknown names are stripped with the consumer's own schema before the call (`ObjectImpl.call`).
            return False
    if sup.get("additionalProperties") is False:
        if sub.get("additionalProperties") is not False:
            return False
        if any(key not in supProperties for key in subProperties):
            return False
    supPatterns = asSchema(sup.get("patternProperties"))
    if len(supPatterns) > 0:
        # Every value the consumer may put under a matching name must fit, and the consumer must not allow arbitrary
        # extra names (it would then allow any value under a matching one).
        subPatterns = asSchema(sub.get("patternProperties"))
        if sub.get("additionalProperties") is not False and len(subPatterns) == 0:
            return False
        for pattern, property in supPatterns.items():
            matcher = UnicodeRegExp(pattern)
            for key, subProperty in subProperties.items():
                if key not in supProperties and matcher.test(key) and not fits(subProperty, property):
                    return False
            if len(subPatterns) > 0 and (pattern not in subPatterns or not fits(subPatterns[pattern], property)):
                return False
        for pattern in subPatterns:
            if pattern not in supPatterns:
                return False
    return True


def arrayFits(sub: Schema, sup: Schema) -> bool:
    supItems = sup.get("items")
    subItems = sub.get("items")
    if isinstance(supItems, list):
        if not isinstance(subItems, list) or len(subItems) != len(supItems):
            return False
        return all(fits(item, supItems[index]) for index, item in enumerate(subItems))
    if isinstance(subItems, list):
        return all(fits(item, supItems) for item in subItems)
    return fits(subItems, supItems)


def functionFits(sub: Schema, sup: Schema) -> bool:
    subParameters = sub.get("parameters") or []
    supParameters = sup.get("parameters") or []
    # A function accepting fewer parameters is fine; each shared parameter is contravariant.
    if len(subParameters) > len(supParameters):
        return False
    if not all(fits(supParameters[index], parameter) for index, parameter in enumerate(subParameters)):
        return False
    return fits(sub.get("returnType"), sup.get("returnType"))


__all__ = ["assertValid", "canonicalIdentity", "canonicalJson", "deepEqual", "fits", "hash64", "normalize"]
