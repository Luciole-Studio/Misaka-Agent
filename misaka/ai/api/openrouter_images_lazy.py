"""Translated from pi's ``packages/ai/src/api/openrouter-images.lazy.ts`` (v1.1.0).

MISAKA fork: ``importlib.import_module`` stands in for the dynamic ``import()``; the object literal is a
``SimpleNamespace``.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

from misaka.ai.types import ProviderImages


def openrouterImagesApi() -> ProviderImages:
    async def generateImages(model: Any, context: Any, options: Any = None) -> Any:
        return await importlib.import_module("misaka.ai.api.openrouter_images").generateImages(model, context, options)

    return SimpleNamespace(generateImages=generateImages)  # type: ignore[return-value]


__all__ = ["openrouterImagesApi"]
