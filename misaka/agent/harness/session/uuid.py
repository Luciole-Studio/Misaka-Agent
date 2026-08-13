"""UUIDv7 helpers for session identifiers and entry ids."""

from __future__ import annotations

import secrets
import time

_last_timestamp = -1
_sequence = 0


def _fill_random_bytes(length: int) -> bytes:
    return secrets.token_bytes(length)


def uuidv7() -> str:
    global _last_timestamp, _sequence

    random_bytes = bytearray(_fill_random_bytes(16))
    timestamp = int(time.time() * 1000)

    if timestamp > _last_timestamp:
        _sequence = (
            (random_bytes[6] << 24)
            | (random_bytes[7] << 16)
            | (random_bytes[8] << 8)
            | random_bytes[9]
        )
        _last_timestamp = timestamp
    else:
        _sequence = (_sequence + 1) & 0xFFFFFFFF
        if _sequence == 0:
            _last_timestamp += 1

    bytes_out = bytearray(16)
    bytes_out[0] = (_last_timestamp // 0x10000000000) & 0xFF
    bytes_out[1] = (_last_timestamp // 0x100000000) & 0xFF
    bytes_out[2] = (_last_timestamp // 0x1000000) & 0xFF
    bytes_out[3] = (_last_timestamp // 0x10000) & 0xFF
    bytes_out[4] = (_last_timestamp // 0x100) & 0xFF
    bytes_out[5] = _last_timestamp & 0xFF
    bytes_out[6] = 0x70 | ((_sequence >> 28) & 0x0F)
    bytes_out[7] = (_sequence >> 20) & 0xFF
    bytes_out[8] = 0x80 | ((_sequence >> 14) & 0x3F)
    bytes_out[9] = (_sequence >> 6) & 0xFF
    bytes_out[10] = ((_sequence & 0x3F) << 2) | (random_bytes[10] & 0x03)
    bytes_out[11] = random_bytes[11]
    bytes_out[12] = random_bytes[12]
    bytes_out[13] = random_bytes[13]
    bytes_out[14] = random_bytes[14]
    bytes_out[15] = random_bytes[15]
    return _format_uuid(bytes_out)


def _format_uuid(bytes_out: bytes | bytearray) -> str:
    hex_bytes = [f"{byte:02x}" for byte in bytes_out]
    return (
        f"{''.join(hex_bytes[0:4])}-"
        f"{''.join(hex_bytes[4:6])}-"
        f"{''.join(hex_bytes[6:8])}-"
        f"{''.join(hex_bytes[8:10])}-"
        f"{''.join(hex_bytes[10:16])}"
    )


__all__ = ["uuidv7"]
