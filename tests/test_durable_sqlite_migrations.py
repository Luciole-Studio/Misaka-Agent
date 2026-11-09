"""pi durable's ``test/sqlite-migrations.test.ts``."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.storage.sqlite import (
    CURRENT_SQLITE_SCHEMA_VERSION,
    SQLITE_MIGRATIONS,
    SqliteMigration,
    applySqliteMigrations,
)
from misaka.durable.storage.sqlite.node import (
    openNodeSqliteDatabase,
    openNodeSqliteStorage,
)
from misaka.durable.types import ROOT_CONVERSATION_ID
from misaka.utils.js import jsonStringify

context = BACKGROUND_CONTEXT


@pytest.fixture
def databasePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-migrations-")
    yield os.path.join(directory, "storage.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


async def test_creates_the_current_schema_and_can_be_applied_repeatedly(databasePath):
    database = await openNodeSqliteDatabase(databasePath)
    try:
        await applySqliteMigrations(database)
        await applySqliteMigrations(database)
        assert await database.get("SELECT version FROM durable_schema WHERE singleton = 1") == {
            "version": CURRENT_SQLITE_SCHEMA_VERSION
        }
        assert await database.get("SELECT next_id, next_seq FROM durable_metadata WHERE singleton = 1") == {
            "next_id": "2",
            "next_seq": 1,
        }
    finally:
        await database.close()


async def test_rejects_a_database_newer_than_the_portable_core(databasePath):
    database = await openNodeSqliteDatabase(databasePath)
    await applySqliteMigrations(database)
    await database.run(
        "UPDATE durable_schema SET version = ? WHERE singleton = 1", CURRENT_SQLITE_SCHEMA_VERSION + 1
    )
    await database.close()

    with pytest.raises(Exception, match="is newer than supported version"):
        await openNodeSqliteStorage(databasePath)


async def test_rolls_initial_bootstrap_and_every_pending_migration_back_together(databasePath):
    database = await openNodeSqliteDatabase(databasePath)
    try:
        failed: list[SqliteMigration] = [
            {
                "version": 1,
                "statements": [
                    "CREATE TABLE migration_first (value TEXT) STRICT",
                    "INSERT INTO migration_first (value) VALUES ('retained')",
                ],
            },
            {
                "version": 2,
                "statements": ["CREATE TABLE migration_second (value TEXT) STRICT", "THIS IS NOT SQL"],
            },
        ]
        with pytest.raises(Exception):  # noqa: B017 - any failure of the statements
            await applySqliteMigrations(database, failed)
        assert await database.get(
            "SELECT count(*) AS count FROM sqlite_schema WHERE name IN ('durable_schema', 'migration_first', 'migration_second')"
        ) == {"count": 0}

        await applySqliteMigrations(
            database,
            [failed[0], {"version": 2, "statements": ["CREATE TABLE migration_second (value TEXT) STRICT"]}],
        )
        assert await database.get("SELECT version FROM durable_schema WHERE singleton = 1") == {"version": 2}
        assert await database.get("SELECT value FROM migration_first") == {"value": "retained"}
    finally:
        await database.close()


async def test_rejects_migrations_without_contiguous_versions(databasePath):
    database = await openNodeSqliteDatabase(databasePath)
    try:
        with pytest.raises(Exception, match="contiguous versions starting at 1"):
            await applySqliteMigrations(database, [{"version": 2, "statements": []}])
        with pytest.raises(Exception, match="contiguous versions starting at 1"):
            await applySqliteMigrations(database, [*SQLITE_MIGRATIONS, {"version": 3, "statements": []}])
    finally:
        await database.close()


async def test_rolls_a_failed_migration_back_and_preserves_stored_data_for_a_successful_retry(databasePath):
    storage = await openNodeSqliteStorage(databasePath)
    await storage.commit(
        [
            {"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}},
            {
                "type": "entry",
                "value": {
                    "id": 2,
                    "conversationId": ROOT_CONVERSATION_ID,
                    "kind": "retained",
                    "data": {"retained": True},
                },
            },
        ],
        context,
    )
    await storage.close(context)

    database = await openNodeSqliteDatabase(databasePath)
    nextVersion = CURRENT_SQLITE_SCHEMA_VERSION + 1
    failedMigrations: list[SqliteMigration] = [
        *SQLITE_MIGRATIONS,
        {"version": nextVersion, "statements": ["CREATE TABLE migration_probe (value TEXT) STRICT", "THIS IS NOT SQL"]},
    ]
    with pytest.raises(Exception):  # noqa: B017 - any failure of the statements
        await applySqliteMigrations(database, failedMigrations)
    assert await database.get("SELECT version FROM durable_schema WHERE singleton = 1") == {
        "version": CURRENT_SQLITE_SCHEMA_VERSION
    }
    assert await database.get(
        "SELECT count(*) AS count FROM sqlite_schema WHERE type = 'table' AND name = 'migration_probe'"
    ) == {"count": 0}

    successfulMigrations: list[SqliteMigration] = [
        *SQLITE_MIGRATIONS,
        {"version": nextVersion, "statements": ["CREATE TABLE migration_probe (value TEXT) STRICT"]},
    ]
    await applySqliteMigrations(database, successfulMigrations)
    assert await database.get("SELECT version FROM durable_schema WHERE singleton = 1") == {"version": nextVersion}
    assert await database.get("SELECT record, commit_seq FROM entries WHERE id = 2") == {
        "record": jsonStringify(
            {"id": 2, "conversationId": ROOT_CONVERSATION_ID, "kind": "retained", "data": {"retained": True}}
        ),
        "commit_seq": 1,
    }
    assert await database.get("SELECT next_id, next_seq FROM durable_metadata WHERE singleton = 1") == {
        "next_id": "3",
        "next_seq": 2,
    }
    await database.close()
