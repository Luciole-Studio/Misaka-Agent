"""Translated from pi's ``packages/durable/src/ids.ts``. IDs are plain integers; the nominal brand is erased."""

from __future__ import annotations


def idFromNumber(value: int) -> int:
    """Apply an erased ID brand at a trusted numeric allocation or decoding boundary."""
    return value


def seqFromNumber(value: int) -> int:
    """Apply the erased commit-sequence brand at a trusted storage boundary."""
    return value


__all__ = ["idFromNumber", "seqFromNumber"]
