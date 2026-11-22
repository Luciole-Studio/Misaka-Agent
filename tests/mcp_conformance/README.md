# MCP client conformance

Runs the official [MCP conformance suite](https://github.com/modelcontextprotocol/conformance) against MISAKA's MCP client and compares the result with a committed baseline.

```bash
python tests/mcp_conformance/run.py
```

It is not part of the test gate. It needs Node.js for `npx`, network access the first time to fetch the pinned suite (`@modelcontextprotocol/conformance@0.2.0-alpha.11`), and a POSIX shell. Install scripts are disabled.

## How it works

For every protocol version MISAKA negotiates (`2025-03-26`, `2025-06-18`, `2025-11-25`), `run.py` lists the suite's client scenarios and runs them one at a time. The suite starts a scenario server and runs `client.py` against it.

`client.py` uses the code MISAKA runs for an HTTP server in `mcp.json`: `McpServerConnection` connects and calls tools, and `signInMcpServer` runs the OAuth sign-in that `/mcp` starts. A simulated browser fetches the authorization URL and delivers the redirect to the loopback callback server. When the server asks for sign-in again (for example for more scope), the simulated user signs in again, up to three times.

Every check the suite reports is compared with `baseline.json`, which is pi's. The extra `pi-client` check records whether `client.py` completed the scenario. Some scenarios expect the client to give up (`auth/scope-retry-limit`), so it is baselined like the others.

The run fails when:

- a check that passes in the baseline fails or is missing,
- a check fails that the baseline does not list as failing,
- a baselined scenario did not run.

## Options

```bash
python tests/mcp_conformance/run.py --mode 2025-11-25 --scenario auth/scope-step-up --verbose
python tests/mcp_conformance/run.py --update-baseline
```

- `--mode`, `--scenario`: run a subset (repeatable).
- `--verbose`: print the suite's output, including each check and the client's log.
- `--keep-results`: keep the suite's `checks.json` and client output.
- `--update-baseline`: write the results of a full run to `baseline.json`. Review the diff; do not regenerate it to hide a regression.

## Known failures

- `elicitation-sep1034-client-defaults`: elicitation is not supported.
- `auth/basic-cimd`: MISAKA registers clients dynamically; a Client ID Metadata Document is refused in configuration.
- `auth/scope-retry-limit`: `pi-client` fails by design; the server never accepts the granted scope.
