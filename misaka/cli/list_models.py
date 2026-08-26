"""List available models with optional fuzzy search."""

from __future__ import annotations

import sys

from misaka.core.auth_guidance import format_no_models_available_message
from misaka.ui.tui import fuzzyFilter

_YELLOW = "\x1b[33m"
_RESET = "\x1b[0m"


def format_token_count(count: int) -> str:
    if count >= 1_000_000:
        millions = count / 1_000_000
        return f"{millions:.1f}M" if millions % 1 else f"{int(millions)}M"
    if count >= 1_000:
        thousands = count / 1_000
        return f"{thousands:.1f}K" if thousands % 1 else f"{int(thousands)}K"
    return str(count)


async def list_models(
    model_registry: object,
    search_pattern: str | None = None,
) -> None:
    load_error = getattr(model_registry, "getError", lambda: None)()
    if load_error:
        print(f"{_YELLOW}Warning: errors loading models.json:\n{load_error}{_RESET}", file=sys.stderr)

    models = list(model_registry.getAvailable())
    if not models:
        print(format_no_models_available_message())
        return

    filtered_models = (
        fuzzyFilter(models, search_pattern, lambda model: f"{model.provider} {model.id}") if search_pattern else models
    )
    if not filtered_models:
        print(f'No models matching "{search_pattern}"')
        return

    filtered_models = sorted(filtered_models, key=lambda model: (model.provider, model.id))
    rows = [
        {
            "provider": model.provider,
            "model": model.id,
            "context": format_token_count(model.contextWindow),
            "maxOut": format_token_count(model.maxTokens),
            "thinking": "yes" if model.reasoning else "no",
            "images": "yes" if "image" in model.input else "no",
        }
        for model in filtered_models
    ]
    headers = {
        "provider": "provider",
        "model": "model",
        "context": "context",
        "maxOut": "max-out",
        "thinking": "thinking",
        "images": "images",
    }
    widths = {
        key: max(len(headers[key]), *(len(row[key]) for row in rows))
        for key in ("provider", "model", "context", "maxOut", "thinking", "images")
    }

    header_line = "  ".join(
        [
            headers["provider"].ljust(widths["provider"]),
            headers["model"].ljust(widths["model"]),
            headers["context"].ljust(widths["context"]),
            headers["maxOut"].ljust(widths["maxOut"]),
            headers["thinking"].ljust(widths["thinking"]),
            headers["images"].ljust(widths["images"]),
        ]
    )
    print(header_line)
    for row in rows:
        print(
            "  ".join(
                [
                    row["provider"].ljust(widths["provider"]),
                    row["model"].ljust(widths["model"]),
                    row["context"].ljust(widths["context"]),
                    row["maxOut"].ljust(widths["maxOut"]),
                    row["thinking"].ljust(widths["thinking"]),
                    row["images"].ljust(widths["images"]),
                ]
            )
        )

listModels = list_models

__all__ = ["listModels"]
