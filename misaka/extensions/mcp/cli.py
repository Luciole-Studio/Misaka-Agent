"""``misaka mcp``: add, remove, and check MCP servers and sign in to them outside a session. Agents run
it through bash to configure servers, verify an ``mcp.json`` they wrote, and start an OAuth sign-in;
the user only approves access in the browser. Running sessions pick up new credentials on their
next turn.

Translated from pi's ``coding-agent/src/extensions/mcp/cli.ts``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict, cast

from misaka.config import home
from misaka.config.engine import APP_NAME
from misaka.core.mcp_servers import validateMcpServerConfig
from misaka.core.trust_manager import ProjectTrustStore
from misaka.utils._abort_signal import wait_for_abort
from misaka.utils.chalk import chalk
from misaka.utils.js import (
    AbortSignal,
    jsNumber,
    jsObjectOrder,
    jsonStringify,
    jsRound,
    raceAbort,
)
from misaka.utils.js import errorMessage as _errorMessage
from misaka.utils.open_browser import openBrowser
from misaka.utils.values import signal_aborted

from .config import (
    LoadedMcpConfig,
    McpServerEntry,
    addMcpServerConfig,
    getMcpToolExposure,
    loadMcpConfig,
    removeMcpServerConfig,
)
from .runtime import (
    McpOAuthCredentialStore,
    McpServerConnection,
    McpServerLog,
    McpSignInCancelledError,
    createDefaultTransport,
    signInMcpServer,
)


def _logPath(agentDir: str) -> str:
    # MISAKA fork: the home keeps the server log with the other logs; another agent directory keeps it inside.
    if os.path.realpath(agentDir) == os.path.realpath(home.home()):
        return str(home.path("mcp_log"))
    return os.path.join(agentDir, "mcp.log")


# MISAKA fork: the paths name the MISAKA home (`home.display`, `home.DIR_NAME`) where pi names ~/.pi, and the
# global file is a role's (`--as`).
HELP = f"""{chalk.bold("Usage:")}
  {APP_NAME} mcp add <server> [options] -- <command> [args...]
  {APP_NAME} mcp add <server> [options] --url <url>
  {APP_NAME} mcp remove <server> [-l]
  {APP_NAME} mcp list [--json]
  {APP_NAME} mcp login <server> [--timeout <seconds>]
  {APP_NAME} mcp logout <server>

Configure and check MCP servers and sign in to OAuth servers without starting a session.
Reads Last Order's {home.display(home.path("roles_root") / "last_order" / "mcp.json")} (a Sister's own with --as)
and, in trusted projects, {home.DIR_NAME}/mcp.json.

Commands:
  add <server>            Add or replace a server in mcp.json
  remove <server>         Remove a server from mcp.json
  list                    Show state, tools, and errors (exits 1 on failure)
  login <server>          Sign in through the browser
  logout <server>         Delete the stored OAuth credentials

Options for add and remove:
  -l, --local             Use {home.DIR_NAME}/mcp.json in the current project instead of the global file

Options for add:
  --url <url>             Streamable HTTP server URL (instead of a command)
  --env <KEY=VALUE>       Environment variable for a stdio server (repeatable)
  --cwd <dir>             Working directory for a stdio server
  --header <KEY=VALUE>    HTTP header (repeatable)
  --bearer-token-env-var <NAME>
                          Send "Authorization: Bearer ${{NAME}}"
  --oauth-client-id <id>  Pre-registered OAuth client id
  --oauth-client-secret <secret>
                          OAuth client secret (may be ${{NAME}} or !command)
  --oauth-callback-port <port>
                          Fixed OAuth callback port
  --oauth-client-name <name>
                          Client name sent when registering with the OAuth server
  --exposure <mode>       codemode (default), deferred, direct, or hidden
  --description <text>    What the server offers, shown in the system prompt

Other options:
  --as <Sister>           Use the Sister's mcp.json and sign-ins instead of Last Order's
  --json                  Print the list as JSON
  --timeout <seconds>     Time limit for the whole sign-in (default: 300)"""

HELP_HINT = chalk.dim(f'Use "{APP_NAME} mcp --help" for usage.')

DEFAULT_LOGIN_TIMEOUT_SECONDS = 300


class McpCommandOptions(TypedDict):
    cwd: str
    agentDir: str
    # MISAKA fork: the directory of the global `mcp.json`, a role's profile. Defaults to `agentDir`.
    mcpDir: NotRequired[str]
    # MISAKA fork: the Sister `--as` named, repeated in the commands the output suggests.
    asRole: NotRequired[str]
    # Defaults to `mcp-auth.json` under the home's credentials.
    credentials: NotRequired[McpOAuthCredentialStore]
    # Defaults to the platform browser.
    openUrl: NotRequired[Callable[[str], None]]
    # Defaults to console output.
    log: NotRequired[Callable[[str], None]]
    error: NotRequired[Callable[[str], None]]


class ServerReport(TypedDict):
    name: str
    scope: str
    source: str
    # Project `mcp.json` that overrides `enabled`, `exposure`, or `toolExposure` of this global server.
    override: NotRequired[str]
    enabled: bool
    exposure: str
    transport: str
    state: str
    tools: list[str]
    # Tools whose exposure differs from the server's, from `toolExposure`.
    toolExposure: NotRequired[dict[str, str]]
    # Whether the server offers resources. They are not listed: listing every one can take long.
    resources: NotRequired[Literal[True]]
    error: NotRequired[str]



def errorMessage(error: Any) -> str:
    """``error instanceof Error ? error.message : String(error)``. A file system error's message is Node's
    (``uvMessage``)."""
    return getattr(error, "uvMessage", None) or _errorMessage(error)


def describeTransport(entry: McpServerEntry) -> str:
    config = entry["config"]
    if "url" in config:
        return config["url"]  # type: ignore[typeddict-item]
    return " ".join([config["command"], *(config.get("args") or [])])  # type: ignore[typeddict-item]


def _mcpDir(options: McpCommandOptions) -> str:
    return options.get("mcpDir") or options["agentDir"]


def _command(options: McpCommandOptions) -> str:
    asRole = options.get("asRole")
    return f"{APP_NAME} mcp --as {asRole}" if asRole else f"{APP_NAME} mcp"


def createConnection(
    entry: McpServerEntry, options: McpCommandOptions, credentials: McpOAuthCredentialStore
) -> McpServerConnection:
    return McpServerConnection({
        "entry": entry,
        "cwd": options["cwd"],
        "createTransport": createDefaultTransport,
        "credentials": credentials,
        "log": McpServerLog(_logPath(options["agentDir"])),
        "onTools": lambda _connection: None,
    })


# Short spellings of options. `-l`/`--local` match `pi install`; MISAKA fork: misaka has no `install`.
OPTION_ALIASES = {"-l": "--local"}

type _OptionKind = Literal["flag", "value", "list"]


@dataclass(slots=True)
class ParsedOptions:
    positional: list[str] = field(default_factory=list)
    values: dict[str, str | Literal[True]] = field(default_factory=dict)
    # Values of `list` options, in order.
    lists: dict[str, list[str]] = field(default_factory=dict)


def parseOptions(
    args: list[str],
    known: dict[str, _OptionKind],
    error: Callable[[str], None],
    maxPositionals: float = math.inf,
) -> ParsedOptions | None:
    """Parse ``--name value`` options; returns ``None`` and reports unknown ones. ``--`` ends the options,
    as does reaching ``maxPositionals`` positional arguments: the remaining arguments are positional,
    so a command's own options (``add <server> <command> --flag``) are passed through."""
    parsed = ParsedOptions()
    index = 0
    while index < len(args):
        arg = OPTION_ALIASES.get(args[index], args[index])
        if arg == "--" or len(parsed.positional) >= maxPositionals:
            parsed.positional.extend(args[index + 1 if arg == "--" else index :])
            break
        if not arg.startswith("--"):
            parsed.positional.append(arg)
            index += 1
            continue
        name = arg[2:]
        kind = known.get(name)
        if not kind:
            error(f"Unknown option {arg}.\n{HELP_HINT}")
            return None
        if kind == "flag":
            parsed.values[name] = True
            index += 1
            continue
        index += 1
        value = args[index] if index < len(args) else None
        if value is None:
            error(f"{arg} needs a value.")
            return None
        if kind == "list":
            parsed.lists[name] = [*parsed.lists.get(name, []), value]
        else:
            parsed.values[name] = value
        index += 1
    return parsed


def _print(line: str) -> None:
    # MISAKA fork: flushed as console.log is, so the sign-in URL reaches a pipe before the process exits.
    print(line, flush=True)


def _printError(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


async def runMcpCommand(args: list[str], options: McpCommandOptions) -> int:
    """Run ``misaka mcp <args>`` and return the exit code."""
    log = options.get("log") or _print
    error = options.get("error") or _printError
    command = args[0] if len(args) > 0 else None
    rest = args[1:]
    if command is None or command == "help" or "--help" in args or "-h" in args:
        log(HELP)
        return 0

    # MISAKA fork: the project's config directory comes from the home layout, and there is none at `$HOME`.
    projectDir = home.project_dir(options["cwd"])
    projectConfig = str(projectDir / "mcp.json") if projectDir is not None else None
    if command == "add" or command == "remove":
        return (
            add(rest, projectConfig, options, log, error)
            if command == "add"
            else remove(rest, projectConfig, options, log, error)
        )
    projectTrusted = ProjectTrustStore(options["agentDir"]).get(options["cwd"]) is True
    loaded = loadMcpConfig({"agentDir": _mcpDir(options), "cwd": options["cwd"], "projectTrusted": projectTrusted})
    untrustedNote = (
        f"{projectConfig} is ignored because the project is not trusted. Start {APP_NAME} in the project to trust it."
        if not projectTrusted and projectConfig is not None and os.path.exists(projectConfig)
        else None
    )
    credentials = options.get("credentials")
    if credentials is None:
        credentials = McpOAuthCredentialStore()

    if command == "list":
        parsed = parseOptions(rest, {"json": "flag"}, error)
        if parsed is None:
            return 1
        if len(parsed.positional) > 0:
            error(f"Usage: {APP_NAME} mcp list [--json]\n{HELP_HINT}")
            return 1
        return await _list(loaded, "json" in parsed.values, untrustedNote, options, credentials, log)
    if command == "login" or command == "logout":
        parsed = parseOptions(rest, {"timeout": "value"} if command == "login" else {}, error)
        if parsed is None:
            return 1
        name = parsed.positional[0] if len(parsed.positional) > 0 else None
        extra = parsed.positional[1:]
        if not name or len(extra) > 0:
            error(f"Usage: {APP_NAME} mcp {command} <server>\n{HELP_HINT}")
            return 1
        entry = next((server for server in loaded["servers"] if server["name"] == name), None)
        if entry is None:
            configured = ", ".join(server["name"] for server in loaded["servers"]) or "none"
            error(f'No MCP server named "{name}".{f" {untrustedNote}" if untrustedNote else ""} Configured: {configured}.')
            return 1
        connection = createConnection(entry, options, credentials)
        url = connection.oauthUrl
        if not url:
            error(f'MCP server "{name}" does not use OAuth. Only HTTP servers without an Authorization header do.')
            return 1
        if command == "logout":
            removed = credentials.remove(name, url)
            log(f'Signed out of MCP server "{name}".' if removed else f'No stored credentials for MCP server "{name}".')
            return 0
        timeoutValue = parsed.values.get("timeout")
        timeout = jsNumber(timeoutValue if timeoutValue is not None else DEFAULT_LOGIN_TIMEOUT_SECONDS)
        if not math.isfinite(timeout) or timeout <= 0:
            error("--timeout must be a positive number of seconds.")
            return 1
        try:
            return await login(entry, connection, url, timeout * 1000, options, credentials, log, error)
        finally:
            await connection.close()
    error(f'Unknown mcp command "{command}".\n{HELP_HINT}')
    return 1


def parsePairs(option: str, pairs: list[str] | None, error: Callable[[str], None]) -> dict[str, str] | None:
    """Parse ``KEY=VALUE`` pairs of a repeatable option into a record."""
    if pairs is None:
        return {}
    record: dict[str, str] = {}
    for pair in pairs:
        separator = pair.find("=")
        if separator <= 0:
            error(f'--{option} expects KEY=VALUE, got "{pair}".')
            return None
        record[pair[:separator]] = pair[separator + 1 :]
    # A JavaScript object lists integer keys first.
    return jsObjectOrder(record)


def add(
    args: list[str],
    projectConfig: str | None,
    options: McpCommandOptions,
    log: Callable[[str], None],
    error: Callable[[str], None],
) -> int:
    usage = f"Usage: {APP_NAME} mcp add <server> [options] (--url <url> | -- <command> [args...])\n{HELP_HINT}"
    parsed = parseOptions(
        args,
        {
            "local": "flag",
            "url": "value",
            "env": "list",
            "cwd": "value",
            "header": "list",
            "bearer-token-env-var": "value",
            "oauth-client-id": "value",
            "oauth-client-secret": "value",
            "oauth-callback-port": "value",
            "oauth-client-name": "value",
            "exposure": "value",
            "description": "value",
        },
        error,
        2,
    )
    if parsed is None:
        return 1
    positional, values, lists = parsed.positional, parsed.values, parsed.lists
    name = positional[0] if len(positional) > 0 else None
    command = positional[1:]
    url = values.get("url")
    if not name or (url is None) == (len(command) == 0):
        error(usage)
        return 1

    def value(option: str) -> str | None:
        found = values.get(option)
        return found if isinstance(found, str) else None

    exposure = value("exposure")
    httpOnly = [
        "header",
        "bearer-token-env-var",
        "oauth-client-id",
        "oauth-client-secret",
        "oauth-callback-port",
        "oauth-client-name",
    ]
    stdioOnly = ["env", "cwd"]
    misplaced = next(
        (option for option in (httpOnly if url is None else stdioOnly) if option in values or option in lists), None
    )
    if misplaced:
        error(f"--{misplaced} only applies to {'HTTP servers (--url)' if url is None else 'stdio servers'}.")
        return 1

    config: dict[str, Any]
    if isinstance(url, str):
        headers = parsePairs("header", lists.get("header"), error)
        if headers is None:
            return 1
        bearer = value("bearer-token-env-var")
        if bearer is not None:
            headers["Authorization"] = f"Bearer ${{{bearer}}}"
        port = value("oauth-callback-port")
        oauth: dict[str, Any] = {
            **({} if value("oauth-client-id") is None else {"clientId": value("oauth-client-id")}),
            **({} if value("oauth-client-secret") is None else {"clientSecret": value("oauth-client-secret")}),
            **({} if port is None else {"callbackPort": jsNumber(port)}),
            **({} if value("oauth-client-name") is None else {"clientName": value("oauth-client-name")}),
        }
        config = {
            "url": url,
            **({"headers": headers} if len(headers) > 0 else {}),
            **({"oauth": oauth} if len(oauth) > 0 else {}),
        }
    else:
        env = parsePairs("env", lists.get("env"), error)
        if env is None:
            return 1
        executable, commandArgs = command[0], command[1:]
        config = {
            "command": executable,
            **({"args": commandArgs} if len(commandArgs) > 0 else {}),
            **({"env": env} if len(env) > 0 else {}),
            **({} if value("cwd") is None else {"cwd": value("cwd")}),
        }
    if exposure is not None:
        config["exposure"] = exposure
    description = value("description")
    if description is not None:
        config["description"] = description
    validated = validateMcpServerConfig(name, config)
    if isinstance(validated, str):
        error(validated)
        return 1

    project = "local" in values
    if project and projectConfig is None:
        # MISAKA fork: a project file needs a project directory, which the home root is not.
        error(f"There is no project at {options['cwd']} to hold a {home.DIR_NAME}/mcp.json.")
        return 1
    path = projectConfig if project and projectConfig is not None else os.path.join(_mcpDir(options), "mcp.json")
    scope = "project" if project else "global"
    try:
        replaced = addMcpServerConfig(path, name, validated)
    except Exception as addError:  # noqa: BLE001 - upstream `catch (addError)`: reported, exit 1
        error(f"Could not update {path}: {errorMessage(addError)}")
        return 1
    log(f'{"Replaced" if replaced else "Added"} {scope} MCP server "{name}" in {path}.')
    if project and ProjectTrustStore(options["agentDir"]).get(options["cwd"]) is not True:
        log(f"The project is not trusted, so {path} is ignored until you start {APP_NAME} in the project and trust it.")
    # HTTP servers without an Authorization header may use OAuth.
    mayNeedSignIn = "url" in validated and not any(
        header.lower() == "authorization" for header in (validated.get("headers") or {})  # type: ignore[attr-defined]
    )
    log(
        f"Check it with: {_command(options)} list"
        f"{f'. If it requires sign-in: {_command(options)} login {name}' if mayNeedSignIn else ''}"
    )
    return 0


def remove(
    args: list[str],
    projectConfig: str | None,
    options: McpCommandOptions,
    log: Callable[[str], None],
    error: Callable[[str], None],
) -> int:
    parsed = parseOptions(args, {"local": "flag"}, error)
    if parsed is None:
        return 1
    name = parsed.positional[0] if len(parsed.positional) > 0 else None
    extra = parsed.positional[1:]
    if not name or len(extra) > 0:
        error(f"Usage: {APP_NAME} mcp remove <server> [-l]\n{HELP_HINT}")
        return 1
    project = "local" in parsed.values
    globalConfig = os.path.join(_mcpDir(options), "mcp.json")
    if project and projectConfig is None:
        # MISAKA fork: see `add`.
        error(f"There is no project at {options['cwd']} to hold a {home.DIR_NAME}/mcp.json.")
        return 1
    path = projectConfig if project and projectConfig is not None else globalConfig
    scope = "project" if project else "global"
    try:
        removed = removeMcpServerConfig(path, name)
    except Exception as removeError:  # noqa: BLE001 - upstream `catch (removeError)`: reported, exit 1
        error(f"Could not update {path}: {errorMessage(removeError)}")
        return 1
    if removed:
        log(f'Removed {scope} MCP server "{name}" from {path}.')
        return 0
    other = next(
        (
            server
            for server in loadMcpConfig({
                "agentDir": _mcpDir(options),
                "cwd": options["cwd"],
                "projectTrusted": True,
            })["servers"]
            if server["name"] == name and server.get("scope") != scope
        ),
        None,
    )
    hint = ""
    if other is not None:
        hint = f" It is defined in {other['source']}{'; use --local' if other.get('scope') == 'project' else '; omit --local'}."
    error(f'No {scope} MCP server named "{name}" in {path}.{hint}')
    return 1


async def _list(
    loaded: LoadedMcpConfig,
    json: bool,
    untrustedNote: str | None,
    options: McpCommandOptions,
    credentials: McpOAuthCredentialStore,
    log: Callable[[str], None],
) -> int:
    async def reportOf(entry: McpServerEntry) -> ServerReport:
        config = entry["config"]
        report = cast(ServerReport, {
            "name": entry["name"],
            "scope": entry.get("scope") or "global",
            "source": entry["source"],
            **({"override": entry["override"]} if entry.get("override") else {}),
            "enabled": config.get("enabled") is not False,
            "exposure": config.get("exposure") or "codemode",
            "transport": describeTransport(entry),
            "state": "disabled",
            "tools": [],
        })
        if not report["enabled"]:
            return report
        connection = createConnection(entry, options, credentials)
        try:
            await connection.getClient()
        except Exception:  # noqa: BLE001, S110 - the connection records the state and error
            pass
        report["state"] = connection.state
        report["tools"] = [tool["name"] for tool in connection.tools]
        overrides = [
            (tool["name"], exposure)
            for tool in connection.tools
            if (exposure := getMcpToolExposure(config, tool["name"])) != report["exposure"]
        ]
        if len(overrides) > 0:
            report["toolExposure"] = jsObjectOrder(dict(overrides))
        if connection.hasResources:
            report["resources"] = True
        if connection.state != "connected" and connection.error:
            report["error"] = connection.error
        await connection.close()
        return report

    reports = await asyncio.gather(*(reportOf(entry) for entry in loaded["servers"]))
    failed = len(loaded["errors"]) > 0 or any(
        entry["enabled"] and entry["state"] != "connected" for entry in reports
    )

    if json:
        log(
            jsonStringify(
                {
                    "servers": reports,
                    "errors": loaded["errors"],
                    **({"note": untrustedNote} if untrustedNote else {}),
                },
                2,
            )
        )
        return 1 if failed else 0
    if len(reports) == 0 and len(loaded["errors"]) == 0:
        log(
            f"No MCP servers configured. Add them to {os.path.join(_mcpDir(options), 'mcp.json')} or {home.DIR_NAME}/mcp.json."
        )
    for entry in reports:
        state = (
            f"connected, {len(entry['tools'])} tool{'' if len(entry['tools']) == 1 else 's'}"
            if entry["state"] == "connected"
            else ("needs sign-in" if entry["state"] == "needs-auth" else entry["state"])
        )
        log(f"{entry['name']}: {state} ({entry['exposure']}, {entry['scope']})")
        log(f"  {entry['transport']}")
        if entry.get("override"):
            log(f"  project override: {entry['override']}")
        if entry["state"] == "needs-auth":
            log(f"  sign in with: {_command(options)} login {entry['name']}")
        if len(entry["tools"]) > 0:
            overridden = entry.get("toolExposure") or {}
            tools = [f"{tool} [{overridden[tool]}]" if overridden.get(tool) else tool for tool in entry["tools"]]
            log(f"  tools: {', '.join(tools)}")
        if entry.get("resources"):
            log("  offers resources")
        if entry.get("error"):
            log(f"  {entry['error'].replace(chr(10), chr(10) + '  ')}")
    for configError in loaded["errors"]:
        log(f"config error: {configError}")
    if untrustedNote:
        log(untrustedNote)
    return 1 if failed else 0


async def login(
    entry: McpServerEntry,
    connection: McpServerConnection,
    url: str,
    timeoutMs: float,
    options: McpCommandOptions,
    credentials: McpOAuthCredentialStore,
    log: Callable[[str], None],
    error: Callable[[str], None],
) -> int:
    name = entry["name"]
    # Connecting first answers whether a sign-in is needed and records the server's challenge.
    try:
        await connection.getClient()
        log(f'Already signed in to MCP server "{name}" ({len(connection.tools)} tools).')
        return 0
    except Exception:  # noqa: BLE001 - upstream `catch`: a failure other than needs-auth is reported below
        if connection.state != "needs-auth":
            failure = connection.error if connection.error is not None else "unknown error"
            error(f'MCP server "{name}" failed to connect: {failure}')
            return 1

    openUrl = options.get("openUrl") or openBrowser
    interactive = sys.stdin is not None and sys.stdin.isatty() and options.get("openUrl") is None

    class Prompt:
        def showAuthorizationUrl(self, authorizationUrl: Any) -> None:
            log(f'Sign in to MCP server "{name}" in your browser:\n{authorizationUrl.href}')
            openUrl(authorizationUrl.href)

        def promptForRedirectUrl(self, signal: Any) -> Any:
            return waitForRedirectUrl(signal, interactive)

    try:
        await signInMcpServer({
            "serverUrl": url,
            "store": credentials.forServer(name, url),
            "settings": connection.oauthSettings(),
            "challenge": connection.challenge,
            "prompt": Prompt(),
            "signal": AbortSignal.timeout(timeoutMs),
        })
    except Exception as signInError:  # noqa: BLE001 - upstream `catch (signInError)`: reported, exit 1
        error(
            f'Sign-in to MCP server "{name}" was cancelled or not completed within {jsRound(timeoutMs / 1000)} seconds.'
            if isinstance(signInError, McpSignInCancelledError)
            else f'Sign-in to MCP server "{name}" failed: {errorMessage(signInError)}'
        )
        return 1
    connection.challenge = None
    try:
        await connection.reconnect()
    except Exception as connectError:  # noqa: BLE001 - upstream `catch (connectError)`: reported, exit 1
        error(f"Signed in, but {errorMessage(connectError)}")
        return 1
    log(f'Signed in to MCP server "{name}" ({len(connection.tools)} tools).')
    return 0


async def waitForRedirectUrl(signal: Any, interactive: bool) -> str | None:
    """The pasted redirect URL in a terminal; otherwise only the browser callback can finish the sign-in.
    Resolves to ``None`` when ``signal`` aborts: the callback arrived, or the sign-in timed out."""
    if not interactive:
        if not signal_aborted(signal):
            await wait_for_abort(signal)
        return None
    # MISAKA fork: stdin has no abortable `question`; a daemon thread reads the line and the race
    # against the abort gives up on it.
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[str] = loop.create_future()

    def deliver(result: str | BaseException) -> None:
        if answer.done():
            return
        if isinstance(result, BaseException):
            answer.set_exception(result)
        else:
            answer.set_result(result)

    def read() -> None:
        try:
            line = sys.stdin.readline()
        except Exception as readError:  # noqa: BLE001 - handed to the waiter
            outcome: str | BaseException = readError
        else:
            outcome = line.rstrip("\r\n")
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(deliver, outcome)

    sys.stderr.write("If the browser cannot reach this machine, paste the URL it was redirected to: ")
    sys.stderr.flush()
    threading.Thread(target=read, name="mcp-login-stdin", daemon=True).start()
    try:
        return await raceAbort(answer, signal)
    except Exception:  # noqa: BLE001 - upstream `catch`: an aborted or failed question cancels the sign-in
        # readline ends the abandoned prompt line when the question is aborted.
        sys.stderr.write("\r\n")
        sys.stderr.flush()
        return None


__all__ = ["McpCommandOptions", "runMcpCommand"]
