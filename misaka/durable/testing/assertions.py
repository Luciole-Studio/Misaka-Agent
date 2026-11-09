"""Plain-``assert`` adapters for the storage conformance cases.

Translated from pi's ``packages/durable/src/testing/assertions.ts``, which adapts a Vitest ``expect``; here the
assertions are Python ``assert`` statements, so any test runner reports them.
"""

from __future__ import annotations

from typing import Any


def sameValue(actual: Any, expected: Any) -> bool:
    """``toEqual`` for JSON values: ``true`` is not ``1`` (Python's ``True == 1``), and a number is one type (``1 == 1.0``)."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(sameValue(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(sameValue(a, e) for a, e in zip(actual, expected, strict=True))
    return bool(actual == expected)


def partialMatch(actual: Any, expected: Any) -> bool:
    """``toMatchObject``: every expected key is present and matches, recursively; lists match element by element."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and partialMatch(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(partialMatch(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return sameValue(actual, expected)


class PytestAssertions:
    def ok(self, value: Any, message: str | None = None) -> None:
        assert value, message

    def strictEqual(self, actual: Any, expected: Any) -> None:
        assert sameValue(actual, expected), f"{actual!r} != {expected!r}"

    def deepEqual(self, actual: Any, expected: Any) -> None:
        assert sameValue(actual, expected), f"{actual!r} != {expected!r}"

    def partialDeepEqual(self, actual: Any, expected: Any) -> None:
        assert partialMatch(actual, expected), f"{actual!r} does not match {expected!r}"

    def greaterThan(self, actual: float, expected: float) -> None:
        assert actual > expected, f"{actual!r} is not greater than {expected!r}"

    async def rejects(self, operation: Any, messageIncludes: str) -> None:
        try:
            await operation
        except Exception as error:  # noqa: BLE001
            assert messageIncludes in str(error), f"{str(error)!r} does not include {messageIncludes!r}"
            return
        raise AssertionError(f"Expected the operation to reject with {messageIncludes!r}")


def createAssertions() -> PytestAssertions:
    # MISAKA fork: upstream's `createExpectAssertions(expect)` adapts a Vitest/Jest `expect`; plain `assert` needs no
    # runner, so this takes no argument and is named for what it builds.
    return PytestAssertions()


__all__ = ["PytestAssertions", "createAssertions", "partialMatch", "sameValue"]
