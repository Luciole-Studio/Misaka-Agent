"""Small utilities the chord and durable tests share. (pi chord's ``test/helpers.ts``, which re-exported the loopback
service transport, went with the service runtime in 1.2.0.)"""

from __future__ import annotations

import asyncio

__all__ = ["Deferred", "matches", "waitFor"]


def matches(actual, expected) -> bool:
    """``toMatchObject``: every expected key is present and matches, recursively."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(matches(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return actual == expected


class Deferred:
    """A promise with its resolver and rejecter."""

    def __init__(self) -> None:
        self.promise: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def resolve(self) -> None:
        self.promise.set_result(None)

    def reject(self, error: Exception) -> None:
        self.promise.set_exception(error)


async def waitFor(predicate, timeout: float = 2.0) -> None:
    """``vi.waitFor``: poll until ``predicate()`` is true."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)
