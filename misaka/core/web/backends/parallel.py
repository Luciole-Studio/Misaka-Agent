"""Parallel search and extraction via REST or the anonymous MCP ring.

The existing beta request contract is documented in Parallel's migration guides:
https://docs.parallel.ai/search/search-migration-guide
https://docs.parallel.ai/extract/extract-migration-guide
No optional SDK, implicit install, blocking client or unclosed connection pool.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from typing import Any

import httpx

from misaka.core.web.accounting import account_call
from misaka.core.web.config import (
    keyless_tier_enabled,
    provider_env,
    provider_tier,
    use_keyless,
)
from misaka.core.web.keyless import extract_with_failover, search_with_failover
from misaka.core.web.provider import (
    WebSearchProvider,
    align_documents,
    check_response,
    extraction_error,
)
from misaka.core.web.runtime import api_client

logger = logging.getLogger(__name__)


def _resolve_search_mode() -> str:
    """Return the validated PARALLEL_SEARCH_MODE value (default "agentic")."""
    mode = (provider_env("PARALLEL_SEARCH_MODE") or "agentic").lower().strip()
    if mode not in {"fast", "one-shot", "agentic"}:
        mode = "agentic"
    return mode


def _retry_delay(attempt: int, headers: httpx.Headers | None) -> float:
    """Parallel 0.4.2: retry-after-ms/seconds/date, then capped jittered backoff."""
    delay = None
    if headers is not None:
        try:
            delay = float(headers["retry-after-ms"]) / 1000
        except (KeyError, ValueError):
            try:
                delay = float(headers["retry-after"])
            except (KeyError, ValueError):
                parsed = email.utils.parsedate_tz(headers.get("retry-after", ""))
                if parsed is not None:
                    delay = email.utils.mktime_tz(parsed) - time.time()
    if delay is not None and 0 < delay <= 60:
        return delay
    return min(0.5 * 2 ** attempt, 8.0) * (1 - 0.25 * random.random())


def _should_retry(response: httpx.Response) -> bool:
    directive = response.headers.get("x-should-retry")
    if directive in {"true", "false"}:
        return directive == "true"
    return response.status_code in {408, 409, 429} or response.status_code >= 500


async def _post(api_key: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = (provider_env("PARALLEL_BASE_URL") or "https://api.parallel.ai").rstrip("/")
    # The existing bounded timeout is a host policy, not the SDK's 5/600 default.
    async with api_client("parallel", endpoint, api_key, follow_redirects=True) as client:
        for attempt in range(3):  # SDK max_retries=2, in addition to the initial attempt.
            retry_headers = None
            try:
                async with account_call(f"web_{operation}", "parallel", payload.get("objective") or "\n".join(payload.get("urls") or [])):
                    response = await client.post(
                        f"{endpoint}/v1beta/{operation}",
                        headers={"x-api-key": api_key, "parallel-beta": "search-extract-2025-10-10",
                                 "x-stainless-retry-count": str(attempt)},
                        json=payload,
                    )
            except httpx.TransportError:
                if attempt == 2:
                    raise
            else:
                if not response.is_error or attempt == 2 or not _should_retry(response):
                    response.raise_for_status()
                    return check_response(response.json())
                retry_headers = response.headers
                await response.aclose()
            await asyncio.sleep(_retry_delay(attempt, retry_headers))
        raise AssertionError("Parallel retry loop exhausted without a result")


async def _keyed_search(api_key: str, query: str, limit: int) -> list[dict[str, Any]]:
    response = await _post(api_key, "search", {
        "search_queries": [query], "objective": query,
        "mode": _resolve_search_mode(), "max_results": min(limit, 20),
    })
    return [
        {"url": result.get("url") or "", "title": result.get("title") or "",
         "description": " ".join(result.get("excerpts") or []), "position": i + 1}
        for i, result in enumerate(response.get("results") or [])
    ]



async def _keyed_extract(api_key: str, urls: list[str]) -> Any:
    """Request full page content in one batch."""
    return await _post(api_key, "extract", {"urls": list(urls), "full_content": True})


class ParallelWebSearchProvider(WebSearchProvider):
    """Parallel.ai search provider."""

    @property
    def name(self) -> str:
        return "parallel"

    @property
    def display_name(self) -> str:
        return "Parallel"

    def is_available(self) -> bool:
        """Return True when ``PARALLEL_API_KEY`` is set to a non-empty value.

        Deliberately does NOT consider the keyless free tier -- that would let the legacy
        preference walk route keyed users of lower-priority backends onto Parallel's
        anonymous tier.
        """
        return bool(provider_env("PARALLEL_API_KEY"))

    def is_keyless_available(self) -> bool:
        """Parallel serves anonymous free-tier calls via its public MCP endpoint.

        False when the user forced ``"provider_tier": {"parallel": "paid"}``.
        """
        return keyless_tier_enabled() and provider_tier("parallel") != "paid"

    def uses_keyless_ring(self) -> bool:
        return use_keyless("parallel", provider_env("PARALLEL_API_KEY"))

    def supports_extract(self) -> bool:
        """Parallel reads whole pages through ``beta.extract``; see :meth:`extract`."""
        return True

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Parallel search."""
        try:
            api_key = provider_env("PARALLEL_API_KEY")
            if self.uses_keyless_ring():
                # Keyless free tier -- public MCP endpoint, no SDK needed.
                logger.info("Parallel keyless search: '%s' (limit=%d)", query, limit)
                return await search_with_failover("parallel", query, limit)

            if not api_key:
                raise ValueError(
                    "PARALLEL_API_KEY environment variable not set. "
                    "Get your API key at https://parallel.ai"
                )

            logger.info(
                "Parallel search: '%s' (mode=%s, limit=%d)",
                query,
                _resolve_search_mode(),
                limit,
            )
            web_results = await _keyed_search(api_key, query, limit)
            return {"success": True, "data": {"web": web_results}}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface as failure, as in Hermes
            logger.warning("Parallel search error: %s", exc)
            return {"success": False, "error": f"Parallel search failed: {exc}"}

    async def extract(
        self, urls: list[str], *, format: str | None = None
    ) -> list[dict[str, Any]]:
        """Read every URL through Parallel's ``beta.extract``, in one batched request.

        ``full_content=True`` asks for whole pages; a page that comes back without one
        falls back to its excerpts joined by blank lines, which is the only other text the
        endpoint offers. *format* is ignored -- there is no second rendition to choose.

        Associate exact URLs, or one unambiguous single-URL response. Unmapped batch
        material is preserved separately, never labelled as a different requested page.
        """
        api_key = provider_env("PARALLEL_API_KEY")
        if self.uses_keyless_ring():
            # The same decision :meth:`search` makes, through the same chokepoint.
            logger.info("Parallel keyless extract: %d URL(s)", len(urls))
            return await extract_with_failover("parallel", list(urls))

        # Both refusals below are configuration answers rather than outages, so each URL
        # carries the reason it failed; the dispatcher's all-entries-failed check still
        # gives the batch the one-shot rescue a raise would have earned it.
        if not api_key:
            return [
                extraction_error(
                    url,
                    "PARALLEL_API_KEY environment variable not set. "
                    "Get your API key at https://parallel.ai",
                )
                for url in urls
            ]

        logger.info("Parallel extract: %d URL(s)", len(urls))
        response = await _keyed_extract(api_key, list(urls))

        documents: list[dict[str, Any]] = []
        for result in response.get("results", None) or []:
            url = str(result.get("url", "") or "")
            title = str(result.get("title", "") or "")
            content = str(result.get("full_content", "") or "") or "\n\n".join(
                result.get("excerpts", None) or []
            )
            documents.append(
                {
                    "url": url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": {"sourceURL": url, "title": title,
                                 "content_kind": "page_text" if result.get("full_content") else "excerpts"},
                },
            )
        for failure in response.get("errors", None) or []:
            url = str(failure.get("url", "") or "")
            detail = (
                failure.get("content", "")
                or failure.get("error_type", "")
                or "extraction failed"
            )
            documents.append(extraction_error(url, str(detail)))

        return align_documents(urls, documents)

    def get_setup_schema(self) -> dict[str, Any]:
        from misaka.core.web.provider import keyless_setup_schema

        return keyless_setup_schema('Parallel', 'PARALLEL_API_KEY', 'https://parallel.ai',
                                    'Objective-tuned search and page extraction.')
