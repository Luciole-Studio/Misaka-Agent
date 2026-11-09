"""The asynchronous SQL facade the portable SQLite storage runs on.

Translated from pi's ``packages/durable/src/storage/sqlite/database.ts``. A row is a plain ``dict`` keyed by column
name, a bound value is ``None``, an ``int`` or ``float``, a ``str`` or ``bytes`` (``bigint`` and ``number`` are both
``int`` here), and every operation returns an awaitable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

type SqliteValue = None | int | float | str | bytes
"""Values supported by the portable SQLite storage core."""


class SqliteExecutor(Protocol):
    """Asynchronous SQL operations shared by a database and its transaction handles.

    ``exec`` runs SQL text without bindings and may contain several statements. ``run``, ``get``, and ``all`` execute
    one statement with positional bindings. Adapters may cache prepared statements by SQL text, so callers pass values
    as bindings instead of interpolating them."""

    def exec(self, sql: str) -> Awaitable[None]: ...

    def run(self, sql: str, *params: SqliteValue) -> Awaitable[None]: ...

    def get(self, sql: str, *params: SqliteValue) -> Awaitable[dict[str, Any] | None]: ...

    def all(self, sql: str, *params: SqliteValue) -> Awaitable[list[dict[str, Any]]]: ...


class SqliteDatabase(SqliteExecutor, Protocol):
    """Minimal database facade required by ``SqliteStorage``.

    All operations are asynchronous so adapters may execute outside the harness runtime.

    ``transaction`` passes the callback a transaction handle. All work in the transaction must use that handle; the
    handle is invalid after the callback settles. Adapters must queue unrelated operations and other transactions until
    the transaction finishes. The returned awaitable settles after commit or rollback. Calling the database itself
    (including ``transaction`` or ``close``) from inside a callback therefore waits for that transaction and never
    settles.

    When the callback fails, the adapter must roll the transaction back before failing with that same error. If
    rollback fails, it must fail with a different error (for example an ``ExceptionGroup``) so callers cannot mistake
    the callback error for a guaranteed rollback."""

    def transaction[T](self, callback: Callable[[SqliteExecutor], Awaitable[T]]) -> Awaitable[T]: ...

    def close(self) -> Awaitable[None]: ...


__all__ = ["SqliteDatabase", "SqliteExecutor", "SqliteValue"]
