"""Translated from pi's ``packages/ai/test/model-data-validation.test.ts`` (v1.1.0).

MISAKA fork: the fixture is misaka's layout (``scripts/model_data.py``): the aggregator ``misaka/ai/models_generated.py``
names its providers in the ``MODELS`` table, and a shard is ``misaka/ai/providers/<module>_models.py``.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from misaka.utils.js import jsonStringify

# pi's tests import `../scripts/<name>.ts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from hydrate_model_catalog import hydrateModelCatalog
from model_data import (
    MODEL_DATA_MANIFEST_FILE,
    MODEL_DATA_SCHEMA_VERSION,
    assertExactModelIds,
    createModelDataManifest,
    readModelDataStructure,
    validateModelDataDirectory,
)

GENERATED_AT = "2026-07-23T10:00:00.000Z"

AGGREGATOR = '''from misaka.ai.providers.test_provider_models import (
    TEST_PROVIDER_CLASSIFIER_MODELS,
    TEST_PROVIDER_IMAGE_MODELS,
    TEST_PROVIDER_MODELS,
)

MODELS = {
    "test-provider": TEST_PROVIDER_MODELS,
}
'''


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="")


def createFixture(tmp_path: Path) -> dict[str, Any]:
    packageRoot = tmp_path / "pi-model-data"
    providersDir = packageRoot / "misaka" / "ai" / "providers"
    dataDir = providersDir / "data"
    dataDir.mkdir(parents=True)
    _write(packageRoot / "misaka" / "ai" / "models_generated.py", AGGREGATOR)
    _write(
        providersDir / "test_provider_models.py",
        '_values = json.loads((Path(__file__).parent / "data" / "test-provider.json").read_text(encoding="utf-8"))\n',
    )

    structure = {
        "test-provider": {
            "chat:model-a": "openai-completions",
        },
    }
    values: dict[str, Any] = {
        "chat:model-a": {
            "type": "chat",
            "id": "model-a",
            "name": "Model A",
            "api": "openai-completions",
            "provider": "test-provider",
            "baseUrl": "https://example.test/v1",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 1000,
            "maxTokens": 100,
        },
    }
    writeFixtureData(dataDir, structure, values)
    return {"dataDir": dataDir, "packageRoot": packageRoot, "structure": structure, "values": values}


def writeFixtureData(
    dataDir: Path,
    structure: dict[str, dict[str, str]],
    values: dict[str, Any],
    manifestSchemaVersion: int = MODEL_DATA_SCHEMA_VERSION,
    apiGroup: str = "openai-completions",
) -> None:
    filename = "test-provider.json"
    content = f"{jsonStringify({apiGroup: values})}\n"
    _write(dataDir / filename, content)
    manifest = createModelDataManifest(structure, {filename: content}, GENERATED_AT)
    manifest["schemaVersion"] = manifestSchemaVersion
    _write(dataDir / MODEL_DATA_MANIFEST_FILE, f"{jsonStringify(manifest)}\n")


def chatModel(values: dict[str, Any]) -> dict[str, Any]:
    return values["chat:model-a"]


class TestPublishedModelCatalogHydration:
    def test_hydrates_a_clean_checkout_deterministically_without_changing_generated_modules(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        dataDir, packageRoot = fixture["dataDir"], fixture["packageRoot"]
        aggregatorPath = packageRoot / "misaka" / "ai" / "models_generated.py"
        aggregator = _read(aggregatorPath)
        catalogPath = packageRoot / "catalog.json"
        models = [chatModel(fixture["values"])]
        _write(catalogPath, json.dumps({"test-provider": models, "extra-provider": models}))
        shutil.rmtree(dataDir)

        hydrateModelCatalog(packageRoot, catalogPath)
        validateModelDataDirectory(fixture["structure"], dataDir)
        assert readModelDataStructure(packageRoot) == fixture["structure"]
        assert _read(aggregatorPath) == aggregator
        manifest = _read(dataDir / MODEL_DATA_MANIFEST_FILE)
        provider = _read(dataDir / "test-provider.json")
        hydrateModelCatalog(packageRoot, catalogPath)
        assert _read(dataDir / MODEL_DATA_MANIFEST_FILE) == manifest
        assert _read(dataDir / "test-provider.json") == provider

    def test_groups_models_by_api_and_keys_them_by_type_and_id(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        packageRoot = fixture["packageRoot"]
        catalogPath = packageRoot / "catalog.json"
        model = chatModel(fixture["values"])
        _write(
            catalogPath,
            json.dumps({"test-provider": [model, {**model, "id": "model-b", "api": "anthropic-messages"}]}),
        )
        hydrateModelCatalog(packageRoot, catalogPath)
        assert readModelDataStructure(packageRoot) == {
            "test-provider": {"chat:model-a": "openai-completions", "chat:model-b": "anthropic-messages"},
        }

    def test_keys_chat_and_image_models_with_the_same_id_separately(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        packageRoot = fixture["packageRoot"]
        image = {
            "type": "image",
            "id": "model-a",
            "name": "Model A Image",
            "api": "openrouter-images",
            "provider": "test-provider",
            "baseUrl": "https://example.test/v1",
            "input": ["text"],
            "output": ["image"],
            "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        }
        catalogPath = packageRoot / "catalog.json"
        _write(catalogPath, json.dumps({"test-provider": [chatModel(fixture["values"]), image]}))
        hydrateModelCatalog(packageRoot, catalogPath)
        assert readModelDataStructure(packageRoot) == {
            "test-provider": {"chat:model-a": "openai-completions", "image:model-a": "openrouter-images"},
        }

    def test_validates_without_replacing_existing_data(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        dataDir, packageRoot = fixture["dataDir"], fixture["packageRoot"]
        original = _read(dataDir / "test-provider.json")
        catalogPath = packageRoot / "catalog.json"
        model = chatModel(fixture["values"])
        _write(catalogPath, json.dumps({"test-provider": [model, {**model, "id": "model-b"}]}))
        hydrateModelCatalog(packageRoot, catalogPath, {"validateOnly": True})
        assert _read(dataDir / "test-provider.json") == original
        _write(catalogPath, json.dumps({"other-provider": [model]}))
        with pytest.raises(RuntimeError, match="missing provider"):
            hydrateModelCatalog(packageRoot, catalogPath, {"validateOnly": True})

    @pytest.mark.parametrize(
        "catalog",
        [None, [], {}, {"test-provider": []}, {"test-provider": {}}, {"test-provider": [{"id": "x"}]}],
        ids=json.dumps,
    )
    def test_rejects_incomplete_catalogs_without_replacing_existing_data(self, tmp_path: Path, catalog: Any) -> None:
        fixture = createFixture(tmp_path)
        dataDir, packageRoot = fixture["dataDir"], fixture["packageRoot"]
        catalogPath = packageRoot / "catalog.json"
        original = _read(dataDir / MODEL_DATA_MANIFEST_FILE)
        _write(catalogPath, json.dumps(catalog))
        with pytest.raises(RuntimeError):
            hydrateModelCatalog(packageRoot, catalogPath)
        assert _read(dataDir / MODEL_DATA_MANIFEST_FILE) == original

    @pytest.mark.parametrize("field", ["api", "provider", "cost"])
    def test_validates_model_field_before_replacing_existing_data(self, tmp_path: Path, field: str) -> None:
        fixture = createFixture(tmp_path)
        dataDir, packageRoot = fixture["dataDir"], fixture["packageRoot"]
        original = _read(dataDir / "test-provider.json")
        model = chatModel(fixture["values"])
        model[field] = None
        catalogPath = packageRoot / "catalog.json"
        _write(catalogPath, json.dumps({"test-provider": [model]}))
        with pytest.raises(RuntimeError):
            hydrateModelCatalog(packageRoot, catalogPath)
        assert _read(dataDir / "test-provider.json") == original


class TestGeneratedModelDataValidation:
    def test_rejects_a_missing_upstream_model_from_an_exact_generated_allowlist(self) -> None:
        with pytest.raises(
            RuntimeError, match=re.escape("qwen-token-plan-individual model IDs do not match (missing: model-b)")
        ):
            assertExactModelIds("qwen-token-plan-individual", ["model-a", "model-b"], ["model-a"])

    def test_rejects_an_unexpected_model_from_an_exact_generated_allowlist(self) -> None:
        with pytest.raises(RuntimeError, match=re.escape("test-provider model IDs do not match (extra: model-b)")):
            assertExactModelIds("test-provider", ["model-a"], ["model-a", "model-b"])

    def test_reads_and_validates_api_grouped_model_data(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        assert readModelDataStructure(fixture["packageRoot"]) == fixture["structure"]
        validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_rejects_a_missing_model_data_directory(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        shutil.rmtree(fixture["dataDir"])
        with pytest.raises(RuntimeError, match="does not exist"):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    @pytest.mark.parametrize(
        ("field", "value", "expectedMessage"),
        [
            ("id", "wrong-id", "has id"),
            ("provider", "wrong-provider", "has provider"),
            ("api", "anthropic-messages", "has api"),
        ],
    )
    def test_rejects_a_wrong_model_field(self, tmp_path: Path, field: str, value: str, expectedMessage: str) -> None:
        fixture = createFixture(tmp_path)
        model = fixture["values"]["chat:model-a"]
        model[field] = value
        writeFixtureData(fixture["dataDir"], fixture["structure"], fixture["values"])
        with pytest.raises(RuntimeError, match=expectedMessage):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_rejects_a_model_without_a_known_type(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        model = fixture["values"]["chat:model-a"]
        del model["type"]
        writeFixtureData(fixture["dataDir"], fixture["structure"], fixture["values"])
        with pytest.raises(RuntimeError, match=re.escape('expected "chat", "image", or "classifier"')):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_validates_image_models_with_output_modalities_and_without_chat_limits(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        structure = {"test-provider": {"image:image-a": "test-images"}}
        image: dict[str, Any] = {
            "type": "image",
            "id": "image-a",
            "name": "Image A",
            "api": "test-images",
            "provider": "test-provider",
            "baseUrl": "https://example.test/v1",
            "input": ["text"],
            "output": ["image", "text"],
            "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        }

        def validate() -> None:
            writeFixtureData(
                fixture["dataDir"],
                structure,
                {"image:image-a": image},
                MODEL_DATA_SCHEMA_VERSION,
                "test-images",
            )
            validateModelDataDirectory(structure, fixture["dataDir"])

        validate()

        del image["output"]
        with pytest.raises(RuntimeError, match="invalid output modalities"):
            validate()
        image["output"] = ["text"]
        with pytest.raises(RuntimeError, match="invalid output modalities"):
            validate()

    def test_rejects_output_modalities_on_chat_models(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        model = fixture["values"]["chat:model-a"]
        model["output"] = ["text"]
        writeFixtureData(fixture["dataDir"], fixture["structure"], fixture["values"])
        with pytest.raises(RuntimeError, match="unsupported output modalities"):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_validates_classifier_models_without_chat_output_limits(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        structure = {
            "test-provider": {"classifier:classifier-a": "test-classifier"},
        }
        classifier = {
            "type": "classifier",
            "id": "classifier-a",
            "name": "Classifier A",
            "api": "test-classifier",
            "provider": "test-provider",
            "baseUrl": "https://example.test/v1",
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 1000,
        }
        writeFixtureData(
            fixture["dataDir"],
            structure,
            {"classifier:classifier-a": classifier},
            MODEL_DATA_SCHEMA_VERSION,
            "test-classifier",
        )
        validateModelDataDirectory(structure, fixture["dataDir"])

    def test_rejects_a_model_in_the_wrong_api_group(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        writeFixtureData(
            fixture["dataDir"],
            fixture["structure"],
            fixture["values"],
            MODEL_DATA_SCHEMA_VERSION,
            "anthropic-messages",
        )
        with pytest.raises(RuntimeError, match="grouped under API"):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_rejects_duplicate_model_ids_across_api_groups(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        filename = "test-provider.json"
        groups = {
            "openai-completions": fixture["values"],
            "anthropic-messages": fixture["values"],
        }
        content = f"{jsonStringify(groups)}\n"
        _write(fixture["dataDir"] / filename, content)
        manifest = createModelDataManifest(fixture["structure"], {filename: content}, GENERATED_AT)
        _write(fixture["dataDir"] / MODEL_DATA_MANIFEST_FILE, f"{jsonStringify(manifest)}\n")
        with pytest.raises(RuntimeError, match="more than one API group"):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_rejects_missing_model_ids_and_stale_file_hashes(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        _write(fixture["dataDir"] / "test-provider.json", "{}\n")
        with pytest.raises(RuntimeError, match="manifest hash|model IDs"):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_rejects_incompatible_schema_and_generation_stamps(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        writeFixtureData(fixture["dataDir"], fixture["structure"], fixture["values"], MODEL_DATA_SCHEMA_VERSION + 1)
        with pytest.raises(RuntimeError, match="model data schema"):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

        manifestPath = fixture["dataDir"] / MODEL_DATA_MANIFEST_FILE
        manifest = json.loads(_read(manifestPath))
        manifest["structureHash"] = "stale"
        _write(manifestPath, f"{jsonStringify(manifest)}\n")
        with pytest.raises(RuntimeError, match="generation stamp"):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_rejects_an_invalid_generation_timestamp(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        manifestPath = fixture["dataDir"] / MODEL_DATA_MANIFEST_FILE
        manifest = json.loads(_read(manifestPath))
        manifest["generatedAt"] = "invalid"
        _write(manifestPath, f"{jsonStringify(manifest)}\n")
        with pytest.raises(RuntimeError, match="generation timestamp"):
            validateModelDataDirectory(fixture["structure"], fixture["dataDir"])

    def test_rejects_missing_provider_shards_named_by_the_aggregator(self, tmp_path: Path) -> None:
        fixture = createFixture(tmp_path)
        packageRoot = fixture["packageRoot"]
        _write(
            packageRoot / "misaka" / "ai" / "models_generated.py",
            AGGREGATOR.replace(
                '    "test-provider": TEST_PROVIDER_MODELS,\n',
                '    "test-provider": TEST_PROVIDER_MODELS,\n    "missing": MISSING_MODELS,\n',
            ),
        )
        with pytest.raises(RuntimeError, match="aggregator and provider shards do not match"):
            readModelDataStructure(packageRoot)
