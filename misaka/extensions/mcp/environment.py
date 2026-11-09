"""MISAKA fork: what a stdio MCP server inherits from MISAKA's environment.

pi starts a stdio server with the whole ``process.env``. MISAKA's environment holds API keys (the
home's .env is loaded into it), so a server gets Hermes' safe names (``_SAFE_ENV_KEYS`` and its
Windows list, tools/mcp_tool_config.py; the MCP SDK's ``get_default_environment`` is the same idea)
and what a networked server needs to reach the network where MISAKA does: the proxy and certificate
variables, and the package managers' own settings -- an `npx -y` or `uvx` server fetches itself at
start, through the registry mirror and proxy the user set for npm, uv or pip. Anything else a server
needs goes in its ``env``, where ``${VAR}`` takes the value from the environment (issue #10).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

_SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR", "MISAKA_HOME"})
_SAFE_ENV_KEYS_CASE_INSENSITIVE = frozenset({
    "ALLUSERSPROFILE", "APPDATA", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "COMPUTERNAME", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "OS",
    "PATHEXT", "PROCESSOR_ARCHITECTURE", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "PUBLIC", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "NODE_OPTIONS", "PYTHONUTF8", "PYTHONIOENCODING", "LOGNAME", "JAVA_HOME"})
_PACKAGE_MANAGER_PREFIXES = ("NPM_CONFIG_", "YARN_", "PNPM_", "COREPACK_", "UV_", "PIP_")
# A registry's credentials among those settings stay behind -- by name (npm's _authToken and key,
# UV_INDEX_<N>_PASSWORD, ...) or in the value (an index URL with user:password@): a server that
# needs one names it in its env.
_REGISTRY_SECRET = re.compile(r"AUTH|TOKEN|PASSWORD|SECRET|_IDENT|CLIENT_CERT|CLIENT_KEY|_KEY$", re.IGNORECASE)
_URL_USERINFO = re.compile(r"://[^/@\s]+@")


def inheritedEnvironment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """What a stdio server takes from MISAKA's environment: the safe names, ``LC_*``, ``XDG_*`` and
    the package managers' settings."""
    environ = os.environ if environ is None else environ
    return {
        key: value
        for key, value in environ.items()
        if key in _SAFE_ENV_KEYS
        or key.upper() in _SAFE_ENV_KEYS_CASE_INSENSITIVE
        or key.startswith(("XDG_", "LC_"))
        or (
            key.upper().startswith(_PACKAGE_MANAGER_PREFIXES)
            and not _REGISTRY_SECRET.search(key)
            and not _URL_USERINFO.search(value)
        )
    }


__all__ = ["inheritedEnvironment"]
