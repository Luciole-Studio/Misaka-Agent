"""Compatibility wrapper for the Bedrock provider module."""

from __future__ import annotations

from types import SimpleNamespace

from misaka.ai.providers.amazon_bedrock import stream_bedrock, stream_simple_bedrock

streamBedrock = stream_bedrock
streamSimpleBedrock = stream_simple_bedrock

bedrockProviderModule = SimpleNamespace(
    streamBedrock=streamBedrock,
    streamSimpleBedrock=streamSimpleBedrock,
)

__all__ = ["bedrockProviderModule"]
