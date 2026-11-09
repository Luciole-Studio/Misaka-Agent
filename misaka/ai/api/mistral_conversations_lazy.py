"""Translated from pi's ``packages/ai/src/api/mistral-conversations.lazy.ts`` (v1.1.0).

MISAKA fork: ``importlib.import_module`` stands in for the dynamic ``import()``.
"""

from __future__ import annotations

import importlib
from typing import Any

from misaka.ai.api.lazy import lazyApi
from misaka.ai.types import ProviderStreams


async def _load() -> Any:
    return importlib.import_module("misaka.ai.api.mistral_conversations")


def mistralConversationsApi() -> ProviderStreams:
    return lazyApi(_load)


__all__ = ["mistralConversationsApi"]
