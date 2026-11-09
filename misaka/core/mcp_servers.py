"""MCP server configuration and the servers extensions register with ``pi.registerMcpServer()``.

Translated from pi's ``coding-agent/src/core/mcp-servers.ts``.

The core only validates and stores registrations. The MCP extension (built in, or another
extension that handles ``mcp_servers_change``) connects them next to the servers from
``mcp.json``.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, NotRequired, TypedDict

from misaka.utils.js import isNumber, jsNumber, jsTrim
from misaka.utils.whatwg_url import URL


def _toolPatternRegExp(pattern: str) -> re.Pattern[str]:
    """Regular expression for a tool name pattern where `*` matches any characters, as `toolExposure`,
    `--tools`, and `--exclude-tools` accept them."""
    # A JavaScript `.` stops at every line terminator, where Python's stops only at a line feed.
    source = "[^\n\r\u2028\u2029]*".join(re.escape(part) for part in pattern.split("*"))
    # `\Z`, not `$`: a JavaScript `$` does not match before a trailing newline.
    return re.compile(f"^{source}\\Z")


def createToolNameMatcher(entries: Sequence[str]) -> Callable[[str], bool]:
    """Whether a tool name matches any of the entries, each an exact name or a pattern."""
    names = {entry for entry in entries if "*" not in entry}
    patterns = [_toolPatternRegExp(entry) for entry in entries if "*" in entry]
    return lambda name: name in names or any(pattern.match(name) for pattern in patterns)


# MCP resource tools, which reach every server with resources.
LIST_MCP_RESOURCES_TOOL = "list_mcp_resources"
LIST_MCP_RESOURCE_TEMPLATES_TOOL = "list_mcp_resource_templates"
READ_MCP_RESOURCE_TOOL = "read_mcp_resource"

_MCP_RESOURCE_TOOLS: frozenset[str] = frozenset(
    {
        LIST_MCP_RESOURCES_TOOL,
        LIST_MCP_RESOURCE_TEMPLATES_TOOL,
        READ_MCP_RESOURCE_TOOL,
    }
)


def isMcpToolName(name: str) -> bool:
    """Whether a tool comes from MCP: a server tool (`mcp__<server>__<tool>`) or a resource tool."""
    return name.startswith("mcp__") or name in _MCP_RESOURCE_TOOLS


# - `codemode`: tools are callable from codemode scripts but neither declared to the model nor
#   listed in the codemode description, which lists only the server's namespace. Scripts find them
#   with `searchTools()`. `codemode-deferred` is accepted as an alias.
# - `deferred`: not declared to the model until the `tool_search` tool loads them; the model then
#   calls them directly. Does not need codemode.
# - `direct`: tools are declared to the model like any other tool (and callable from codemode).
# - `hidden`: tools are registered but unreachable.
type McpExposure = Literal["codemode", "deferred", "direct", "hidden"]

MCP_EXPOSURES: tuple[str, ...] = ("codemode", "deferred", "direct", "hidden")

# Older exposure names, accepted in configs and replaced by their current name when validated.
MCP_EXPOSURE_ALIASES: Mapping[str, McpExposure] = {"codemode-deferred": "codemode"}


class McpServerConfigBase(TypedDict, total=False):
    # Default: `codemode`.
    exposure: McpExposure
    # What the server offers, in a sentence. The `mcp_servers` system prompt section lists the server
    # with it, tool search ranks the server's tools by it, and codemode's `describeNamespace()` returns it.
    description: str
    # Exposure of single tools, overriding `exposure`. Keys are tool names as the server offers
    # them, or patterns where `*` matches any characters. An exact name wins over patterns; among
    # patterns the first match in the object wins. `hidden` removes tools, so
    # `"exposure": "hidden"` with overrides for a few tools exposes only those.
    toolExposure: dict[str, McpExposure]
    # Set to false to keep the entry without connecting. Default: true.
    enabled: bool
    # Per-request timeout in seconds. Progress notifications from the server reset it. Default: 60.
    timeout: float


class McpStdioServerConfig(McpServerConfigBase):
    type: NotRequired[Literal["stdio"]]
    command: str
    args: NotRequired[list[str]]
    # Values may reference environment variables (`${NAME}`) or commands (`!cmd`).
    env: NotRequired[dict[str, str]]
    # Relative paths resolve against the session working directory.
    cwd: NotRequired[str]


class McpOAuthConfig(TypedDict, total=False):
    """OAuth client settings for servers that do not support dynamic client registration."""

    # Pre-registered client id. Without it, pi registers a client with the authorization server.
    clientId: str
    # May reference environment variables (`${NAME}`) or commands (`!cmd`).
    clientSecret: str
    # Port of the loopback callback server, for clients registered with a fixed redirect URI.
    # Without `callbackUrl`, the redirect URI is `http://127.0.0.1:<port>/callback`.
    callbackPort: int
    # Redirect URI registered for `clientId`, for example `http://localhost:8080/oauth/callback`.
    # It must be an `http` URI on `localhost`, `127.0.0.1`, or `[::1]`. Without a port, the
    # callback server listens on `callbackPort` or a free port, which is added to the URI
    # (RFC 8252).
    callbackUrl: str
    # Scopes to request, separated by spaces. Default: the scopes the server advertises.
    scope: str
    # `client_name` sent with dynamic client registration, for servers that only accept known clients.
    # Default: `pi`.
    clientName: str
    # How pi identifies itself without `clientId`. `dcr` (default): dynamic client registration. `cimd`:
    # pi's Client ID Metadata Document on pi.dev, for authorization servers that allow pi by that URL. The
    # server must support it for public clients, and the callback must use the default path `/callback`.
    # MISAKA fork: `cimd` is rejected, since misaka has no document of its own and does not present pi's.
    clientRegistration: Literal["dcr", "cimd"]
    # Authorization server metadata document (RFC 8414 or OpenID Connect discovery) to use instead of
    # discovery through the server, for servers that advertise a wrong authorization server or none.
    # The document is trusted as configured. Must use https, except on loopback hosts.
    authServerMetadataUrl: str


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def isLoopbackRedirectUri(value: str) -> bool:
    """Whether a redirect URI can be served by pi's loopback callback server."""
    if not URL.canParse(value):
        return False
    url = URL(value)
    return url.protocol == "http:" and url.hostname in LOOPBACK_HOSTS and url.search == "" and url.hash == ""


class McpProviderAuth(TypedDict):
    provider: str


class McpHttpServerConfig(McpServerConfigBase):
    type: NotRequired[Literal["http"]]
    url: str
    # Values may reference environment variables (`${NAME}`) or commands (`!cmd`).
    headers: NotRequired[dict[str, str]]
    oauth: NotRequired[McpOAuthConfig]
    # Send the token of a pi provider (`/login <provider>`) instead of using OAuth. Not allowed in project
    # `mcp.json` files, and requires https except on loopback hosts, since it sends the credential to `url`.
    auth: NotRequired[McpProviderAuth]


type McpServerConfig = McpStdioServerConfig | McpHttpServerConfig

SERVER_NAME = re.compile(r"[A-Za-z0-9_-]+")
# Upstream compares `type === undefined`: an explicit null is a type that matches neither branch.
_MISSING = object()


def mcpNamespace(server: str) -> str:
    """Namespace of a server's tools: ``mcp__<server>`` with ``-`` replaced by ``_``, like the tool names."""
    return f"mcp__{server.replace('-', '_')}"


def _isRecord(value: Any) -> bool:
    return isinstance(value, Mapping)


def _isStringRecord(value: Any) -> bool:
    return _isRecord(value) and all(isinstance(entry, str) for entry in value.values())


def _validateOAuth(value: Any) -> str | None:
    """Validate a present ``oauth`` entry; the caller skips an absent one (upstream's ``undefined``)."""
    if not _isRecord(value):
        return "oauth must be an object"
    if "clientId" in value and not isinstance(value["clientId"], str):
        return "oauth.clientId must be a string"
    if "clientSecret" in value and not isinstance(value["clientSecret"], str):
        return "oauth.clientSecret must be a string"
    port = value.get("callbackPort")
    if "callbackPort" in value and (
        not isNumber(port) or not math.isfinite(port) or port != int(port) or port < 1 or port > 65535
    ):
        return "oauth.callbackPort must be a port number"
    if "callbackUrl" in value:
        callbackUrl = value["callbackUrl"]
        if not isinstance(callbackUrl, str) or not isLoopbackRedirectUri(callbackUrl):
            return "oauth.callbackUrl must be an http URI on localhost, 127.0.0.1, or [::1] without query or fragment"
        urlPort = URL(callbackUrl).port
        if urlPort and "callbackPort" in value and jsNumber(urlPort) != port:
            return "oauth.callbackUrl and oauth.callbackPort name different ports"
    if "scope" in value and not isinstance(value["scope"], str):
        return "oauth.scope must be a string"
    if "clientName" in value and (not isinstance(value["clientName"], str) or not jsTrim(value["clientName"])):
        return "oauth.clientName must be a non-empty string"
    if "clientRegistration" in value and value["clientRegistration"] != "dcr":
        if value["clientRegistration"] != "cimd":
            return 'oauth.clientRegistration must be "dcr" or "cimd"'
        if "clientId" in value or "clientName" in value:
            return 'oauth.clientRegistration "cimd" cannot be combined with oauth.clientId or oauth.clientName'
        callback = URL(value["callbackUrl"]) if isinstance(value.get("callbackUrl"), str) else None
        if callback and (callback.hostname == "[::1]" or callback.pathname != "/callback"):
            return 'oauth.clientRegistration "cimd" requires oauth.callbackUrl on localhost or 127.0.0.1 with path /callback'
        # MISAKA fork: the mechanism is ported, but the only documents it could name are pi's on pi.dev.
        return 'oauth.clientRegistration "cimd" is not supported: misaka has no Client ID Metadata Document of its own'
    if "authServerMetadataUrl" in value:
        metadataUrl = value["authServerMetadataUrl"]
        url = URL(metadataUrl) if isinstance(metadataUrl, str) and URL.canParse(metadataUrl) else None
        if url is None or not (url.protocol == "https:" or (url.protocol == "http:" and url.hostname in LOOPBACK_HOSTS)):
            return "oauth.authServerMetadataUrl must be an https URL, or http on localhost, 127.0.0.1, or [::1]"
    return None


def _isExposure(value: Any) -> bool:
    return isinstance(value, str) and value in MCP_EXPOSURES


def _resolveExposureAlias(value: Any) -> Any:
    """The exposure an alias stands for; other values are returned unchanged."""
    return MCP_EXPOSURE_ALIASES.get(value, value) if isinstance(value, str) else value


def _resolveExposureAliases(value: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the server entry with exposure aliases replaced by their current names."""
    resolved: dict[str, Any] = dict(value)
    if "exposure" in value:
        resolved["exposure"] = _resolveExposureAlias(value["exposure"])
    toolExposure = value.get("toolExposure")
    if isinstance(toolExposure, Mapping):  # `_isRecord`, spelled so mypy narrows the type
        resolved["toolExposure"] = {tool: _resolveExposureAlias(entry) for tool, entry in toolExposure.items()}
    return resolved


def getMcpToolExposure(config: McpServerConfig, toolName: str) -> McpExposure:
    """Exposure of one tool of a server: its ``toolExposure`` entry, else the server's ``exposure``."""
    overrides = config.get("toolExposure") or {}
    # MISAKA fork: a plain dict lookup; pi's `overrides[toolName]` also finds Object.prototype keys
    # (a tool named "constructor" or "toString").
    exact = overrides.get(toolName)
    if exact is not None:
        return exact
    for pattern, exposure in overrides.items():
        if "*" in pattern and _toolPatternRegExp(pattern).match(toolName):
            return exposure
    return config.get("exposure") or "codemode"


def validateMcpServerConfig(name: str, raw: Any) -> McpServerConfig | str:
    """Validate one server entry of the ``mcpServers`` shape. Returns a copy of the config with exposure
    aliases resolved, or an error message."""
    if not SERVER_NAME.fullmatch(name):
        return f'invalid server name "{name}" (use letters, digits, "_" and "-")'
    if not _isRecord(raw):
        return f'server "{name}" must be an object'
    value = _resolveExposureAliases(raw)
    type_ = value.get("type", _MISSING)
    exposures = ", ".join(f'"{exposure}"' for exposure in MCP_EXPOSURES)
    if "exposure" in value and not _isExposure(value["exposure"]):
        return f'server "{name}": exposure must be one of {exposures}'
    if "toolExposure" in value:
        toolExposure = value["toolExposure"]
        if not _isRecord(toolExposure):
            return f'server "{name}": toolExposure must map tool names to exposures'
        for tool, exposure in toolExposure.items():
            if not _isExposure(exposure):
                return f'server "{name}": toolExposure "{tool}" must be one of {exposures}'
    if "enabled" in value and not isinstance(value["enabled"], bool):
        return f'server "{name}": enabled must be a boolean'
    if "description" in value and not isinstance(value["description"], str):
        return f'server "{name}": description must be a string'
    if "timeout" in value:
        timeout = value["timeout"]
        if not isNumber(timeout) or not timeout > 0:
            return f'server "{name}": timeout must be a positive number of seconds'
    if type_ == "sse":
        return f'server "{name}": legacy SSE transport is not supported; use the streamable HTTP URL'

    if isinstance(value.get("url"), str) and type_ in (_MISSING, "http", "streamable-http"):
        if not URL.canParse(value["url"]) or URL(value["url"]).protocol not in ("http:", "https:"):
            return f'server "{name}": url must be an http or https URL'
        if "headers" in value and not _isStringRecord(value["headers"]):
            return f'server "{name}": headers must map names to strings'
        oauthError = _validateOAuth(value["oauth"]) if "oauth" in value else None
        if oauthError:
            return f'server "{name}": {oauthError}'
        if "auth" in value:
            auth = value["auth"]
            if not _isRecord(auth) or not isinstance(auth.get("provider"), str) or not auth.get("provider"):
                return f'server "{name}": auth.provider must be a provider name'
            url = URL(value["url"])
            if url.protocol != "https:" and url.hostname not in LOOPBACK_HOSTS:
                return f'server "{name}": auth requires an https URL, or http on localhost, 127.0.0.1, or [::1]'
        return value  # type: ignore[return-value]
    if isinstance(value.get("command"), str) and type_ in (_MISSING, "stdio"):
        args = value.get("args")
        if "args" in value and not (isinstance(args, list) and all(isinstance(arg, str) for arg in args)):
            return f'server "{name}": args must be an array of strings'
        if "env" in value and not _isStringRecord(value["env"]):
            return f'server "{name}": env must map names to strings'
        if "cwd" in value and not isinstance(value["cwd"], str):
            return f'server "{name}": cwd must be a string'
        return value  # type: ignore[return-value]
    return f'server "{name}" needs either "command" (stdio) or "url" (streamable HTTP)'


@dataclass(slots=True)
class RegisteredMcpServer:
    """A server an extension registered with ``pi.registerMcpServer()``."""

    name: str
    config: McpServerConfig
    # Path of the extension that registered the server.
    extensionPath: str


class McpServerRegistry:
    """Servers registered by the extensions of one runtime."""

    def __init__(self) -> None:
        self._servers: dict[str, RegisteredMcpServer] = {}
        self._changeListener: Callable[[], None] | None = None

    def register(self, server: RegisteredMcpServer) -> None:
        """Register or replace a server. The caller checks ownership."""
        self._servers[server.name] = server
        if self._changeListener is not None:
            self._changeListener()

    def unregister(self, name: str, extensionPath: str) -> None:
        """Remove a server registered by ``extensionPath``. Servers of other extensions are left alone."""
        existing = self._servers.get(name)
        if existing is None or existing.extensionPath != extensionPath:
            return
        del self._servers[name]
        if self._changeListener is not None:
            self._changeListener()

    def get(self, name: str) -> RegisteredMcpServer | None:
        return self._servers.get(name)

    def list(self) -> list[RegisteredMcpServer]:
        """Copies of the registered servers, in registration order."""
        return [replace(server, config=copy.deepcopy(server.config)) for server in self._servers.values()]

    def setChangeListener(self, listener: Callable[[], None] | None) -> None:
        """Called after every change. The runner sets it when it binds, to emit ``mcp_servers_change``."""
        self._changeListener = listener


__all__ = [
    "LOOPBACK_HOSTS",
    "MCP_EXPOSURES",
    "MCP_EXPOSURE_ALIASES",
    "McpExposure",
    "McpHttpServerConfig",
    "McpOAuthConfig",
    "McpProviderAuth",
    "McpServerConfig",
    "McpServerConfigBase",
    "McpServerRegistry",
    "McpStdioServerConfig",
    "RegisteredMcpServer",
    "getMcpToolExposure",
    "isLoopbackRedirectUri",
    "mcpNamespace",
    "validateMcpServerConfig",
]
