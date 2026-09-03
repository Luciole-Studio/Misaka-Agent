"""Keenable web search and page fetch (keyed, or keyless through the ring).

Ported from Hermes' ``plugins/web/keenable/provider.py``. Keenable
(https://keenable.ai) operates an independent web index for AI apps with public keyless
endpoints (rate-limited free tier; keyed access via KEENABLE_API_KEY for higher limits).

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "keenable"      # explicit per-capability
    "extract_backend": "keenable"     # explicit per-capability
    "backend": "keenable"             # shared fallback
    "provider_tier": {"keenable": "free"|"paid"}

Env var::

    KEENABLE_API_KEY=...   # optional - the keyless free tier works without it
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
from misaka.extensions.web.keyless import (
    CLIENT_NAME,
    KEENABLE_API_URL,
    extract_with_failover,
    search_with_failover,
)
from misaka.extensions.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _keenable_headers(api_key: str) -> dict[str, str]:
    """Build Keenable request headers for keyed or keyless access.

    Their keyless tier structurally requires an app-identifier header
    (X-Keenable-Title); no user identifiers are sent.
    """
    headers = {"X-Keenable-Title": CLIENT_NAME}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class KeenableWebSearchProvider(WebSearchProvider):
    """Keenable search provider (keyed or keyless)."""

    @property
    def name(self) -> str:
        return "keenable"

    @property
    def display_name(self) -> str:
        return "Keenable"

    def is_available(self) -> bool:
        """Return True when ``KEENABLE_API_KEY`` is set to a non-empty value."""
        return bool(provider_env("KEENABLE_API_KEY"))

    def is_keyless_available(self) -> bool:
        """Keenable serves anonymous free-tier calls via its public endpoints.

        Default-on ring member of the keyless free tier. False when the user pinned
        ``"provider_tier": {"keenable": "paid"}``.
        """
        return keyless_tier_enabled() and provider_tier("keenable") != "paid"

    def supports_extract(self) -> bool:
        """Keenable reads whole pages through ``/v1/fetch``; see :meth:`extract`."""
        return True

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Keenable search (keyed path or keyless ring)."""
        try:
            api_key = provider_env("KEENABLE_API_KEY")
            if use_keyless("keenable", api_key):
                logger.info("Keenable keyless search: '%s' (limit=%d)", query, limit)
                return await search_with_failover("keenable", query, limit)

            logger.info("Keenable search: '%s' (limit=%d)", query, limit)
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{KEENABLE_API_URL}/v1/search",
                    json={"query": query, "max_results": min(max(1, int(limit)), 20)},
                    headers=_keenable_headers(api_key),
                )
            if response.status_code >= 400:
                detail = (response.text or "").strip() or f"HTTP {response.status_code}"
                return {"success": False, "error": f"Keenable search failed: {detail}"}
            data = response.json()

            web_results = []
            for i, result in enumerate(data.get("results") or []):
                web_results.append(
                    {
                        "url": result.get("url") or "",
                        "title": result.get("title") or "",
                        "description": result.get("snippet")
                        or result.get("description")
                        or "",
                        "position": i + 1,
                    }
                )
            return {"success": True, "data": {"web": web_results}}
        except Exception as exc:  # noqa: BLE001 - surface as failure, as in Hermes
            logger.warning("Keenable search error: %s", exc)
            return {"success": False, "error": f"Keenable search failed: {exc}"}

    async def extract(
        self, urls: list[str], *, format: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch each URL through Keenable's ``/v1/fetch``, one request per URL.

        Per-URL rather than batched because the endpoint is: it takes one ``url`` query
        parameter and answers ``{url, title, content}`` with the content in markdown.
        *format* is ignored -- there is no second rendition to choose between.

        The try/except is per URL and that is the point: a 404, a login wall or a PDF
        Keenable could not read is that page's error entry, and the remaining pages in the
        batch still get fetched. A whole-backend failure has no such shape here, because
        every request stands alone; the dispatcher's all-entries-failed check is what
        turns "every page failed" back into the one-shot rescue.

        Divergence from Hermes, small and matching what
        :func:`misaka.extensions.web.keyless.keenable_extract_keyless` already does: the
        entry is filed under the URL that was *asked for*, not the one Keenable echoes
        back. A redirect makes the two differ, and an entry whose ``url`` is not the
        caller's is exactly the mispairing the positional contract exists to prevent.
        """
        api_key = provider_env("KEENABLE_API_KEY")
        if use_keyless("keenable", api_key):
            # The same decision :meth:`search` makes, through the same chokepoint.
            logger.info("Keenable keyless extract: %d URL(s)", len(urls))
            return await extract_with_failover("keenable", list(urls))

        logger.info("Keenable extract: %d URL(s)", len(urls))
        results: list[dict[str, Any]] = []
        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(
                        f"{KEENABLE_API_URL}/v1/fetch",
                        params={"url": url},
                        headers=_keenable_headers(api_key),
                    )
                if response.status_code >= 400:
                    raise ValueError(
                        (response.text or "").strip() or f"HTTP {response.status_code}"
                    )
                data = response.json()
                if not isinstance(data, dict):
                    raise TypeError(f"expected a JSON object, got {type(data).__name__}")
                title = str(data.get("title") or "")
                content = str(data.get("content") or "")
                results.append(
                    {
                        "url": url,
                        "title": title,
                        "content": content,
                        "raw_content": content,
                        "metadata": {"sourceURL": url, "title": title},
                    }
                )
            except Exception as exc:  # noqa: BLE001 - per-URL error entry, as in Hermes
                logger.debug("Keenable fetch failed for %s: %s", url, exc)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": f"Keenable extract failed: {exc}",
                        "metadata": {"sourceURL": url},
                    }
                )
        return results

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "Keenable - Free (keyless)",
            "badge": "free - no key",
            "tag": (
                "Independent web index for AI apps - fast search and page fetch on "
                "Keenable's anonymous free tier."
            ),
            "env_vars": [
                {
                    "key": "KEENABLE_API_KEY",
                    "prompt": "Keenable API key",
                    "url": "https://keenable.ai",
                },
            ],
        }
