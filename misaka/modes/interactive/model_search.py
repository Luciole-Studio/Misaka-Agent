"""Search text for model pickers, translated from pi's ``modes/interactive/model-search.ts``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ModelSearchItem:
    id: str
    provider: str
    name: str | None = None


def getModelSearchText(item: ModelSearchItem) -> str:
    id, provider = item.id, item.provider
    name = f" {item.name}" if item.name else ""
    return f"{id} {provider} {provider}/{id} {provider} {id}{name}"


def getModelSelectorSearchText(item: ModelSearchItem) -> str:
    """The /model selector search should rank exact provider-prefixed queries before proxy-provider IDs
    like openrouter/openai/gpt-5, so keep the bare model ID out of the leading position.
    """
    id, provider = item.id, item.provider
    name = f" {item.name}" if item.name else ""
    return f"{provider} {provider}/{id} {provider} {id}{name}"


__all__ = ["ModelSearchItem", "getModelSearchText", "getModelSelectorSearchText"]
