"""pi env's ``test/ssh-external.test.ts``: the conformance suite over a real SSH server on this machine, configured by the
environment: ``PI_ENV_SSH_HOST``, ``PI_ENV_SSH_USER``, ``PI_ENV_SSH_PORT``, ``PI_ENV_SSH_KEY`` and ``PI_ENV_SSH_PROGRAM``
(``PI_ENV_SSH_SHELL``: the server's default shell on Windows). Skipped without ``PI_ENV_SSH_HOST``."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.env import getOrThrow
from misaka.durable.testing import createAssertions, createEnvConformance
from misaka.env import (
    RemoteExecutionEnv,
    acceptHostKey,
    connectSsh,
    deployDaemon,
    detectPlatform,
    nodePlatform,
    scanHostKey,
)
from tests.env_support import closeConnection, daemon, removeTempDir

host = os.environ.get("PI_ENV_SSH_HOST")
windows = sys.platform == "win32"
gitBash = os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Git", "bin", "bash.exe")

pytestmark = [
    pytest.mark.skipif(host is None, reason="PI_ENV_SSH_HOST is not set"),
    pytest.mark.asyncio(loop_scope="module"),
]

state: dict[str, Any] = {}


def log(text: str) -> None:
    # Each stage is logged, so a hang in CI shows where it is.
    sys.stderr.write(f"[ssh-external {datetime.now(UTC).isoformat()}] {text}\n")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def connected() -> AsyncIterator[dict[str, Any]]:
    root = tempfile.mkdtemp(prefix="pi-env-ssh-external-")
    target: dict[str, Any] = {
        "host": host,
        **({} if os.environ.get("PI_ENV_SSH_USER") is None else {"user": os.environ["PI_ENV_SSH_USER"]}),
        **({} if os.environ.get("PI_ENV_SSH_PORT") is None else {"port": int(os.environ["PI_ENV_SSH_PORT"])}),
        **({} if os.environ.get("PI_ENV_SSH_KEY") is None else {"identityFile": os.environ["PI_ENV_SSH_KEY"]}),
        **({} if os.environ.get("PI_ENV_SSH_PROGRAM") is None else {"ssh": os.environ["PI_ENV_SSH_PROGRAM"]}),
        "knownHostsFile": os.path.join(root, "known_hosts"),
        "hostKeyAlias": "pi-env-external",
    }
    log("scanning the host key")
    await acceptHostKey(target, (await scanHostKey(target))["lines"])  # type: ignore[arg-type]
    log("detecting the platform")
    remote = await detectPlatform(target)  # type: ignore[arg-type]
    log(f"detected {remote}; deploying")
    log(f"deployed {await deployDaemon(target, remote, daemon)}; connecting")  # type: ignore[arg-type]
    result = await connectSsh({**target, "binary": daemon, "onLog": lambda text: log(f"daemon: {text.strip()}")})  # type: ignore[typeddict-item]
    log("connected; saying hello")
    log(f"hello {await result['connection'].info()}")
    state["connection"] = result["connection"]
    yield result
    await closeConnection(result["connection"])
    removeTempDir(root)


async def test_reaches_the_remote_system_through_its_default_shell(connected):
    info = await connected["connection"].info()
    assert nodePlatform(info)["platform"] == sys.platform
    if windows:
        assert connected["remote"]["shell"] == os.environ.get("PI_ENV_SSH_SHELL", "cmd")


async def withEnv(use: Any) -> None:
    connection = state["connection"]
    info = await connection.info()
    home = RemoteExecutionEnv({"connection": connection, "id": "pi-env:external", "cwd": info["home"]})
    cwd = getOrThrow(await home.createTempDir("pi-env-conformance-", BACKGROUND_CONTEXT))
    try:
        await use(
            RemoteExecutionEnv({"connection": connection, "id": "pi-env:external", "cwd": cwd, "watch": {"pollIntervalMs": 100}})
        )
    finally:
        await home.remove(cwd, {"recursive": True, "force": True}, BACKGROUND_CONTEXT)


cases = createEnvConformance(
    {
        **({"shell": [gitBash, "-c"], "symlinks": False, "listFiles": True} if windows else {"listFiles": True}),
        "assertions": createAssertions(),
        "withEnv": withEnv,
    }
)


@pytest.mark.parametrize("case", cases, ids=[case["name"] for case in cases])
async def test_remote_execution_env_over_ssh(case: Any, connected: Any) -> None:
    await case["run"]()
