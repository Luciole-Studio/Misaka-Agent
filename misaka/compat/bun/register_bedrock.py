"""Eagerly register the Bedrock provider module for the Bun-style wrapper."""

from __future__ import annotations

from misaka.ai import setBedrockProviderModule
from misaka.ai.bedrock_provider import bedrockProviderModule


def register_bedrock() -> None:
    setBedrockProviderModule(bedrockProviderModule)


registerBedrock = register_bedrock
register_bedrock()

__all__: list[str] = []
