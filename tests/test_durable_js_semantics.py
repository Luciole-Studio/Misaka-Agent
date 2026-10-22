"""Durable behaviour that follows from JavaScript semantics upstream relies on: async storage methods start when
called, a cursor whose ``after`` is ``null`` is not an absent cursor, and listener registries are Sets."""

from __future__ import annotations

import asyncio

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.errors import StorageRequestError
from misaka.durable.session.session import SessionImpl
from misaka.durable.storage.memory import MemoryStorage
from misaka.durable.storage.memory import cursorId as memoryCursorId
from misaka.durable.storage.sqlite.storage import cursorId as sqliteCursorId


async def test_a_memory_storage_call_starts_before_it_is_awaited():
    storage = MemoryStorage()
    start = storage._nextId
    pending = storage.mintId()
    second = await storage.mintId()
    first = await pending
    assert (first, second) == (start, start + 1)


@pytest.mark.parametrize("cursorId", [memoryCursorId, sqliteCursorId], ids=["memory", "sqlite"])
def test_a_null_cursor_position_is_refused_by_every_backend(cursorId):
    assert cursorId(None) is None
    assert cursorId({}) is None
    # pi 1.3.0: storages refuse an invalid cursor with StorageRequestError, which fails only that read.
    with pytest.raises(StorageRequestError, match="Invalid storage cursor"):
        cursorId({"after": None})


async def test_a_listener_subscribed_twice_is_one_listener():
    session = SessionImpl(MemoryStorage())
    calls: list[str] = []

    def listener(publication, context):
        calls.append("commit")

    first = session.subscribeCommits(listener)
    session.subscribeCommits(listener)
    assert first() is True
    assert first() is False
    assert listener not in session._commitListeners
    closed = session.subscribeClose(lambda: calls.append("close"))
    assert closed() is True
    await session.close(BACKGROUND_CONTEXT)
    await asyncio.sleep(0)
    assert calls == []
