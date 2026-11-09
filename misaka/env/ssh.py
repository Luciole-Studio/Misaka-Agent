"""Reaching a machine with the system ``ssh``: host keys, detection, deployment of the daemon, and the connection.

Translated from pi's ``packages/env/src/ssh.ts`` (v1.3.0). MISAKA fork: the daemon is a Python zip application
(``misaka.env.bundle``), so the remote needs Python 3.9 or later. Detection also finds that interpreter
(``RemotePlatform["python"]``), a Windows host keeps the daemon as ``pi-env-<hash>.pyz``, and the launch command runs the
daemon with the interpreter.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import Any, Literal, NotRequired, TypedDict

from misaka.utils.js import (
    errorMessage,
    formatNumber,
    jsIsInteger,
    jsonStringify,
    jsString,
    jsTrim,
    startTask,
)

from .bundle import daemonPath as bundledDaemonPath
from .connection import Connection, ConnectionOptions


class RemotePlatform(TypedDict):
    """A remote system the package ships a daemon for. ``home``: the remote home directory, in the remote system's own
    spelling. ``shell``: Windows: the shell ``sshd`` runs commands with (``DefaultShell``). ``warnings``: problems worth
    telling the owner about, such as Termux without ``termux-exec``. ``python`` (MISAKA fork): the interpreter that runs
    the daemon."""

    platform: Literal["linux", "android", "darwin", "windows"]
    arch: Literal["x64", "arm64"]
    home: str
    shell: NotRequired[Literal["cmd", "powershell"]]
    warnings: list[str]
    python: str


class SshTarget(TypedDict):
    """How to reach a machine with the system ``ssh``. ``host``: host name or ``~/.ssh/config`` alias.
    ``knownHostsFile``: host keys this application trusts; ``acceptHostKey`` adds to it. ``hostKeyAlias``: the name keys
    are stored under, independent of aliases, ports and jump hosts, e.g. ``pi-env-<env name>``. ``ssh``: the ``ssh``
    program; default ``ssh``. ``configFile``: an ``ssh`` configuration file instead of ``~/.ssh/config`` (``-F``)."""

    host: str
    user: NotRequired[str]
    port: NotRequired[int]
    identityFile: NotRequired[str]
    knownHostsFile: str
    hostKeyAlias: str
    ssh: NotRequired[str]
    configFile: NotRequired[str]


class HostKeyUnknownError(Exception):
    """The remote host's key is not in ``knownHostsFile``; ``scanHostKey`` shows it so the owner can accept it."""


class HostKeyChangedError(Exception):
    """The remote host's key differs from the one in ``knownHostsFile``; it is never accepted automatically."""


class SshError(Exception):
    """An ``ssh`` invocation that failed for another reason, with its exit code and diagnostics."""

    def __init__(self, message: str, exitCode: int | None, stderr: str) -> None:
        super().__init__(message)
        self.message = message
        self.exitCode = exitCode
        self.stderr = stderr


# JavaScript's `\s`.
_WHITESPACE = "\t\n\v\f\r    -     　﻿"


def checkField(name: str, value: str) -> None:
    """No leading ``-`` (it would be read as an ``ssh`` option) and no whitespace or control characters."""
    if value == "" or value.startswith("-") or re.search(f"[{_WHITESPACE}\\x00-\\x1f\\x7f]", value):
        raise ValueError(f"Invalid {name}: {jsonStringify(value)}")


def configPath(name: str, path: str) -> str:
    """A path for an ``ssh`` option that expands ``%`` tokens: quoted, with ``%`` literal."""
    if path == "" or re.search(r'["\x00-\x1f\x7f]', path):
        raise ValueError(f"Invalid {name}: {jsonStringify(path)}")
    return '"' + path.replace("%", "%%") + '"'


def sshArguments(target: SshTarget, strictHostKeys: bool = True, knownHostsFile: str | None = None) -> list[str]:
    """Arguments for ``ssh`` up to the host: no prompts, no forwarding of any kind, no shared connections, no commands
    from the configuration, no locale forwarding (the remote uses its own), and host keys checked strictly against the
    application's own file under a fixed alias."""
    if knownHostsFile is None:
        knownHostsFile = target["knownHostsFile"]
    checkField("host", target["host"])
    checkField("host key alias", target["hostKeyAlias"])
    if target.get("user") is not None:
        checkField("user", target["user"])
    port = target.get("port")
    if port is not None and (not jsIsInteger(port) or port < 1 or port > 65535):
        raise ValueError(f"Invalid port: {jsString(port)}")

    def option(setting: str) -> list[str]:
        return ["-o", setting]

    identityFile = target.get("identityFile")
    return [
        *([] if target.get("configFile") is None else ["-F", target["configFile"]]),
        "-T",
        "-a",
        "-x",
        *option("BatchMode=yes"),
        *option("ClearAllForwardings=yes"),
        *option("ForwardAgent=no"),
        *option("ForwardX11=no"),
        *option("ControlMaster=no"),
        *option("ControlPath=none"),
        *option("RemoteCommand=none"),
        *option("PermitLocalCommand=no"),
        *option("SendEnv=-*"),
        *option("ServerAliveInterval=15"),
        *option(f"StrictHostKeyChecking={'yes' if strictHostKeys else 'accept-new'}"),
        *option(f"UserKnownHostsFile={configPath('known hosts file', knownHostsFile)}"),
        *option("GlobalKnownHostsFile=none"),
        *option("HashKnownHosts=no"),
        *option(f"HostKeyAlias={target['hostKeyAlias']}"),
        *([] if target.get("user") is None else ["-l", target["user"]]),
        *([] if port is None else ["-p", jsString(port)]),
        *(
            []
            if identityFile is None
            else [
                *option(f"IdentityFile={configPath('identity file', identityFile)}"),
                *option("IdentitiesOnly=yes"),
            ]
        ),
        "--",
        target["host"],
    ]


# How long a detection, check or upload over `ssh` may take before it is abandoned.
SSH_TIMEOUT_MS = 60_000
UPLOAD_TIMEOUT_MS = 300_000


async def runSsh(target: SshTarget, command: str, options: dict[str, Any] | None = None) -> str:
    """Run one remote command over ``ssh``, optionally feeding stdin; resolves with stdout, rejects on failure."""
    options = options or {}
    args = options["args"] if options.get("args") is not None else sshArguments(target)
    timeoutMs = options["timeoutMs"] if options.get("timeoutMs") is not None else SSH_TIMEOUT_MS
    stdin: bytes = options["stdin"] if options.get("stdin") is not None else b""
    try:
        process = await asyncio.create_subprocess_exec(
            target.get("ssh") or "ssh",
            *args,
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, ValueError) as error:
        raise SshError(errorMessage(error), None, "") from None
    stdout: list[bytes] = []
    stderr: list[bytes] = []

    async def pump(stream: asyncio.StreamReader | None, into: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return
            into.append(chunk)

    async def feed() -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(stdin)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    closing = asyncio.gather(pump(process.stdout, stdout), pump(process.stderr, stderr), feed(), process.wait())
    try:
        await asyncio.wait_for(asyncio.shield(closing), timeoutMs / 1000)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await asyncio.gather(closing, return_exceptions=True)
        diagnostics = b"".join(stderr).decode("utf-8", "replace")
        raise SshError(
            f"ssh {target['host']} did not finish within {formatNumber(timeoutMs / 1000)} s: {jsTrim(diagnostics)}",
            None,
            diagnostics,
        ) from None
    output = b"".join(stdout).decode("utf-8", "replace")
    diagnostics = b"".join(stderr).decode("utf-8", "replace")
    returncode = process.returncode
    # Node reports a process killed by a signal with code `null`.
    code = returncode if returncode is not None and returncode >= 0 else None
    if code == 0:
        return output
    if re.search(r"REMOTE HOST IDENTIFICATION HAS CHANGED", diagnostics):
        raise HostKeyChangedError(f"The host key of {target['host']} changed; remove the old key to continue")
    if re.search(r"Host key verification failed|No .* host key is known", diagnostics):
        raise HostKeyUnknownError(f"The host key of {target['host']} is not trusted yet")
    raise SshError(
        f"ssh {target['host']} failed with exit code {jsString(code)}: {jsTrim(diagnostics)}", code, diagnostics
    )


async def _fingerprints(file: str) -> list[str]:
    process = await asyncio.create_subprocess_exec(
        "ssh-keygen", "-lf", file, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    output, _ = await process.communicate()
    return [line for line in output.decode("utf-8", "replace").split("\n") if jsTrim(line) != ""]


def _readLines(file: str) -> list[str]:
    if not os.path.exists(file):
        return []
    with open(file, "rb") as handle:
        return handle.read().decode("utf-8", "replace").split("\n")


async def scanHostKey(target: SshTarget) -> dict[str, list[str]]:
    """Connect once with ``accept-new`` against a temporary known-hosts file to capture the key the host presents,
    through the same route (``~/.ssh/config``, jump hosts) as real connections. Returns its known-hosts ``lines`` and
    ``fingerprints``. Showing a fingerprint is not authentication: compare it with one obtained out of band before
    accepting it."""
    directory = await asyncio.to_thread(tempfile.mkdtemp, prefix="pi-env-hostkey-")
    try:
        scanned = os.path.join(directory, "known_hosts")
        # Authentication may fail without the key; the key is recorded before authentication.
        try:
            await runSsh(target, "exit 0", {"args": sshArguments(target, False, scanned)})
        except (SshError, HostKeyUnknownError):
            pass
        lines = [line for line in await asyncio.to_thread(_readLines, scanned) if jsTrim(line) != ""]
        if not lines:
            raise ValueError(f"No host key received from {target['host']}")
        return {"lines": lines, "fingerprints": await _fingerprints(scanned)}
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, True)


def _split(line: str) -> list[str]:
    """``line.trim().split(/\\s+/)``."""
    trimmed = jsTrim(line)
    return re.split(f"[{_WHITESPACE}]+", trimmed)


def parseHostKeyLine(alias: str, line: str) -> dict[str, str]:
    """A known-hosts line for ``alias``: ``alias type key [comment]``, no markers, patterns or hashed names."""
    parts = _split(line)
    host = parts[0] if parts else None
    type = parts[1] if len(parts) > 1 else None
    key = parts[2] if len(parts) > 2 else None
    if (
        host != alias
        or type is None
        or key is None
        or not re.fullmatch(r"(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+)", type)
        or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", key)
    ):
        raise ValueError(f"Not a host key line for {alias}: {jsonStringify(line)}")
    return {"type": type, "key": key}


# Known-hosts files being changed, so changes to one file happen one at a time.
knownHostsChanges: dict[str, asyncio.Future[None]] = {}


def _rewriteKnownHosts(file: str, change: Callable[[list[str]], list[str]]) -> None:
    os.makedirs(os.path.dirname(file), mode=0o700, exist_ok=True)
    existing = [line for line in _readLines(file) if line != ""]
    lines = change(existing)
    temporary = f"{file}.{secrets.token_hex(6)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(("" if not lines else "\n".join(lines) + "\n").encode("utf-8"))
    try:
        os.replace(temporary, file)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


async def changeKnownHosts(file: str, change: Callable[[list[str]], list[str]]) -> None:
    """Rewrite the known-hosts file one change at a time, atomically."""
    previous = knownHostsChanges.get(file)

    async def apply() -> None:
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except Exception:  # noqa: BLE001, S110 - a failed earlier change does not stop this one
                pass
        await asyncio.to_thread(_rewriteKnownHosts, file, change)

    next: asyncio.Future[None] = startTask(apply())
    knownHostsChanges[file] = next
    try:
        await asyncio.shield(next)
    finally:
        if knownHostsChanges.get(file) is next:
            del knownHostsChanges[file]


async def acceptHostKey(target: SshTarget, lines: list[str]) -> None:
    """Trust host-key lines from ``scanHostKey`` by adding them to the target's known-hosts file. Only plain lines for
    the target's alias are accepted. A key that differs from a trusted key of the same type is refused with
    ``HostKeyChangedError``; ``forgetHostKey`` must remove the old keys first."""
    alias = target["hostKeyAlias"]
    accepted = [{"line": jsTrim(line), **parseHostKeyLine(alias, line)} for line in lines]

    def change(existing: list[str]) -> list[str]:
        trusted: dict[str, str] = {}
        for line in existing:
            parts = _split(line)
            if len(parts) >= 3 and parts[0] == alias:
                trusted[parts[1]] = parts[2]
        added: list[str] = []
        for entry in accepted:
            known = trusted.get(entry["type"])
            if known == entry["key"]:
                continue
            if known is not None:
                raise HostKeyChangedError(
                    f"The {entry['type']} host key of {target['host']} changed; forget the old key first"
                )
            trusted[entry["type"]] = entry["key"]
            added.append(entry["line"])
        return [*existing, *added]

    await changeKnownHosts(target["knownHostsFile"], change)


async def forgetHostKey(target: SshTarget) -> None:
    """Stop trusting every key stored for the target's alias, e.g. after the owner confirmed a changed host key."""
    await changeKnownHosts(
        target["knownHostsFile"],
        lambda existing: [line for line in existing if _split(line)[0] != target["hostKeyAlias"]],
    )


# MISAKA fork: each probe also reports a Python 3.9 or later on the remote, or `-`.
_POSIX_PYTHON = (
    'py=-; for p in python3 python; do c=$(command -v "$p" 2>/dev/null) || continue; '
    '"$c" -c "import sys; sys.exit(sys.version_info < (3, 9))" 2>/dev/null && { py=$c; break; }; done; echo "$py"'
)
# POSIX detection, run by the remote login shell; a Windows host answers through PowerShell instead.
POSIX_PROBE = (
    "sh -c 'echo PI-ENV-PROBE; uname -s; uname -m; uname -o 2>/dev/null || echo -; "
    'printf "%s\\n" "$HOME" "${TMPDIR:--}" "${LD_PRELOAD:--}"; ' + _POSIX_PYTHON + "'"
)


def powershell(script: str) -> str:
    return (
        "powershell -NoProfile -NonInteractive -EncodedCommand "
        + base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    )


_WINDOWS_PYTHON = (
    "$py = '-'; foreach ($n in 'python3', 'python', 'py') { "
    "$c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1; "
    "if ($c) { & $c.Source -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>$null; "
    "if ($LASTEXITCODE -eq 0) { $py = $c.Source; break } } }; $py"
)
WINDOWS_PROBE = powershell(
    "'PI-ENV-PROBE'; 'Windows'; [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString(); '-'; "
    "$HOME; '-'; '-'; " + _WINDOWS_PYTHON
)


def normalizeArch(machine: str) -> Literal["x64", "arm64"]:
    lower = machine.lower()
    if lower in ("x86_64", "amd64", "x64"):
        return "x64"
    if lower in ("aarch64", "arm64"):
        return "arm64"
    raise ValueError(f"Unsupported remote architecture: {machine}")


class Probe(TypedDict):
    system: str
    machine: str
    os: str
    home: str
    tmpdir: str
    preload: str
    python: str


def parseProbe(target: SshTarget, output: str) -> Probe:
    # Login shells may print a banner first.
    lines = re.split(r"\r?\n", output)
    if "PI-ENV-PROBE" not in lines:
        raise ValueError(f"Unexpected answer from {target['host']}: {jsTrim(output)}")
    rest = lines[lines.index("PI-ENV-PROBE") + 1 :]

    def at(index: int, default: str) -> str:
        return rest[index] if index < len(rest) else default

    return {
        "system": at(0, ""),
        "machine": at(1, ""),
        "os": at(2, ""),
        "home": at(3, ""),
        "tmpdir": at(4, "-"),
        "preload": at(5, "-"),
        "python": at(6, "-"),
    }


def termuxWarnings(probe: Probe) -> list[str]:
    """Termux works only with its own ``TMPDIR`` and with ``termux-exec``, which makes ``#!/usr/bin/env`` shebangs
    work."""
    warnings: list[str] = []
    if probe["tmpdir"] == "-":
        warnings.append("TMPDIR is not set; Termux's sshd normally sets it to $PREFIX/tmp.")
    if "termux-exec" not in probe["preload"]:
        warnings.append("termux-exec is not loaded (LD_PRELOAD); scripts with #!/usr/bin/env shebangs will fail.")
    warnings.append("Android may suspend Termux; run termux-wake-lock on the device to keep the connection alive.")
    return warnings


def _python(target: SshTarget, probe: Probe) -> str:
    """MISAKA fork: the daemon is Python; a host without Python 3.9 or later cannot run it."""
    python = jsTrim(probe["python"])
    if python in ("", "-"):
        raise ValueError(f"{target['host']} has no Python 3.9 or later to run pi-env")
    return python


async def detectPlatform(target: SshTarget) -> RemotePlatform:
    """Which system the target runs: ``uname`` through the login shell, or PowerShell on Windows."""
    try:
        probe = parseProbe(target, await runSsh(target, POSIX_PROBE))
    except (HostKeyUnknownError, HostKeyChangedError):
        raise
    except Exception:  # noqa: BLE001 - cmd.exe or PowerShell as the remote shell: no `sh`, or one that gets the quotes wrong
        probe = parseProbe(target, await runSsh(target, WINDOWS_PROBE))
    # Git Bash as Windows' default SSH shell: ask PowerShell for Windows' own architecture and home spelling.
    if re.match(r"^(MINGW|MSYS|CYGWIN)", probe["system"]):
        probe = parseProbe(target, await runSsh(target, WINDOWS_PROBE))
    arch = normalizeArch(probe["machine"])
    system, os_name, home = probe["system"], probe["os"], probe["home"]
    if system == "Windows":
        # cmd.exe expands %OS%; PowerShell prints it as is.
        shell: Literal["cmd", "powershell"] = (
            "cmd" if "Windows_NT" in await runSsh(target, "echo %OS%") else "powershell"
        )
        return {
            "platform": "windows",
            "arch": arch,
            "home": home,
            "shell": shell,
            "warnings": [],
            "python": _python(target, probe),
        }
    if system == "Darwin":
        return {"platform": "darwin", "arch": arch, "home": home, "warnings": [], "python": _python(target, probe)}
    if system == "Linux":
        android = os_name == "Android"
        return {
            "platform": "android" if android else "linux",
            "arch": arch,
            "home": home,
            "warnings": termuxWarnings(probe) if android else [],
            "python": _python(target, probe),
        }
    raise ValueError(f"Unsupported remote system: {system}")


def packagedDaemon(remote: RemotePlatform | dict[str, Any] | None = None) -> str:
    """The daemon this package ships for a remote system. MISAKA fork: one zip application serves every system."""
    return bundledDaemonPath()


def localDaemon() -> str:
    """The daemon this package ships for the machine it runs on, e.g. for terminals of local sessions. MISAKA fork: the
    zip application, which runs under ``sys.executable`` (``[sys.executable, localDaemon()]``) rather than as a native
    binary; the platform check stays upstream's."""
    platform = "windows" if sys.platform == "win32" else "android" if hasattr(sys, "getandroidapilevel") else sys.platform
    if platform.startswith("linux"):
        platform = "linux"
    if platform not in ("linux", "android", "darwin", "windows"):
        raise RuntimeError(f"No pi-env daemon for {sys.platform}")
    return packagedDaemon({"platform": platform, "arch": normalizeArch(_machine())})


def _machine() -> str:
    """``process.arch`` of this machine, in the spelling ``normalizeArch`` reads."""
    import platform

    return platform.machine()


def quotePosix(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def quotePowerShell(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def daemonPath(remote: RemotePlatform, sha256: str) -> str:
    """Where a daemon with this content lives on the remote machine: named by its SHA-256, so versions never collide."""
    name = f"pi-env-{sha256[:32]}"
    if remote["platform"] == "windows":
        # MISAKA fork: a zip application, not an executable.
        return f"{remote['home']}\\.pi\\mobile\\tools\\{name}.pyz"
    return f"{remote['home']}/.pi/mobile/tools/{name}"


POSIX_HASH = (
    "hash() { if command -v sha256sum >/dev/null 2>&1; then sha256sum \"$1\" | cut -d' ' -f1; "
    "elif command -v shasum >/dev/null 2>&1; then shasum -a 256 \"$1\" | cut -d' ' -f1; "
    "elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 \"$1\" | sed 's/.*= //'; else echo none; fi; }"
)


async def checkDaemon(target: SshTarget, remote: RemotePlatform, file: str, sha256: str) -> str:
    """Whether the remote file has this content: ``present``, ``missing``, or (POSIX without a hash tool) ``nohash``."""
    if remote["platform"] == "windows":
        check = powershell(
            f"$f = {quotePowerShell(file)}; if ((Test-Path -LiteralPath $f) -and ((Get-FileHash -Algorithm SHA256 "
            f"-LiteralPath $f).Hash.ToLower() -eq '{sha256}')) {{ 'present' }} else {{ 'missing' }}"
        )
        return "present" if jsTrim(await runSsh(target, check)).endswith("present") else "missing"
    check = (
        f"{POSIX_HASH}; f={quotePosix(file)}; if [ -f \"$f\" ] && [ \"$(hash \"$f\")\" = {sha256} ]; then echo present; "
        'elif [ "$(hash /dev/null)" = none ]; then echo nohash; else echo missing; fi'
    )
    lines = jsTrim(await runSsh(target, f"sh -c {quotePosix(check)}")).split("\n")
    return lines[-1] if lines else "missing"


def _readBytes(path: str) -> bytes:
    with open(path, "rb") as file:
        return file.read()


async def deployDaemon(target: SshTarget, remote: RemotePlatform, binary: str | None = None) -> str:
    """Make sure the daemon is on the remote machine, verified by its SHA-256 before it ever runs, and remove daemons of
    other contents. The upload goes to a new temporary file and is renamed into place only once its hash matches.
    Returns the remote path of the daemon."""
    if binary is None:
        binary = packagedDaemon(remote)
    if not os.path.exists(binary):
        raise ValueError(f"No pi-env daemon for {remote['platform']}-{remote['arch']} at {binary}")
    data = await asyncio.to_thread(_readBytes, binary)
    sha256 = hashlib.sha256(data).hexdigest()
    file = daemonPath(remote, sha256)
    state = await checkDaemon(target, remote, file, sha256)
    if state == "present":
        return file
    if state == "nohash":
        raise ValueError(f"{target['host']} has no sha256sum, shasum or openssl to verify pi-env")
    if remote["platform"] == "windows":
        upload = powershell(
            "; ".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    f"$f = {quotePowerShell(file)}",
                    "$d = Split-Path -Parent $f",
                    "New-Item -ItemType Directory -Force -Path $d | Out-Null",
                    "$t = Join-Path $d ('.pi-env-' + [guid]::NewGuid().ToString() + '.tmp')",
                    # PowerShell reads redirected stdin itself, as text lines for `$input`, so the daemon comes as base64
                    # lines up to an end marker: Windows' sshd may never pass on the end of stdin.
                    "$text = New-Object System.Text.StringBuilder",
                    "foreach ($line in $input) { if ($line -eq 'PI-ENV-END') { break }; [void]$text.Append($line) }",
                    "$bytes = [Convert]::FromBase64String($text.ToString())",
                    (
                        "$out = [IO.File]::Open($t, 'CreateNew', 'Write', 'None'); "
                        "$out.Write($bytes, 0, $bytes.Length); $out.Close()"
                    ),
                    (
                        f"if ((Get-FileHash -Algorithm SHA256 -LiteralPath $t).Hash.ToLower() -ne '{sha256}') "
                        "{ Remove-Item -LiteralPath $t; throw 'pi-env upload is corrupt' }"
                    ),
                    # A running daemon or a virus scanner can hold the old file for a moment.
                    (
                        "for ($i = 0; ; $i++) { try { Move-Item -Force -LiteralPath $t -Destination $f; break } "
                        "catch { if ($i -ge 20) { throw }; Start-Sleep -Milliseconds 250 } }"
                    ),
                    # Older daemons; one that is running stays until it exits.
                    (
                        "Get-ChildItem -LiteralPath $d -Filter 'pi-env-*.pyz' | Where-Object { $_.FullName -ne $f } | "
                        "ForEach-Object { Remove-Item -LiteralPath $_.FullName -ErrorAction SilentlyContinue }"
                    ),
                    "'deployed'",
                ]
            )
        )
        encoded = base64.b64encode(data).decode("ascii")
        lines = "".join(f"{encoded[index : index + 76]}\n" for index in range(0, len(encoded), 76))
        await runSsh(target, upload, {"stdin": f"{lines}PI-ENV-END\n".encode(), "timeoutMs": UPLOAD_TIMEOUT_MS})
        return file
    upload = "\n".join(
        [
            "set -e",
            POSIX_HASH,
            f"f={quotePosix(file)}",
            'd=$(dirname "$f")',
            'mkdir -p "$d"',
            'chmod 700 "$d"',
            't=$(mktemp "$d/.pi-env.XXXXXX")',
            'cat > "$t"',
            f'if [ "$(hash "$t")" != {sha256} ]; then rm -f "$t"; echo "pi-env upload is corrupt" >&2; exit 1; fi',
            'chmod 700 "$t"',
            'mv -f "$t" "$f"',
            # Older daemons; running ones keep their file open until they exit.
            'for old in "$d"/pi-env-*; do [ "$old" = "$f" ] || rm -f "$old"; done',
            "echo deployed",
        ]
    )
    await runSsh(target, f"sh -c {quotePosix(upload)}", {"stdin": data, "timeoutMs": UPLOAD_TIMEOUT_MS})
    return file


class SshConnectOptions(SshTarget):
    """Options for ``sshConnection`` and ``connectSsh``: the target, plus the daemon to deploy (default: the one this
    package ships). ``loginShell``: POSIX: start the daemon through the user's login shell (``$SHELL -l``), so commands
    see the environment of ``~/.profile`` and similar files. Off by default: ``ssh`` runs commands without a login
    shell."""

    binary: NotRequired[str]
    loginShell: NotRequired[bool]
    onLog: NotRequired[Callable[[str], None]]


def launchCommand(remote: RemotePlatform, file: str, loginShell: bool) -> str:
    """The remote command that starts the daemon at ``file``, before the ``serve`` arguments. MISAKA fork: the daemon
    runs under the detected interpreter."""
    python = remote["python"]
    if remote["platform"] == "windows":
        # PowerShell runs a quoted path only with the call operator; cmd.exe keeps one pair of quotes around a program.
        if remote.get("shell") == "powershell":
            return f"& {quotePowerShell(python)} {quotePowerShell(file)}"
        return " ".join(f'"{part}"' if " " in part else part for part in (python, file))
    # `$0` is the interpreter, `"$@"` the daemon and its arguments; `$SHELL` is the login shell's own name for itself.
    if loginShell:
        return f"exec \"$SHELL\" -lc 'exec \"$0\" \"$@\"' {quotePosix(python)} {quotePosix(file)}"
    return f"{quotePosix(python)} {quotePosix(file)}"


class SshState(TypedDict, total=False):
    """What an SSH connection learned about its remote system, and the daemon it verified last."""

    remote: RemotePlatform
    verified: str | None


def sshConnectionFor(options: SshConnectOptions, state: SshState) -> Connection:
    """A ``Connection`` whose every start detects the remote system (once), verifies or deploys the daemon, then starts
    it."""

    async def command() -> list[str]:
        if state.get("remote") is None:
            remote = await detectPlatform(options)
            onLog = options.get("onLog")
            for warning in remote["warnings"]:
                if onLog is not None:
                    onLog(f"{warning}\n")
            state["remote"] = remote
        remote = state["remote"]
        # A deployment that just verified the daemon counts for the first start.
        file = state.get("verified")
        if file is None:
            file = await deployDaemon(options, remote, options.get("binary") or packagedDaemon(remote))
        state["verified"] = None
        return [options.get("ssh") or "ssh", *sshArguments(options), launchCommand(remote, file, options.get("loginShell") is True)]

    connectionOptions: ConnectionOptions = {"command": command}
    if options.get("onLog") is not None:
        connectionOptions["onLog"] = options["onLog"]
    return Connection(connectionOptions)


def sshConnection(options: SshConnectOptions) -> dict[str, Any]:
    """A ``Connection`` to the target that does nothing until its first request. Each start detects the remote system
    (the first time), verifies the daemon and deploys it if it is missing or changed, then starts it over ``ssh``. A
    failure (no network, an untrusted or changed host key) fails the requests waiting for that start with code
    ``spawn_error`` and the ``ssh`` diagnostics as message; the next request tries again. ``remote()`` is the detected
    system, once known."""
    state: SshState = {}
    return {"connection": sshConnectionFor(options, state), "remote": lambda: state.get("remote")}


async def connectSsh(options: SshConnectOptions) -> dict[str, Any]:
    """Detect the remote system and deploy the daemon now, then return a ``Connection`` that starts it over ``ssh``, like
    ``sshConnection``. Failures raise here: host keys must already be trusted (``scanHostKey``, ``acceptHostKey``),
    otherwise this raises ``HostKeyUnknownError``."""
    remote = await detectPlatform(options)
    verified = await deployDaemon(options, remote, options.get("binary") or packagedDaemon(remote))
    onLog = options.get("onLog")
    for warning in remote["warnings"]:
        if onLog is not None:
            onLog(f"{warning}\n")
    return {"connection": sshConnectionFor(options, {"remote": remote, "verified": verified}), "remote": remote}


__all__ = [
    "HostKeyChangedError",
    "HostKeyUnknownError",
    "RemotePlatform",
    "SshConnectOptions",
    "SshError",
    "SshTarget",
    "acceptHostKey",
    "connectSsh",
    "deployDaemon",
    "detectPlatform",
    "forgetHostKey",
    "localDaemon",
    "packagedDaemon",
    "scanHostKey",
    "sshArguments",
    "sshConnection",
]
