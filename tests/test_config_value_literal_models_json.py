"""Translated from pi's ``packages/coding-agent/test/config-value-migration.test.ts`` (v1.1.0).

MISAKA fork: MISAKA has no ``runMigrations`` (no backward-compatibility rewrites of configuration files), so the
migration step is not run and pi's auth.json case (which only checks that migrations leave the file alone) has no
counterpart. What these cases hold for the model registry is ported: a malformed or blank models.json is reported with
its path, and upper-case ``apiKey``/header values are literal values, not environment variable names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from misaka.core.auth_storage import AuthStorage
from tests.model_runtime_test_utils import createModelRegistry


@pytest.mark.parametrize(("_name", "content"), [("malformed", '{\n  "providers": {\n'), ("blank", "")])
async def test_reports_a_malformed_or_blank_models_json(_name: str, content: str, tmp_path: Path):
    agentDir = tmp_path
    modelsPath = str(agentDir / "models.json")
    Path(modelsPath).write_text(content, encoding="utf-8")

    assert Path(modelsPath).read_text(encoding="utf-8") == content
    registry = await createModelRegistry(AuthStorage.create(str(agentDir / "auth.json")), modelsPath)
    loadError = registry.getError()
    assert "Failed to parse models.json" in loadError
    assert f"File: {modelsPath}" in loadError


async def test_leaves_uppercase_models_json_api_key_and_header_values_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    agentDir = tmp_path
    for key in ["CUSTOM_API_KEY", "HEADER_API_KEY", "MODEL_API_KEY", "OVERRIDE_API_KEY"]:
        monkeypatch.setenv(key, f"env-{key}")

    (agentDir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "custom-provider": {
                        "baseUrl": "https://example.com/v1",
                        "apiKey": "CUSTOM_API_KEY",
                        "api": "openai-completions",
                        "headers": {
                            "x-api-key": "HEADER_API_KEY",
                            "x-literal": "literal",
                        },
                        "models": [
                            {
                                "id": "model-a",
                                "headers": {"x-model-key": "MODEL_API_KEY"},
                            },
                        ],
                        "modelOverrides": {
                            "model-b": {"headers": {"x-override-key": "OVERRIDE_API_KEY"}},
                        },
                    },
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    registry = await createModelRegistry(
        AuthStorage.create(str(agentDir / "auth.json")), os.path.join(agentDir, "models.json")
    )
    model = registry.find("custom-provider", "model-a")
    assert model is not None
    assert await registry.getApiKeyForProvider("custom-provider") == "CUSTOM_API_KEY"
    resolved = await registry.getApiKeyAndHeaders(model)
    assert resolved["ok"] is True
    assert resolved["apiKey"] == "CUSTOM_API_KEY"
    assert {key: resolved["headers"].get(key) for key in ("x-api-key", "x-literal", "x-model-key")} == {
        "x-api-key": "HEADER_API_KEY",
        "x-literal": "literal",
        "x-model-key": "MODEL_API_KEY",
    }
