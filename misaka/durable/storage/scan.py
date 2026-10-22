"""Ported from: pi packages/durable/src/storage/scan.ts @ v1.3.0"""

from __future__ import annotations

from typing import Any, TypedDict

from misaka.utils.js import UNDEFINED, jsIsSafeInteger, jsString

from ..errors import StorageRequestError
from ..types import ScanOrder


class ScanStart(TypedDict):
    """Where a built-in storage scan starts: its order and the last ID a previous page returned."""

    order: ScanOrder
    after: int | None


def scanStart(requested: Any, cursor: dict[str, Any] | None, fallback: ScanOrder) -> ScanStart:
    """Resolve a scan's order and position. A cursor continues in the order it was created with, whether the query
    repeats that order or omits it; a different ``order`` throws. A cursor without an order, written before scans had
    one, continues in the scan's default order. ``requested`` is ``UNDEFINED`` when the query has no ``order``."""
    if requested is not UNDEFINED and requested != "ascending" and requested != "descending":
        raise StorageRequestError(f"Invalid scan order: {jsString(requested)}")
    if cursor is None:
        return {"order": fallback if requested is UNDEFINED else requested, "after": None}
    after = cursor.get("after", UNDEFINED)
    stored = cursor.get("order", UNDEFINED)
    if isinstance(after, bool) or not jsIsSafeInteger(after):
        raise StorageRequestError("Invalid storage cursor")
    if stored is not UNDEFINED and stored != "ascending" and stored != "descending":
        raise StorageRequestError("Invalid storage cursor")
    order: ScanOrder = fallback if stored is UNDEFINED else stored
    if requested is not UNDEFINED and requested != order:
        raise StorageRequestError(f"The cursor continues a {order} scan; the query asks for {requested}")
    return {"order": order, "after": int(after)}


def nextCursor(id: int, order: ScanOrder) -> dict[str, Any]:
    """The continuation of a scan whose last returned item has ``id``."""
    return {"after": id, "order": order}


__all__ = ["ScanStart", "nextCursor", "scanStart"]
