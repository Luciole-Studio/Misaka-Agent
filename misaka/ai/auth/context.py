"""The default ``AuthContext``, translated from pi's ``packages/ai/src/auth/context.ts``.

Upstream reaches for ``node:fs``/``node:os`` through a variable specifier so browser
bundlers leave the import alone, and answers ``fileExists`` with ``false`` when there is
no filesystem. Python has no such split, so the indirection is dropped and the behaviour
it protected -- a missing or unreadable path is simply "no" -- is what stays.
"""

from __future__ import annotations

import os
from pathlib import Path

from misaka.ai.auth.types import AuthContext


class _DefaultAuthContext:
    async def env(self, name: str) -> str | None:
        """The variable's value, or ``None`` when it is unset or blank.

        A variable set to whitespace reads as unset on purpose: an empty
        ``ANTHROPIC_API_KEY`` left over in a shell profile should not count as configured.
        """
        value = os.environ.get(name)
        return value if value is not None and value.strip() else None

    async def fileExists(self, path: str) -> bool:
        """Whether the path exists, expanding a leading ``~``.

        Any failure to answer -- permissions, a malformed path -- is "no", because the
        caller's next move is to try another source, not to report an error.
        """
        try:
            resolved = Path(path).expanduser() if path.startswith("~") else Path(path)
            return resolved.exists()
        except (OSError, ValueError, RuntimeError):
            # RuntimeError: expanduser() with no resolvable home. Anything here means we
            # could not answer, and "not configured" is the answer that keeps the caller moving.
            return False


def defaultProviderAuthContext() -> AuthContext:
    """Env vars from the process environment, file existence from the filesystem."""
    return _DefaultAuthContext()


__all__ = ["defaultProviderAuthContext"]
