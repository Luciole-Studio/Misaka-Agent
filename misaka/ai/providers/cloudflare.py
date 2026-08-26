"""Cloudflare provider URL helpers."""

from __future__ import annotations

import os
import re

from misaka.ai.types import Model

CLOUDFLARE_WORKERS_AI_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1"
CLOUDFLARE_AI_GATEWAY_COMPAT_BASE_URL = (
    "https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_GATEWAY_ID}/compat"
)
CLOUDFLARE_AI_GATEWAY_OPENAI_BASE_URL = (
    "https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_GATEWAY_ID}/openai"
)
CLOUDFLARE_AI_GATEWAY_ANTHROPIC_BASE_URL = (
    "https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_GATEWAY_ID}/anthropic"
)

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
    "CLOUDFLARE_WORKERS_AI_BASE_URL",
    "CLOUDFLARE_AI_GATEWAY_COMPAT_BASE_URL",
    "CLOUDFLARE_AI_GATEWAY_OPENAI_BASE_URL",
    "CLOUDFLARE_AI_GATEWAY_ANTHROPIC_BASE_URL",
    ]
