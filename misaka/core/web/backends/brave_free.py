"""Brave Search (free tier).

Ported from Hermes' ``plugins/web/brave_free/provider.py``.

Config keys this provider responds to (the ``web`` section of ``settings.json``)::

    "search_backend": "brave-free"     # explicit per-capability
    "backend": "brave-free"            # shared fallback

Credential::

    BRAVE_SEARCH_API_KEY=...    # https://brave.com/search/api/ (free tier)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from misaka.core.web.accounting import account_call
from misaka.core.web.config import provider_env
from misaka.core.web.network import api_network_options
from misaka.core.web.provider import WebSearchProvider
from misaka.core.web.timeouts import http_timeout

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveFreeWebSearchProvider(WebSearchProvider):
    """Search-only Brave provider using the free-tier Data-for-Search API.

    Free tier is 2,000 queries/month (1 qps).
    """

    @property
    def name(self) -> str:
        # Hyphen form preserved for backward compat with the existing
        # ``"search_backend": "brave-free"`` config keys users have set.
        return "brave-free"

    @property
    def display_name(self) -> str:
        return "Brave Search (Free)"

    def is_available(self) -> bool:
        """Return True when ``BRAVE_SEARCH_API_KEY`` is set to a non-empty value."""
        return bool(provider_env("BRAVE_SEARCH_API_KEY"))

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a search against the Brave Search API."""
        api_key = provider_env("BRAVE_SEARCH_API_KEY")
        if not api_key:
            return {"success": False, "error": "BRAVE_SEARCH_API_KEY is not set"}

        # Brave's `count` is capped at 20.
        count = max(1, min(int(limit), 20))

        try:
            async with (
                httpx.AsyncClient(timeout=http_timeout("brave-free", 15), **api_network_options(_BRAVE_ENDPOINT)) as client,
                account_call("web_search", self.name, query),
            ):
                resp = await client.get(
                    _BRAVE_ENDPOINT,
                    params={"q": query, "count": count},
                    headers={
                        "X-Subscription-Token": api_key,
                        "Accept": "application/json",
                    },
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Brave Search HTTP error: %s", exc)
            return {
                "success": False,
                "error": f"Brave Search returned HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            logger.warning("Brave Search request error: %s", exc)
            return {"success": False, "error": f"Could not reach Brave Search: {exc}"}

        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("Brave Search response parse error: %s", exc)
            return {
                "success": False,
                "error": "Could not parse Brave Search response as JSON",
            }

        raw_results = (data.get("web") or {}).get("results", []) or []
        truncated = raw_results[:limit]

        web_results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("description", "")),
                "position": i + 1,
            }
            for i, r in enumerate(truncated)
        ]

        logger.info(
            "Brave Search '%s': %d results (from %d raw, limit %d)",
            query,
            len(web_results),
            len(raw_results),
            limit,
        )

        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Brave Search (Free)",
            "badge": "free",
            "tag": "Free-tier API key - 2k queries/mo, search only.",
            "env_vars": [
                {
                    "key": "BRAVE_SEARCH_API_KEY",
                    "prompt": "Brave Search API key (free tier)",
                    "url": "https://brave.com/search/api/",
                },
            ],
        }
