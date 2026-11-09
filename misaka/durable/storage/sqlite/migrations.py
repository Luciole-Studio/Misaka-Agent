"""The ordered schema history of the durable SQLite storage.

Translated from pi's ``packages/durable/src/storage/sqlite/migrations.ts`` (v1.3.0). A migration is ``{"version": int,
"statements": [sql, ...]}``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

from .database import SqliteDatabase, SqliteExecutor
from .schema import DEFAULT_SQLITE_SCHEMA, SqliteSchema


class SqliteMigration(TypedDict):
    version: int
    statements: Sequence[str]


def createSqliteMigrations(schema: SqliteSchema) -> Sequence[SqliteMigration]:
    """Build immutable, ordered schema history for a set of SQLite identifiers."""
    tables, indexes = schema["tables"], schema["indexes"]
    # next_id is TEXT because node:sqlite rejects INTEGER results outside JavaScript's safe integer range. The column
    # stays TEXT here so the file keeps the upstream layout.
    initialSchema: Sequence[str] = [
        f"""CREATE TABLE {tables['durableMetadata']} (
			singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
			next_id TEXT NOT NULL,
			next_seq INTEGER NOT NULL
		) STRICT""",
        f"INSERT INTO {tables['durableMetadata']} (singleton, next_id, next_seq) VALUES (1, '2', 1)",
        f"""CREATE TABLE {tables['recordIds']} (
			id INTEGER PRIMARY KEY,
			record_type TEXT NOT NULL CHECK (record_type IN ('conversation', 'entry', 'task', 'submission', 'document'))
		) STRICT""",
        f"""CREATE TABLE {tables['conversations']} (
			id INTEGER PRIMARY KEY,
			owner_conversation_id INTEGER,
			owner_task_id INTEGER,
			record TEXT NOT NULL CHECK (json_valid(record))
		) STRICT""",
        f"CREATE INDEX {indexes['conversationsByOwnerConversation']} ON {tables['conversations']} (owner_conversation_id, id)",
        f"CREATE INDEX {indexes['conversationsByOwnerTask']} ON {tables['conversations']} (owner_task_id, id)",
        f"""CREATE TABLE {tables['entries']} (
			id INTEGER PRIMARY KEY,
			conversation_id INTEGER NOT NULL,
			head INTEGER,
			commit_seq INTEGER NOT NULL,
			record TEXT NOT NULL CHECK (json_valid(record))
		) STRICT""",
        f"CREATE INDEX {indexes['entriesByConversation']} ON {tables['entries']} (conversation_id, id DESC)",
        f"CREATE INDEX {indexes['entryHeadsByConversation']} ON {tables['entries']} (conversation_id, id DESC) WHERE head IS NOT NULL",
        f"""CREATE TABLE {tables['tasks']} (
			id INTEGER PRIMARY KEY,
			conversation_id INTEGER NOT NULL,
			kind TEXT NOT NULL,
			status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'waiting', 'completing', 'terminal')),
			abort_requested INTEGER NOT NULL CHECK (abort_requested IN (0, 1)),
			background INTEGER NOT NULL CHECK (background IN (0, 1)),
			record TEXT NOT NULL CHECK (json_valid(record))
		) STRICT""",
        f"CREATE INDEX {indexes['tasksByStatus']} ON {tables['tasks']} (status, id)",
        f"CREATE INDEX {indexes['tasksByConversation']} ON {tables['tasks']} (conversation_id, id)",
        f"CREATE INDEX {indexes['tasksByKind']} ON {tables['tasks']} (kind, id)",
        f"CREATE INDEX {indexes['tasksByAbortRequested']} ON {tables['tasks']} (abort_requested, id)",
        f"CREATE INDEX {indexes['tasksByBackground']} ON {tables['tasks']} (background, id)",
        f"""CREATE TABLE {tables['submissions']} (
			id INTEGER PRIMARY KEY,
			conversation_id INTEGER NOT NULL,
			request_id TEXT,
			status TEXT NOT NULL CHECK (status IN ('queued', 'placed', 'done', 'unanswered')),
			record TEXT NOT NULL CHECK (json_valid(record))
		) STRICT""",
        f"CREATE INDEX {indexes['submissionsByRequest']} ON {tables['submissions']} (conversation_id, request_id)",
        f"CREATE INDEX {indexes['submissionsByConversation']} ON {tables['submissions']} (conversation_id, id)",
        f"CREATE INDEX {indexes['submissionsByStatus']} ON {tables['submissions']} (status, id)",
        f"""CREATE TABLE {tables['documents']} (
			id INTEGER PRIMARY KEY,
			kind TEXT NOT NULL,
			family INTEGER NOT NULL CHECK (family IN (0, 1)),
			key_value TEXT NOT NULL,
			scope_kind TEXT NOT NULL CHECK (scope_kind IN ('session', 'conversation', 'task')),
			owner_id INTEGER NOT NULL,
			created_at INTEGER NOT NULL,
			retired_at INTEGER,
			record TEXT NOT NULL CHECK (json_valid(record))
		) STRICT""",
        f"""CREATE INDEX {indexes['documentsByAddress']}
			ON {tables['documents']} (kind, scope_kind, owner_id, family, key_value, created_at DESC, retired_at)""",
        f"CREATE INDEX {indexes['documentsByScope']} ON {tables['documents']} (scope_kind, owner_id, id)",
        f"CREATE INDEX {indexes['documentsByScopeKind']} ON {tables['documents']} (scope_kind, owner_id, kind, id)",
        f"""CREATE TABLE {tables['documentRevisions']} (
			document_id INTEGER NOT NULL,
			seq INTEGER NOT NULL,
			kind TEXT NOT NULL CHECK (kind IN ('base', 'delta')),
			version INTEGER NOT NULL,
			content TEXT NOT NULL CHECK (json_valid(content)),
			PRIMARY KEY (document_id, seq)
		) STRICT""",
        f"CREATE INDEX {indexes['documentRevisionsByKind']} ON {tables['documentRevisions']} (document_id, kind, seq DESC)",
    ]
    return [{"version": 1, "statements": initialSchema}]


SQLITE_MIGRATIONS: Sequence[SqliteMigration] = createSqliteMigrations(DEFAULT_SQLITE_SCHEMA)

CURRENT_SQLITE_SCHEMA_VERSION: int = SQLITE_MIGRATIONS[-1]["version"] if SQLITE_MIGRATIONS else 0


async def applySqliteMigrations(database: SqliteDatabase, migrations: Sequence[SqliteMigration] | None = None) -> None:
    """Apply all pending schema migrations atomically."""
    await applySqliteMigrationsForSchema(database, DEFAULT_SQLITE_SCHEMA, SQLITE_MIGRATIONS if migrations is None else migrations)


async def applySqliteMigrationsForSchema(
    database: SqliteDatabase, schema: SqliteSchema, migrations: Sequence[SqliteMigration] | None = None
) -> None:
    """Apply migrations rendered for a particular schema."""
    if migrations is None:
        migrations = createSqliteMigrations(schema)
    for index, migration in enumerate(migrations):
        if migration["version"] != index + 1:
            raise RuntimeError("Durable SQLite migrations must have contiguous versions starting at 1")

    durableSchema = schema["tables"]["durableSchema"]

    async def migrate(transaction: SqliteExecutor) -> None:
        await transaction.exec(f"""CREATE TABLE IF NOT EXISTS {durableSchema} (
			singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
			version INTEGER NOT NULL CHECK (version >= 0)
		) STRICT""")
        await transaction.run(f"INSERT OR IGNORE INTO {durableSchema} (singleton, version) VALUES (1, 0)")
        row = await transaction.get(f"SELECT version FROM {durableSchema} WHERE singleton = 1")
        if row is None:
            raise RuntimeError("Durable SQLite schema metadata is missing")
        currentVersion = migrations[-1]["version"] if migrations else 0
        if row["version"] > currentVersion:
            raise RuntimeError(
                f"Durable SQLite schema version {row['version']} is newer than supported version {currentVersion}"
            )
        for migration in migrations:
            if migration["version"] <= row["version"]:
                continue
            for statement in migration["statements"]:
                await transaction.exec(statement)
            await transaction.run(f"UPDATE {durableSchema} SET version = ? WHERE singleton = 1", migration["version"])

    await database.transaction(migrate)


__all__ = [
    "CURRENT_SQLITE_SCHEMA_VERSION",
    "SQLITE_MIGRATIONS",
    "SqliteMigration",
    "applySqliteMigrations",
    "applySqliteMigrationsForSchema",
    "createSqliteMigrations",
]
