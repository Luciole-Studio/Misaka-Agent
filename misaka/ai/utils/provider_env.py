"""Provider-scoped environment lookup, translated from pi's ``utils/provider-env.ts``.

The order is the point: a value the caller scoped to this request wins over the ambient
environment, so a per-request ``env`` override is not silently ignored on a machine that
happens to export the same variable.

Upstream's third source -- reading ``/proc/self/environ`` because Bun compiled binaries
can hand back an empty ``process.env`` inside Linux sandboxes -- has no counterpart here.
It is a workaround for a Bun bug, not a behaviour.
"""

from __future__ import annotations

import os

from misaka.ai.auth.types import ProviderEnv


def get_provider_env_value(name: str, env: ProviderEnv | None = None) -> str | None:
    """The scoped override, else the process environment, else ``None``.

    An empty string falls through rather than counting as a value, matching upstream's
    ``||`` chain: a variable exported as empty is not configuration.
    """
    if env:
        scoped = env.get(name)
        if scoped:
            return scoped
    return os.environ.get(name) or None


__all__ = ["get_provider_env_value"]
