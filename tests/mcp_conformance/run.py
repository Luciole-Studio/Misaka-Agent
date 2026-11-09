"""Runs the official MCP client conformance suite against misaka (``client.py``) and compares every check
with the committed baseline (``baseline.json``). Fails when a check that passed in the baseline no longer
passes, or when a check fails that the baseline does not list. Known failures stay visible in the output.
See README.md.

Translated from pi's ``coding-agent/test/mcp-conformance/run.ts``. Not part of the test gate: it fetches the
pinned suite with ``npx`` (network, Node.js) and needs a POSIX shell.

    python tests/mcp_conformance/run.py [--mode VERSION] [--scenario NAME] [--update-baseline] [--keep-results] [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The pinned upstream suite. Bumping it requires reviewing and regenerating the baseline.
CONFORMANCE_PACKAGE = "@modelcontextprotocol/conformance@0.2.0-alpha.11"
# Protocol versions misaka negotiates. 2026-07-28 is a different (stateless) protocol misaka does not speak.
MODES = ("2025-03-26", "2025-06-18", "2025-11-25")
SCENARIO_TIMEOUT_MS = 30_000
# Checks that are part of the suite's HTTP trace, not assertions.
IGNORED_STATUSES = {"INFO", "SKIPPED"}

HERE = Path(__file__).resolve().parent
BASELINE_PATH = HERE / "baseline.json"
CLIENT_PATH = HERE / "client.py"


@dataclass
class ScenarioResult:
    mode: str
    scenario: str
    # Check id to status. Repeated ids pass only when every instance passes.
    checks: dict[str, str] = field(default_factory=dict)
    # Failure messages by check id, for the report.
    messages: dict[str, str] = field(default_factory=dict)


async def runCommand(
    command: str, args: list[str], *, cwd: str, env: dict[str, str] | None = None, timeoutMs: float | None = None
) -> tuple[int, str, str]:
    child = await asyncio.create_subprocess_exec(
        command, *args, cwd=cwd, env=env if env is not None else dict(os.environ),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), None if timeoutMs is None else timeoutMs / 1000)
    except TimeoutError:
        # The runner spawns the client and scenario servers; end the whole group.
        try:
            os.killpg(child.pid, signal.SIGKILL)  # windows-footgun: ok - the runner refuses Windows
        except OSError:
            pass
        stdout, stderr = await child.communicate()
    code = child.returncode if child.returncode is not None and child.returncode >= 0 else 128
    return code, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def conformanceEnv() -> dict[str, str]:
    # The suite comes from npm with prebuilt files; nothing needs install scripts.
    return {**os.environ, "npm_config_ignore_scripts": "true", "npm_config_yes": "true"}


async def listScenarios(mode: str, workDir: str) -> list[str]:
    code, stdout, stderr = await runCommand(
        "npx", ["--yes", CONFORMANCE_PACKAGE, "list", "--client", "--spec-version", mode], cwd=workDir, env=conformanceEnv()
    )
    if code != 0:
        raise RuntimeError(f"Listing {mode} scenarios failed:\n{stderr or stdout}")
    return re.findall(r"(?m)^\s+- (\S+)", stdout)


def findFile(directory: Path, name: str) -> Path | None:
    if not directory.exists():
        return None
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.name == name:
            return entry
        if entry.is_dir():
            found = findFile(entry, name)
            if found is not None:
                return found
    return None


def record(result: ScenarioResult, id: str, status: str, message: str | None = None) -> None:
    if result.checks.get(id) != "fail":
        result.checks[id] = status
    if status == "fail" and message and id not in result.messages:
        result.messages[id] = message


async def runScenario(mode: str, scenario: str, launcher: str, workDir: str, verbose: bool) -> ScenarioResult:
    scenarioDir = Path(workDir) / mode / re.sub(r"[^A-Za-z0-9_.-]+", "-", scenario)
    scenarioDir.mkdir(parents=True, exist_ok=True)
    reportPath = scenarioDir / "pi-client.json"
    result = ScenarioResult(mode, scenario)
    code, _stdout, stderr = await runCommand(
        "npx",
        [
            "--yes", CONFORMANCE_PACKAGE, "client", "--command", launcher, "--scenario", scenario,
            "--spec-version", mode, "--timeout", str(SCENARIO_TIMEOUT_MS), "--output-dir", str(scenarioDir),
        ],
        cwd=workDir,
        env={**conformanceEnv(), "MISAKA_MCP_CONFORMANCE_REPORT": str(reportPath)},
        timeoutMs=SCENARIO_TIMEOUT_MS + 30_000,
    )
    if verbose:
        sys.stderr.write(stderr)

    checksPath = findFile(scenarioDir, "checks.json")
    if checksPath is None:
        record(result, "runner", "fail", f"No checks.json (runner exit {code}): {stderr.strip()[-2000:]}")
        return result
    for check in json.loads(checksPath.read_text(encoding="utf-8")):
        status = str(check.get("status") or "FAILURE").upper()
        if status in IGNORED_STATUSES:
            continue
        id = str(check.get("id") or check.get("name") or "unnamed")
        message = str(check.get("errorMessage") or check.get("description") or "")
        record(result, id, "pass" if status == "SUCCESS" else "fail", message)
    # Whether misaka completed the scenario. Some scenarios expect the client to give up, so this is
    # baselined like any other check.
    client: dict[str, Any] = {"error": "client.py wrote no report"}
    if reportPath.exists():
        client = json.loads(reportPath.read_text(encoding="utf-8"))
    record(result, "pi-client", "pass" if client.get("success") is True else "fail", str(client.get("error") or ""))
    return result


def loadBaseline() -> dict[str, Any] | None:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.exists() else None


def toBaseline(results: list[ScenarioResult]) -> dict[str, Any]:
    baseline: dict[str, Any] = {"conformance": CONFORMANCE_PACKAGE, "modes": {}}
    for result in results:
        ids = sorted(result.checks)
        baseline["modes"].setdefault(result.mode, {})[result.scenario] = {
            "passing": [id for id in ids if result.checks[id] == "pass"],
            "failing": [id for id in ids if result.checks[id] == "fail"],
        }
    return baseline


def evaluate(results: list[ScenarioResult], baseline: dict[str, Any]) -> bool:
    """Compare with the baseline. Returns whether there is no regression."""
    regressions: list[str] = []
    fixed: list[str] = []
    for result in results:
        expected = baseline["modes"].get(result.mode, {}).get(result.scenario)
        label = f"{result.mode} {result.scenario}"
        for id, status in result.checks.items():
            message = result.messages.get(id)
            if status == "fail" and not (expected and id in expected["failing"]):
                regressions.append(f"{label}: {id} fails{f': {message}' if message else ''}")
            if status == "pass" and expected and id in expected["failing"]:
                fixed.append(f"{label}: {id}")
        for id in (expected or {}).get("passing", []):
            if id not in result.checks:
                regressions.append(f"{label}: {id} was not reported")
    for mode, scenarios in baseline["modes"].items():
        for scenario in scenarios:
            if not any(result.mode == mode and result.scenario == scenario for result in results):
                regressions.append(f"{mode} {scenario}: scenario did not run")
    if fixed:
        print("\nFixed since the baseline (run with --update-baseline):\n" + "\n".join(f"  {line}" for line in fixed))
    if regressions:
        print("\nRegressions:\n" + "\n".join(f"  {line}" for line in regressions))
        return False
    print("\nNo regressions against the baseline.")
    return True


def printResults(results: list[ScenarioResult]) -> None:
    for mode in MODES:
        modeResults = [result for result in results if result.mode == mode]
        if not modeResults:
            continue
        print(f"\n{mode}")
        for result in modeResults:
            failed = sum(1 for status in result.checks.values() if status == "fail")
            passed = len(result.checks) - failed
            print(f"  {'pass' if failed == 0 else 'FAIL'} {result.scenario} ({passed} passed, {failed} failed)")
            for id, status in result.checks.items():
                if status != "fail":
                    continue
                message = re.sub(r"\s+", " ", result.messages.get(id, ""))[:300]
                print(f"         {id}{f': {message}' if message else ''}")


async def main() -> int:
    parser = argparse.ArgumentParser(prog="tests/mcp_conformance/run.py")
    parser.add_argument("--mode", action="append", help=f"Only run this protocol version (repeatable): {', '.join(MODES)}")
    parser.add_argument("--scenario", action="append", help="Only run this scenario (repeatable)")
    parser.add_argument("--update-baseline", action="store_true", help="Write the results to baseline.json instead of comparing")
    parser.add_argument("--keep-results", action="store_true", help="Keep the upstream checks.json and client output")
    parser.add_argument("--verbose", action="store_true", help="Print the upstream runner output")
    values = parser.parse_args()
    if sys.platform == "win32":
        print("The conformance runner needs a POSIX shell.", file=sys.stderr)
        return 1
    modes = values.mode or list(MODES)
    for mode in modes:
        if mode not in MODES:
            print(f"Unsupported mode {mode}. Supported: {', '.join(MODES)}", file=sys.stderr)
            return 1
    partial = values.mode is not None or values.scenario is not None
    if values.update_baseline and partial:
        print("--update-baseline needs a full run, without --mode or --scenario.", file=sys.stderr)
        return 1

    workDir = tempfile.mkdtemp(prefix="misaka-mcp-conformance-")
    try:
        # The upstream runner splits --command on spaces, so use a launcher path without any.
        launcher = Path(workDir) / "pi-client"
        launcher.write_text(f'#!/bin/sh\nexec {json.dumps(sys.executable)} {json.dumps(str(CLIENT_PATH))} "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)

        results: list[ScenarioResult] = []
        for mode in modes:
            scenarios = [s for s in await listScenarios(mode, workDir) if not values.scenario or s in values.scenario]
            for scenario in scenarios:
                sys.stderr.write(f"{mode} {scenario}\n")
                results.append(await runScenario(mode, scenario, str(launcher), workDir, values.verbose))
        if not results:
            print("No scenarios matched.", file=sys.stderr)
            return 1
        printResults(results)

        if values.update_baseline:
            BASELINE_PATH.write_text(f"{json.dumps(toBaseline(results), indent=chr(9))}\n", encoding="utf-8")
            print(f"\nWrote {BASELINE_PATH}")
            return 0
        baseline = loadBaseline()
        if baseline is None:
            print("\nNo baseline.json. Run with --update-baseline and review it.", file=sys.stderr)
            return 1
        if baseline["conformance"] != CONFORMANCE_PACKAGE:
            print(f"\nbaseline.json is for {baseline['conformance']}, not {CONFORMANCE_PACKAGE}. Regenerate it.", file=sys.stderr)
            return 1
        scoped = (
            {
                "conformance": baseline["conformance"],
                "modes": {
                    mode: {
                        scenario: data
                        for scenario, data in baseline["modes"].get(mode, {}).items()
                        if not values.scenario or scenario in values.scenario
                    }
                    for mode in modes
                },
            }
            if partial
            else baseline
        )
        return 0 if evaluate(results, scoped) else 1
    finally:
        if values.keep_results:
            print(f"\nResults kept in {workDir}")
        else:
            shutil.rmtree(workDir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
