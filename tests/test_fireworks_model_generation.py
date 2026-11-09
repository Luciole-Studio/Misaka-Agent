"""Translated from pi's ``packages/ai/test/fireworks-model-generation.test.ts`` (v1.1.0).

MISAKA fork: pi preloads a module that replaces ``globalThis.fetch`` (``node --import``); here a preload script replaces
``misaka.utils.whatwg_fetch.fetch`` and then runs the generator.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from misaka.ai.api.anthropic_messages import streamSimple
from misaka.ai.models import getSupportedThinkingLevels, hasApi
from misaka.ai.types import Model
from misaka.ai.utils.transcript import normalizeContext

packageRoot = Path(__file__).resolve().parent.parent

PRELOAD = """import json
import runpy
import sys

from misaka.utils import whatwg_fetch

catalog = json.loads({catalog!r})


def responseJson(value):
    return whatwg_fetch.Response(json.dumps(value), status=200, headers={{"content-type": "application/json"}})


async def fetch(input, init=None):
    url = str(input)
    if url == "https://models.dev/api.json":
        return responseJson(catalog)
    if url == "https://models.dev/models.json?type=decision":
        return responseJson({{"typesafe/jev-latest": {{"name": "Jev", "type": "decision", "limit": {{"context": 64000, "output": 0}}}}}})
    if url.startswith("https://openrouter.ai/api/v1/models") or url == "https://ai-gateway.vercel.sh/v1/models":
        return responseJson({{"data": []}})
    if url == "https://radius.pi.dev/v1/config":
        return responseJson({{"baseUrl": "https://radius.pi.dev", "models": [{{"id": "test", "name": "Test", "reasoning": False, "input": ["text"], "cost": {{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}, "contextWindow": 4096, "maxTokens": 4096}}]}})
    raise RuntimeError(f"Unexpected fetch: {{url}}")


whatwg_fetch.fetch = fetch
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def generateFireworksModels(root: Path, options: dict[str, list[dict[str, Any]] | None]) -> dict[str, dict[str, Any]]:
    preloadPath = root / "mock_catalog.py"
    outputPath = root / "catalog"
    catalog = {
        "fireworks-ai": {
            "models": {
                f"accounts/fireworks/models/{id}": {
                    "id": id,
                    "tool_call": True,
                    "reasoning": True,
                    # `reasoning_options: undefined` is left out of the JSON.
                    **({"reasoning_options": reasoning_options} if reasoning_options is not None else {}),
                }
                for id, reasoning_options in options.items()
            },
        },
    }
    preloadPath.write_text(PRELOAD.format(catalog=json.dumps(catalog)), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(preloadPath),
            "scripts/generate_models.py",
            "--json-only",
            "--json-output",
            str(outputPath),
        ],
        cwd=packageRoot,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stderr == ""
    return json.loads((outputPath / "providers" / "fireworks.json").read_text(encoding="utf-8"))


class TestFireworksModelGeneration:
    # Regression for #9323: import catalog efforts and correct only the known omissions.
    def test_combines_upstream_effort_and_toggle_metadata_with_narrow_corrections(self, tmp_path: Path) -> None:
        models = generateFireworksModels(
            tmp_path,
            {
                "deepseek-v4-flash-0731": [{"type": "toggle"}, {"type": "effort", "values": ["low", "high", "max"]}],
                "deepseek-v4-flash-vision-exp": [{"type": "toggle"}, {"type": "effort", "values": ["low", "high", "max"]}],
                "deepseek-v4-pro-0813": [{"type": "toggle"}, {"type": "effort", "values": ["high", "max"]}],
                "qwen3p8-max": [{"type": "toggle"}],
                "qwen3p8-2p4t-a95b": [{"type": "effort", "values": ["low", "medium", "xhigh"]}],
                "kimi-k2p6": [{"type": "toggle"}],
            },
        )
        for id in ["deepseek-v4-flash-0731", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro-0813"]:
            assert models[f"accounts/fireworks/models/{id}"]["thinkingLevelMap"] == {
                "off": "none",
                "minimal": None,
                "low": "low",
                "medium": None,
                "high": "high",
                "xhigh": None,
                "max": "max",
            }
        for id in ["qwen3p8-max", "qwen3p8-2p4t-a95b"]:
            assert models[f"accounts/fireworks/models/{id}"]["thinkingLevelMap"] == {
                "off": "none",
                "minimal": None,
                "low": "low",
                "medium": "medium",
                "high": None,
                "xhigh": "xhigh",
                "max": None,
            }
        for model in models.values():
            if not hasApi(Model.model_validate(model), "anthropic-messages"):
                raise AssertionError("Expected Messages model")
            compat = model.get("compat") or {}
            assert compat.get("allowEmptySignature") is True
            assert compat.get("forceAdaptiveThinking") is (None if model["id"].endswith("kimi-k2p6") else True)
        assert "thinkingLevelMap" not in models["accounts/fireworks/models/kimi-k2p6"]

    # Regression for #9323: new effort-capable models must not require an allowlist update.
    @pytest.mark.asyncio
    async def test_automatically_sends_native_effort_for_newly_cataloged_messages_models(self, tmp_path: Path) -> None:
        models = generateFireworksModels(
            tmp_path,
            {
                "new-reasoner": [{"type": "toggle"}, {"type": "effort", "values": ["low", "max"]}],
            },
        )
        model = Model.model_validate(models["accounts/fireworks/models/new-reasoner"])
        if not hasApi(model, "anthropic-messages"):
            raise AssertionError("Expected Messages model")
        assert models["accounts/fireworks/models/new-reasoner"]["compat"]["forceAdaptiveThinking"] is True
        assert getSupportedThinkingLevels(model) == ["off", "low", "max"]
        payloads: list[dict[str, Any]] = []

        def onPayload(value: Any, _model: Any) -> Any:
            payloads.append(value)
            raise RuntimeError("payload captured")

        await streamSimple(
            model,
            normalizeContext({"messages": [{"role": "user", "content": "test", "timestamp": 0}]}),
            {
                "apiKey": "test-fireworks-key",
                "reasoning": "max",
                "onPayload": onPayload,
            },
        ).result()
        payload = payloads[0] if payloads else None
        assert payload is not None
        assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert payload["output_config"] == {"effort": "max"}

    def test_does_not_infer_adaptive_thinking_from_toggle_budget_or_missing_metadata(self, tmp_path: Path) -> None:
        models = generateFireworksModels(
            tmp_path,
            {
                "toggle-only": [{"type": "toggle"}],
                "budget-only": [{"type": "budget_tokens", "min": 1024}],
                "fixed-reasoning": [],
                "missing-metadata": None,
            },
        )
        for model in models.values():
            if not hasApi(Model.model_validate(model), "anthropic-messages"):
                raise AssertionError("Expected Messages model")
            assert (model.get("compat") or {}).get("forceAdaptiveThinking") is None
            assert "thinkingLevelMap" not in model

    # Regression for #9323: full hardcoded maps must not override updated catalog efforts.
    def test_prefers_updated_upstream_efforts_over_fixed_maps_and_the_qwen_fallback(self, tmp_path: Path) -> None:
        models = generateFireworksModels(
            tmp_path,
            {
                "deepseek-v4-flash-0731": [{"type": "effort", "values": ["high", "max"]}],
                "qwen3p8-max": [{"type": "toggle"}, {"type": "effort", "values": ["medium", "xhigh"]}],
            },
        )
        assert models["accounts/fireworks/models/deepseek-v4-flash-0731"]["thinkingLevelMap"] == {
            "off": None,
            "minimal": None,
            "low": None,
            "medium": None,
            "high": "high",
            "xhigh": None,
            "max": "max",
        }
        assert models["accounts/fireworks/models/qwen3p8-max"]["thinkingLevelMap"] == {
            "off": "none",
            "minimal": None,
            "low": None,
            "medium": "medium",
            "high": None,
            "xhigh": "xhigh",
            "max": None,
        }
