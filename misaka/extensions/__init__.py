"""Bundled Pi-style extensions shipped with MISAKA.

Concrete capabilities live here as self-contained feature modules.  The
engine-level extension protocol remains in :mod:`misaka.core.extensions`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def inline(name: str, factory: Callable[[Any], Any], *, hidden: bool = False) -> dict[str, Any]:
    """Build the named inline-extension descriptor used by Pi's resource loader."""

    spec: dict[str, Any] = {"name": name, "factory": factory}
    if hidden:
        spec["hidden"] = True
    return spec


__all__ = ["inline"]
