"""Tavily web search (keyed, or keyless through the ring).

Ported from Hermes' ``plugins/web/tavily/provider.py`` (search half).

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "tavily"     # explicit per-capability
    "backend": "tavily"            # shared fallback
    "provider_tier": {"tavily": "free"|"paid"}

Env vars::

    TAVILY_API_KEY=...           # https://app.tavily.com/home (optional)
    TAVILY_BASE_URL=...          # optional override of https://api.tavily.com

Auth is header-based. A key uses ``Authorization: Bearer``; without a key the request is
keyless (``X-Tavily-Access-Mode: keyless``), which is what the ring dispatches.
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
from misaka.extensions.web.keyless import CLIENT_NAME, search_with_failover
from misaka.extensions.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _tavily_headers(api_key: str) -> dict[str, str]:
    """Build Tavily request headers for keyed or keyless access."""
    headers = {"X-Client-Name": CLIENT_NAME}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"
    return headers


async def tavily_request(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the Tavily API and return the parsed JSON response.

    Keyed when ``TAVILY_API_KEY`` is set (Bearer auth); otherwise keyless. Non-2xx
    responses raise ``ValueError`` with the response body so Tavily's keyless rate-limit
    and upgrade text reaches the model.
    """
    api_key = provider_env("TAVILY_API_KEY")
    base_url = provider_env("TAVILY_BASE_URL") or "https://api.tavily.com"
    url = f"{base_url}/{endpoint.lstrip('/')}"
    logger.info("Tavily %s request to %s", endpoint, url)

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=payload, headers=_tavily_headers(api_key))
    if response.status_code >= 400:
        body = (response.text or "").strip()
        detail = body or f"HTTP {response.status_code}"
        raise ValueError(detail)
    return response.json()


def normalize_search_results(response: dict[str, Any]) -> dict[str, Any]:
    """Map a Tavily ``/search`` response to ``{success, data: {web: [...]}}``."""
    web_results = []
    for i, result in enumerate(response.get("results", [])):
        web_results.append(
            {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "description": result.get("content", ""),
                "position": i + 1,
            }
        )
    return {"success": True, "data": {"web": web_results}}


class TavilyWebSearchProvider(WebSearchProvider):
    """Tavily search provider."""

    @property
    def name(self) -> str:
        return "tavily"

    @property
    def display_name(self) -> str:
        return "Tavily"

    def is_available(self) -> bool:
        """Return True when ``TAVILY_API_KEY`` is set to a non-empty value."""
        return bool(provider_env("TAVILY_API_KEY"))

    def is_keyless_available(self) -> bool:
        """Tavily serves anonymous keyless requests (X-Tavily-Access-Mode).

        Default-on ring member of the keyless free tier: fresh installs rotate across
        Exa/Parallel/Tavily/Firecrawl/Keenable. False when the user pinned
        ``"provider_tier": {"tavily": "paid"}`` -- an explicit paid selection opts the
        free endpoint out.
        """
        return keyless_tier_enabled() and provider_tier("tavily") != "paid"

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Tavily search."""
        try:
            if use_keyless("tavily", provider_env("TAVILY_API_KEY")):
                # Keyless free tier -- ring dispatch with next-in-line failover on rate
                # limits.
                logger.info("Tavily keyless search: '%s' (limit=%d)", query, limit)
                return await search_with_failover("tavily", query, limit)

            logger.info("Tavily search: '%s' (limit=%d)", query, limit)
            raw = await tavily_request(
                "search",
                {
                    "query": query,
                    "max_results": min(limit, 20),
                    "include_raw_content": False,
                    "include_images": False,
                },
            )
            return normalize_search_results(raw)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - including httpx errors, as in Hermes
            logger.warning("Tavily search error: %s", exc)
            return {"success": False, "error": f"Tavily search failed: {exc}"}

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "Tavily",
            "badge": "free - key optional",
            "tag": "Search. Works keyless; set TAVILY_API_KEY for higher limits.",
            "env_vars": [
                {
                    "key": "TAVILY_API_KEY",
                    "prompt": "Tavily API key (optional - keyless works without it)",
                    "url": "https://app.tavily.com/home",
                },
            ],
        }
