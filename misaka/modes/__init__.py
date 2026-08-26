"""Shared mode exports for coding-agent."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_PUBLIC_EXPORTS: dict[str, tuple[str, str]] = {
    "InteractiveMode": ("misaka.ui.tui.interactive.interactive_mode", "InteractiveMode"),
    "InteractiveModeOptions": ("misaka.ui.tui.interactive.interactive_mode", "InteractiveModeOptions"),
    "PrintModeOptions": ("misaka.modes.print_mode", "PrintModeOptions"),
    "runPrintMode": ("misaka.modes.print_mode", "runPrintMode"),
}

__all__ = list(_PUBLIC_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _PUBLIC_EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    module = import_module(module_name)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(__all__)
