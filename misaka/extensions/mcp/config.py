"""MCP server configuration.

Servers are read from ``mcp.json`` in the agent directory and, for trusted projects, from
``<project>/.misaka/mcp.json``. Both use the ``mcpServers`` shape shared by other MCP clients, so
existing configurations can be copied over. Project entries replace global entries with the
same name.

A project entry without ``command``, ``url``, or ``type`` overrides only ``enabled``, ``exposure``, and
``toolExposure`` of the global server with the same name, for example to turn it off in one project:
``{ "mcpServers": { "internal-tools": { "enabled": false } } }``. The rest of the global entry is kept,
including credentials the project could not set itself.

.. code-block:: json

    {
      "mcpServers": {
        "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] },
        "docs": { "url": "https://example.com/mcp", "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" } },
        "sentry": { "url": "https://mcp.sentry.dev/mcp" }
      }
    }

HTTP servers without an ``Authorization`` header use OAuth when they answer 401 (sign in with ``/mcp``).
``"auth": { "provider": "<provider>" }`` sends the token of a ``/login`` provider instead. Project files
cannot use it, so a repository cannot pick where the credential goes.

The top-level ``autoEnableCodemode`` (default true) activates the codemode tool when a server
with ``codemode`` exposure connects. A project value overrides the global one.

Translated from pi's ``coding-agent/src/extensions/mcp/config.ts``.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, TypeGuard

from misaka.config import home
from misaka.core.mcp_servers import (
    McpExposure,
    McpHttpServerConfig,
    McpOAuthConfig,
    McpServerConfig,
    McpStdioServerConfig,
    getMcpToolExposure,
    mcpNamespace,
    validateMcpServerConfig,
)
from misaka.utils import v8_json
from misaka.utils.js import errorMessage, jsObjectOrder, jsonStringify
from misaka.utils.js_regexp import RegExp
from misaka.utils.node_errors import withSyscall
from misaka.utils.text import stripBom

type McpServerScope = Literal["global", "project", "extension"]


class McpServerEntry(TypedDict):
    name: str
    config: McpServerConfig
    # Config file that defined the entry, or the path of the extension that registered it.
    source: str
    # The global or the project `mcp.json`, or `extension` for servers registered with
    # `pi.registerMcpServer()`. Changes to extension servers are not saved.
    scope: NotRequired[McpServerScope]
    # Project `mcp.json` with an override of this global server's `enabled`, `exposure`, or `toolExposure`.
    override: NotRequired[str]


class LoadedMcpConfig(TypedDict):
    servers: list[McpServerEntry]
    # Activate the codemode tool when `codemode` servers connect. Default: true.
    autoEnableCodemode: NotRequired[bool]
    errors: list[str]
    # The project `mcp.json` when the project is trusted, where `/mcp` saves project overrides.
    projectConfig: NotRequired[str]


OVERRIDE_KEYS = ["enabled", "exposure", "toolExposure"]


def isOverride(value: dict[str, Any]) -> bool:
    """Whether an entry overrides a server defined elsewhere instead of defining one."""
    return "command" not in value and "url" not in value and "type" not in value


def isRecord(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _readFile(path: str) -> str:
    """``readFileSync(path, "utf8")``: bytes that are not UTF-8 become U+FFFD, line endings stay as they are, and a
    failure carries Node's message (``error.uvMessage``)."""
    if os.path.isdir(path):
        failure = IsADirectoryError(errno.EISDIR, "illegal operation on a directory")
        failure.uvMessage = "EISDIR: illegal operation on a directory, read"  # type: ignore[attr-defined]
        raise failure
    return withSyscall("open", Path(path).read_bytes, path=path).decode("utf-8", "replace")


def _parse(text: str) -> Any:
    # `JSON.parse`, whose objects list integer keys first and whose syntax errors read as V8's.
    return jsObjectOrder(v8_json.jsonParse(text))


def _message(error: BaseException) -> str:
    return getattr(error, "uvMessage", None) or errorMessage(error)


@dataclass(slots=True)
class McpConfigState:
    servers: dict[str, McpServerEntry]
    autoEnableCodemode: bool | None
    errors: list[str]


def readConfigFile(path: str, scope: Literal["global", "project"], state: McpConfigState) -> None:
    servers, errors = state.servers, state.errors
    if not os.path.exists(path):
        return
    try:
        parsed: Any = _parse(stripBom(_readFile(path)))
    except Exception as error:  # noqa: BLE001 - upstream `catch (error)`: recorded as a config error
        errors.append(f"{path}: {_message(error)}")
        return
    if not isRecord(parsed) or ("mcpServers" in parsed and not isRecord(parsed["mcpServers"])):
        errors.append(f'{path}: expected an object with an "mcpServers" object')
        return
    if isinstance(parsed.get("autoEnableCodemode"), bool):
        state.autoEnableCodemode = parsed["autoEnableCodemode"]
    elif "autoEnableCodemode" in parsed:
        errors.append(f"{path}: autoEnableCodemode must be a boolean")
    mcpServers = parsed.get("mcpServers")
    for name, value in (mcpServers if mcpServers is not None else {}).items():
        if scope == "project" and isRecord(value) and isOverride(value):
            base = servers.get(name)
            extra = [key for key in value if key not in OVERRIDE_KEYS]
            if not base:
                errors.append(f'{path}: server "{name}" needs "command" or "url", or a global server to override')
            elif len(extra) > 0:
                errors.append(f'{path}: server "{name}": an override can only set {", ".join(OVERRIDE_KEYS)}')
            else:
                config = validateMcpServerConfig(name, {**base["config"], **value})
                if isinstance(config, str):
                    errors.append(f"{path}: {config}")
                else:
                    servers[name] = {**base, "config": config, "override": path}
            continue
        config = validateMcpServerConfig(name, value)
        if isinstance(config, str):
            errors.append(f"{path}: {config}")
            continue
        # Names that differ only in `-` and `_` would share a namespace.
        clash = next((other for other in servers if other != name and mcpNamespace(other) == mcpNamespace(name)), None)
        if clash:
            errors.append(f'{path}: server "{name}" conflicts with "{clash}"')
            continue
        if scope == "project" and "url" in config and config.get("auth"):
            errors.append(f'{path}: server "{name}": auth is only allowed in the global mcp.json')
            continue
        servers[name] = {"name": name, "config": config, "source": path, "scope": scope}


class LoadMcpConfigOptions(TypedDict):
    agentDir: str
    cwd: str
    projectTrusted: bool


def loadMcpConfig(options: LoadMcpConfigOptions) -> LoadedMcpConfig:
    """Load global and (when trusted) project MCP configuration. Disabled servers are included with
    ``enabled: false``, so they can be enabled again."""
    state = McpConfigState(servers={}, autoEnableCodemode=None, errors=[])
    readConfigFile(os.path.join(options["agentDir"], "mcp.json"), "global", state)
    # MISAKA fork: `home.project_dir` names the project's config directory, and none at `$HOME`.
    projectDir = home.project_dir(options["cwd"]) if options["projectTrusted"] else None
    projectConfig = str(projectDir / "mcp.json") if projectDir is not None else None
    if projectConfig:
        readConfigFile(projectConfig, "project", state)
    loaded: LoadedMcpConfig = {"servers": list(state.servers.values()), "errors": state.errors}
    if state.autoEnableCodemode is not None:
        loaded = {
            "servers": loaded["servers"],
            "autoEnableCodemode": state.autoEnableCodemode,
            "errors": loaded["errors"],
        }
    if projectConfig:
        loaded["projectConfig"] = projectConfig
    return loaded


class McpServerConfigPatch(TypedDict, total=False):
    """Settings ``/mcp`` changes. ``enabled: true`` and ``exposure: "codemode"`` are the defaults and
    remove the key."""

    enabled: bool
    exposure: McpExposure


class UpdateMcpServerConfigOptions(TypedDict, total=False):
    override: bool


def updateMcpServerConfig(
    path: str,
    name: str,
    patch: McpServerConfigPatch,
    options: UpdateMcpServerConfigOptions | None = None,
) -> None:
    """Change one server's settings in the ``mcp.json`` that defines or overrides it. With ``override``, a
    missing entry is added as an override. Overrides keep default values, since they replace the global
    server's. Other content is kept; the file is rewritten with its indentation."""
    options = options if options is not None else {}

    def edit(servers: dict[str, Any] | None, parsed: dict[str, Any]) -> bool:
        server = servers.get(name) if servers is not None else None
        # `servers?.[name] === undefined`: the key is missing, not set to null.
        if (servers is None or name not in servers) and options.get("override"):
            server = {}
            parsed["mcpServers"] = {**(servers if servers is not None else {}), name: server}
        if not isRecord(server):
            raise RuntimeError(f'{path} does not define MCP server "{name}"')
        keepDefaults = isOverride(server)
        if patch.get("enabled") is not None:
            if patch["enabled"] and not keepDefaults:
                server.pop("enabled", None)
            else:
                server["enabled"] = patch["enabled"]
        if patch.get("exposure") is not None:
            if patch["exposure"] == "codemode" and not keepDefaults:
                server.pop("exposure", None)
            else:
                server["exposure"] = patch["exposure"]
        return True

    editMcpServers(path, edit)


def addMcpServerConfig(path: str, name: str, config: McpServerConfig) -> bool:
    """Add a server to an ``mcp.json``, creating the file when missing. An existing entry with the same
    name is replaced. Returns true when an entry was replaced."""
    replaced = False

    def edit(servers: dict[str, Any] | None, parsed: dict[str, Any]) -> bool:
        nonlocal replaced
        target = servers if servers is not None else {}
        replaced = name in target
        target[name] = config
        parsed["mcpServers"] = target
        return True

    editMcpServers(path, edit)
    return replaced


def removeMcpServerConfig(path: str, name: str) -> bool:
    """Remove a server from an ``mcp.json``. Returns false when the file does not define it."""
    if not os.path.exists(path):
        return False
    removed = False

    def edit(servers: dict[str, Any] | None, _parsed: dict[str, Any]) -> bool:
        nonlocal removed
        if servers is None or name not in servers:
            return False
        del servers[name]
        removed = True
        return True

    editMcpServers(path, edit)
    return removed


def editMcpServers(path: str, edit: Callable[[dict[str, Any] | None, dict[str, Any]], bool]) -> None:
    """Read an ``mcp.json`` (an empty config when missing), let ``edit`` change its ``mcpServers``, and
    write it back with its indentation when ``edit`` returns true. Other content is kept."""
    text = stripBom(_readFile(path)) if os.path.exists(path) else None
    parsed: Any = {} if text is None else _parse(text)
    if not isRecord(parsed) or ("mcpServers" in parsed and not isRecord(parsed["mcpServers"])):
        raise RuntimeError(f'{path}: expected an object with an "mcpServers" object')
    servers = parsed["mcpServers"] if isRecord(parsed.get("mcpServers")) else None
    if not edit(servers, parsed):
        return
    match = RegExp(r"^([ \t]+)\S", "m").exec(text) if text else None
    indent = (match[1] if match is not None else None) or "  "
    withSyscall("mkdir", lambda: Path(path).parent.mkdir(parents=True, exist_ok=True), path=os.path.dirname(path))
    serialized = f"{jsonStringify(jsObjectOrder(parsed), indent)}\n"
    withSyscall("open", lambda: Path(path).write_text(serialized, encoding="utf-8", newline="\n"), path=path)


__all__ = [
    "LoadMcpConfigOptions",
    "LoadedMcpConfig",
    "McpExposure",
    "McpHttpServerConfig",
    "McpOAuthConfig",
    "McpServerConfig",
    "McpServerConfigPatch",
    "McpServerEntry",
    "McpServerScope",
    "McpStdioServerConfig",
    "UpdateMcpServerConfigOptions",
    "addMcpServerConfig",
    "getMcpToolExposure",
    "loadMcpConfig",
    "removeMcpServerConfig",
    "updateMcpServerConfig",
]
