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

    A ``None`` reads as absent, because this stands in for upstream's ``a.b ?? default``
    and ``??`` falls back on null. It matters most where the source is a pydantic model:
    every optional field exists with the value ``None``, so returning it would mean that a
    catalog entry carrying *any* compat key lost the detected default for every key it did
    not set. Measured on the shipped catalog that was 655 of 1312 models -- among them
    ``maxTokensField`` on 499 and ``supportsReasoningEffort`` on 486.
    """
    value = source.get(name, default) if isinstance(source, Mapping) else getattr(source, name, default)
    return default if value is None else value


async def maybe_await(value: Any) -> Any:
    """Await what is awaitable, and pass everything else straight through.

    ``inspect.isawaitable`` rather than ``hasattr(value, "__await__")``: the latter misses
    a bare ``asyncio.Future``, which is exactly what half the merged copies were handed.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def signal_aborted(signal: Any) -> bool:
    """Whether the caller has given up on this call.

    Eleven modules had grown their own copy of this line under three names -- ``_is_aborted``,
    ``_signal_aborted``, ``_aborted``. It is the same question, so it has one answer.
    """
    return bool(read_field(signal, "aborted", False))
