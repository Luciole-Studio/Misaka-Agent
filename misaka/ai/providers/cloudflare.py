"""Cloudflare provider URL helpers."""

from __future__ import annotations

import os
import re

from misaka.ai.types import Model

_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Z_][A-Z0-9_]*)\}")


def is_cloudflare_provider(provider: str) -> bool:
    return provider in {"cloudflare-workers-ai", "cloudflare-ai-gateway"}


def resolve_cloudflare_base_url(model: Model) -> str:
    url = model.baseUrl
    if "{" not in url:
        return url

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"{name} is required for provider {model.provider} but is not set.")
        return value

    return _PLACEHOLDER_PATTERN.sub(replace, url)


__all__ = [
    ]
