"""The SQLite identifiers of the durable storage, optionally under a table prefix.

Translated from pi's ``packages/durable/src/storage/sqlite/schema.ts`` (v1.3.0). A schema is ``{"tables": {...},
"indexes": {...}}``, each mapping the upstream key to its (prefixed) identifier.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TypedDict

# Immutable identifier history for migrations and runtime queries. Never change or remove an existing value; a rename
# adds a new entry so historical migrations keep rendering their original object names.
TABLE_NAMES: Mapping[str, str] = {
    "durableSchema": "durable_schema",
    "durableMetadata": "durable_metadata",
    "recordIds": "record_ids",
    "conversations": "conversations",
    "entries": "entries",
    "tasks": "tasks",
    "submissions": "submissions",
    "documents": "documents",
    "documentRevisions": "document_revisions",
}

INDEX_NAMES: Mapping[str, str] = {
    "conversationsByOwnerConversation": "conversations_by_owner_conversation",
    "conversationsByOwnerTask": "conversations_by_owner_task",
    "entriesByConversation": "entries_by_conversation",
    "entryHeadsByConversation": "entry_heads_by_conversation",
    "tasksByStatus": "tasks_by_status",
    "tasksByConversation": "tasks_by_conversation",
    "tasksByKind": "tasks_by_kind",
    "tasksByAbortRequested": "tasks_by_abort_requested",
    "tasksByBackground": "tasks_by_background",
    "submissionsByRequest": "submissions_by_request",
    "submissionsByConversation": "submissions_by_conversation",
    "submissionsByStatus": "submissions_by_status",
    "documentsByAddress": "documents_by_address",
    "documentsByScope": "documents_by_scope",
    "documentsByScopeKind": "documents_by_scope_kind",
    "documentRevisionsByKind": "document_revisions_by_kind",
}


class SqliteSchema(TypedDict):
    tables: Mapping[str, str]
    indexes: Mapping[str, str]


_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validateSqliteSchemaPrefix(prefix: str) -> None:
    """Validate a prefix before using it as part of a SQLite identifier."""
    if not isinstance(prefix, str) or _PREFIX.fullmatch(prefix) is None:
        raise RuntimeError(f"Invalid SQLite schema prefix {json.dumps(prefix, ensure_ascii=False)}")
    if prefix.lower().startswith("sqlite_"):
        raise RuntimeError(f"SQLite schema prefix {json.dumps(prefix, ensure_ascii=False)} is reserved")


def prefixIdentifiers(names: Mapping[str, str], prefix: str) -> Mapping[str, str]:
    return {key: f"{prefix}{name}" for key, name in names.items()}


def createSqliteSchema(prefix: str | None = None) -> SqliteSchema:
    """Build the identifiers used by every migration and storage query."""
    if prefix is not None:
        validateSqliteSchemaPrefix(prefix)
    actualPrefix = "" if prefix is None else prefix
    return {"tables": prefixIdentifiers(TABLE_NAMES, actualPrefix), "indexes": prefixIdentifiers(INDEX_NAMES, actualPrefix)}


DEFAULT_SQLITE_SCHEMA: SqliteSchema = createSqliteSchema()


__all__ = [
    "DEFAULT_SQLITE_SCHEMA",
    "INDEX_NAMES",
    "TABLE_NAMES",
    "SqliteSchema",
    "createSqliteSchema",
    "prefixIdentifiers",
    "validateSqliteSchemaPrefix",
]
