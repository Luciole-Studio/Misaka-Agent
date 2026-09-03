"""Tavily web search (keyed, or opt-in keyless).

Ported from Hermes' ``plugins/web/tavily/provider.py`` (search half).

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "tavily"     # explicit per-capability
    "backend": "tavily"            # shared fallback
    "provider_tier": {"tavily": "free"|"paid"}

Env vars::

    TAVILY_API_KEY=...           # https://app.tavily.com/home (optional)
    TAVILY_BASE_URL=...          # optional override of https://api.tavily.com

Auth is header-based. A key uses ``Authorization: Bearer``; without a key the request is
keyless (``X-Tavily-Access-Mode: keyless``).

Tavily is **not** a member of the zero-config keyless ring
(:data:`misaka.extensions.web.keyless.KEYLESS_RING`), matching Hermes. Keyless access is
opt-in: it serves an explicit ``"backend": "tavily"`` without a key, but a fresh install
with no web credentials rotates across Exa / Parallel / Firecrawl / Keenable instead and
never lands here. A ring vendor's failure is walked past to the next vendor; Tavily's is
returned to the caller, which is what makes it eligible for the one-shot keyless rescue.
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
from misaka.extensions.web.keyless import CLIENT_NAME
from misaka.extensions.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Sent on every search. Raw content and images are what make a Tavily response large;
# the tool wants titles, URLs and two-line descriptions, and pays per call either way.
_SEARCH_PAYLOAD = {
    "include_raw_content": False,
    "include_images": False,
}


def _missing_key_error(action: str) -> str:
    """The refusal when Tavily can run neither keyed nor keyless.

    Reached only when the user shut the keyless door themselves: ``provider_tier.tavily``
    pinned to ``paid``, or ``keyless_fallback`` turned off. Naming both levers matters --
    Hermes says "select Tavily in `hermes tools`", which is the wrong advice here because
    selecting the backend is what got the caller to this line.
    """
    return (
        "TAVILY_API_KEY is not set. Get a key at https://app.tavily.com/home, or allow "
        f"opt-in keyless {action} in `~/.misaka/web.json`: unpin `provider_tier.tavily` "
        "from `paid` and leave `keyless_fallback` on."
    )


def _tavily_headers(api_key: str) -> dict[str, str]:
    """Build Tavily request headers for keyed or keyless access."""
    headers = {"X-Client-Name": CLIENT_NAME}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"
    return headers


async def tavily_request(
    endpoint: str, payload: dict[str, Any], *, api_key: str | None = None
) -> dict[str, Any]:
    """POST to the Tavily API and return the parsed JSON response.

    Keyed when *api_key* (or ``TAVILY_API_KEY``) is set (Bearer auth); otherwise keyless.
    Pass ``api_key=""`` to force the keyless header even when a key is present, which is
    what ``provider_tier.tavily: free`` asks for. Non-2xx responses raise ``ValueError``
    with the response body so Tavily's keyless rate-limit and upgrade text reaches the
    model.
    """
    if api_key is None:
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

        Opt-in only -- Tavily is not a member of the zero-config keyless ring. This is True
        so that an explicit ``"backend": "tavily"`` works without a key at all, which is the
        only way the resolver reaches this provider unkeyed. False when the user pinned
        ``"provider_tier": {"tavily": "paid"}`` -- an explicit paid selection opts the free
        endpoint out.
        """
        return keyless_tier_enabled() and provider_tier("tavily") != "paid"

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Tavily search: the keyed path, or the opt-in keyless one."""
        try:
            api_key = provider_env("TAVILY_API_KEY")
            force_keyless = use_keyless("tavily", api_key)
            if not force_keyless and not api_key:
                return {"success": False, "error": _missing_key_error("search")}

            logger.info(
                "Tavily %ssearch: '%s' (limit=%d)",
                "keyless " if force_keyless else "",
                query,
                limit,
            )
            raw = await tavily_request(
                "search",
                {
                    "query": query,
                    "max_results": min(limit, 20),
                    **_SEARCH_PAYLOAD,
                },
                api_key="" if force_keyless else api_key,
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
