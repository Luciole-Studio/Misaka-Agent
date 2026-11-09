"""Portable SQLite storage.

Translated from pi's ``packages/durable/src/storage/sqlite/index.ts``. Upstream's Cloudflare Durable Object storage
(``storage/sqlite/cloudflare.ts``) is not ported: misaka has no Workers runtime.
"""

from __future__ import annotations

from .database import SqliteDatabase, SqliteExecutor, SqliteValue
from .migrations import (
    CURRENT_SQLITE_SCHEMA_VERSION,
    SQLITE_MIGRATIONS,
    SqliteMigration,
    applySqliteMigrations,
)
from .storage import SqliteStorage, SqliteStorageOptions

__all__ = [
    "CURRENT_SQLITE_SCHEMA_VERSION",
    "SQLITE_MIGRATIONS",
    "SqliteDatabase",
    "SqliteExecutor",
    "SqliteMigration",
    "SqliteStorage",
    "SqliteStorageOptions",
    "SqliteValue",
    "applySqliteMigrations",
]
