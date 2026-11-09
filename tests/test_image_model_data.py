"""Translated from pi's ``packages/ai/test/image-model-data.test.ts`` (v1.1.0)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# pi's tests import `../scripts/<name>.ts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from openrouter_catalog import buildOpenRouterCatalog

imageOnly: dict[str, Any] = {
    "id": "example/image-model",
    "name": "Example Image Model",
    "architecture": {
        "input_modalities": ["text", "image"],
        "output_modalities": ["image"],
    },
    "pricing": {
        "prompt": "0.000001",
        "completion": "0.000002",
    },
}

chatWithImages: dict[str, Any] = {
    "id": "example/multimodal",
    "name": "Example Multimodal",
    "supported_parameters": ["tools"],
    "architecture": {
        "modality": "text+image->text+image",
        "input_modalities": ["text", "image"],
        "output_modalities": ["text", "image"],
    },
    "context_length": 32000,
}

chatOnly: dict[str, Any] = {
    "id": "example/chat",
    "name": "Example Chat",
    "supported_parameters": ["tools"],
    "architecture": {"modality": "text->text", "output_modalities": ["text"]},
}

decisionModel: dict[str, Any] = {
    "id": "typesafe/jev-1.13",
    "name": "TypeSafe: Jev 1.13",
    "supported_parameters": [],
    "architecture": {"modality": "text->decisions", "input_modalities": ["text"], "output_modalities": ["decisions"]},
    "pricing": {"prompt": "0.000000042", "completion": "0"},
    "context_length": 32000,
    "top_provider": {"context_length": 32000, "max_completion_tokens": 28800},
}


class TestOpenRouterCatalogParsing:
    def test_emits_image_only_models_from_the_image_listing_as_image_models(self) -> None:
        catalog = buildOpenRouterCatalog([], [imageOnly], [])
        assert catalog["chat"] == []
        assert len(catalog["images"]) == 1
        image = catalog["images"][0]
        assert image["type"] == "image"
        assert image["id"] == "example/image-model"
        assert image["api"] == "openrouter-images"
        assert image["input"] == ["text", "image"]
        assert image["output"] == ["image"]
        assert image["cost"]["input"] == 1
        assert image["cost"]["output"] == 2

    def test_emits_separate_chat_and_image_entries_for_an_id_that_supports_both_operations(self) -> None:
        catalog = buildOpenRouterCatalog([chatWithImages, chatOnly], [chatWithImages, imageOnly], [])
        assert [model["id"] for model in catalog["chat"]] == ["example/multimodal", "example/chat"]
        assert [model["type"] for model in catalog["chat"]] == ["chat", "chat"]
        assert [model["id"] for model in catalog["images"]] == ["example/multimodal", "example/image-model"]
        assert all(model["type"] == "image" for model in catalog["images"])
        assert [model["output"] for model in catalog["images"]] == [["text", "image"], ["image"]]

    def test_ignores_listed_models_that_neither_support_tools_nor_emit_images(self) -> None:
        catalog = buildOpenRouterCatalog(
            [{**chatOnly, "supported_parameters": []}],
            [{**imageOnly, "architecture": {"output_modalities": ["text"]}}],
            [{**decisionModel, "architecture": {"output_modalities": ["text"]}}],
        )
        assert catalog["chat"] == []
        assert catalog["images"] == []
        assert catalog["classifiers"] == []

    def test_emits_decision_models_as_system_one_classifier_models(self) -> None:
        catalog = buildOpenRouterCatalog([], [], [decisionModel, decisionModel])
        assert catalog["chat"] == []
        assert catalog["classifiers"] == [
            {
                "type": "classifier",
                "id": "typesafe/jev-1.13",
                "name": "TypeSafe: Jev 1.13",
                "api": "typesafe-system-one",
                "provider": "openrouter",
                "baseUrl": "https://openrouter.ai/api/v1",
                "input": ["text"],
                "cost": {"input": 0.042, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 32000,
            },
        ]
