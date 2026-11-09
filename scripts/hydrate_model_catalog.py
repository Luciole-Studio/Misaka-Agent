#!/usr/bin/env python3
"""Hydrate the checkout's provider data files from a published typed catalog (``models.all.json``) without network
access.

Translated from pi's ``packages/ai/scripts/hydrate-model-catalog.ts`` (v1.1.0)::

    python scripts/hydrate_model_catalog.py <models.all.json>
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# Sibling scripts, as pi's scripts import `./model-data.ts`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_data import (
    MODEL_DATA_MANIFEST_FILE,
    PROVIDERS,
    createModelDataManifest,
    groupProviderModelData,
    readModelDataProviderIds,
    validateModelDataDirectory,
)

from misaka.utils.js import jsObjectOrder, jsonParse, jsonStringify


def isRecord(value: Any) -> bool:
    return isinstance(value, dict)


def isCatalogEntry(value: Any) -> bool:
    return (
        isRecord(value)
        and isinstance(value.get("type"), str)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("api"), str)
        and len(value["api"]) > 0
    )


def _write(path: Path, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.write(content)


def hydrateModelCatalog(packageRoot: Path | str, catalogPath: Path | str, options: dict[str, Any] | None = None) -> None:
    """Hydrate the checkout's provider data files from a published typed catalog (``models.all.json``) without network
    access. With ``validateOnly``, stage and validate the data without replacing the checkout's current data."""
    options = options or {}
    with open(catalogPath, encoding="utf-8", errors="replace") as file:
        catalog = jsObjectOrder(jsonParse(file.read()))
    if not isRecord(catalog):
        raise RuntimeError("Model catalog must be an object")

    files: dict[str, str] = {}
    structure: dict[str, dict[str, str]] = {}
    for provider in readModelDataProviderIds(packageRoot):
        if provider not in catalog:
            raise RuntimeError(f"Model catalog is missing provider: {provider}")
        models = catalog[provider]
        if not isinstance(models, list) or len(models) == 0:
            raise RuntimeError(f"Model catalog has no typed model list for provider: {provider}")
        entries: list[dict[str, Any]] = []
        for model in models:
            if not isCatalogEntry(model):
                raise RuntimeError(f"Model catalog has an invalid entry for provider {provider}")
            entries.append(model)
        grouped = groupProviderModelData(provider, entries)
        structure[provider] = grouped["structure"]
        files[f"{provider}.json"] = f"{jsonStringify(grouped['groups'])}\n"

    # Public catalogs have no generation timestamp. Use a fixed stamp so hydration
    # produces identical bytes for identical input, regardless of build time.
    manifest = createModelDataManifest(structure, files, "1970-01-01T00:00:00.000Z")
    providersDir = Path(packageRoot) / PROVIDERS
    stagingRoot = Path(tempfile.mkdtemp(prefix=".model-hydration-", dir=providersDir))
    stagedData = stagingRoot / "data"
    try:
        os.mkdir(stagedData)
        for name, content in files.items():
            _write(stagedData / name, content)
        _write(stagedData / MODEL_DATA_MANIFEST_FILE, f"{jsonStringify(manifest)}\n")
        validateModelDataDirectory(structure, stagedData)
        if options.get("validateOnly"):
            return
        dataDir = providersDir / "data"
        shutil.rmtree(dataDir, ignore_errors=True)
        os.rename(stagedData, dataDir)
    finally:
        shutil.rmtree(stagingRoot, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("Usage: python scripts/hydrate_model_catalog.py <models.all.json>")
    hydrateModelCatalog(Path(__file__).resolve().parent.parent, Path(argv[1]).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
