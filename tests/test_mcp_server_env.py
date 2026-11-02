"""A stdio MCP server does not inherit MISAKA's API keys (issue #10, the key-channel report).

MISAKA loads the home's .env -- search keys, the OpenAlex key -- into its environment, and every
MCP server started with all of it. A server now gets Hermes' safe names, the proxy and certificate
variables, and its own configured env, where ${VAR} takes a value from the environment (pi's
``resolveConfigValue``, so an unset variable fails the server, as upstream)."""
import asyncio
import os
import sys

import pytest

from misaka.core.mcp_servers import validateMcpServerConfig
from misaka.extensions.mcp import _configLiteral
from misaka.extensions.mcp.environment import inheritedEnvironment
from misaka.extensions.mcp.runtime import createDefaultTransport
from misaka.mcp.transports import _cross_spawn

ENVIRON = {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "LC_CTYPE": "UTF-8", "XDG_CACHE_HOME": "/c",
           "HTTPS_PROXY": "http://127.0.0.1:7890", "no_proxy": "localhost", "SSL_CERT_FILE": "/ca.pem",
           "MISAKA_HOME": "/m", "OPENAI_API_KEY": "sk-secret", "EXA_API_KEY": "exa-secret",
           "GITHUB_TOKEN": "ghp-secret", "MISAKA_USAGE_DB": "/m/board.db", "Path": "C:\\Windows", "SystemRoot": "C:\\Windows"}


def test_a_server_gets_the_safe_names_and_no_key():
    env = inheritedEnvironment(ENVIRON)
    assert {"PATH", "HOME", "LANG", "LC_CTYPE", "XDG_CACHE_HOME", "HTTPS_PROXY", "no_proxy", "SSL_CERT_FILE",
            "MISAKA_HOME", "SystemRoot"} <= set(env)
    assert not {"OPENAI_API_KEY", "EXA_API_KEY", "GITHUB_TOKEN", "MISAKA_USAGE_DB"} & set(env)


def _entry(env):
    return {"name": "s", "scope": "user", "config": {"command": "python", "args": ["-c", "pass"], "env": env}}


def test_a_key_a_server_needs_is_named_in_its_env(monkeypatch):
    for key in list(__import__("os").environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in ENVIRON.items():
        monkeypatch.setenv(key, value)
    transport = createDefaultTransport(_entry({"GITHUB_TOKEN": "${GITHUB_TOKEN}"}), ".", None)
    assert transport.options["inheritEnv"] is False
    env = transport.options["env"]
    assert env["GITHUB_TOKEN"] == "ghp-secret" and env["HOME"] == "/home/u"
    assert "OPENAI_API_KEY" not in env and "EXA_API_KEY" not in env
    with pytest.raises(RuntimeError, match="MISSING"):
        createDefaultTransport(_entry({"MODE": "x-${MISSING}"}), ".", None)


def test_the_spawned_server_gets_only_what_it_was_given(monkeypatch):
    """The whole path: the stdio transport passes the allowlisted environment to the process."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    seen = {}

    def spawn(command, args, options):
        seen.update(options["env"])
        raise FileNotFoundError(command)

    monkeypatch.setattr(_cross_spawn, "spawn", spawn)
    transport = createDefaultTransport(_entry({"MODE": "a"}), ".", None)
    with pytest.raises(FileNotFoundError):
        asyncio.run(transport.start())
    assert seen.get("MODE") == "a" and "OPENAI_API_KEY" not in seen


def test_an_npx_or_uvx_server_reaches_the_registry_the_user_set():
    """0.18.9 dropped the package managers' settings: behind a registry mirror or an npm proxy, an
    `npx -y` server fetched itself from the default registry and timed out."""
    mirrors = {"npm_config_registry": "https://registry.npmmirror.com", "npm_config_https_proxy": "http://p:1",
               "UV_INDEX_URL": "https://mirrors.example/simple", "PIP_INDEX_URL": "https://mirrors.example/simple",
               "UV_NATIVE_TLS": "1", "PYTHONUTF8": "1", "NODE_OPTIONS": "--use-openssl-ca"}
    env = inheritedEnvironment({**ENVIRON, **mirrors})
    assert mirrors.items() <= env.items()
    assert "OPENAI_API_KEY" not in env


def test_a_null_value_in_a_servers_env_is_a_config_error():
    """pi validates env as names to strings; a null value is reported, not passed on as "None"."""
    result = validateMcpServerConfig("s", {"command": "x", "env": {"TOKEN": None, "MODE": "a"}})
    assert isinstance(result, str) and "env must map names to strings" in result


def test_a_registrys_credentials_stay_behind_with_the_ssh_agent():
    secrets = {"npm_config_//registry.npmjs.org/:_authToken": "npm-secret", "NPM_CONFIG__AUTH": "b64",
               "UV_INDEX_1_PASSWORD": "pw", "YARN_NPM_AUTH_TOKEN": "y", "UV_PUBLISH_TOKEN": "t",
               "PIP_CLIENT_CERT": "/c.pem", "SSH_AUTH_SOCK": "/tmp/agent.sock", "NPM_CONFIG_KEY": "-----BEGIN",
               "PIP_INDEX_URL": "https://user:pw@mirror.example/simple"}
    kept = {"npm_config_registry": "https://r", "UV_KEYRING_PROVIDER": "subprocess", "PIP_TRUSTED_HOST": "m",
            "npm_config_strict_ssl": "false", "UV_INDEX_STRATEGY": "unsafe-best-match"}
    env = inheritedEnvironment({**ENVIRON, **secrets, **kept})
    assert not set(secrets) & set(env) and kept.items() <= env.items()


@pytest.mark.parametrize("value", ["C:/Users/a$b/profile", "!odd", "plain", "$$x"])
def test_a_role_value_reaches_the_server_literally(value):
    """A profile path is not a config value: a "$" in it is not a variable, a "!" not a command."""
    transport = createDefaultTransport(_entry({"MISAKA_PROFILE_DIR": _configLiteral(value)}), ".", None)
    assert transport.options["env"]["MISAKA_PROFILE_DIR"] == value


def test_a_role_session_spawns_its_servers_with_the_allowlist_and_its_own_names(monkeypatch):
    """The glue misaka's sessions load (createMisakaMcpExtension) builds its stdio servers through
    createDefaultTransport, so the role's names join the allowlisted environment and nothing else."""
    import types

    import misaka.extensions.mcp as mcp_extension

    for key in list(__import__("os").environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in ENVIRON.items():
        monkeypatch.setenv(key, value)
    captured = {}

    def createMcpExtension(options):
        captured.update(options)
        return lambda _pi: None

    monkeypatch.setattr(mcp_extension, "createMcpExtension", createMcpExtension)
    mcp_extension.createMisakaMcpExtension(types.SimpleNamespace(role="10032", profile_dir="/roles/a$b"))
    transport = captured["createTransport"](_entry({"MODE": "a"}), ".", None)

    assert transport.options["inheritEnv"] is False
    env = transport.options["env"]
    # Windows keeps the name as the process started with it (Path), as Node does (processEnv).
    assert env["HOME"] == "/home/u" and env["MODE"] == "a" and _cross_spawn.pathKey(env) in env
    assert env["MISAKA_WHO"] == "10032" and env["MISAKA_MCP_ROLE"] == "10032"
    assert env["MISAKA_PROFILE_DIR"] == "/roles/a$b" and "PYTHONUNBUFFERED" not in env
    assert not {"OPENAI_API_KEY", "EXA_API_KEY", "GITHUB_TOKEN", "MISAKA_USAGE_DB"} & set(env)

    seen = {}

    def spawn(command, args, options):
        seen.update(options["env"])
        raise FileNotFoundError(command)

    monkeypatch.setattr(_cross_spawn, "spawn", spawn)
    with pytest.raises(FileNotFoundError):
        asyncio.run(transport.start())
    assert seen.get("MISAKA_WHO") == "10032" and "OPENAI_API_KEY" not in seen


def test_a_server_env_naming_path_as_windows_spells_it_replaces_the_inherited_one(monkeypatch):
    """Python capitalises Windows names in os.environ (PATH), where Node keeps the spelling the process started with
    (Path). Spelled back as Node spells it, a server env naming `Path` replaces the inherited value, as in pi,
    instead of standing beside it and losing to it when the child's environment drops duplicate names."""
    monkeypatch.setattr(_cross_spawn, "isWin", True)
    monkeypatch.setattr(_cross_spawn, "_startupNames", lambda: ["Path", "SystemRoot", "HOME"])
    for key in list(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"PATH": "C:\\Windows\\system32", "SYSTEMROOT": "C:\\Windows", "HOME": "C:\\Users\\u"}.items():
        monkeypatch.setenv(key, value)

    env = createDefaultTransport(_entry({"Path": "C:\\mytools"}), ".", None).options["env"]

    assert {key: value for key, value in env.items() if key.upper() == "PATH"} == {"Path": "C:\\mytools"}
    assert env["SystemRoot"] == "C:\\Windows" and env["HOME"] == "C:\\Users\\u"
    child = _cross_spawn._windowsEnv(env)
    assert {key: value for key, value in child.items() if key.upper() == "PATH"} == {"Path": "C:\\mytools"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows spelling")
def test_the_inherited_names_are_spelled_as_windows_started_with_them():
    import nt

    env = _cross_spawn.processEnv()
    assert [name for name in nt.environ if name.upper() in os.environ and name not in env] == []
