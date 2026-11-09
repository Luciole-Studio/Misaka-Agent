"""Translated from marked's ``src/defaults.ts`` (marked 18.0.11, MIT)."""

from __future__ import annotations

from .tokens import JSObject


def _getDefaults() -> JSObject:
    """Gets the original marked default options."""
    return JSObject({
        "async": False,
        "breaks": False,
        "extensions": None,
        "gfm": True,
        "hooks": None,
        "pedantic": False,
        "renderer": None,
        "silent": False,
        "tokenizer": None,
        "walkTokens": None,
    })


_defaults: JSObject = _getDefaults()


def changeDefaults(newDefaults: JSObject) -> None:
    global _defaults
    _defaults = newDefaults


def defaults() -> JSObject:
    """MISAKA fork: the current defaults, read through a function since ``changeDefaults`` rebinds them."""
    return _defaults


__all__ = ["_getDefaults", "changeDefaults", "defaults"]
