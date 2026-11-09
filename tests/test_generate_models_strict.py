"""Translated from pi's ``packages/ai/test/generate-models-strict.test.ts`` (v1.1.0).

MISAKA fork: pi preloads a module that replaces ``globalThis.fetch`` (``node --import``); here a preload script replaces
``misaka.utils.whatwg_fetch.fetch`` and then runs the generator. The copied package is misaka's layout.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

packageRoot = Path(__file__).resolve().parent.parent

PRELOAD = """import json
import runpy
import sys

from misaka.utils import whatwg_fetch

catalog = json.loads({catalog!r})


async def fetch(input, init=None):
    if str(input) == "https://models.dev/api.json":
        return whatwg_fetch.Response(json.dumps(catalog), status=200)
    raise RuntimeError(f"Unexpected fetch: {{input}}")


whatwg_fetch.fetch = fetch
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


class TestStrictModelGeneration:
    def test_fails_before_mutating_generated_data_when_an_individual_model_loses_tool_support(
        self, tmp_path: Path
    ) -> None:
        fixtureRoot = tmp_path / "pi-generate-models"
        isolatedPackageRoot = fixtureRoot / "package"
        isolatedPackageRoot.mkdir(parents=True)
        shutil.copytree(packageRoot / "scripts", isolatedPackageRoot / "scripts")
        providers = isolatedPackageRoot / "misaka" / "ai" / "providers"
        shutil.copytree(packageRoot / "misaka" / "ai" / "providers" / "data", providers / "data")
        for shard in (packageRoot / "misaka" / "ai" / "providers").glob("*_models.py"):
            shutil.copyfile(shard, providers / shard.name)
        shutil.copyfile(
            packageRoot / "misaka" / "ai" / "models_generated.py",
            isolatedPackageRoot / "misaka" / "ai" / "models_generated.py",
        )
        preloadPath = fixtureRoot / "mock_models_dev.py"
        modelIds = [
            "deepseek-v4-flash-0731",
            "deepseek-v4-pro",
            "deepseek-v4-pro-0813",
            "glm-5.2",
            "qwen3.6-flash",
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen3.8-flash",
            "qwen3.8-max",
            "qwen3.8-max-preview",
        ]
        sourceModels = {
            id: {
                "id": id,
                "name": id,
                "tool_call": id != "deepseek-v4-flash-0731",
            }
            for id in modelIds
        }
        catalog = {"alibaba-token-plan": {"models": sourceModels}}
        preloadPath.write_text(PRELOAD.format(catalog=json.dumps(catalog)), encoding="utf-8")

        generatedPaths = [
            "misaka/ai/models_generated.py",
            "misaka/ai/providers/qwen_token_plan_individual_models.py",
            "misaka/ai/providers/data/qwen-token-plan-individual.json",
            "misaka/ai/providers/data/.manifest.json",
        ]
        sourceBefore = [(packageRoot / path).read_bytes() for path in generatedPaths]
        isolatedBefore = [(isolatedPackageRoot / path).read_bytes() for path in generatedPaths]

        result = subprocess.run(
            [sys.executable, str(preloadPath), "scripts/generate_models.py", "--strict"],
            cwd=isolatedPackageRoot,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )

        assert result.returncode == 1
        assert "qwen-token-plan-individual model IDs do not match (missing: deepseek-v4-flash-0731)" in (
            f"{result.stdout}\n{result.stderr}"
        )
        assert [(isolatedPackageRoot / path).read_bytes() for path in generatedPaths] == isolatedBefore
        assert [(packageRoot / path).read_bytes() for path in generatedPaths] == sourceBefore
