"""SearXNG search, against a user-hosted instance.

Ported from Hermes' ``plugins/web/searxng/provider.py``. Same JSON API call
(``/search?format=json``), same result normalization.

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "searxng"     # explicit per-capability
    "backend": "searxng"            # shared fallback

Env var::

    SEARXNG_URL=http://localhost:8080

**No SSRF vetting, deliberately.** ``misaka.core.tools._web.bounded`` exists because the
model picks those URLs; this one comes from the user's own config file, and the whole
point of SearXNG is that the instance is theirs -- ``http://localhost:8080`` and a LAN
address are the two normal values, and both are exactly what the vetting rejects. The
threat the vetting answers (a model steering a fetch at the metadata endpoint) is not
reachable here: nothing model-supplied enters the URL, only the query string.
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


class SearXNGWebSearchProvider(WebSearchProvider):
    """Search via a user-hosted SearXNG instance."""

    @property
    def name(self) -> str:
        return "searxng"

    @property
    def display_name(self) -> str:
        return "SearXNG"

    def is_available(self) -> bool:
        """Return True when ``SEARXNG_URL`` is set."""
        return bool(provider_env("SEARXNG_URL"))

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a search against the configured SearXNG instance."""
        base_url = provider_env("SEARXNG_URL").rstrip("/")
        if not base_url:
            return {"success": False, "error": "SEARXNG_URL is not set"}

        params: dict[str, Any] = {"q": query, "format": "json", "pageno": 1}

        try:
            async with (
                httpx.AsyncClient(timeout=http_timeout("searxng", 15), **api_network_options(base_url)) as client,
                account_call("web_search", "searxng", query),
            ):
                resp = await client.get(
                    f"{base_url}/search",
                    params=params,
                    headers={"Accept": "application/json"},
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("SearXNG HTTP error: %s", exc)
            return {
                "success": False,
                "error": f"SearXNG returned HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            logger.warning("SearXNG request error: %s", exc)
            return {
                "success": False,
                "error": f"Could not reach SearXNG at {base_url}: {exc}",
            }

        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("SearXNG response parse error: %s", exc)
            return {"success": False, "error": "Could not parse SearXNG response as JSON"}

        raw_results = data.get("results", [])

        # SearXNG may return a score field; sort descending and cap to limit.
        sorted_results = sorted(
            raw_results, key=lambda r: float(r.get("score", 0)), reverse=True
        )[:limit]

        web_results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("content", "")),
                "position": i + 1,
            }
            for i, r in enumerate(sorted_results)
        ]

        logger.info(
            "SearXNG search '%s': %d results (from %d raw, limit %d)",
            query,
            len(web_results),
            len(raw_results),
            limit,
        )

        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "SearXNG",
            "badge": "free - self-hosted",
            "tag": "Free, privacy-respecting metasearch. Point SEARXNG_URL at your instance.",
            "env_vars": [
                {
                    "key": "SEARXNG_URL",
                    "prompt": "SearXNG instance URL (e.g. http://localhost:8080)",
                    "url": "https://searx.space/",
                },
            ],
        }
