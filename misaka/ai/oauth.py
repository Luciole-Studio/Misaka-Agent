"""Translated from pi's ``packages/ai/src/oauth.ts`` (v1.1.0).

Type-only compatibility entry point for coding-agent extension OAuth declarations.

MISAKA fork: TypeScript's ``export type`` is erased; the names resolve lazily (PEP 562), so importing this module does
not run ``misaka.ai.compat``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = (
    "OAuthAuthInfo",
    "OAuthCredentials",
    "OAuthDeviceCodeInfo",
    "OAuthLoginCallbacks",
    "OAuthPrompt",
    "OAuthSelectOption",
    "OAuthSelectPrompt",
)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("misaka.ai.compat.extension_oauth_types"), name)
    globals()[name] = value
    return value


__all__ = list(_EXPORTS)
