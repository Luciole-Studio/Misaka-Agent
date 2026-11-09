"""Translated from pi's ``packages/ai/src/utils/hash.ts`` (v1.1.0).

MISAKA fork: ``Math.imul``, ``>>>``, ``toString(36)`` and ``charCodeAt`` have no Python counterparts; the helpers below
stand in for them.
"""

from __future__ import annotations


def _to_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def _imul(a: int, b: int) -> int:
    return _to_int32((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF))


def _unsigned_right_shift(value: int, bits: int) -> int:
    return (value & 0xFFFFFFFF) >> bits


def _to_base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    chars: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        chars.append(digits[remainder])
    return "".join(reversed(chars))


def _utf16_code_units(text: str) -> list[int]:
    encoded = text.encode("utf-16-le", "surrogatepass")
    return [encoded[index] | (encoded[index + 1] << 8) for index in range(0, len(encoded), 2)]


def shortHash(str: str) -> str:
    """Fast deterministic hash to shorten long strings"""
    h1 = _to_int32(0xDEADBEEF)
    h2 = _to_int32(0x41C6CE57)
    for ch in _utf16_code_units(str):
        h1 = _imul(h1 ^ ch, 2654435761)
        h2 = _imul(h2 ^ ch, 1597334677)
    h1 = _imul(h1 ^ _unsigned_right_shift(h1, 16), 2246822507) ^ _imul(h2 ^ _unsigned_right_shift(h2, 13), 3266489909)
    h2 = _imul(h2 ^ _unsigned_right_shift(h2, 16), 2246822507) ^ _imul(h1 ^ _unsigned_right_shift(h1, 13), 3266489909)
    return _to_base36(h2 & 0xFFFFFFFF) + _to_base36(h1 & 0xFFFFFFFF)


__all__ = ["shortHash"]
