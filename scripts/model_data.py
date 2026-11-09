"""Translated from pi's ``packages/ai/scripts/model-data.ts`` (v1.1.0).

MISAKA fork: the layout is misaka's. pi's ``src/models.generated.ts`` is ``misaka/ai/models_generated.py``, a provider's
``src/providers/<id>.models.ts`` is ``misaka/ai/providers/<module>_models.py`` (the id with ``-`` as ``_``), and the
data stays in ``misaka/ai/providers/data``. pi finds the provider ids in the aggregator's import lines; a Python module
name cannot hold a ``-``, so they come from the aggregator's ``MODELS`` table instead.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from misaka.utils.js import jsObjectOrder, jsonParse, jsonStringify

MODEL_DATA_SCHEMA_VERSION = 6
MODEL_DATA_MANIFEST_FILE = ".manifest.json"

type ModelDataStructure = dict[str, dict[str, str]]

AGGREGATOR = Path("misaka", "ai", "models_generated.py")
PROVIDERS = Path("misaka", "ai", "providers")
DATA = PROVIDERS / "data"

# The aggregator's chat table: `MODELS = {`, one `"<id>": <CONSTANT>_MODELS,` line per provider, `}`.
MODEL_DATA_TABLE_PATTERN = re.compile(r'^MODELS = \{\n((?:    "[^"\n]+": [A-Z][A-Z0-9_]*_MODELS,\n)*)\}$', re.MULTILINE)
MODEL_DATA_ENTRY_PATTERN = re.compile(r'^    "([^"\n]+)": ', re.MULTILINE)


def providerModule(providerId: str) -> str:
    """The module of a provider's catalog shard: pi's ``<id>.models.ts``."""
    return providerId.replace("-", "_") + "_models"


def _utf16(value: str) -> bytes:
    return value.encode("utf-16-be", "surrogatepass")


def _sortStrings(values: Iterable[str]) -> list[str]:
    """``array.sort()``: by UTF-16 code units."""
    return sorted(values, key=_utf16)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sortedRecord(entries: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    return dict(sorted(entries, key=lambda entry: _utf16(entry[0])))


def sameStrings(a: list[str], b: list[str]) -> bool:
    return len(a) == len(b) and all(value == b[index] for index, value in enumerate(a))


def describeSetDifference(expected: list[str], actual: list[str]) -> str:
    expectedSet = set(expected)
    actualSet = set(actual)
    missing = [value for value in expected if value not in actualSet]
    extra = [value for value in actual if value not in expectedSet]
    return "; ".join(
        part
        for part in [
            f"missing: {', '.join(missing)}" if len(missing) > 0 else "",
            f"extra: {', '.join(extra)}" if len(extra) > 0 else "",
        ]
        if part
    )


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def assertExactModelIds(label: str, expected: Iterable[str], actual: Iterable[str]) -> None:
    expectedIds = _sortStrings(_unique(expected))
    actualIds = _sortStrings(_unique(actual))
    if sameStrings(expectedIds, actualIds):
        return
    raise RuntimeError(f"{label} model IDs do not match ({describeSetDifference(expectedIds, actualIds)})")


def isRecord(value: Any) -> bool:
    return isinstance(value, dict)


def isModalityList(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0 and all(entry == "text" or entry == "image" for entry in value)


def _readText(path: Path | str) -> str:
    """``readFileSync(path, "utf8")``: invalid UTF-8 becomes U+FFFD."""
    with open(path, encoding="utf-8", errors="replace", newline="") as file:
        return file.read()


def readJsonObject(path: Path | str, description: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        parsed = jsObjectOrder(jsonParse(_readText(path)))
    except (OSError, ValueError) as error:
        errors.append(f"{description} is not valid JSON: {error}")
        return None
    if not isRecord(parsed):
        errors.append(f"{description} must contain a JSON object")
        return None
    return parsed


def readProviderStructure(path: Path, providerId: str) -> dict[str, str]:
    errors: list[str] = []
    groups = readJsonObject(path, f"{providerId}.json", errors)
    if groups is None:
        raise RuntimeError("\n".join(errors))

    models: dict[str, str] = {}
    for api, value in groups.items():
        if not isRecord(value):
            raise RuntimeError(f"{path} API group {jsonStringify(api)} must be an object")
        for modelKey in value:
            if modelKey in models:
                raise RuntimeError(f"{path} contains {modelKey} in more than one API group")
            models[modelKey] = api
    if len(models) == 0:
        raise RuntimeError(f"{path} contains no generated model data")
    return sortedRecord(models.items())


def readModelDataProviderIds(packageRoot: Path | str) -> list[str]:
    aggregatorPath = Path(packageRoot) / AGGREGATOR
    aggregator = _readText(aggregatorPath)
    table = MODEL_DATA_TABLE_PATTERN.search(aggregator)
    providerIds = _sortStrings(MODEL_DATA_ENTRY_PATTERN.findall(table.group(1)) if table else [])
    if len(providerIds) == 0:
        raise RuntimeError(f"No generated provider imports found in {aggregatorPath}")
    if len(set(providerIds)) != len(providerIds):
        raise RuntimeError(f"Generated model aggregator contains duplicate provider imports: {aggregatorPath}")
    return providerIds


def readModelDataStructure(packageRoot: Path | str) -> ModelDataStructure:
    providersDir = Path(packageRoot) / PROVIDERS
    dataDir = providersDir / "data"
    providerIds = readModelDataProviderIds(packageRoot)
    expectedShards = _sortStrings(f"{providerModule(providerId)}.py" for providerId in providerIds)
    actualShards = _sortStrings(entry for entry in os.listdir(providersDir) if entry.endswith("_models.py"))
    if not sameStrings(expectedShards, actualShards):
        raise RuntimeError(
            "Generated model aggregator and provider shards do not match "
            f"({describeSetDifference(expectedShards, actualShards)})"
        )

    return sortedRecord(
        (providerId, readProviderStructure(dataDir / f"{providerId}.json", providerId)) for providerId in providerIds
    )


def modelDataStructureHash(structure: ModelDataStructure) -> str:
    normalized = sortedRecord((providerId, sortedRecord(models.items())) for providerId, models in structure.items())
    return sha256(jsonStringify(normalized))


def createModelDataManifest(
    structure: ModelDataStructure,
    fileContents: dict[str, str],
    generatedAt: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": MODEL_DATA_SCHEMA_VERSION,
        "generatedAt": generatedAt,
        "structureHash": modelDataStructureHash(structure),
        "files": sortedRecord((file, sha256(content)) for file, content in fileContents.items()),
    }


def _isFiniteNumber(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def validateModelValue(value: Any, providerId: str, modelId: str, expectedApi: str, errors: list[str]) -> None:
    label = f"{providerId}/{modelId}"
    if not isRecord(value):
        errors.append(f"{label} must be an object")
        return
    if value.get("id") != modelId:
        errors.append(f"{label} has id {jsonStringify(value.get('id'))}, expected {jsonStringify(modelId)}")
    if value.get("provider") != providerId:
        errors.append(
            f"{label} has provider {jsonStringify(value.get('provider'))}, expected {jsonStringify(providerId)}"
        )
    if value.get("api") != expectedApi:
        errors.append(f"{label} has api {jsonStringify(value.get('api'))}, expected {jsonStringify(expectedApi)}")
    if not isinstance(value.get("name"), str) or len(value["name"]) == 0:
        errors.append(f"{label} has no model name")
    if not isinstance(value.get("baseUrl"), str):
        errors.append(f"{label} has no baseUrl string")
    if not isModalityList(value.get("input")):
        errors.append(f"{label} has invalid input modalities")
    if value.get("type") == "image":
        if not isModalityList(value.get("output")) or "image" not in value["output"]:
            errors.append(f"{label} has invalid output modalities")
    elif "output" in value:
        errors.append(f"{label} has unsupported output modalities")
    if value.get("type") == "chat":
        if not isinstance(value.get("reasoning"), bool):
            errors.append(f"{label} has no reasoning boolean")
        if not _isFiniteNumber(value.get("contextWindow")) or value["contextWindow"] <= 0:
            errors.append(f"{label} has invalid contextWindow")
        if not _isFiniteNumber(value.get("maxTokens")) or value["maxTokens"] <= 0:
            errors.append(f"{label} has invalid maxTokens")
    elif value.get("type") == "classifier":
        if not _isFiniteNumber(value.get("contextWindow")) or value["contextWindow"] <= 0:
            errors.append(f"{label} has invalid contextWindow")
    elif value.get("type") != "image":
        errors.append(
            f'{label} has type {jsonStringify(value.get("type"))}, expected "chat", "image", or "classifier"'
        )
    if not isRecord(value.get("cost")):
        errors.append(f"{label} has invalid cost metadata")
    else:
        for field in ["input", "output", "cacheRead", "cacheWrite"]:
            if not _isFiniteNumber(value["cost"].get(field)):
                errors.append(f"{label} has invalid cost.{field}")


def throwValidationErrors(errors: list[str]) -> None:
    visible = errors[:30]
    suffix = f"\n  ... and {len(errors) - len(visible)} more" if len(errors) > len(visible) else ""
    raise RuntimeError("Invalid generated model data:\n" + "\n".join(f"  - {error}" for error in visible) + suffix)


def _isDate(value: Any) -> bool:
    """``!Number.isNaN(Date.parse(value))`` for the timestamps a manifest holds. MISAKA fork: ISO 8601, the format the
    generator writes, where ``Date.parse`` also takes the formats of ``Date.prototype.toString``."""
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validateModelDataDirectory(structure: ModelDataStructure, dataDir: Path | str) -> None:
    dataDir = Path(dataDir)
    if not dataDir.is_dir():
        raise RuntimeError(f"Generated model data directory does not exist: {dataDir}")

    errors: list[str] = []
    expectedFiles = _sortStrings(f"{providerId}.json" for providerId in structure)
    actualFiles = _sortStrings(
        entry for entry in os.listdir(dataDir) if entry.endswith(".json") and entry != MODEL_DATA_MANIFEST_FILE
    )
    if not sameStrings(expectedFiles, actualFiles):
        errors.append(
            "provider data files do not match the generated catalog "
            f"({describeSetDifference(expectedFiles, actualFiles)})"
        )

    manifestPath = dataDir / MODEL_DATA_MANIFEST_FILE
    manifest = readJsonObject(manifestPath, "model data manifest", errors)
    if (manifest or {}).get("schemaVersion") != MODEL_DATA_SCHEMA_VERSION or isinstance(
        (manifest or {}).get("schemaVersion"), bool
    ):
        errors.append(
            f"model data schema is {jsonStringify((manifest or {}).get('schemaVersion'))}, "
            f"expected {MODEL_DATA_SCHEMA_VERSION}"
        )
    if not _isDate((manifest or {}).get("generatedAt")):
        errors.append("model data manifest has an invalid generation timestamp")
    expectedStructureHash = modelDataStructureHash(structure)
    if (manifest or {}).get("structureHash") != expectedStructureHash:
        errors.append("model data generation stamp does not match the generated catalog")
    manifestFiles = (manifest or {}).get("files") if isRecord((manifest or {}).get("files")) else None
    if manifestFiles is None:
        errors.append("model data manifest has no file hashes")
    else:
        manifestFileNames = _sortStrings(manifestFiles)
        if not sameStrings(expectedFiles, manifestFileNames):
            errors.append(
                "manifest file hashes do not match provider data files "
                f"({describeSetDifference(expectedFiles, manifestFileNames)})"
            )

    for providerId, expectedModels in structure.items():
        filename = f"{providerId}.json"
        path = dataDir / filename
        if not path.exists():
            continue
        content = _readText(path)
        if manifestFiles is not None and manifestFiles.get(filename) != sha256(content):
            errors.append(f"{filename} does not match its manifest hash")
        groups = readJsonObject(path, filename, errors)
        if groups is None:
            continue

        actualModels: dict[str, str] = {}
        for api, value in groups.items():
            if not isRecord(value):
                errors.append(f"{filename} API group {jsonStringify(api)} must be an object")
                continue
            for modelKey, model in value.items():
                if modelKey in actualModels:
                    errors.append(f"{providerId}/{modelKey} appears in more than one API group")
                    continue
                actualModels[modelKey] = api
                separator = modelKey.find(":")
                modelId = modelKey[separator + 1 :] if separator >= 0 else modelKey
                validateModelValue(model, providerId, modelId, api, errors)
                if isRecord(model) and modelKey != f"{_jsString(model.get('type'))}:{_jsString(model.get('id'))}":
                    errors.append(f"{providerId}/{modelKey} has mismatched type/id identity")

        expectedModelIds = _sortStrings(expectedModels)
        actualModelIds = _sortStrings(actualModels)
        if not sameStrings(expectedModelIds, actualModelIds):
            errors.append(
                f"{filename} model IDs do not match the generated catalog "
                f"({describeSetDifference(expectedModelIds, actualModelIds)})"
            )
        for modelId, expectedApi in expectedModels.items():
            actualApi = actualModels.get(modelId)
            if actualApi is not None and actualApi != expectedApi:
                errors.append(
                    f"{providerId}/{modelId} is grouped under API {jsonStringify(actualApi)}, "
                    f"expected {jsonStringify(expectedApi)}"
                )

    if len(errors) > 0:
        throwValidationErrors(errors)


def _jsString(value: Any) -> str:
    """``String(value)`` of a missing property: ``undefined``."""
    from misaka.utils.js import UNDEFINED, jsString

    return jsString(UNDEFINED if value is None else value)


def validateGeneratedModelData(packageRoot: Path | str) -> None:
    structure = readModelDataStructure(packageRoot)
    validateModelDataDirectory(structure, Path(packageRoot) / DATA)


def groupProviderModelData(providerId: str, models: list[dict[str, Any]]) -> dict[str, Any]:
    """Group one provider's typed catalog entries by API and key them by ``type:id``, the layout of
    ``misaka/ai/providers/data/<provider>.json``."""
    groups: dict[str, dict[str, Any]] = {}
    structure: dict[str, str] = {}
    for api in _sortStrings(_unique(model["api"] for model in models)):
        group: dict[str, Any] = {}
        for model in models:
            if model["api"] != api:
                continue
            identity = f"{_jsString(model.get('type'))}:{_jsString(model.get('id'))}"
            if group.get(identity):
                raise RuntimeError(f"{providerId}/{identity} has duplicate {api} catalog entries")
            group[identity] = model
            structure[identity] = api
        groups[api] = group
    return {"groups": groups, "structure": structure}


__all__ = [
    "AGGREGATOR",
    "DATA",
    "MODEL_DATA_MANIFEST_FILE",
    "MODEL_DATA_SCHEMA_VERSION",
    "PROVIDERS",
    "ModelDataStructure",
    "assertExactModelIds",
    "createModelDataManifest",
    "groupProviderModelData",
    "modelDataStructureHash",
    "providerModule",
    "readModelDataProviderIds",
    "readModelDataStructure",
    "validateGeneratedModelData",
    "validateModelDataDirectory",
]
