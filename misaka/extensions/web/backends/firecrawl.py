"""Firecrawl web search (keyed or self-hosted over REST, or keyless through the ring).

Ported from the search half of Hermes' ``plugins/web/firecrawl/provider.py``. That file
is 832 lines because most of it is extraction, per-URL SSRF re-checks, website policy, and
the Nous tool-gateway; none of those have a MISAKA counterpart. What is kept is what
``web_search`` touches: credential resolution, the ``/v2/search`` call, and the
response-shape normalizer -- Firecrawl answers in three different shapes depending on
whether the caller used the SDK, the cloud API, or a self-hosted instance.

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "firecrawl"     # explicit per-capability
    "backend": "firecrawl"            # shared fallback (and the final default)
    "provider_tier": {"firecrawl": "free"|"paid"}

Env vars::

    FIRECRAWL_API_KEY=...            # direct cloud auth
    FIRECRAWL_API_URL=...            # self-hosted Firecrawl
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from misaka.extensions.web.config import (
    keyless_tier_enabled,
    provider_env,
    provider_tier,
    use_keyless,
)
from misaka.extensions.web.keyless import FIRECRAWL_API_URL, search_with_failover
from misaka.extensions.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _normalize_result_list(values: Any) -> list[dict[str, Any]]:
    """Normalize a mixed payload into a list of dicts."""
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def normalize_search_results(response: Any) -> list[dict[str, Any]]:
    """Extract Firecrawl search results across its response shapes, in contract form.

    Hermes hands the raw vendor dicts on unchanged, which quietly drops ``position``
    from every Firecrawl result -- the one place its own contract is not met. Numbering
    happens here instead, because the tool above renders by position and a missing key
    would silently renumber the list. Narrowing to the four contract keys is the other
    half of that: forwarding the vendor's extra fields is what let the omission hide.

    ``snippet`` is accepted beside ``description`` for the same reason the keyed and
    keyless paths share this function: a self-hosted Firecrawl is a different build from
    the cloud one, and the summary key is the field most likely to differ between them.
    """
    entries: list[dict[str, Any]] = []
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, list):
            entries = _normalize_result_list(data)
        elif isinstance(data, dict):
            entries = _normalize_result_list(data.get("web")) or _normalize_result_list(
                data.get("results")
            )
        if not entries:
            entries = _normalize_result_list(
                response.get("web")
            ) or _normalize_result_list(response.get("results"))

    return [
        {
            "title": str(entry.get("title") or ""),
            "url": str(entry.get("url") or ""),
            "description": str(entry.get("description") or entry.get("snippet") or ""),
            "position": i + 1,
        }
        for i, entry in enumerate(entries)
    ]


class FirecrawlWebSearchProvider(WebSearchProvider):
    """Firecrawl search provider."""

    @property
    def name(self) -> str:
        return "firecrawl"

    @property
    def display_name(self) -> str:
        return "Firecrawl"

    def is_available(self) -> bool:
        """Return True when a key or a self-hosted instance URL is configured."""
        return bool(provider_env("FIRECRAWL_API_KEY") or provider_env("FIRECRAWL_API_URL"))

    def is_keyless_available(self) -> bool:
        """Firecrawl's public cloud API accepts anonymous rate-limited requests.

        False when the user pinned ``"provider_tier": {"firecrawl": "paid"}``.
        """
        return keyless_tier_enabled() and provider_tier("firecrawl") != "paid"

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Firecrawl search."""
        try:
            api_key = provider_env("FIRECRAWL_API_KEY")
            api_url = provider_env("FIRECRAWL_API_URL").rstrip("/")
            # A self-hosted instance is a deliberate setup even without a key: it must
            # never be traded for the public cloud endpoint by the keyless ring.
            #
            # Divergence from Hermes, deliberate: its ``_use_keyless_ring()`` returns
            # False the moment FIRECRAWL_API_KEY is set, so a ``"firecrawl": "free"``
            # tier pin is silently ignored here and honoured by every other vendor.
            # Firecrawl is the odd one out in its own tree; ``use_keyless`` is the
            # documented chokepoint ("free forces the keyless endpoint even when the
            # vendor API key is present"), so this asks it like the other four do.
            if not api_url and use_keyless("firecrawl", api_key):
                logger.info("Firecrawl keyless search: '%s' (limit=%d)", query, limit)
                return await search_with_failover("firecrawl", query, limit)

            if not api_key and not api_url:
                return {
                    "success": False,
                    "error": (
                        "FIRECRAWL_API_KEY environment variable not set. "
                        "Get your API key at https://firecrawl.dev"
                    ),
                }

            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            logger.info("Firecrawl search: '%s' (limit=%d)", query, limit)
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{api_url or FIRECRAWL_API_URL}/v2/search",
                    json={"query": query, "limit": max(1, int(limit))},
                    headers=headers,
                )
            if response.status_code >= 400:
                detail = (response.text or "").strip() or f"HTTP {response.status_code}"
                return {"success": False, "error": f"Firecrawl search failed: {detail}"}
            return {
                "success": True,
                "data": {"web": normalize_search_results(response.json())},
            }
        except Exception as exc:  # noqa: BLE001 - surface as failure, as in Hermes
            logger.warning("Firecrawl search error: %s", exc)
            return {"success": False, "error": f"Firecrawl search failed: {exc}"}

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "Firecrawl",
            "badge": "free - key optional",
            "tag": "Search via Firecrawl cloud or a self-hosted instance.",
            "env_vars": [
                {
                    "key": "FIRECRAWL_API_KEY",
                    "prompt": "Firecrawl API key (optional - keyless cloud works without it)",
                    "url": "https://firecrawl.dev",
                },
                {
                    "key": "FIRECRAWL_API_URL",
                    "prompt": "Self-hosted Firecrawl URL",
                    "url": "https://docs.firecrawl.dev/contributing/self-host",
                },
            ],
        }
