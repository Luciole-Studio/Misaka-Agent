"""Translated from pi's ``packages/ai/src/utils/uuid.ts`` (v1.1.0)."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from misaka.utils.js import RangeError, jsIsInteger

_MAX_UUID_V7_TIMESTAMP = 0xFFFFFFFFFFFF
_MAX_SEQUENCE = (1 << 41) - 1

_lastOrdinaryTimestamp = -1
_sequence: int | None = None
# MISAKA fork: ids are minted from worker threads too, and the counter below must not hand the same value out twice.
_lock = threading.Lock()


def uuidv7(timestampMs: Any = None) -> str:
    """Generate a time-ordered UUIDv7. A supplied timestamp is preserved for follower ids."""
    global _lastOrdinaryTimestamp, _sequence

    requestedTimestamp = timestampMs if timestampMs is not None else time.time_ns() // 1_000_000
    if not jsIsInteger(requestedTimestamp) or requestedTimestamp < 0 or requestedTimestamp > _MAX_UUID_V7_TIMESTAMP:
        raise RangeError(f"UUIDv7 timestamp must be an integer between 0 and {_MAX_UUID_V7_TIMESTAMP}")

    with _lock:
        effectiveTimestamp = int(
            max(requestedTimestamp, _lastOrdinaryTimestamp) if timestampMs is None else timestampMs
        )
        if timestampMs is None:
            _lastOrdinaryTimestamp = effectiveTimestamp

        # MISAKA fork: `bytes` (upstream's name) is a builtin here.
        randomBytes = bytearray(secrets.token_bytes(16))
        if _sequence is None:
            _sequence = (
                (randomBytes[1] << 32)
                | (randomBytes[2] << 24)
                | (randomBytes[3] << 16)
                | (randomBytes[4] << 8)
                | randomBytes[5]
            )
        else:
            if _sequence == _MAX_SEQUENCE:
                raise RangeError("UUIDv7 generator sequence exhausted")
            _sequence += 1
        sequence = _sequence

    timestamp = effectiveTimestamp
    for index in range(5, -1, -1):
        randomBytes[index] = (timestamp >> ((5 - index) * 8)) & 0xFF
    randomBytes[6] = 0x70 | ((sequence >> 37) & 0x0F)
    randomBytes[7] = (sequence >> 29) & 0xFF
    randomBytes[8] = 0x80 | ((sequence >> 23) & 0x3F)
    randomBytes[9] = (sequence >> 15) & 0xFF
    randomBytes[10] = (sequence >> 7) & 0xFF
    randomBytes[11] = ((sequence & 0x7F) << 1) | (randomBytes[11] & 0x01)

    hex = [f"{byte:02x}" for byte in randomBytes]
    return f"{''.join(hex[0:4])}-{''.join(hex[4:6])}-{''.join(hex[6:8])}-{''.join(hex[8:10])}-{''.join(hex[10:])}"


__all__ = ["uuidv7"]
