"""Translated from pi's ``packages/ai/scripts/openrouter-catalog.ts`` (v1.3.0).

An OpenRouter list item (``OpenRouterModelListItem``) is the API's JSON object: the ``OpenRouterModel`` fields
(``id``, ``context_length``, ``top_provider``, ``pricing``, see ``misaka.ai.providers.openrouter_api``) plus ``name``,
``supported_parameters``, ``architecture`` and ``reasoning``; the catalog is
``{"chat": [...], "images": [...], "classifiers": [...]}`` of model records. A property the source lacks is
``UNDEFINED``, which ``jsonStringify`` leaves out as ``JSON.stringify`` does.
"""

from __future__ import annotations

import re
from typing import Any

from openrouter_reasoning_options import getOpenRouterThinkingLevelMap

from misaka.ai.providers.openrouter_api import openRouterCost
from misaka.utils.js import UNDEFINED


def modalities(values: list[Any] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        if (value == "text" or value == "image") and value not in result:
            result.append(value)
    return result


def _architecture(model: dict[str, Any]) -> dict[str, Any]:
    architecture = model.get("architecture")
    return architecture if isinstance(architecture, dict) else {}


def _contextWindow(model: dict[str, Any]) -> Any:
    topProvider = model.get("top_provider") or {}
    return topProvider.get("context_length") or model.get("context_length") or 4096


def buildOpenRouterCatalog(
    listed: list[dict[str, Any]],
    imageListed: list[dict[str, Any]],
    decisionListed: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build the OpenRouter catalog from the default listing and the ``output_modalities=image`` and
    ``output_modalities=decisions`` listings. The default listing omits image-only and decision models, so those come
    from the other listings. An upstream model may appear in several results; it then gets separate entries per
    operation."""
    chat: list[dict[str, Any]] = []

    for model in listed:
        # Only include models that support tools
        if "tools" not in (model.get("supported_parameters") or []):
            continue
        # Parse input modalities
        input = ["text"]
        modality = _architecture(model).get("modality")
        if isinstance(modality, str) and "image" in modality:
            input.append("image")

        thinkingLevelMap = getOpenRouterThinkingLevelMap(model.get("reasoning"))
        useAnthropicMessages = re.match(r"^anthropic/", model["id"]) is not None and not model["id"].endswith(":batch")
        chat.append(
            {
                "type": "chat",
                "id": model["id"],
                "name": model.get("name", UNDEFINED),
                "api": "anthropic-messages" if useAnthropicMessages else "openai-completions",
                "baseUrl": "https://openrouter.ai/api" if useAnthropicMessages else "https://openrouter.ai/api/v1",
                "provider": "openrouter",
                "reasoning": "reasoning" in (model.get("supported_parameters") or []) or False,
                **({"thinkingLevelMap": thinkingLevelMap} if thinkingLevelMap else {}),
                "input": input,
                "cost": openRouterCost(model.get("pricing")),
                "contextWindow": _contextWindow(model),
                "maxTokens": (model.get("top_provider") or {}).get("max_completion_tokens") or 4096,
            }
        )

    images: list[dict[str, Any]] = []
    for model in imageListed:
        if any(entry["id"] == model["id"] for entry in images):
            continue
        output = modalities(_architecture(model).get("output_modalities"))
        if "image" not in output:
            continue
        input = modalities(_architecture(model).get("input_modalities"))
        images.append(
            {
                "type": "image",
                "id": model["id"],
                "name": model.get("name", UNDEFINED),
                "api": "openrouter-images",
                "provider": "openrouter",
                "baseUrl": "https://openrouter.ai/api/v1",
                "input": input if len(input) > 0 else ["text"],
                "output": output,
                "cost": openRouterCost(model.get("pricing")),
            }
        )

    # Decision models such as TypeSafe's Jev are served through OpenRouter's
    # TypeSafe-compatible System One endpoint.
    classifiers: list[dict[str, Any]] = []
    for model in decisionListed:
        if any(entry["id"] == model["id"] for entry in classifiers):
            continue
        if "decisions" not in (_architecture(model).get("output_modalities") or []):
            continue
        input = modalities(_architecture(model).get("input_modalities"))
        classifiers.append(
            {
                "type": "classifier",
                "id": model["id"],
                "name": model.get("name", UNDEFINED),
                "api": "typesafe-system-one",
                "provider": "openrouter",
                "baseUrl": "https://openrouter.ai/api/v1",
                "input": input if len(input) > 0 else ["text"],
                "cost": openRouterCost(model.get("pricing")),
                "contextWindow": _contextWindow(model),
            }
        )

    return {"chat": chat, "images": images, "classifiers": classifiers}


__all__ = ["buildOpenRouterCatalog", "modalities"]
