"""Translated from pi's ``packages/durable/src/testing/types.ts`` (v1.3.0)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..types import Storage


class StorageConformanceAssertions(Protocol):
    def ok(self, value: Any, message: str | None = None) -> None: ...

    def strictEqual(self, actual: Any, expected: Any) -> None: ...

    def deepEqual(self, actual: Any, expected: Any) -> None: ...

    def partialDeepEqual(self, actual: Any, expected: Any) -> None: ...

    def greaterThan(self, actual: float, expected: float) -> None: ...

    def rejects(self, operation: Awaitable[Any], messageIncludes: str) -> Awaitable[None]: ...


type StorageConformanceProvider = Callable[[Callable[[Storage], Awaitable[None]]], Awaitable[None]]
"""``withStorage(use)``: calls and awaits ``use(storage)`` exactly once."""
type StorageConformanceOptions = dict[str, Any]
"""``{"assertions": StorageConformanceAssertions, "withStorage": StorageConformanceProvider}``."""
type StorageConformanceCase = dict[str, Any]
"""``{"name": str, "run": () -> Awaitable[None]}``."""

EnvConformanceAssertions = StorageConformanceAssertions

type EnvConformanceProvider = Callable[[Callable[[Any], Awaitable[None]]], Awaitable[None]]
"""``withEnv(use)``: calls ``use`` once with an environment whose ``cwd`` is a fresh, empty, writable directory, then
cleans up."""
type EnvConformanceOptions = dict[str, Any]
"""``{"assertions": EnvConformanceAssertions, "withEnv": EnvConformanceProvider, "shell"?, "symlinks"?, "listFiles"?}``:
``shell`` is the program and flag that run a POSIX shell script from the next argument (default ``["sh", "-c"]``);
``symlinks`` is whether the shell's ``ln -s`` creates symbolic links (default true); ``listFiles`` is whether the
environment implements the optional ``listFiles``, which the suite then checks (default false)."""
type EnvConformanceCase = dict[str, Any]
"""``{"name": str, "timeoutMs"?: int, "run": () -> Awaitable[None]}``. Cases that wait for an environment's watch
latency need longer than a test runner's default timeout."""

__all__ = [
    "EnvConformanceAssertions",
    "EnvConformanceCase",
    "EnvConformanceOptions",
    "EnvConformanceProvider",
    "StorageConformanceAssertions",
    "StorageConformanceCase",
    "StorageConformanceOptions",
    "StorageConformanceProvider",
]
