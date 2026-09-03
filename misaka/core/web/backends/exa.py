"""Exa web search and page extraction (keyed over REST, or keyless through the ring).

Ported from Hermes' ``plugins/web/exa/provider.py``.

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "exa"      # explicit per-capability
    "extract_backend": "exa"     # explicit per-capability
    "backend": "exa"             # shared fallback
    "provider_tier": {"exa": "free"|"paid"}

Env var::

    EXA_API_KEY=...    # https://exa.ai (paid tier; free trial available)

**Keyed paths rewritten from the SDK to REST.** Hermes calls ``exa_py``, lazily installed
on demand. MISAKA does not install packages behind the user's back and does not carry the
SDK, so the same calls are made over HTTP: ``POST /search`` with ``x-api-key`` asking for
highlights, which is exactly what ``Exa.search(contents={"highlights": True})`` sends, and
``POST /contents`` with ``{"text": true}``, which is what ``Exa.get_contents(urls,
text=True)`` sends. The keyless paths are unchanged -- they never used the SDK in Hermes
either.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from misaka.core.web.config import (
    keyless_tier_enabled,
    provider_env,
    provider_tier,
    use_keyless,
)
from misaka.core.web.keyless import (
    CLIENT_NAME,
    extract_with_failover,
    search_with_failover,
)
from misaka.core.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)

_EXA_SEARCH_URL = "https://api.exa.ai/search"
_EXA_CONTENTS_URL = "https://api.exa.ai/contents"


def _failed(url: str, error: str) -> dict[str, Any]:
    """The contract entry for a page Exa did not read.

    An entry, never a hole in the list: the caller reassembles its argument list by
    position, so a dropped failure hands it the next page's text under this page's
    address.
    """
    return {
        "url": url,
        "title": "",
        "content": "",
        "raw_content": "",
        "error": error,
        "metadata": {"sourceURL": url},
    }


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

    def supports_extract(self) -> bool:
        """Exa reads whole pages through ``/contents``; see :meth:`extract`."""
        return True

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

    async def extract(
        self, urls: list[str], *, format: str | None = None
    ) -> list[dict[str, Any]]:
        """Read every URL through Exa's ``/contents`` endpoint, in one batched request.

        *format* is ignored: Exa returns one plain-text rendition of a page and has no
        HTML mode to select between, so ``content`` and ``raw_content`` are the same
        string -- which is what Hermes does with ``result.text`` too.

        Divergence from Hermes, deliberate. It appends one entry per result the vendor
        *returned*, so a batch Exa answers short comes back short and every caller pairing
        by argument position then reads one page's text under another page's address. The
        reply is re-keyed onto the requested list here, a URL Exa named nothing for
        becomes that URL's error entry, and a URL nobody asked for is dropped with a debug
        line rather than lengthening the list past its request. This is the same back-fill
        :func:`misaka.core.web.keyless.parallel_extract_keyless` already performs,
        for the same reason.
        """
        api_key = provider_env("EXA_API_KEY")
        if use_keyless("exa", api_key):
            # The same decision :meth:`search` makes, asked of the same chokepoint: a
            # vendor whose two capabilities disagree about which tier they are on is a
            # bug nobody finds for months.
            logger.info("Exa keyless extract: %d URL(s)", len(urls))
            return await extract_with_failover("exa", list(urls))

        if not api_key:
            # A configuration refusal rather than a raise: every URL failed for one
            # reason and each says so, and the dispatcher's all-entries-failed check
            # gives the batch the same one-shot rescue a raise would have.
            return [
                _failed(
                    url,
                    "EXA_API_KEY environment variable not set. "
                    "Get your API key at https://exa.ai",
                )
                for url in urls
            ]

        logger.info("Exa extract: %d URL(s)", len(urls))
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                _EXA_CONTENTS_URL,
                json={"urls": list(urls), "text": True},
                headers={
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                    "x-exa-integration": CLIENT_NAME,
                },
            )
        # A rejected key or an unreachable endpoint is a whole-backend failure, and
        # raising is how the contract says to report one: the dispatcher catches it and
        # may route the whole batch through the keyless ring once. Only per-page problems
        # are per-entry errors, and blurring the two costs the batch its rescue.
        if response.status_code >= 400:
            detail = (response.text or "").strip() or f"HTTP {response.status_code}"
            raise ValueError(f"Exa extract failed: {detail}")

        by_url: dict[str, dict[str, Any]] = {}
        for result in response.json().get("results") or []:
            if not isinstance(result, dict):
                continue
            url = str(result.get("url") or "")
            title = str(result.get("title") or "")
            content = str(result.get("text") or "")
            by_url.setdefault(
                url,
                {
                    "url": url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": {"sourceURL": url, "title": title},
                },
            )

        unrequested = sorted(set(by_url) - set(urls))
        if unrequested:
            logger.debug("exa extract: reply named unrequested url(s) %s", unrequested)
        return [by_url.get(url) or _failed(url, "no content returned") for url in urls]

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "Exa - Free (keyless)",
            "badge": "free - no key",
            "tag": (
                "Semantic + neural web search and page reads on Exa's anonymous "
                "free tier. "
                "Rate-limited under burst load."
            ),
            "env_vars": [
                {"key": "EXA_API_KEY", "prompt": "Exa API key", "url": "https://exa.ai"},
            ],
        }
