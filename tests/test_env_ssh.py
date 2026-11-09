"""pi env's ``test/ssh.test.ts``: the SSH bootstrap against a disposable ``sshd`` on localhost (skipped without one, and on
Windows, as upstream), and MISAKA's tests of the same path without ``sshd``: a stand-in ``ssh``
(``tests/fixtures/fake_ssh.py``) runs each remote command on this machine, so detection, the hash-verified deployment,
the launch and the connection all run for real."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.env import getOrThrow
from misaka.env import (
    Connection,
    HostKeyChangedError,
    HostKeyUnknownError,
    LocalExecutionEnv,
    RemoteExecutionEnv,
    acceptHostKey,
    connectSsh,
    forgetHostKey,
    scanHostKey,
    sshConnection,
)
from misaka.env.ssh import POSIX_PROBE, launchCommand, parseProbe, sshArguments
from tests.env_support import closeConnection, daemon, daemonCommand

context = BACKGROUND_CONTEXT
windows = sys.platform == "win32"

# The deployed daemon is named by its content.
deployedName = f"pi-env-{hashlib.sha256(Path(daemon).read_bytes()).hexdigest()[:32]}"


def which(program: str) -> str | None:
    return shutil.which(program)


sshd = None if windows else (which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None))


def freePort() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def waitForPort(port: int) -> None:
    for _ in range(100):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError("sshd did not start")


def keygen(file: str) -> None:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", file], check=True)


def fingerprint(file: str) -> str:
    return subprocess.run(["ssh-keygen", "-lf", file], capture_output=True, text=True, check=True).stdout.split(" ")[1]


def publicKey(file: str) -> str:
    return " ".join(Path(f"{file}.pub").read_text(encoding="utf-8").split(" ")[:2])


async def killProcess(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)
    await asyncio.sleep(0.3)


# A disposable sshd on localhost with its own host key, client key and home directory.
@pytest.fixture(scope="module")
def server() -> Iterator[dict[str, Any]]:
    root = tempfile.mkdtemp(prefix="pi-env-ssh-")
    home = os.path.join(root, "home")
    os.makedirs(home)
    keygen(os.path.join(root, "host_key"))
    keygen(os.path.join(root, "client_key"))
    shutil.copyfile(os.path.join(root, "client_key.pub"), os.path.join(root, "authorized_keys"))
    port = freePort()
    Path(root, "sshd_config").write_text(
        "\n".join(
            [
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {os.path.join(root, 'host_key')}",
                f"AuthorizedKeysFile {os.path.join(root, 'authorized_keys')}",
                f"PidFile {os.path.join(root, 'sshd.pid')}",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "StrictModes no",
                f"SetEnv HOME={home}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    Path(root, "ssh_config").write_text("", encoding="utf-8")
    process = subprocess.Popen(
        [sshd, "-D", "-e", "-f", os.path.join(root, "sshd_config")],  # type: ignore[list-item]
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    asyncio.run(waitForPort(port))
    target = {
        "host": "127.0.0.1",
        "port": port,
        "user": getpass.getuser(),
        "identityFile": os.path.join(root, "client_key"),
        "knownHostsFile": os.path.join(root, "known_hosts"),
        "hostKeyAlias": "pi-env-test",
        "configFile": os.path.join(root, "ssh_config"),
    }
    yield {"root": root, "home": home, "target": target}
    process.kill()
    process.wait()
    shutil.rmtree(root, ignore_errors=True)


needsSshd = pytest.mark.skipif(sshd is None, reason="no sshd on this machine")


@needsSshd
async def test_refuses_an_untrusted_host_then_deploys_and_runs_the_daemon_once_its_key_is_accepted(server):
    target, home, root = server["target"], server["home"], server["root"]
    with pytest.raises(HostKeyUnknownError):
        await connectSsh({**target, "binary": daemon})

    scanned = await scanHostKey(target)
    expected = fingerprint(os.path.join(root, "host_key.pub"))
    assert expected in "\n".join(scanned["fingerprints"])
    await acceptHostKey(target, scanned["lines"])

    tools = os.path.join(home, ".pi/mobile/tools")
    os.makedirs(tools, exist_ok=True)
    Path(tools, "pi-env-0123456789abcdef0123456789abcdef").write_text("old", encoding="utf-8")
    connected = await connectSsh({**target, "binary": daemon})
    connection, remote = connected["connection"], connected["remote"]
    try:
        assert remote["home"] == home
        assert Path(tools, deployedName).read_bytes() == Path(daemon).read_bytes()
        # Daemons of other contents are removed.
        assert [name for name in os.listdir(tools) if name.startswith("pi-env-")] == [deployedName]
        env = RemoteExecutionEnv({"connection": connection, "id": "pi-env:test", "cwd": home})
        getOrThrow(await env.writeFile("over-ssh.txt", "hello", context))
        assert getOrThrow(await env.readTextFile("over-ssh.txt", context)) == "hello"
        output: list[str] = []
        result = getOrThrow(
            await env.exec(
                ["sh", "-c", 'printf "%s" "$HOME"'], {"onOutput": lambda text, *_: output.append(text)}, context
            )
        )
        assert result["exitCode"] == 0
        assert "".join(output) == home
    finally:
        await closeConnection(connection)


@needsSshd
async def test_reuses_a_verified_daemon_and_replaces_a_tampered_one(server):
    target, home = server["target"], server["home"]
    file = os.path.join(home, ".pi/mobile/tools", deployedName)
    before = os.stat(file).st_mtime_ns
    await closeConnection((await connectSsh({**target, "binary": daemon}))["connection"])
    assert os.stat(file).st_mtime_ns == before

    Path(file).write_text("tampered", encoding="utf-8")
    connection = (await connectSsh({**target, "binary": daemon}))["connection"]
    try:
        assert Path(file).read_bytes() == Path(daemon).read_bytes()
        assert (await connection.info())["home"] == home
    finally:
        await closeConnection(connection)


@needsSshd
async def test_verifies_the_daemon_again_before_starting_it_after_a_lost_connection(server):
    target, home = server["target"], server["home"]
    connection = (await connectSsh({**target, "binary": daemon}))["connection"]
    file = os.path.join(home, ".pi/mobile/tools", deployedName)
    try:
        pid = (await connection.info())["pid"]
        await killProcess(pid)
        Path(file).write_text("tampered", encoding="utf-8")
        # The next start redeploys the verified daemon instead of running whatever is there.
        assert (await connection.info())["pid"] != pid
        assert Path(file).read_bytes() == Path(daemon).read_bytes()
    finally:
        await closeConnection(connection)


@needsSshd
async def test_lists_files_over_ssh_as_the_local_environment_lists_them(server):
    await listsFilesOverSsh(server["target"], server["home"])


async def listsFilesOverSsh(target: dict[str, Any], home: str) -> None:
    tree = os.path.join(home, "tree")
    for file in ["a/b.txt", "a/c/d.txt", ".hidden", "out/x.js", "skip/y.txt", ".git/HEAD"]:
        os.makedirs(os.path.dirname(os.path.join(tree, file)), exist_ok=True)
        Path(tree, file).write_text("", encoding="utf-8")
    os.mkdir(os.path.join(tree, "empty"))
    Path(tree, ".gitignore").write_text("out/\n", encoding="utf-8")
    connection = (await connectSsh({**target, "binary": daemon}))["connection"]
    local = Connection({"command": daemonCommand()})
    try:
        remote = RemoteExecutionEnv({"connection": connection, "id": "pi-env:test", "cwd": home})
        listing = getOrThrow(await remote.listFiles("tree", {"exclude": ["skip"]}, context))
        assert listing == {
            "entries": [
                {"path": ".gitignore", "kind": "file"},
                {"path": ".hidden", "kind": "file"},
                {"path": "a", "kind": "directory"},
                {"path": "a/b.txt", "kind": "file"},
                {"path": "a/c", "kind": "directory"},
                {"path": "a/c/d.txt", "kind": "file"},
                {"path": "empty", "kind": "directory"},
            ],
            "truncated": False,
        }
        env = LocalExecutionEnv({"connection": local, "cwd": home})
        assert getOrThrow(await env.listFiles("tree", {"exclude": ["skip"]}, context)) == listing
    finally:
        await closeConnection(connection)
        await closeConnection(local)


@needsSshd
async def test_starts_the_daemon_through_the_login_shell_only_when_asked(server):
    target, home = server["target"], server["home"]
    # The login shell is the account's shell: sh and bash read .profile, zsh (the macOS default) reads .zprofile.
    Path(home, ".profile").write_text("export PI_ENV_LOGIN=yes\n", encoding="utf-8")
    Path(home, ".zprofile").write_text("export PI_ENV_LOGIN=yes\n", encoding="utf-8")

    async def login(loginShell: bool) -> str:
        connection = (await connectSsh({**target, "binary": daemon, "loginShell": loginShell}))["connection"]
        try:
            env = RemoteExecutionEnv({"connection": connection, "id": "pi-env:test", "cwd": home})
            output: list[str] = []
            getOrThrow(
                await env.exec(
                    ["sh", "-c", 'printf "%s" "$PI_ENV_LOGIN"'],
                    {"onOutput": lambda text, *_: output.append(text)},
                    context,
                )
            )
            return "".join(output)
        finally:
            await closeConnection(connection)

    assert await login(False) == ""
    assert await login(True) == "yes"


@needsSshd
async def test_connects_lazily_and_reports_a_failed_start_as_the_error_of_the_operation(server):
    lazyTarget = {**server["target"], "knownHostsFile": os.path.join(server["root"], "lazy_known_hosts")}
    home = server["home"]
    lazy = sshConnection({**lazyTarget, "binary": daemon})
    connection, remote = lazy["connection"], lazy["remote"]
    try:
        assert remote() is None
        env = RemoteExecutionEnv({"connection": connection, "id": "pi-env:test", "cwd": home})
        # Nothing is trusted yet: each operation fails with the reason and tries again.
        read = await env.readTextFile("missing.txt", context)
        assert "not trusted" in ("ok" if read["ok"] else read["error"].message)
        command = await env.exec(["sh", "-c", "exit 0"], None, context)
        assert ("ok" if command["ok"] else command["error"].code) == "spawn_error"
        await acceptHostKey(lazyTarget, (await scanHostKey(lazyTarget))["lines"])
        accepted = await env.exec(["sh", "-c", "exit 0"], None, context)
        assert (accepted["value"]["exitCode"] if accepted["ok"] else accepted["error"].message) == 0
        assert remote()["home"] == home
    finally:
        await closeConnection(connection)


@needsSshd
async def test_accepts_only_host_keys_for_the_alias_and_never_replaces_a_trusted_key_silently(server):
    await acceptsOnlyHostKeysForTheAlias(server["root"], server["target"])


@needsSshd
async def test_refuses_a_changed_host_key(server):
    root, target = server["root"], server["target"]
    other = os.path.join(root, "other_key")
    keygen(other)
    knownHosts = os.path.join(root, "changed_known_hosts")
    Path(knownHosts).write_text(f"pi-env-test {publicKey(other)}\n", encoding="utf-8")
    with pytest.raises(HostKeyChangedError):
        await connectSsh({**target, "knownHostsFile": knownHosts, "binary": daemon})


async def acceptsOnlyHostKeysForTheAlias(root: str, target: dict[str, Any]) -> None:
    knownHosts = os.path.join(root, "accept_known_hosts")
    scoped = {**target, "knownHostsFile": knownHosts}
    keygen(os.path.join(root, "accept_first"))
    keygen(os.path.join(root, "accept_second"))
    first = publicKey(os.path.join(root, "accept_first"))
    second = publicKey(os.path.join(root, "accept_second"))
    with pytest.raises(ValueError):
        await acceptHostKey(scoped, [f"other-alias {first}"])
    with pytest.raises(ValueError):
        await acceptHostKey(scoped, [f"@cert-authority pi-env-test {first}"])
    await asyncio.gather(
        acceptHostKey(scoped, [f"pi-env-test {first}"]),
        acceptHostKey(scoped, [f"pi-env-test {first}"]),
    )
    assert Path(knownHosts).read_text(encoding="utf-8") == f"pi-env-test {first}\n"
    with pytest.raises(HostKeyChangedError):
        await acceptHostKey(scoped, [f"pi-env-test {second}"])
    await forgetHostKey(scoped)
    await acceptHostKey(scoped, [f"pi-env-test {second}"])
    assert Path(knownHosts).read_text(encoding="utf-8") == f"pi-env-test {second}\n"


# MISAKA: the same path without sshd.

needsPosix = pytest.mark.skipif(windows, reason="the stand-in ssh runs remote commands with a POSIX sh")
needsKeygen = pytest.mark.skipif(which("ssh-keygen") is None, reason="no ssh-keygen")


@pytest.fixture
def fake() -> Iterator[dict[str, Any]]:
    root = tempfile.mkdtemp(prefix="pi-env-fake-ssh-")
    home = os.path.join(root, "home")
    os.makedirs(home)
    program = os.path.join(root, "ssh")
    script = Path(__file__).parent / "fixtures" / "fake_ssh.py"
    Path(program).write_text(
        f'#!/bin/sh\nFAKE_SSH_HOME="{home}" exec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
    )
    os.chmod(program, stat.S_IRWXU)
    target = {
        "host": "fake-host",
        "ssh": program,
        "knownHostsFile": os.path.join(root, "known_hosts"),
        "hostKeyAlias": "pi-env-test",
    }
    yield {"root": root, "home": home, "target": target}
    shutil.rmtree(root, ignore_errors=True)


@needsKeygen
async def test_accepts_only_host_keys_for_the_alias_without_a_server():
    root = tempfile.mkdtemp(prefix="pi-env-keys-")
    try:
        await acceptsOnlyHostKeysForTheAlias(
            root, {"host": "fake-host", "knownHostsFile": os.path.join(root, "known_hosts"), "hostKeyAlias": "pi-env-test"}
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_ssh_arguments_refuse_options_in_fields_and_quote_paths():
    target = {"host": "box", "knownHostsFile": "/data/known%hosts", "hostKeyAlias": "pi-env-box", "port": 2222}
    args = sshArguments(target)
    assert args[-2:] == ["--", "box"]
    assert 'UserKnownHostsFile="/data/known%%hosts"' in args
    assert "StrictHostKeyChecking=yes" in args
    assert args[args.index("-p") + 1] == "2222"
    for field, value in [("host", "-oProxyCommand=x"), ("host", "a b"), ("hostKeyAlias", ""), ("user", "-l")]:
        with pytest.raises(ValueError):
            sshArguments({**target, field: value})
    with pytest.raises(ValueError):
        sshArguments({**target, "port": 70000})


def test_the_launch_command_runs_the_daemon_with_the_detected_python():
    posix = {"platform": "linux", "arch": "x64", "home": "/h", "warnings": [], "python": "/usr/bin/python3"}
    assert launchCommand(posix, "/h/pi-env-x", False) == "'/usr/bin/python3' '/h/pi-env-x'"
    assert launchCommand(posix, "/h/pi-env-x", True) == (
        "exec \"$SHELL\" -lc 'exec \"$0\" \"$@\"' '/usr/bin/python3' '/h/pi-env-x'"
    )
    cmd = {"platform": "windows", "arch": "x64", "home": "C:\\Users\\a b", "shell": "cmd", "warnings": [], "python": "C:\\Py\\python.exe"}
    assert launchCommand(cmd, "C:\\Users\\a b\\x.pyz", False) == 'C:\\Py\\python.exe "C:\\Users\\a b\\x.pyz"'
    powershell = {**cmd, "shell": "powershell"}
    assert launchCommand(powershell, "C:\\Users\\a b\\x.pyz", False) == "& 'C:\\Py\\python.exe' 'C:\\Users\\a b\\x.pyz'"


@needsPosix
def test_the_posix_probe_reports_this_system_and_a_python_to_run_the_daemon():
    output = subprocess.run(["sh", "-c", POSIX_PROBE], capture_output=True, text=True, check=True).stdout
    probe = parseProbe({"host": "here", "knownHostsFile": "", "hostKeyAlias": "a"}, f"login banner\n{output}")
    assert probe["system"] == os.uname().sysname
    assert probe["home"] == os.environ.get("HOME", "")
    python = probe["python"]
    assert os.path.isabs(python)
    version = subprocess.run(
        [python, "-c", "import sys; print(sys.version_info >= (3, 9))"], capture_output=True, text=True, check=False
    )
    assert version.stdout.strip() == "True"


@needsPosix
async def test_connect_ssh_deploys_the_daemon_by_its_hash_and_runs_it(fake):
    target, home = fake["target"], fake["home"]
    tools = os.path.join(home, ".pi/mobile/tools")
    os.makedirs(tools)
    Path(tools, "pi-env-0123456789abcdef0123456789abcdef").write_text("old", encoding="utf-8")
    connected = await connectSsh({**target, "binary": daemon})
    connection, remote = connected["connection"], connected["remote"]
    try:
        assert remote["home"] == home
        assert os.path.isabs(remote["python"])
        assert Path(tools, deployedName).read_bytes() == Path(daemon).read_bytes()
        # Daemons of other contents are removed.
        assert [name for name in os.listdir(tools) if name.startswith("pi-env-")] == [deployedName]
        env = RemoteExecutionEnv({"connection": connection, "id": "pi-env:test", "cwd": home})
        getOrThrow(await env.writeFile("over-ssh.txt", "hello", context))
        assert getOrThrow(await env.readTextFile("over-ssh.txt", context)) == "hello"
        output: list[str] = []
        result = getOrThrow(
            await env.exec(["sh", "-c", 'printf "%s" "$HOME"'], {"onOutput": lambda text, *_: output.append(text)}, context)
        )
        assert result["exitCode"] == 0
        assert "".join(output) == home
    finally:
        await closeConnection(connection)


@needsPosix
async def test_a_verified_daemon_is_reused_and_a_tampered_one_replaced_also_after_a_lost_connection(fake):
    target, home = fake["target"], fake["home"]
    file = os.path.join(home, ".pi/mobile/tools", deployedName)
    await closeConnection((await connectSsh({**target, "binary": daemon}))["connection"])
    before = os.stat(file).st_mtime_ns
    await closeConnection((await connectSsh({**target, "binary": daemon}))["connection"])
    assert os.stat(file).st_mtime_ns == before

    Path(file).write_text("tampered", encoding="utf-8")
    connection = (await connectSsh({**target, "binary": daemon}))["connection"]
    try:
        assert Path(file).read_bytes() == Path(daemon).read_bytes()
        pid = (await connection.info())["pid"]
        await killProcess(pid)
        Path(file).write_text("tampered", encoding="utf-8")
        # The next start redeploys the verified daemon instead of running whatever is there.
        assert (await connection.info())["pid"] != pid
        assert Path(file).read_bytes() == Path(daemon).read_bytes()
    finally:
        await closeConnection(connection)


@needsPosix
async def test_lists_files_over_the_stand_in_ssh_as_the_local_environment_lists_them(fake):
    await listsFilesOverSsh(fake["target"], fake["home"])


@needsPosix
async def test_the_login_shell_starts_the_daemon_only_when_asked(fake, monkeypatch):
    target, home = fake["target"], fake["home"]
    monkeypatch.setenv("SHELL", "/bin/sh")
    Path(home, ".profile").write_text("export PI_ENV_LOGIN=yes\n", encoding="utf-8")

    async def login(loginShell: bool) -> str:
        connection = (await connectSsh({**target, "binary": daemon, "loginShell": loginShell}))["connection"]
        try:
            env = RemoteExecutionEnv({"connection": connection, "id": "pi-env:test", "cwd": home})
            output: list[str] = []
            getOrThrow(
                await env.exec(
                    ["sh", "-c", 'printf "%s" "$PI_ENV_LOGIN"'],
                    {"onOutput": lambda text, *_: output.append(text)},
                    context,
                )
            )
            return "".join(output)
        finally:
            await closeConnection(connection)

    assert await login(False) == ""
    assert await login(True) == "yes"


@needsPosix
async def test_ssh_connection_connects_lazily_and_learns_the_remote_at_the_first_operation(fake):
    target, home = fake["target"], fake["home"]
    lazy = sshConnection({**target, "binary": daemon})
    connection, remote = lazy["connection"], lazy["remote"]
    try:
        assert remote() is None
        env = RemoteExecutionEnv({"connection": connection, "id": "pi-env:test", "cwd": home})
        result = await env.exec(["sh", "-c", "exit 0"], None, context)
        assert (result["value"]["exitCode"] if result["ok"] else result["error"].message) == 0
        assert remote()["home"] == home
    finally:
        await closeConnection(connection)

