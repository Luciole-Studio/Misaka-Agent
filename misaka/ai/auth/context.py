"""Translated from pi's ``packages/ai/src/auth/context.ts`` (v1.1.0).

MISAKA fork: Python has no browser build, so the bundler-opaque ``node:fs``/``node:os`` imports are plain ``os`` calls,
and the returned object literal is an instance of ``_DefaultAuthContext``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from misaka.ai.auth.types import AuthContext
from misaka.utils.js import jsTrim


def _getProcessEnv() -> Mapping[str, str]:
    return os.environ


class _DefaultAuthContext:
    async def env(self, name: str) -> str | None:
        value = _getProcessEnv().get(name)
        return value if isinstance(value, str) and len(jsTrim(value)) > 0 else None

    async def fileExists(self, path: str) -> bool:
        try:
            resolved = path
            if resolved.startswith("~"):
                resolved = os.path.expanduser("~") + resolved[1:]
            # `fs.access(resolved)`: F_OK.
            return os.access(resolved, os.F_OK)
        except Exception:  # noqa: BLE001 - `catch { return false; }`
            return False


def defaultProviderAuthContext() -> AuthContext:
    """Default auth context: env vars from `process.env` (undefined in browsers),
    file existence via node:fs (always false in browsers).
    """
    return _DefaultAuthContext()


__all__ = ["defaultProviderAuthContext"]
