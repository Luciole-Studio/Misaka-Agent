"""Translated from pi's ``packages/ai/src/utils/pi-user-agent.ts`` (v1.1.0).

MISAKA fork: the client string says ``misaka``, not ``pi``: sending pi's name would attribute this traffic to somebody
else's project, the same reasoning that gave ``core/provider_attribution.py`` its own client name. Upstream's browser
guard (``loadNodeOs``, the ``"pi (browser)"`` branch) only keeps a top-level ``node:os`` import out of browser builds;
Python always has ``platform``. The platform and architecture names are Node's, not Python's, because they are part of
the string providers receive: upstream sends ``darwin``/``x64``/``win32`` and this sends the same.
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


def getPiUserAgent() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    return f"{CLIENT_NAME} ({'win32' if system == 'windows' else system} {platform.release()}; {_NODE_ARCH.get(machine, machine)})"


__all__ = ["getPiUserAgent"]
