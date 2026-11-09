"""Translated from pi's ``packages/ai/src/api/openai-decisions.lazy.ts`` (v1.1.0).

MISAKA fork: ``importlib.import_module`` stands in for the dynamic ``import()``; the object literal is a
``SimpleNamespace``.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

from misaka.ai.types import ProviderClassifier


def openAIDecisionsApi() -> ProviderClassifier:
    async def classify(model: Any, context: Any, options: Any = None) -> Any:
        return await importlib.import_module("misaka.ai.api.openai_decisions").classify(model, context, options)

    return SimpleNamespace(classify=classify)  # type: ignore[return-value]


__all__ = ["openAIDecisionsApi"]
