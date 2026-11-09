"""Document and document-family definitions, addressing and definition checks.

Translated from pi's ``packages/durable/src/documents.ts``. A definition is a ``dict`` (``kind``, ``version``, ``scope``,
and for conversation documents ``history`` and ``fork``; ``initial``, optional ``migrate`` and ``checkpointWhen``;
``family: True`` for a family) and a token is ``{"definition": definition}``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from misaka.chord.json import copyJson
from misaka.utils.js import jsIsInteger, jsonStringify

from .ids import idFromNumber
from .types import DocumentAddress, JsonObject


def defineDoc(definition: AnyDocDefinition) -> AnyDocToken:
    """Define a singleton document: Session-scoped, a latest-only or rewindable conversation document, or a task
    document, by ``definition["scope"]``, ``["history"]`` and ``["fork"]``."""
    validateDefinition(definition)
    return {"definition": definition}


def defineDocFamily(definition: AnyDocDefinition) -> AnyDocToken:
    """Define a document family: the same scopes as ``defineDoc``, with ``family: True`` and ``initial(seed)``."""
    validateDefinition(definition)
    return {"definition": definition}

type AnyDocDefinition = dict[str, Any]
"""Erased definition shape used by the Session after overload resolution."""
type AnyDocToken = dict[str, Any]
"""Erased singleton or family token."""

_MAX_SAFE_INTEGER = 2**53 - 1


def _isSafeInteger(value: Any) -> bool:
    return jsIsInteger(value) and abs(value) <= _MAX_SAFE_INTEGER


def validateDefinition(definition: AnyDocDefinition) -> None:
    version = definition["version"]
    if not _isSafeInteger(version) or version < 1:
        raise TypeError(f"Document {definition['kind']} version must be a positive integer")


class ResolvedAddress(NamedTuple):
    """Logical address plus its string identity for maps."""

    address: DocumentAddress
    id: str
    nextArgument: int


def resolveAddress(definition: AnyDocDefinition, args: Any) -> ResolvedAddress:
    """Resolve an overloaded argument list and return the index after the owner and family key."""
    index = 0
    scope: dict[str, Any]
    kind = definition["scope"]
    if kind == "session":
        scope = {"kind": "session"}
    elif kind == "conversation":
        scope = {"kind": "conversation", "conversationId": ownerId(args[index], definition)}
        index += 1
    else:
        scope = {"kind": "task", "taskId": ownerId(args[index], definition)}
        index += 1
    key: Any = None
    if definition.get("family") is True:
        key = args[index]
        index += 1
    address: DocumentAddress = {"kind": definition["kind"], "scope": scope}
    if key is not None:
        address["key"] = key
    return ResolvedAddress(address, addressId(address), index)


def ownerId(value: Any, definition: AnyDocDefinition) -> int:
    if not _isSafeInteger(value):
        raise TypeError(f"Document {definition['kind']} requires a {definition['scope']} ID")
    return idFromNumber(value)


def addressId(address: Mapping[str, Any]) -> str:
    """Stable string identity of one logical address."""
    scope = address["scope"]
    owner = None if scope["kind"] == "session" else (scope["conversationId"] if scope["kind"] == "conversation" else scope["taskId"])
    return jsonStringify([address["kind"], scope["kind"], owner, address.get("key")])


def documentCreate(definition: AnyDocDefinition, address: DocumentAddress, id: int) -> dict[str, Any]:
    """Build the storage create record for a new incarnation at an address."""
    key = {} if "key" not in address else {"key": address["key"]}
    scope = address["scope"]
    if scope["kind"] == "conversation":
        return {
            "id": id,
            "kind": address["kind"],
            **key,
            "scope": scope,
            "history": definition["history"],
            "fork": definition["fork"],
        }
    return {"id": id, "kind": address["kind"], **key, "scope": scope}


def checkRecordScope(definition: AnyDocDefinition, record: dict[str, Any]) -> None:
    """Reject typed access whose token disagrees with the persisted scope, history, or fork semantics."""
    scope = record["scope"]
    if scope["kind"] != definition["scope"] or (
        scope["kind"] == "conversation"
        and (record.get("history") != definition.get("history") or record.get("fork") != definition.get("fork"))
    ):
        raise TypeError(f"Document {record['id']} ({record['kind']}) does not match the supplied definition semantics")


def checkRecordVersion(definition: AnyDocDefinition, record: dict[str, Any], version: int) -> None:
    """Reject typed access to a stored version the supplied definition cannot use."""
    if version > definition["version"]:
        raise RuntimeError(
            f"Document {record['id']} ({record['kind']}) has newer version {version} than {definition['version']}"
        )
    if version < definition["version"] and definition.get("migrate") is None:
        raise RuntimeError(f"Document {record['id']} ({record['kind']}) requires migration from version {version}")


def materializeDocument(definition: AnyDocDefinition, stored: dict[str, Any]) -> JsonObject:
    """Validate and materialize a detached stored value for typed access."""
    return materializeDocumentValue(definition, stored["record"], stored["version"], stored["value"])


def materializeDocumentValue(
    definition: AnyDocDefinition, record: dict[str, Any], version: int, value: JsonObject
) -> JsonObject:
    """Validate and materialize one detached value before its first persisted incarnation."""
    checkRecordScope(definition, record)
    checkRecordVersion(definition, record, version)
    if version == definition["version"]:
        return value
    return copyJson(definition["migrate"](value, version))  # type: ignore[return-value]


__all__ = [
    "AnyDocDefinition",
    "AnyDocToken",
    "ResolvedAddress",
    "addressId",
    "checkRecordScope",
    "checkRecordVersion",
    "defineDoc",
    "defineDocFamily",
    "documentCreate",
    "materializeDocument",
    "materializeDocumentValue",
    "resolveAddress",
]
