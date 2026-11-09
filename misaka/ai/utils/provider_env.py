"""Translated from pi's ``packages/ai/src/utils/provider-env.ts`` (v1.1.0).

MISAKA fork: no ``getBunSandboxEnvValue``; it reads ``/proc/self/environ`` because Bun compiled binaries can hand back
an empty ``process.env`` inside Linux sandboxes (https://github.com/oven-sh/bun/issues/27802), a workaround for a Bun
bug with no counterpart here.
"""

from __future__ import annotations

import os

from misaka.ai.types import ProviderEnv


def getProviderEnvValue(name: str, env: ProviderEnv | None = None) -> str | None:
    """Resolve a provider env value from scoped overrides, normal process.env, then
    the duplicated Bun sandbox fallback for direct pi-ai consumers."""
    return (env.get(name) if env else None) or os.environ.get(name) or None


__all__ = ["getProviderEnvValue"]
