"""Translated from pi's ``packages/ai/src/api/bedrock-converse-stream.lazy.ts`` (v1.1.0).

MISAKA fork: ``importlib.import_module`` stands in for the variable-specifier dynamic ``import()``; there is no
bundler to hide the AWS SDK from, so ``importNodeOnlyApi``'s ``.ts``/``.js`` rewrite has no counterpart.
"""

from __future__ import annotations

import importlib
from typing import Any

from misaka.ai.api.lazy import lazyApi
from misaka.ai.types import ProviderStreams

_bedrockModuleOverride: ProviderStreams | None = None


def setBedrockProviderModule(module: ProviderStreams) -> None:
    """Overrides the dynamically imported bedrock implementation. Used by the Bun
    binary build, where the variable-specifier import cannot be bundled; the
    build registers a statically imported module instead."""
    global _bedrockModuleOverride
    _bedrockModuleOverride = module


async def _load() -> Any:
    if _bedrockModuleOverride is not None:
        return _bedrockModuleOverride
    return importlib.import_module("misaka.ai.api.bedrock_converse_stream")


def bedrockConverseStreamApi() -> ProviderStreams:
    return lazyApi(_load)


__all__ = ["bedrockConverseStreamApi", "setBedrockProviderModule"]
