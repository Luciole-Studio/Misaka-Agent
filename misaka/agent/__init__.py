"""Translated from pi's ``packages/agent/src/index.ts`` (v1.1.0).

MISAKA fork: the index resolves its names lazily (PEP 562), so importing the package loads none of its modules.
"""

from __future__ import annotations

import importlib
from typing import Any

# `export * from` each module, in the upstream order, plus `export { setDefaultStreamFn }`.
_STAR_MODULES = (
    "misaka.agent.agent",
    "misaka.agent.agent_loop",
    "misaka.agent.proxy",
    "misaka.agent.types",
)
_NAMED = {"setDefaultStreamFn": "misaka.agent.stream_fn"}


def _exports() -> dict[str, str]:
    names: dict[str, str] = {}
    for module in _STAR_MODULES:
        for name in importlib.import_module(module).__all__:
            names.setdefault(name, module)
    names.update(_NAMED)
    return names


def __getattr__(name: str) -> Any:
    if name == "__all__":
        return sorted(_exports())
    module = _NAMED.get(name)
    if module is None:
        module = _exports().get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_exports()})
