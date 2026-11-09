"""MISAKA's adaptations in the durable 1.1.0 → 1.3.0 port, pinned where no ported upstream case covers them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.durable import Harness, MemoryStorage, createRegistry
from misaka.durable.errors import SessionFailed, errorMessage
from misaka.durable.harness.harness import SCHEDULER_INDEX_SIZES
from misaka.durable.session.observation import CommittedStateSource
from misaka.durable.session.session import SessionImpl, contained
from misaka.durable.storage.sqlite.schema import (
    createSqliteSchema,
    validateSqliteSchemaPrefix,
)
from tests.durable_support import context


class _Unformattable(Exception):
    def __str__(self) -> str:
        raise TypeError("Cannot convert object to primitive value")


def test_error_message_never_raises_for_the_value_it_formats() -> None:
    # Python raises only exceptions; one whose `str()` throws is the counterpart of `Object.create(null)`.
    assert errorMessage(_Unformattable()) == "[object _Unformattable]"
    assert errorMessage(ValueError("plain")) == "plain"
    assert errorMessage(None) == "null"


async def test_a_storage_read_whose_caller_is_cancelled_by_asyncio_does_not_fail_the_session() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Slow(MemoryStorage):
        async def _slow(self) -> None:
            entered.set()
            await release.wait()

        def conversation(self, id: int, readContext: Any) -> Any:
            async def read() -> Any:
                await self._slow()
                return None

            return asyncio.ensure_future(read())

    session = SessionImpl(Slow())
    read = asyncio.ensure_future(session.storage.conversation(1, context))
    await entered.wait()
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    # JavaScript has no task cancellation: a cancelled Python caller is not a Storage failure.
    assert session.failure is None
    session.assertUsable()
    release.set()
    await session.close(context)
    assert await session.closed == {"reason": "closed"}


async def test_a_failed_session_rejects_its_failed_future_and_settles_closed_with_the_error() -> None:
    session = SessionImpl(MemoryStorage())
    failed = session.failed
    error = RuntimeError("disk gone")
    session.fail(error)
    with pytest.raises(SessionFailed) as raised:
        await failed
    assert raised.value.cause is error
    assert raised.value.__cause__ is error
    assert await session.closed == {"reason": "failed", "error": error}
    with pytest.raises(SessionFailed):
        session.assertUsable()


async def test_contained_reports_a_raise_and_a_failing_awaitable_without_raising() -> None:
    reported: list[BaseException] = []

    def raises() -> None:
        raise RuntimeError("sync")

    async def rejects() -> None:
        raise RuntimeError("async")

    contained(raises, reported.append)
    contained(rejects, reported.append)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [str(error) for error in reported] == ["sync", "async"]


async def test_a_state_source_takes_the_failure_the_session_passes_every_observer() -> None:
    released: list[bool] = []
    source = CommittedStateSource({"a": 1}, lambda: released.append(True))
    # JavaScript drops an argument the method does not declare; Python needs the parameter.
    source.closeSession({"error": RuntimeError("gone")})
    source.closeSession()


async def test_the_scheduler_index_sizes_are_an_attribute_named_by_the_upstream_symbol() -> None:
    harness = await Harness.open(MemoryStorage(), {"registry": createRegistry(), "models": None}, context)
    try:
        sizes = getattr(harness, SCHEDULER_INDEX_SIZES)
        assert sizes == harness.schedulerIndexSizes
        assert set(sizes) == {"live", "settled", "edges", "below", "roots", "unloaded", "dropQueue"}
    finally:
        await harness.close(context)


def test_sqlite_schema_prefixes_are_checked_as_upstream_checks_them() -> None:
    assert createSqliteSchema("app_")["tables"]["entries"] == "app_entries"
    assert createSqliteSchema()["indexes"]["tasksByStatus"] == "tasks_by_status"
    for invalid in ("", "1x", "a-b", "a b", "é"):
        with pytest.raises(RuntimeError, match="Invalid SQLite schema prefix"):
            validateSqliteSchemaPrefix(invalid)
    with pytest.raises(RuntimeError, match="is reserved"):
        validateSqliteSchemaPrefix("SQLite_x")
