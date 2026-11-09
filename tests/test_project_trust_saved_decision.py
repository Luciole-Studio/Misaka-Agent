"""Translated from pi's ``coding-agent/test/project-trust-saved-decision.test.ts`` (v1.5.0).

Regression tests for https://github.com/earendil-works/pi/issues/10947: a saved "Do not trust" decision was ignored
for projects without trust-requiring resources.

MISAKA fork: the probe runs ``misaka.cli.engine.main`` in a child Python process (upstream spawns its CLI), with the
child's ``MISAKA_HOME`` as its agent directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from misaka.cli.project_trust import createProjectTrustContext
from misaka.config import home
from misaka.core.project_trust import resolveProjectTrusted
from misaka.core.settings_manager import SettingsManager
from misaka.core.trust_manager import ProjectTrustStore

REPO = Path(__file__).resolve().parents[1]
ENGINE = "import asyncio, sys; from misaka.cli.engine import main; sys.exit(asyncio.run(main(sys.argv[1:])))"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    agentDir = tmp_path / "misaka-home"
    projectDir = tmp_path / "project"
    agentDir.mkdir()
    projectDir.mkdir()
    monkeypatch.setenv(home.ENV_HOME, str(agentDir))
    # AGENTS.md alone is not a trust-requiring project resource.
    (projectDir / "AGENTS.md").write_text("# untrusted\n", encoding="utf-8")
    probeFile = tmp_path / "probe.json"
    extensionFile = tmp_path / "probe_extension.py"
    extensionFile.write_text(
        f"""import json, os


def default(pi):
    pi.registerProvider("trust-probe", {{
        "baseUrl": "https://trust-probe.invalid/v1",
        "apiKey": "trust-probe-key",
        "api": "openai-completions",
        "models": [{{
            "id": "trust-probe-model",
            "name": "trust-probe-model",
            "reasoning": False,
            "input": ["text"],
            "cost": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}},
            "contextWindow": 10000,
            "maxTokens": 1000,
        }}],
    }})

    def onSessionStart(_event, ctx):
        with open({str(probeFile)!r}, "w", encoding="utf-8") as probe:
            json.dump({{"trusted": ctx.isProjectTrusted()}}, probe)
        os._exit(0)

    pi.on("session_start", onSessionStart)
""",
        encoding="utf-8",
    )
    return {"agentDir": agentDir, "projectDir": projectDir, "probeFile": probeFile, "extensionFile": extensionFile}


def probeProjectTrust(dirs) -> bool:
    env = {
        **os.environ,
        home.ENV_HOME: str(dirs["agentDir"]),
        "MISAKA_OFFLINE": "1",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")])),
    }
    child = subprocess.run(
        [sys.executable, "-c", ENGINE, "--no-session", "-e", str(dirs["extensionFile"]),
         "--provider", "trust-probe", "--model", "trust-probe-model", "-p", "hi"],
        cwd=dirs["projectDir"], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(dirs["probeFile"].read_text(encoding="utf-8"))["trusted"]


def test_reports_a_saved_do_not_trust_decision_to_extensions(dirs):
    ProjectTrustStore(str(dirs["agentDir"])).set(str(dirs["projectDir"]), False)

    assert probeProjectTrust(dirs) is False


def test_auto_trusts_when_no_decision_is_saved(dirs):
    assert probeProjectTrust(dirs) is True


async def test_resolve_project_trusted_honors_the_saved_decision_before_auto_trust(dirs):
    trustStore = ProjectTrustStore(str(dirs["agentDir"]))

    async def resolve() -> bool:
        return await resolveProjectTrusted({
            "cwd": str(dirs["projectDir"]),
            "trustStore": trustStore,
            "projectTrustContext": createProjectTrustContext({
                "cwd": str(dirs["projectDir"]),
                "mode": "print",
                "settingsManager": SettingsManager.inMemory(),
                "hasUI": False,
            }),
        })

    assert await resolve() is True
    trustStore.set(str(dirs["projectDir"]), False)
    assert await resolve() is False
    trustStore.set(str(dirs["projectDir"]), True)
    assert await resolve() is True
