"""Backend-independent test helpers for durable storage and execution environments.

Translated from pi's ``packages/durable/src/testing/index.ts``. The runner adapters (``registerStorageConformance``,
``registerEnvConformance``) are the pytest parametrizations in ``tests``; the benchmark datasets are benchmark programs
and have no Python port.
"""

from __future__ import annotations

from .assertions import PytestAssertions, createAssertions
from .env_conformance import createEnvConformance
from .storage_conformance import createStorageConformance
from .types import (
    EnvConformanceAssertions,
    EnvConformanceCase,
    EnvConformanceOptions,
    EnvConformanceProvider,
    StorageConformanceAssertions,
    StorageConformanceCase,
    StorageConformanceOptions,
    StorageConformanceProvider,
)

__all__ = [
    "EnvConformanceAssertions",
    "EnvConformanceCase",
    "EnvConformanceOptions",
    "EnvConformanceProvider",
    "PytestAssertions",
    "StorageConformanceAssertions",
    "StorageConformanceCase",
    "StorageConformanceOptions",
    "StorageConformanceProvider",
    "createAssertions",
    "createEnvConformance",
    "createStorageConformance",
]
