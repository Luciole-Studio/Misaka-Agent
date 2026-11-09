"""Interactive mode exports for misaka."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["InteractiveMode"]

if TYPE_CHECKING:
    from misaka.modes.interactive.interactive_mode import InteractiveMode


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)

    from misaka.modes.interactive.interactive_mode import InteractiveMode

    return InteractiveMode


def __dir__() -> list[str]:
    return sorted(__all__)
