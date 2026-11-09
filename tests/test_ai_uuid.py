"""Translated from pi's ``packages/ai/test/uuid.test.ts`` (v1.1.0).

MISAKA fork: the fake clock and the stubbed randomness replace the module's ``time`` and ``secrets``; the generator's
last ordinary timestamp is reset first, as a fresh module has it.
"""

from __future__ import annotations

import math
import re
from types import SimpleNamespace

import pytest

import misaka.ai.utils.uuid as uuidModule
from misaka.ai.utils.uuid import uuidv7
from misaka.utils.js import RangeError

UUID_V7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
TIMESTAMP = 0x0123456789AB


def parseTimestamp(uuid: str) -> int:
    return int(uuid.replace("-", "")[:12], 16)


def test_generates_ordered_uuidv7s_while_preserving_follower_timestamps(monkeypatch):
    now = {"ms": TIMESTAMP}
    monkeypatch.setattr(uuidModule, "time", SimpleNamespace(time_ns=lambda: now["ms"] * 1_000_000))
    monkeypatch.setattr(uuidModule, "_lastOrdinaryTimestamp", -1)

    first = uuidv7()
    second = uuidv7()
    now["ms"] = TIMESTAMP - 1
    afterRollback = uuidv7()
    now["ms"] = TIMESTAMP + 1
    afterAdvance = uuidv7()
    ordinaryIds = [first, second, afterRollback, afterAdvance]
    followerTimestamp = TIMESTAMP - 1_000
    followers = [uuidv7(followerTimestamp), uuidv7(followerTimestamp)]

    for id in [*ordinaryIds, *followers]:
        assert UUID_V7_RE.match(id)
    assert ordinaryIds == sorted(ordinaryIds)
    assert len(set(ordinaryIds)) == len(ordinaryIds)
    assert [parseTimestamp(id) for id in ordinaryIds] == [TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP + 1]
    assert [parseTimestamp(id) for id in followers] == [followerTimestamp, followerTimestamp]
    assert len(set(followers)) == len(followers)


def test_uses_fresh_randomness_for_every_uuid_tail(monkeypatch):
    randomByte = 0

    def token_bytes(count: int) -> bytes:
        nonlocal randomByte
        randomByte += 1
        return bytes([randomByte]) * count

    monkeypatch.setattr(uuidModule, "secrets", SimpleNamespace(token_bytes=token_bytes))

    assert [uuidv7(TIMESTAMP)[-8:], uuidv7(TIMESTAMP)[-8:]] == ["01010101", "02020202"]


@pytest.mark.parametrize("timestamp", [0, 2**48 - 1])
def test_accepts_timestamp_boundary(timestamp):
    assert parseTimestamp(uuidv7(timestamp)) == timestamp


@pytest.mark.parametrize("timestamp", [-1, 2**48, 1.5, math.nan, math.inf])
def test_rejects_invalid_timestamp(timestamp):
    with pytest.raises(RangeError):
        uuidv7(timestamp)
