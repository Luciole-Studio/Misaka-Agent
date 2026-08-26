"""Reading a value whose shape the caller does not control.

Ported code passes the same payload around sometimes as a Mapping and sometimes as the
object it was parsed into, and sometimes hands back a coroutine where a plain value would
do. Each module that met this grew its own two-line answer: at the last count there were
twenty-two copies of "read this field either way" under five different names, and seven of
"await this if it needs it" under three. They are here so there is one of each.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any


def read_field(source: Any, name: str, default: Any = None) -> Any:
    """Read a field whether ``source`` arrived as a Mapping or as an object.

    ``Mapping`` rather than ``dict`` on purpose: several of the merged copies tested for
    ``dict``, so a Mapping that was not a dict silently fell through to ``getattr`` and
    returned the default.
    """
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


async def maybe_await(value: Any) -> Any:
    """Await what is awaitable, and pass everything else straight through.

    ``inspect.isawaitable`` rather than ``hasattr(value, "__await__")``: the latter misses
    a bare ``asyncio.Future``, which is exactly what half the merged copies were handed.
    """
    if inspect.isawaitable(value):
        return await value
    return value
