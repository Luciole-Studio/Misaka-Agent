"""pi durable's ``test/memory-storage.test.ts``."""

from __future__ import annotations

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.storage.memory import MemoryStorage
from misaka.durable.testing import createAssertions, createStorageConformance
from misaka.durable.types import ROOT_CONVERSATION_ID


async def withStorage(use):
    await use(MemoryStorage())


cases = createStorageConformance({"assertions": createAssertions(), "withStorage": withStorage})


@pytest.mark.parametrize("case", cases, ids=[case["name"] for case in cases])
async def test_memory_storage_conformance(case):
    await case["run"]()


async def test_does_not_expose_retained_state_through_a_prepared_commit():
    storage = MemoryStorage()
    await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], BACKGROUND_CONTEXT)
    entryId = 2
    prepared = storage.prepareCommit(
        [
            {
                "type": "entry",
                "value": {"id": entryId, "conversationId": ROOT_CONVERSATION_ID, "kind": "test", "data": {"nested": [1]}},
            }
        ]
    )
    exposed = prepared.writes[0]
    assert exposed["type"] == "entry"
    # Python has no frozen objects: changing the exposed writes must not change what the storage retains.
    exposed["value"]["data"]["nested"].append(2)

    assert prepared.apply() == 2
    assert prepared.apply() == 2
    assert (await storage.entry(entryId, BACKGROUND_CONTEXT))["entry"]["data"] == {"nested": [1]}
