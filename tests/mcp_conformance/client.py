"""Client under test for the official MCP client conformance suite. The suite starts a scenario server
and runs this script with the server URL as the last argument, the scenario name in
``MCP_CONFORMANCE_SCENARIO``, and scenario data in ``MCP_CONFORMANCE_CONTEXT``.

It drives the code misaka runs for an ``mcp.json`` HTTP server: ``McpServerConnection`` connects and calls
tools, and ``signInMcpServer`` runs the OAuth sign-in that ``/mcp`` starts. The browser is simulated: the
authorization URL is fetched without following its redirect, and the redirect is delivered to the
loopback callback server. Credentials stay in memory.

Run through ``run.py``, which writes the outcome to ``MISAKA_MCP_CONFORMANCE_REPORT``.

Translated from pi's ``coding-agent/test/mcp-conformance/client.ts``. MISAKA fork: the report variable and
the log prefix carry misaka's name; the check id stays ``pi-client``, as the baseline names it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from misaka.core.auth_storage import InMemoryAuthStorageBackend
from misaka.extensions.mcp.runtime import (
    McpOAuthCredentialStore,
    McpServerConnection,
    createDefaultTransport,
    signInMcpServer,
)
from misaka.utils import whatwg_fetch
from misaka.utils._abort_signal import wait_for_abort
from misaka.utils.js import errorMessage, jsonStringify
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL

# Sign-ins misaka asks the user for are not limited, so a user who keeps approving them would loop
# forever against a server that never accepts the granted scope. This simulated user gives up after
# three, the limit `auth/scope-retry-limit` checks.
MAX_SIGN_INS = 3
REQUEST_TIMEOUT_SECONDS = 20


def toolCalls(scenario: str, connection: McpServerConnection) -> list[dict[str, Any]] | None:
    """Tool calls of a scenario after connecting. ``None`` for scenarios this client does not know."""
    if scenario.startswith("auth/"):
        return [{"name": "test-tool", "arguments": {}}]
    if scenario == "initialize":
        return []
    if scenario == "tools_call":
        return [{"name": "add_numbers", "arguments": {"a": 2, "b": 3}}]
    if scenario == "sse-retry":
        return [{"name": "test_reconnection", "arguments": {}}]
    if scenario == "elicitation-sep1034-client-defaults":
        return [{"name": "test_client_elicitation_defaults", "arguments": {}}]
    if scenario == "json-schema-2020-12-preservation":
        # The server compares the echoed schema with the one it listed, to detect dropped keywords.
        tool = next((candidate for candidate in connection.tools if candidate["name"] == "json_schema_2020_12_tool"), None)
        if tool is None:
            raise RuntimeError("json_schema_2020_12_tool was not listed")
        return [{"name": "json_schema_echo", "arguments": {"schema": tool["inputSchema"]}}]
    return None


def log(message: str) -> None:
    sys.stderr.write(f"[misaka-conformance] {message}\n")


def readContext() -> dict[str, Any]:
    raw = os.environ.get("MCP_CONFORMANCE_CONTEXT")
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("MCP_CONFORMANCE_CONTEXT is not an object")  # noqa: TRY004 - upstream throws a plain Error
    return parsed


def isLoopback(url: URL) -> bool:
    return url.hostname in ("localhost", "127.0.0.1", "[::1]")


async def visitAuthorizationUrl(url: URL) -> None:
    """What a browser does with the authorization URL when the user approves at once."""
    response = await whatwg_fetch.fetch(url, {"redirect": "manual"})
    location = response.headers.get("location")
    if not location:
        raise RuntimeError(f"Authorization endpoint answered {response.status} without a redirect")
    callback = URL(location, url)
    if callback.protocol != "http:" or not isLoopback(callback):
        raise RuntimeError(f"Authorization endpoint redirected to {callback.origin}, not to the loopback callback")
    log(f"delivering authorization response to {callback.origin}{callback.pathname}")
    await (await whatwg_fetch.fetch(callback)).text()


class SimulatedBrowser:
    def __init__(self) -> None:
        self._visit: asyncio.Task[None] | None = None

    def showAuthorizationUrl(self, url: URL) -> None:
        log(f"authorization URL: {url.origin}{url.pathname}")
        self._visit = asyncio.ensure_future(visitAuthorizationUrl(url))

    async def promptForRedirectUrl(self, signal: Any) -> str | None:
        try:
            if self._visit is not None:
                await self._visit
        except Exception as error:  # noqa: BLE001 - cancels the sign-in instead of waiting for a callback that never comes
            log(f"browser failed: {errorMessage(error)}")
            return None
        if not signal_aborted(signal):
            await wait_for_abort(signal)
        return None


async def run(serverUrl: str, scenario: str) -> None:
    context = readContext()
    config: dict[str, Any] = {"url": serverUrl, "timeout": REQUEST_TIMEOUT_SECONDS}
    if isinstance(context.get("client_id"), str):
        # Pre-registered clients are configured in `mcp.json`.
        config["oauth"] = {
            "clientId": context["client_id"],
            **({"clientSecret": context["client_secret"]} if isinstance(context.get("client_secret"), str) else {}),
        }
    entry: Any = {"name": "conformance", "config": config, "source": "conformance"}
    credentials = McpOAuthCredentialStore(InMemoryAuthStorageBackend())
    connection = McpServerConnection({
        "entry": entry,
        "cwd": tempfile.gettempdir(),
        "createTransport": createDefaultTransport,
        "credentials": credentials,
        "onTools": lambda _connection: None,
    })

    signIns = 0

    async def withSignIn(operation: Any) -> Any:
        """Run ``operation``, signing in like a user answering ``/mcp`` whenever the server asks for it."""
        nonlocal signIns
        while True:
            try:
                return await operation()
            except Exception:
                url = connection.oauthUrl
                if connection.state != "needs-auth" or not url:
                    raise
                if signIns >= MAX_SIGN_INS:
                    raise RuntimeError(f"Still requires sign-in after {signIns} sign-ins") from None
                signIns += 1
                scope = (connection.challenge or {}).get("scope")
                log(f"sign-in {signIns}{f' (scope: {scope})' if scope else ''}")
                await signInMcpServer({
                    "serverUrl": url,
                    "store": credentials.forServer(entry["name"], url),
                    "settings": connection.oauthSettings(),
                    "challenge": connection.challenge,
                    "prompt": SimulatedBrowser(),
                })
                connection.challenge = None

    try:
        await withSignIn(lambda: connection.getClient())
        log(f"connected, tools: {', '.join(tool['name'] for tool in connection.tools) or '(none)'}")
        calls = toolCalls(scenario, connection)
        if calls is None:
            raise RuntimeError(f"Unknown scenario {scenario}")
        for call in calls:
            result = await withSignIn(lambda call=call: connection.callTool(call["name"], call["arguments"], {}))
            log(f"{call['name']}: {jsonStringify(result['content'])}")
            if result.get("isError"):
                raise RuntimeError(f"Tool {call['name']} failed: {jsonStringify(result['content'])}")
    finally:
        await connection.close()


async def main() -> int:
    serverUrl = sys.argv[-1] if len(sys.argv) >= 2 else None
    scenario = os.environ.get("MCP_CONFORMANCE_SCENARIO")
    reportPath = os.environ.get("MISAKA_MCP_CONFORMANCE_REPORT")
    report: dict[str, Any]
    if not serverUrl or not scenario:
        report = {"success": False, "error": "Usage: MCP_CONFORMANCE_SCENARIO=<scenario> client.py <server-url>"}
    else:
        try:
            await run(serverUrl, scenario)
            report = {"success": True}
        except Exception as error:  # noqa: BLE001 - the report records it
            report = {"success": False, "error": errorMessage(error)}
    if report.get("error"):
        log(f"error: {report['error']}")
    if reportPath:
        Path(reportPath).write_text(f"{json.dumps(report, indent=2)}\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    # Exit explicitly: a failed scenario can leave sockets of the server under test open.
    os._exit(asyncio.run(main()))
