"""The client string this install sends, translated from pi's ``utils/pi-user-agent.ts``.

Two deliberate differences from upstream. It says ``misaka``, not ``pi``: sending pi's
name would attribute this traffic to somebody else's project, the same reasoning that
gave ``core/provider_attribution.py`` its own client name. And it has no browser guard,
because upstream's exists only to keep a top-level ``node:os`` import out of browser
builds -- Python always has ``platform``.

The platform and architecture names are Node's, not Python's, because they are part of
the string providers receive: upstream sends ``darwin``/``x64``/``win32`` and this sends
the same, rather than Python's ``Darwin``/``x86_64``/``Windows``.
"""

from __future__ import annotations

import platform

from misaka.core.provider_attribution import CLIENT_NAME

# `os.arch()` spellings for the values `platform.machine()` reports.
_NODE_ARCH = {
    "x86_64": "x64",
    "amd64": "x64",
    "i386": "ia32",
    "i686": "ia32",
    "aarch64": "arm64",
}


def get_misaka_user_agent() -> str:
    """``misaka (darwin 25.5.0; arm64)`` -- system, release, architecture. No identity."""
    system = platform.system().lower()
    return (
        f"{CLIENT_NAME} ({'win32' if system == 'windows' else system} "
        f"{platform.release()}; {_NODE_ARCH.get(platform.machine().lower(), platform.machine().lower())})"
    )


__all__ = ["get_misaka_user_agent"]
