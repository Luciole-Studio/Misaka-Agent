"""pi durable's ``test/sqlite-facade.test.ts``, plus the cancellation and locking cases Python adds.

``sqlite3`` runs on the event loop thread, so an operation on an idle database finishes before its call returns and the
interleavings JavaScript gets from promise ticks need a held transaction here.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.storage.sqlite import SqliteStorage
from misaka.durable.storage.sqlite.node import (
    NodeSqliteDatabase,
    openNodeSqliteDatabase,
)
from misaka.durable.types import ROOT_CONVERSATION_ID

context = BACKGROUND_CONTEXT


class ControlledSettlementDatabase:
    """Delays or fails the settlement of the next transaction until the test settles it."""

    def __init__(self, delegate: NodeSqliteDatabase) -> None:
        self._delegate = delegate
        self._mode = "immediate"
        self._pendingSettlement: Any = None

    def exec(self, sql: str) -> Any:
        return self._delegate.exec(sql)

    def run(self, sql: str, *params: Any) -> Any:
        return self._delegate.run(sql, *params)

    def get(self, sql: str, *params: Any) -> Any:
        return self._delegate.get(sql, *params)

    def all(self, sql: str, *params: Any) -> Any:
        return self._delegate.all(sql, *params)

    def transaction(self, callback: Any) -> Any:
        mode = self._mode
        self._mode = "immediate"
        if mode == "immediate":
            return self._delegate.transaction(callback)

        async def settle(transaction: Any) -> Any:
            value = await callback(transaction)
            if mode == "reject":
                raise RuntimeError("controlled settlement rejection")
            return value

        settlement = self._delegate.transaction(settle)
        result: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def deliver(done: asyncio.Future[Any]) -> None:
            error = done.exception()
            if error is not None:
                result.set_exception(error)
            else:
                result.set_result(done.result())

        self._pendingSettlement = lambda: settlement.add_done_callback(deliver)
        return result

    def close(self) -> Any:
        return self._delegate.close()

    def controlNextSettlement(self, mode: str) -> None:
        if self._pendingSettlement is not None:
            raise RuntimeError("A settlement is already pending")
        self._mode = mode

    def settle(self) -> None:
        settle = self._pendingSettlement
        if settle is None:
            raise RuntimeError("No settlement is pending")
        self._pendingSettlement = None
        settle()


class PrepareCountingConnection(sqlite3.Connection):
    """Counts how often each statement text is compiled. SQLite calls the authorizer while it compiles a statement and
    ``sqlite3`` compiles a text only when its per-connection statement cache has no entry for it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.prepareCounts: dict[str, int] = {}
        self._compiled = False
        self.set_authorizer(self._authorize)

    def _authorize(self, *_: Any) -> int:
        self._compiled = True
        return sqlite3.SQLITE_OK

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        self._compiled = False
        cursor = super().execute(sql, parameters)
        if self._compiled:
            self.prepareCounts[sql] = self.prepareCounts.get(sql, 0) + 1
        return cursor

    def repeatedPrepares(self) -> list[str]:
        return [sql for sql, count in self.prepareCounts.items() if count > 1]


async def test_prepares_each_storage_statement_once_per_connection_and_reuses_it_across_transactions():
    connection = sqlite3.connect(":memory:", factory=PrepareCountingConnection, autocommit=True)
    storage = await SqliteStorage.open(NodeSqliteDatabase(connection))
    await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], context)
    await storage.commit(
        [
            {"type": "entry", "value": {"id": index + 2, "conversationId": ROOT_CONVERSATION_ID, "kind": "cached"}}
            for index in range(100)
        ],
        context,
    )
    await storage.commit(
        [{"type": "entry", "value": {"id": 102, "conversationId": ROOT_CONVERSATION_ID, "kind": "cached-again"}}],
        context,
    )
    assert (await storage.entry(2, context))["entry"]["kind"] == "cached"
    assert (await storage.entry(102, context))["entry"]["kind"] == "cached-again"
    with pytest.raises(Exception, match="ID 1 already belongs to conversation"):
        await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], context)
    assert (await storage.entry(2, context))["entry"]["kind"] == "cached"
    assert connection.prepareCounts != {}
    assert connection.repeatedPrepares() == []
    await storage.close(context)


async def test_commits_work_done_through_the_transaction_handle_and_closes_idempotently():
    database = await openNodeSqliteDatabase(":memory:")

    async def work(transaction):
        await transaction.exec("CREATE TABLE async_probe (value INTEGER)")
        await transaction.run("INSERT INTO async_probe (value) VALUES (?)", 1)

    await database.transaction(work)
    assert await database.get("SELECT value FROM async_probe") == {"value": 1}
    await database.close()
    await database.close()


async def test_serializes_concurrent_transactions():
    database = await openNodeSqliteDatabase(":memory:")
    await database.exec("CREATE TABLE transaction_queue (value INTEGER)")
    loop = asyncio.get_running_loop()
    firstStarted: asyncio.Future[None] = loop.create_future()
    firstGate: asyncio.Future[None] = loop.create_future()

    async def first(transaction):
        await transaction.exec("INSERT INTO transaction_queue (value) VALUES (1)")
        firstStarted.set_result(None)
        await firstGate

    firstTransaction = database.transaction(first)
    await firstStarted

    secondStarted = False

    async def second(transaction):
        nonlocal secondStarted
        secondStarted = True
        await transaction.exec("INSERT INTO transaction_queue (value) VALUES (2)")

    secondTransaction = database.transaction(second)
    await asyncio.sleep(0)
    assert secondStarted is False

    firstGate.set_result(None)
    await asyncio.gather(firstTransaction, secondTransaction)
    assert await database.all("SELECT value FROM transaction_queue ORDER BY value") == [{"value": 1}, {"value": 2}]
    await database.close()


async def test_runs_operations_in_call_order_whether_they_start_immediately_or_wait():
    database = await openNodeSqliteDatabase(":memory:")
    await database.exec("CREATE TABLE call_order (value INTEGER)")

    # Operations called during a transaction must neither see its uncommitted rows nor join its rollback.
    async def rollsBack(handle):
        await handle.run("INSERT INTO call_order (value) VALUES (?)", 1)
        await asyncio.sleep(0)
        raise RuntimeError("roll back")

    transaction = database.transaction(rollsBack)
    beforeWrite = database.all("SELECT value FROM call_order ORDER BY value")
    write = database.run("INSERT INTO call_order (value) VALUES (?)", 2)
    afterWrite = database.all("SELECT value FROM call_order ORDER BY value")
    with pytest.raises(RuntimeError, match="roll back"):
        await transaction
    await write
    assert await beforeWrite == []
    assert await afterWrite == [{"value": 2}]

    storage = await SqliteStorage.open(database)
    commit = storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], context)
    read = storage.conversation(ROOT_CONVERSATION_ID, context)
    await commit
    assert await read == {"id": ROOT_CONVERSATION_ID}
    await storage.close(context)


async def test_queues_ordinary_operations_behind_an_active_transaction():
    database = await openNodeSqliteDatabase(":memory:")
    await database.exec("CREATE TABLE operation_queue (value INTEGER)")
    loop = asyncio.get_running_loop()
    transactionStarted: asyncio.Future[None] = loop.create_future()
    transactionGate: asyncio.Future[None] = loop.create_future()

    async def hold(transaction):
        await transaction.exec("INSERT INTO operation_queue (value) VALUES (1)")
        transactionStarted.set_result(None)
        await transactionGate

    pending = database.transaction(hold)
    await transactionStarted

    write = database.exec("INSERT INTO operation_queue (value) VALUES (2)")
    read = database.all("SELECT value FROM operation_queue ORDER BY value")
    await asyncio.sleep(0)
    assert write.done() is False
    assert read.done() is False

    transactionGate.set_result(None)
    await pending
    await write
    assert await read == [{"value": 1}, {"value": 2}]
    await database.close()


async def test_queues_database_calls_made_synchronously_by_a_transaction_that_started_immediately():
    database = await openNodeSqliteDatabase(":memory:")
    await database.exec("CREATE TABLE barrier_probe (value INTEGER)")
    outside: Any = None

    async def misuse(handle):
        nonlocal outside
        # Misuse: this call must wait for the transaction instead of joining it.
        outside = database.run("INSERT INTO barrier_probe (value) VALUES (?)", 2)
        await handle.run("INSERT INTO barrier_probe (value) VALUES (?)", 1)
        await asyncio.sleep(0)
        raise RuntimeError("roll back")

    with pytest.raises(RuntimeError, match="roll back"):
        await database.transaction(misuse)
    await outside
    assert await database.all("SELECT value FROM barrier_probe") == [{"value": 2}]
    await database.close()


async def entriesStorage(database: Any) -> SqliteStorage:
    storage = await SqliteStorage.open(database)
    await storage.commit(
        [
            {"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}},
            {"type": "entry", "value": {"id": 2, "conversationId": ROOT_CONVERSATION_ID, "kind": "probe"}},
        ],
        context,
    )
    return storage


async def test_lets_admitted_multi_query_reads_finish_before_storage_closes():
    storage = await entriesStorage(await openNodeSqliteDatabase(":memory:"))
    scan = storage.scanEntries({"conversationId": ROOT_CONVERSATION_ID}, 10, None, context)
    entry = storage.entry(ROOT_CONVERSATION_ID, 2, context)
    head = storage.findLatestHeadMarker(ROOT_CONVERSATION_ID, None, context)
    closed = storage.close(context)
    # A repeated close settles only when the database is closed.
    assert storage.close(context) is closed
    assert [item["id"] for item in (await scan)["items"]] == [2]
    assert (await entry)["entry"]["kind"] == "probe"
    assert await head is None
    await closed
    with pytest.raises(Exception, match="SqliteStorage is closed"):
        await storage.scanEntries({"conversationId": ROOT_CONVERSATION_ID}, 10, None, context)


async def test_close_waits_for_reads_queued_behind_a_held_transaction():
    database = await openNodeSqliteDatabase(":memory:")
    storage = await entriesStorage(database)
    loop = asyncio.get_running_loop()
    held: asyncio.Future[None] = loop.create_future()
    gate: asyncio.Future[None] = loop.create_future()

    async def hold(transaction):
        held.set_result(None)
        await gate

    holding = database.transaction(hold)
    await held
    scan = storage.scanEntries({"conversationId": ROOT_CONVERSATION_ID}, 10, None, context)
    entry = storage.entry(ROOT_CONVERSATION_ID, 2, context)
    head = storage.findLatestHeadMarker(ROOT_CONVERSATION_ID, None, context)
    closed = storage.close(context)
    assert storage.close(context) is closed
    await asyncio.sleep(0)
    assert closed.done() is False
    with pytest.raises(Exception, match="SqliteStorage is closed"):
        await storage.mintId()

    gate.set_result(None)
    await holding
    assert [item["id"] for item in (await scan)["items"]] == [2]
    assert (await entry)["entry"]["kind"] == "probe"
    assert await head is None
    await closed


async def test_rejects_a_transaction_handle_used_after_its_transaction_settles():
    database = await openNodeSqliteDatabase(":memory:")
    await database.exec("CREATE TABLE stale_probe (value INTEGER)")
    handle: Any = None

    async def use(transaction):
        nonlocal handle
        handle = transaction
        await transaction.run("INSERT INTO stale_probe (value) VALUES (?)", 1)

    await database.transaction(use)
    stale = "SQLite transaction handle is no longer active"
    with pytest.raises(Exception, match=stale):
        await handle.exec("INSERT INTO stale_probe (value) VALUES (2)")
    with pytest.raises(Exception, match=stale):
        await handle.run("INSERT INTO stale_probe (value) VALUES (?)", 3)
    assert await database.all("SELECT value FROM stale_probe") == [{"value": 1}]
    await database.close()


async def test_does_not_preserve_the_callback_error_when_rollback_itself_fails():
    database = await openNodeSqliteDatabase(":memory:")
    await database.exec("CREATE TABLE rollback_probe (value INTEGER)")

    async def escapesCommit(transaction):
        await transaction.exec("INSERT INTO rollback_probe (value) VALUES (1)")
        await transaction.exec("COMMIT")
        raise RuntimeError("rejected after an escaped commit")

    with pytest.raises(ExceptionGroup) as raised:
        await database.transaction(escapesCommit)
    assert [type(error) for error in raised.value.exceptions] == [RuntimeError, sqlite3.OperationalError]
    assert await database.get("SELECT value FROM rollback_probe") == {"value": 1}
    await database.close()


async def test_awaits_async_transaction_settlement_and_adopts_ids_only_after_success():
    database = ControlledSettlementDatabase(await openNodeSqliteDatabase(":memory:"))
    database.controlNextSettlement("delay")
    opening = SqliteStorage.open(database)
    opened = False

    def markOpened(_: Any) -> None:
        nonlocal opened
        opened = True

    opening.add_done_callback(markOpened)
    await asyncio.sleep(0)
    assert opened is False
    database.settle()
    storage = await opening

    database.controlNextSettlement("delay")
    committing = storage.commit(
        [{"type": "entry", "value": {"id": 100, "conversationId": ROOT_CONVERSATION_ID, "kind": "settled"}}], context
    )
    committed = False

    def markCommitted(_: Any) -> None:
        nonlocal committed
        committed = True

    committing.add_done_callback(markCommitted)
    await asyncio.sleep(0)
    assert committed is False
    assert await storage.mintId() == 2
    database.settle()
    assert await committing == 1
    assert await storage.mintId() == 101

    database.controlNextSettlement("reject")
    rejected = storage.commit(
        [{"type": "entry", "value": {"id": 200, "conversationId": ROOT_CONVERSATION_ID, "kind": "rejected"}}], context
    )
    assert await storage.mintId() == 102
    database.settle()
    with pytest.raises(Exception, match="controlled settlement rejection"):
        await rejected
    assert await storage.mintId() == 103
    assert await storage.entry(200, context) is None
    await storage.close(context)


async def test_reads_a_document_from_one_committed_state_while_a_commit_replaces_its_base():
    id = 5
    # Each yield count starts the commit at a different point of the read's record and revision queries.
    for yields in range(16):
        storage = await SqliteStorage.open(await openNodeSqliteDatabase(":memory:"))
        await storage.commit(
            [
                {
                    "type": "document.create",
                    "record": {"id": id, "kind": "replaced", "scope": {"kind": "session"}},
                    "content": {"kind": "base", "version": 1, "value": {"value": 1}},
                }
            ],
            context,
        )
        read = storage.document(id, "current", context)
        for _ in range(yields):
            await asyncio.sleep(0)
        replace = storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"value": 2}}}],
            context,
        )
        stored, _ = await asyncio.gather(read, replace)
        assert stored["value"] in ({"value": 1}, {"value": 2})
        await storage.close(context)


async def test_reads_a_document_while_a_commit_waits_behind_a_held_transaction():
    id = 5
    database = await openNodeSqliteDatabase(":memory:")
    storage = await SqliteStorage.open(database)
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "replaced", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {"value": 1}},
            }
        ],
        context,
    )
    held: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def hold(transaction):
        held.set_result(None)
        await gate

    holding = database.transaction(hold)
    await held
    # Called in this order, the read sees the state before the commit; both wait for the held transaction.
    read = storage.document(id, "current", context)
    replace = storage.commit(
        [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"value": 2}}}],
        context,
    )
    gate.set_result(None)
    await holding
    assert (await read)["value"] == {"value": 1}
    await replace
    assert (await storage.document(id, "current", context))["value"] == {"value": 2}
    await storage.close(context)


async def test_cancelling_a_queued_operation_leaves_the_queue_working():
    database = await openNodeSqliteDatabase(":memory:")
    await database.exec("CREATE TABLE cancelled_probe (value INTEGER)")
    held: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def hold(transaction):
        held.set_result(None)
        await gate

    holding = database.transaction(hold)
    await held
    cancelled = database.run("INSERT INTO cancelled_probe (value) VALUES (?)", 1)
    kept = database.run("INSERT INTO cancelled_probe (value) VALUES (?)", 2)
    cancelled.cancel()
    await asyncio.sleep(0)
    gate.set_result(None)
    await holding
    await kept
    assert await database.all("SELECT value FROM cancelled_probe") == [{"value": 2}]
    await database.close()


async def test_cancelling_a_running_transaction_rolls_it_back_and_releases_the_queue():
    database = await openNodeSqliteDatabase(":memory:")
    await database.exec("CREATE TABLE running_probe (value INTEGER)")
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def hold(transaction):
        await transaction.run("INSERT INTO running_probe (value) VALUES (?)", 1)
        started.set_result(None)
        await asyncio.Event().wait()

    running = database.transaction(hold)
    await started
    queued = database.run("INSERT INTO running_probe (value) VALUES (?)", 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await queued
    assert await database.all("SELECT value FROM running_probe") == [{"value": 2}]
    await database.close()


async def test_a_busy_database_fails_the_transaction_and_releases_the_queue(tmp_path):
    path = str(tmp_path / "busy.sqlite")
    first = await openNodeSqliteDatabase(path, {"busyTimeoutMs": 20})
    second = await openNodeSqliteDatabase(path, {"busyTimeoutMs": 20})
    await first.exec("CREATE TABLE busy_probe (value INTEGER)")
    held: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def hold(transaction):
        await transaction.run("INSERT INTO busy_probe (value) VALUES (?)", 1)
        held.set_result(None)
        await gate

    holding = first.transaction(hold)
    await held

    async def never(transaction):
        raise AssertionError("the transaction must not start")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await second.transaction(never)
    gate.set_result(None)
    await holding
    assert await second.all("SELECT value FROM busy_probe") == [{"value": 1}]
    await first.close()
    await second.close()


async def test_a_failed_statement_is_the_outcome_of_its_future():
    database = await openNodeSqliteDatabase(":memory:")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await database.run("INSERT INTO missing_table (value) VALUES (?)", 1)
    assert await database.get("SELECT 1 AS one") == {"one": 1}
    await database.close()


async def test_refuses_a_sqlite_without_strict_tables(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.36.0")
    with pytest.raises(RuntimeError, match=r"needs SQLite 3\.37 or newer, found 3\.36\.0"):
        await openNodeSqliteDatabase(":memory:")
