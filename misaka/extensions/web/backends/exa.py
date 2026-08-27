"""Exa web search (keyed over REST, or keyless through the ring).

Ported from Hermes' ``plugins/web/exa/provider.py`` (search half).

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "exa"      # explicit per-capability
    "backend": "exa"             # shared fallback
    "provider_tier": {"exa": "free"|"paid"}

Env var::

    EXA_API_KEY=...    # https://exa.ai (paid tier; free trial available)

**Keyed path rewritten from the SDK to REST.** Hermes calls ``exa_py``, lazily installed
on demand. MISAKA does not install packages behind the user's back and does not carry the
SDK, so the same call is made over HTTP: ``POST /search`` with ``x-api-key``, asking for
highlights, which is exactly what ``Exa.search(contents={"highlights": True})`` sends. The
keyless path is unchanged -- it never used the SDK in Hermes either.
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

_EXA_SEARCH_URL = "https://api.exa.ai/search"


class ExaWebSearchProvider(WebSearchProvider):
    """Exa search provider."""

    @property
    def name(self) -> str:
        return "exa"

    @property
    def display_name(self) -> str:
        return "Exa"

    def is_available(self) -> bool:
        """Return True when ``EXA_API_KEY`` is set to a non-empty value.

        Deliberately does NOT consider the keyless free tier -- that would let the legacy
        preference walk route keyed users of lower-priority backends onto Exa's anonymous
        tier. Keyless availability is a separate, last-resort signal
        (:meth:`is_keyless_available`).
        """
        return bool(provider_env("EXA_API_KEY"))

    def is_keyless_available(self) -> bool:
        """Exa serves anonymous free-tier calls via its public MCP endpoint.

        False when the user forced ``"provider_tier": {"exa": "paid"}`` -- an explicit
        paid selection must never silently resolve keyless.
        """
        return keyless_tier_enabled() and provider_tier("exa") != "paid"

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute an Exa search."""
        try:
            api_key = provider_env("EXA_API_KEY")
            if use_keyless("exa", api_key):
                # Keyless free tier -- public MCP endpoint.
                logger.info("Exa keyless search: '%s' (limit=%d)", query, limit)
                return await search_with_failover("exa", query, limit)

            if not api_key:
                raise ValueError(
                    "EXA_API_KEY environment variable not set. "
                    "Get your API key at https://exa.ai"
                )

            logger.info("Exa search: '%s' (limit=%d)", query, limit)
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    _EXA_SEARCH_URL,
                    json={
                        "query": query,
                        "numResults": max(1, int(limit)),
                        "contents": {"highlights": True},
                    },
                    headers={
                        "x-api-key": api_key,
                        "Content-Type": "application/json",
                        "x-exa-integration": CLIENT_NAME,
                    },
                )
            if response.status_code >= 400:
                detail = (response.text or "").strip() or f"HTTP {response.status_code}"
                return {"success": False, "error": f"Exa search failed: {detail}"}
            payload = response.json()

            web_results = []
            for i, result in enumerate(payload.get("results") or []):
                highlights = result.get("highlights") or []
                web_results.append(
                    {
                        "url": result.get("url") or "",
                        "title": result.get("title") or "",
                        "description": " ".join(highlights) if highlights else "",
                        "position": i + 1,
                    }
                )

            return {"success": True, "data": {"web": web_results}}
        except ValueError as exc:
            # Raised for a missing EXA_API_KEY, and by a 2xx body that was not JSON.
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface as failure, as in Hermes
            logger.warning("Exa search error: %s", exc)
            return {"success": False, "error": f"Exa search failed: {exc}"}

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "Exa - Free (keyless)",
            "badge": "free - no key",
            "tag": (
                "Semantic + neural web search on Exa's anonymous free tier. "
                "Rate-limited under burst load."
            ),
            "env_vars": [
                {"key": "EXA_API_KEY", "prompt": "Exa API key", "url": "https://exa.ai"},
            ],
        }
