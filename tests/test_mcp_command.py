"""pi's ``test/mcp-command.test.ts``: ``misaka mcp`` outside a session."""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

from misaka.config import home
from misaka.config.engine import APP_NAME
from misaka.extensions.mcp.cli import runMcpCommand
from tests.mcp_helpers import closeServers

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp" / "stdio_server.py")


@pytest.fixture(autouse=True)
def _noProxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
async def _servers():
    yield
    await closeServers()


async def run(args: list[str], servers: dict | None, agentDir: str | None = None, *, tmp_path=None):
    if agentDir is None:
        agentDir = str(tmp_path / f"agent-{len(os.listdir(tmp_path))}")
        os.makedirs(agentDir)
    if servers is not None:
        Path(agentDir, "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    output: list[str] = []
    exitCode = await runMcpCommand(args, {"cwd": agentDir, "agentDir": agentDir, "log": output.append, "error": output.append})
    return exitCode, "\n".join(output), agentDir


def readConfig(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


SERVERS = {
    "fixture": {"command": sys.executable, "args": [FIXTURE]},
    "broken": {"command": "misaka-test-missing-mcp-server"},
    "parked": {"command": sys.executable, "args": [FIXTURE], "enabled": False},
    "bad": {"args": ["no command"]},
}


async def test_lists_servers_with_their_state_tools_and_errors_and_fails_while_anything_is_wrong(tmp_path):
    exitCode, output, _agentDir = await run(["list"], SERVERS, tmp_path=tmp_path)
    assert exitCode == 1
    assert "fixture: connected, 1 tool (codemode, global)\n" in output
    assert "  tools: echo" in output
    if sys.platform != "win32":
        assert (
            "broken: failed (codemode, global)\n  misaka-test-missing-mcp-server\n"
            "  spawn misaka-test-missing-mcp-server ENOENT"
        ) in output
    else:
        # On Windows cross-spawn runs the command through cmd.exe, which reports the missing program itself.
        assert "broken: failed (codemode, global)\n  misaka-test-missing-mcp-server\n" in output
    assert "parked: disabled (codemode, global)" in output
    assert "config error: " in output
    assert 'server "bad" needs either "command"' in output

    ok, _output, _dir = await run(["list"], {"fixture": SERVERS["fixture"]}, tmp_path=tmp_path)
    assert ok == 0


async def test_prints_json_for_scripts(tmp_path):
    exitCode, output, _agentDir = await run(
        ["list", "--json"], {"fixture": SERVERS["fixture"], "parked": SERVERS["parked"]}, tmp_path=tmp_path
    )
    assert exitCode == 0
    parsed = json.loads(output)
    assert [{"name": s["name"], "state": s["state"], "tools": s["tools"]} for s in parsed["servers"]] == [
        {"name": "fixture", "state": "connected", "tools": ["echo"]},
        {"name": "parked", "state": "disabled", "tools": []},
    ]


async def test_rejects_unknown_servers_and_servers_without_oauth_for_login_and_logout(tmp_path):
    exitCode, output, _dir = await run(["login", "nope"], SERVERS, tmp_path=tmp_path)
    assert (exitCode, output) == (1, 'No MCP server named "nope". Configured: fixture, broken, parked.')
    exitCode, output, _dir = await run(["logout", "fixture"], SERVERS, tmp_path=tmp_path)
    assert (exitCode, output) == (
        1,
        'MCP server "fixture" does not use OAuth. Only HTTP servers without an Authorization header do.',
    )
    assert (await run(["frobnicate"], SERVERS, tmp_path=tmp_path))[0] == 1


async def test_adds_stdio_servers_and_passes_options_after_the_command_through(tmp_path):
    exitCode, output, agentDir = await run(
        ["add", "--env", "A=1", "--env", "B=x=y", "files", "--", "npx", "-y", "server", "--root", "."],
        None,
        tmp_path=tmp_path,
    )
    assert exitCode == 0
    assert 'Added global MCP server "files"' in output
    assert readConfig(os.path.join(agentDir, "mcp.json")) == {
        "mcpServers": {"files": {"command": "npx", "args": ["-y", "server", "--root", "."], "env": {"A": "1", "B": "x=y"}}},
    }

    # Without `--`, options after the command belong to the command too.
    _exitCode, output, _dir = await run(["add", "files", "node", "server.js", "--port", "1"], None, agentDir)
    assert 'Replaced global MCP server "files"' in output
    assert readConfig(os.path.join(agentDir, "mcp.json")) == {
        "mcpServers": {"files": {"command": "node", "args": ["server.js", "--port", "1"]}},
    }


async def test_adds_http_servers_and_keeps_other_content_of_the_file(tmp_path):
    exitCode, output, agentDir = await run(
        [
            "add",
            "docs",
            "--url",
            "https://example.com/mcp",
            "--bearer-token-env-var",
            "DOCS_TOKEN",
            "--header",
            "X-Team=core",
            "--exposure",
            "direct",
            "--description",
            "Product docs",
        ],
        {"fixture": SERVERS["fixture"]},
        tmp_path=tmp_path,
    )
    assert exitCode == 0
    assert "mcp login" not in output
    assert readConfig(os.path.join(agentDir, "mcp.json")) == {
        "mcpServers": {
            "fixture": SERVERS["fixture"],
            "docs": {
                "url": "https://example.com/mcp",
                "headers": {"X-Team": "core", "Authorization": "Bearer ${DOCS_TOKEN}"},
                "exposure": "direct",
                "description": "Product docs",
            },
        },
    }

    _exitCode, output, _dir = await run(
        ["add", "sentry", "--url", "https://mcp.sentry.dev/mcp", "--oauth-client-id", "pi", "--oauth-client-name", "Claude Code"],
        None,
        agentDir,
    )
    assert f"If it requires sign-in: {APP_NAME} mcp login sentry" in output
    assert readConfig(os.path.join(agentDir, "mcp.json"))["mcpServers"]["sentry"] == {
        "url": "https://mcp.sentry.dev/mcp",
        "oauth": {"clientId": "pi", "clientName": "Claude Code"},
    }


async def test_rejects_invalid_add_invocations_without_writing(tmp_path):
    cases = [
        ["add", "x"],
        ["add", "x", "--url", "https://example.com", "--", "cmd"],
        ["add", "bad name", "--", "cmd"],
        ["add", "x", "--url", "ftp://example.com"],
        ["add", "x", "--env", "A=1", "--url", "https://example.com"],
        ["add", "x", "--header", "A=1", "--", "cmd"],
        ["add", "x", "--env", "NOVALUE", "--", "cmd"],
        ["add", "x", "--exposure", "loud", "--", "cmd"],
    ]
    for args in cases:
        exitCode, _output, agentDir = await run(args, None, tmp_path=tmp_path)
        assert exitCode == 1, " ".join(args)
        assert not os.path.exists(os.path.join(agentDir, "mcp.json"))


async def test_adds_and_removes_project_servers(tmp_path):
    _exitCode, output, agentDir = await run(["add", "-l", "local", "--", "node", "server.js"], None, tmp_path=tmp_path)
    assert "The project is not trusted" in output
    projectDir = home.project_dir(agentDir)
    assert projectDir is not None
    projectConfig = str(projectDir / "mcp.json")
    assert readConfig(projectConfig) == {"mcpServers": {"local": {"command": "node", "args": ["server.js"]}}}

    exitCode, output, _dir = await run(["remove", "local"], None, agentDir)
    assert exitCode == 1
    assert f"It is defined in {projectConfig}; use --local." in output

    exitCode, output, _dir = await run(["remove", "local", "--local"], None, agentDir)
    assert exitCode == 0
    assert 'Removed project MCP server "local"' in output
    assert readConfig(projectConfig) == {"mcpServers": {}}


async def test_removes_global_servers(tmp_path):
    exitCode, _output, agentDir = await run(["remove", "broken"], SERVERS, tmp_path=tmp_path)
    assert exitCode == 0
    assert list(readConfig(os.path.join(agentDir, "mcp.json"))["mcpServers"]) == ["fixture", "parked", "bad"]
    exitCode, output, _dir = await run(["remove", "broken"], None, agentDir)
    assert exitCode == 1
    assert 'No global MCP server named "broken"' in output


def test_the_command_reads_no_project_settings_before_the_project_is_trusted(tmp_path, monkeypatch):
    import misaka.core.http_dispatcher as dispatcher
    import misaka.extensions.mcp as mcp_extension
    from misaka.extensions.mcp import cli_lazy

    project = tmp_path / "project"
    (project / ".misaka").mkdir(parents=True)
    (project / ".misaka" / "settings.json").write_text('{"httpProxy": "http://project-proxy:1"}', encoding="utf-8")
    monkeypatch.chdir(project)
    proxies: list = []
    monkeypatch.setattr(dispatcher, "applyHttpProxySettings", proxies.append)

    async def runMcpCommand(_argv, _options):
        return 0

    monkeypatch.setattr(cli_lazy, "loadMcpCommand", lambda: types.SimpleNamespace(runMcpCommand=runMcpCommand))

    assert mcp_extension.command(["list"]) == 0
    assert proxies == [None]
    assert sorted(os.listdir(project / ".misaka")) == ["settings.json"]


def test_as_picks_a_sisters_own_mcp_json_and_last_order_s_is_the_default(monkeypatch, tmp_path, capsys):
    """MISAKA fork: like `hermes -p <profile> mcp`, `--as <Sister>` (before `--`) uses her profile's `mcp.json`;
    without it, Last Order's. The home keeps none."""
    import misaka.core.http_dispatcher as dispatcher
    import misaka.extensions.mcp as mcp_extension

    monkeypatch.setattr(dispatcher, "applyHttpProxySettings", lambda _proxy: None)
    monkeypatch.setattr(dispatcher, "configureHttpDispatcher", lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISAKA_PROFILE_DIR", raising=False)
    sister = home.path("profiles_root") / "10032"
    sister.mkdir(parents=True)

    assert mcp_extension.command(["--as", "10032", "add", "docs", "--", "docs-mcp", "--as", "kept"]) == 0
    assert readConfig(str(sister / "mcp.json"))["mcpServers"]["docs"]["args"] == ["--as", "kept"]
    assert f"{APP_NAME} mcp --as 10032 list" in capsys.readouterr().out

    assert mcp_extension.command(["add", "web", "--", "web-mcp"]) == 0
    assert list(readConfig(str(home.path("roles_root") / "last_order" / "mcp.json"))["mcpServers"]) == ["web"]
    assert list(readConfig(str(sister / "mcp.json"))["mcpServers"]) == ["docs"]
    assert not home.path("mcp_config").exists()

    assert mcp_extension.command(["--as", "20001", "list"]) == 1
    assert "Unknown Sister '20001'" in capsys.readouterr().err


def test_global_writes_the_file_every_role_reads_and_the_roles_own_is_the_default(monkeypatch, tmp_path, capsys):
    """MISAKA fork: `-g` writes the home's global mcp.json, which every role reads; without it the command changes
    the role's own. `list` shows a Sister both, and `remove` names the option that reaches the file defining one."""
    import misaka.core.http_dispatcher as dispatcher
    import misaka.extensions.mcp as mcp_extension

    monkeypatch.setattr(dispatcher, "applyHttpProxySettings", lambda _proxy: None)
    monkeypatch.setattr(dispatcher, "configureHttpDispatcher", lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISAKA_PROFILE_DIR", raising=False)
    sister = home.path("profiles_root") / "10032"
    sister.mkdir(parents=True)
    lastOrder = home.path("roles_root") / "last_order"

    assert mcp_extension.command(["add", "-g", "shared", "--", "shared-mcp"]) == 0
    assert f'Added global MCP server "shared" in {home.path("mcp_config")}.' in capsys.readouterr().out
    assert mcp_extension.command(["add", "own", "--", "own-mcp"]) == 0
    assert f'Added role MCP server "own" in {lastOrder / "mcp.json"}.' in capsys.readouterr().out
    assert mcp_extension.command(["--as", "10032", "add", "hers", "--", "hers-mcp"]) == 0
    capsys.readouterr()

    assert mcp_extension.command(["--as", "10032", "list", "--json"]) == 1
    listed = {server["name"]: server["scope"] for server in json.loads(capsys.readouterr().out)["servers"]}
    assert listed == {"shared": "global", "hers": "role"}
    hers = json.loads((sister / "mcp.json").read_text(encoding="utf-8"))
    (sister / "mcp.json").write_text(json.dumps({"mcpServers": {**hers["mcpServers"], "shared": {"enabled": False}}}),
                                     encoding="utf-8")
    assert mcp_extension.command(["--as", "10032", "list"]) == 1
    assert f"shared: disabled (codemode, global)\n  shared-mcp\n  role override: {sister / 'mcp.json'}\n" in (
        capsys.readouterr().out)
    (sister / "mcp.json").write_text(json.dumps(hers), encoding="utf-8")

    assert mcp_extension.command(["--as", "10032", "remove", "shared"]) == 1
    assert f"It is defined in {home.path('mcp_config')}; use --global." in capsys.readouterr().err
    assert mcp_extension.command(["remove", "-g", "-l", "shared"]) == 1
    assert "Use either --global or --local." in capsys.readouterr().err
    assert mcp_extension.command(["--as", "10032", "remove", "-g", "shared"]) == 0
    assert readConfig(str(home.path("mcp_config")))["mcpServers"] == {}
    assert list(readConfig(str(lastOrder / "mcp.json"))["mcpServers"]) == ["own"]


def test_run_from_a_roles_process_the_command_is_hers_and_servers_get_her_names(monkeypatch, tmp_path):
    """MISAKA fork: a role's process, such as her bash tool, carries her `MISAKA_PROFILE_DIR`, as a Hermes profile's
    carries its HERMES_HOME. Without `--as` the command uses her `mcp.json` and sign-ins, so a sign-in she runs
    reaches her session; and wherever it runs, its stdio servers get the names her sessions give them."""
    import misaka.core.http_dispatcher as dispatcher
    import misaka.extensions.mcp as mcp_extension
    from misaka.extensions.mcp import cli_lazy

    monkeypatch.setattr(dispatcher, "applyHttpProxySettings", lambda _proxy: None)
    monkeypatch.setattr(dispatcher, "configureHttpDispatcher", lambda: None)
    monkeypatch.chdir(tmp_path)
    sister = home.path("profiles_root") / "10032"
    sister.mkdir(parents=True)
    lastOrder = str(home.path("roles_root") / "last_order")
    for name, value in (("MISAKA_PROFILE_DIR", str(sister)), ("MISAKA_MCP_ROLE", "10032"), ("MISAKA_WHO", "10032")):
        monkeypatch.setenv(name, value)

    assert mcp_extension.command(["add", "docs", "--", "docs-mcp"]) == 0
    assert list(readConfig(str(sister / "mcp.json"))["mcpServers"]) == ["docs"]
    assert not os.path.exists(os.path.join(lastOrder, "mcp.json"))

    captured: dict = {}

    async def runMcpCommand(_argv, options):
        captured.update(options)
        return 0

    monkeypatch.setattr(cli_lazy, "loadMcpCommand", lambda: types.SimpleNamespace(runMcpCommand=runMcpCommand))

    def role(argv):
        captured.clear()
        assert mcp_extension.command(argv) == 0
        env = captured["createTransport"]({"name": "s", "config": {"command": "server"}}, ".", None).options["env"]
        return captured["mcpDir"], env["MISAKA_PROFILE_DIR"], env["MISAKA_MCP_ROLE"], env["MISAKA_WHO"]

    assert role(["list"]) == (str(sister), str(sister), "10032", "10032")
    for name in ("MISAKA_PROFILE_DIR", "MISAKA_MCP_ROLE", "MISAKA_WHO"):
        monkeypatch.delenv(name)
    assert role(["list"]) == (lastOrder, lastOrder, "last-order", "last-order")
    assert role(["--as", "10032", "list"]) == (str(sister), str(sister), "10032", "10032")
