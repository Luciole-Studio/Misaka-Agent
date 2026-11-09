"""Durable SQLite storage over Python's built-in ``sqlite3``.

Translated from pi's ``packages/durable/src/storage/sqlite/node.ts`` (v1.3.0), which runs on ``node:sqlite``. ``sqlite3`` is
synchronous like ``DatabaseSync``: an operation runs on the event loop thread when it is called and returns a future
that has already settled, and a serial queue orders the operations of overlapping callers and transactions. The
connection runs in autocommit mode (``BEGIN IMMEDIATE`` and ``COMMIT`` are explicit statements) and ``sqlite3`` caches
prepared statements by SQL text per connection itself, so this adapter keeps no statement map. Rows are plain ``dict``
objects keyed by column name.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from misaka.utils.js import rejectedPromise, resolvedPromise, startTask

from .database import SqliteExecutor, SqliteValue
from .schema import validateSqliteSchemaPrefix
from .storage import SqliteStorage, SqliteStorageOptions


class NodeSqliteDatabaseOptions(TypedDict, total=False):
    """SQLite connection settings for a durable storage file."""

    walAutoCheckpointPages: int
    """SQLite WAL auto-checkpoint threshold. SQLite and this adapter default to 1,000 pages; 0 disables it."""
    busyTimeoutMs: int
    """Time SQLite waits for a competing file lock. SQLite defaults to 0; this adapter defaults to 5,000 ms."""


class NodeSqliteStorageOptions(NodeSqliteDatabaseOptions, SqliteStorageOptions, total=False):
    """SQLite connection settings plus durable storage options."""


DEFAULT_WAL_AUTO_CHECKPOINT_PAGES = 1_000
DEFAULT_BUSY_TIMEOUT_MS = 5_000
# Prepared statements kept per connection. The storage issues well under a hundred distinct statement texts.
_STATEMENT_CACHE_SIZE = 512
# MISAKA fork: STRICT tables need SQLite 3.37, and Python links the SQLite of the system rather than a bundled one.
_MINIMUM_SQLITE_VERSION = (3, 37, 0)


class _TransactionScope:
    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


def _settle[T](operation: Callable[[], T]) -> asyncio.Future[T]:
    """Run a synchronous operation now and return the future of its outcome."""
    try:
        return resolvedPromise(operation())
    except Exception as error:  # noqa: BLE001 - the failure of the operation is the outcome of its future
        return rejectedPromise(error)


def _dictRow(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: value for column, value in zip(cursor.description, row, strict=True)}


class _SerialOperationQueue:
    """Runs operations in call order. An operation starts immediately when nothing is running or waiting; otherwise it
    waits for everything before it. An asynchronous operation holds the queue until it settles."""

    def __init__(self) -> None:
        self._tail: asyncio.Future[None] | None = None  # None: nothing to wait for.
        self._pending = 0

    def run[T](self, operation: Callable[[], T]) -> asyncio.Future[T]:
        if self._pending > 0:
            return self._enqueue(operation)
        return _settle(operation)

    def runAsync[T](self, operation: Callable[[], Awaitable[T]]) -> asyncio.Future[T]:
        if self._pending > 0:
            return self._enqueue(operation)
        self._pending += 1
        # Publish the barrier before the operation starts, so calls it makes synchronously wait behind it.
        barrier: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._tail = barrier

        async def hold() -> T:
            try:
                return await operation()
            finally:
                self._pending -= 1
                if not barrier.done():
                    barrier.set_result(None)

        return startTask(hold())

    def _enqueue[T](self, operation: Callable[[], T | Awaitable[T]]) -> asyncio.Future[T]:
        self._pending += 1
        previous = self._tail

        async def wait() -> T:
            try:
                if previous is not None:
                    # The tail only ends, it never fails; the shield keeps a cancelled waiter from cancelling it.
                    await asyncio.shield(previous)
                result = operation()
                if inspect.isawaitable(result):
                    return await result
                return result
            finally:
                self._pending -= 1

        settled = startTask(wait())
        tail: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def release(done: asyncio.Future[T]) -> None:
            if not done.cancelled():
                done.exception()  # The failure belongs to the caller; the queue only waits for the end.
            # A cancelled wait must not open the line early: the tail ends after the one before it as well.
            if previous is not None and not previous.done():
                previous.add_done_callback(lambda _previous: tail.done() or tail.set_result(None))
            elif not tail.done():
                tail.set_result(None)

        settled.add_done_callback(release)
        self._tail = tail
        return settled


class _NodeSqliteExecutor:
    """Executes SQL on one connection."""

    def __init__(self, database: sqlite3.Connection) -> None:
        self._database = database

    def exec(self, sql: str) -> asyncio.Future[None]:
        return self._runOperation(lambda: self._execute(sql))

    def run(self, sql: str, *params: SqliteValue) -> asyncio.Future[None]:
        return self._runOperation(lambda: self._runStatement(sql, params))

    def get(self, sql: str, *params: SqliteValue) -> asyncio.Future[dict[str, Any] | None]:
        return self._runOperation(lambda: self._getRow(sql, params))

    def all(self, sql: str, *params: SqliteValue) -> asyncio.Future[list[dict[str, Any]]]:
        return self._runOperation(lambda: self._allRows(sql, params))

    def _runOperation[T](self, operation: Callable[[], T]) -> asyncio.Future[T]:
        raise NotImplementedError

    def _execute(self, sql: str) -> None:
        self._database.executescript(sql)

    def _runStatement(self, sql: str, params: tuple[SqliteValue, ...]) -> None:
        self._database.execute(sql, params).close()

    def _getRow(self, sql: str, params: tuple[SqliteValue, ...]) -> dict[str, Any] | None:
        cursor = self._database.execute(sql, params)
        try:
            return cursor.fetchone()  # type: ignore[no-any-return]
        finally:
            cursor.close()

    def _allRows(self, sql: str, params: tuple[SqliteValue, ...]) -> list[dict[str, Any]]:
        cursor = self._database.execute(sql, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()


class _NodeSqliteTransaction(_NodeSqliteExecutor):
    def __init__(self, database: sqlite3.Connection, scope: _TransactionScope) -> None:
        super().__init__(database)
        self._scope = scope

    def _runOperation[T](self, operation: Callable[[], T]) -> asyncio.Future[T]:
        if not self._scope.active:
            return rejectedPromise(RuntimeError("SQLite transaction handle is no longer active"))
        return _settle(operation)


class NodeSqliteDatabase(_NodeSqliteExecutor):
    """``SqliteDatabase`` adapter backed by Python's built-in ``sqlite3``. The adapter owns the connection and puts it
    in autocommit mode with dictionary rows."""

    def __init__(self, database: sqlite3.Connection) -> None:
        super().__init__(database)
        database.autocommit = True
        database.row_factory = _dictRow
        self._access = _SerialOperationQueue()
        self._closed = False

    def transaction[T](self, callback: Callable[[SqliteExecutor], Awaitable[T]]) -> asyncio.Future[T]:
        async def perform() -> T:
            self._database.executescript("BEGIN IMMEDIATE")
            scope = _TransactionScope()
            try:
                result = await callback(_NodeSqliteTransaction(self._database, scope))
                scope.active = False
                self._database.executescript("COMMIT")
                return result
            except BaseException as error:
                scope.active = False
                try:
                    self._database.executescript("ROLLBACK")
                except Exception as rollbackError:  # noqa: BLE001 - reported together with the original failure
                    raise BaseExceptionGroup(
                        "SQLite transaction failed and rollback failed", [error, rollbackError]
                    ) from None
                raise

        return self._access.runAsync(perform)

    def close(self) -> asyncio.Future[None]:
        return self._access.run(self._closeConnection)

    def _closeConnection(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._database.executescript("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self._database.close()

    def _runOperation[T](self, operation: Callable[[], T]) -> asyncio.Future[T]:
        return self._access.run(operation)


async def openNodeSqliteDatabase(path: str, options: NodeSqliteDatabaseOptions | None = None) -> NodeSqliteDatabase:
    """Open and configure a Python-backed SQLite database facade."""
    if options is None:
        options = {}
    checkpointPages = options.get("walAutoCheckpointPages")
    if checkpointPages is None:
        checkpointPages = DEFAULT_WAL_AUTO_CHECKPOINT_PAGES
    timeout = options.get("busyTimeoutMs")
    if timeout is None:
        timeout = DEFAULT_BUSY_TIMEOUT_MS
    if sqlite3.sqlite_version_info < _MINIMUM_SQLITE_VERSION:
        raise RuntimeError(f"Durable SQLite storage needs SQLite 3.37 or newer, found {sqlite3.sqlite_version}")
    if path != ":memory:":
        await asyncio.to_thread(os.makedirs, os.path.dirname(path) or ".", exist_ok=True)
    database = sqlite3.connect(path, timeout=timeout / 1000, autocommit=True, cached_statements=_STATEMENT_CACHE_SIZE)
    adapter = NodeSqliteDatabase(database)
    try:
        await adapter.exec("PRAGMA journal_mode = WAL")
        await adapter.exec("PRAGMA synchronous = NORMAL")
        await adapter.exec(f"PRAGMA wal_autocheckpoint = {int(checkpointPages)}")
        return adapter
    except BaseException:
        with contextlib.suppress(Exception):  # Preserve the configuration failure.
            await adapter.close()
        raise


async def openNodeSqliteStorage(path: str, options: NodeSqliteStorageOptions | None = None) -> SqliteStorage:
    """Open or create file-backed durable storage using Python's built-in SQLite, optionally under a table prefix."""
    databaseOptions: dict[str, Any] = dict(options or {})
    prefix = databaseOptions.pop("prefix", None)
    if prefix is not None:
        validateSqliteSchemaPrefix(prefix)
    return await SqliteStorage.open(
        await openNodeSqliteDatabase(path, databaseOptions),  # type: ignore[arg-type]
        {} if prefix is None else {"prefix": prefix},
    )


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_WAL_AUTO_CHECKPOINT_PAGES",
    "NodeSqliteDatabase",
    "NodeSqliteDatabaseOptions",
    "NodeSqliteStorageOptions",
    "openNodeSqliteDatabase",
    "openNodeSqliteStorage",
]
