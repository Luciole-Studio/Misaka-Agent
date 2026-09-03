"""Parallel.ai web search and extraction (keyed via the optional SDK, or keyless).

Ported from Hermes' ``plugins/web/parallel/provider.py``.

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "parallel"      # explicit per-capability
    "extract_backend": "parallel"     # explicit per-capability
    "backend": "parallel"             # shared fallback
    "provider_tier": {"parallel": "free"|"paid"}

Env vars::

    PARALLEL_API_KEY=...             # https://parallel.ai (required for the keyed path)
    PARALLEL_SEARCH_MODE=agentic     # optional: agentic|fast|one-shot

**The keyed paths need the ``parallel`` package and degrade without it.** Hermes
lazy-installs the SDK on first use; MISAKA does not install packages behind the user's
back, and Parallel's keyed search and extract are beta endpoints whose request shapes live
in that SDK -- hand-rolling them against a guessed URL would be a worse lie than an honest
"not installed". The keyless paths need nothing: they speak MCP over plain HTTP, so a
credential-free install still gets Parallel through the ring.

Search runs on the blocking client in a thread and extract on the async one, because that
is what each SDK offers: ``beta.search`` has no async entry point, ``beta.extract`` does.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from misaka.extensions.web.config import (
    keyless_tier_enabled,
    provider_env,
    provider_tier,
    use_keyless,
)
from misaka.extensions.web.keyless import extract_with_failover, search_with_failover
from misaka.extensions.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _resolve_search_mode() -> str:
    """Return the validated PARALLEL_SEARCH_MODE value (default "agentic")."""
    mode = (provider_env("PARALLEL_SEARCH_MODE") or "agentic").lower().strip()
    if mode not in {"fast", "one-shot", "agentic"}:
        mode = "agentic"
    return mode


def _keyed_search(api_key: str, query: str, limit: int) -> list[dict[str, Any]]:
    """Run the blocking SDK search and return normalized hits.

    Module-level rather than a closure so a test can patch it, and so the blocking work
    is one clearly-marked function to hand to a thread.
    """
    # Optional dependency, imported on use: a missing package must be an error the
    # caller can turn into a message, not an import failure at registration time.
    from parallel import Parallel

    response = Parallel(api_key=api_key).beta.search(
        search_queries=[query],
        objective=query,
        mode=_resolve_search_mode(),
        max_results=min(limit, 20),
    )
    web_results = []
    for i, result in enumerate(response.results or []):
        excerpts = result.excerpts or []
        web_results.append(
            {
                "url": result.url or "",
                "title": result.title or "",
                "description": " ".join(excerpts) if excerpts else "",
                "position": i + 1,
            }
        )
    return web_results


def _failed(url: str, error: str) -> dict[str, Any]:
    """The contract entry for a page Parallel did not return.

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


async def _keyed_extract(api_key: str, urls: list[str]) -> Any:
    """Run the async SDK extract and return its raw response object.

    Module-level for the reason :func:`_keyed_search` is: it is the one function that
    touches the optional package, so a test can drive the mapping in
    :meth:`ParallelWebSearchProvider.extract` without installing an SDK this repo does not
    ship. Unlike search, nothing here goes to a thread -- ``AsyncParallel`` awaits, and
    handing an already-async call to :func:`asyncio.to_thread` would burn a worker to sit
    on a second event loop.

    The client is built per call and not closed, matching what :func:`_keyed_search` does
    with ``Parallel(...)``: one pool per batch, released when it is collected. Hermes
    caches one instead, which is what its ``reset_clients()`` test hook exists to undo.
    """
    # Optional dependency, imported on use: a missing package must be an error the
    # caller can turn into a message, not an import failure at registration time.
    from parallel import AsyncParallel

    return await AsyncParallel(api_key=api_key).beta.extract(
        urls=list(urls), full_content=True
    )


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

    def supports_extract(self) -> bool:
        """Parallel reads whole pages through ``beta.extract``; see :meth:`extract`."""
        return True

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Parallel search."""
        try:
            api_key = provider_env("PARALLEL_API_KEY")
            if use_keyless("parallel", api_key):
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
            web_results = await asyncio.to_thread(_keyed_search, api_key, query, limit)
            return {"success": True, "data": {"web": web_results}}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except ImportError as exc:
            return {"success": False, "error": f"Parallel SDK not installed: {exc}"}
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

        Divergence from Hermes, deliberate. It appends the read pages and then the
        ``errors`` list, so a three-URL batch whose middle page failed answers with the
        third page's text in the second position and every caller pairing by argument
        order reads the wrong page under the wrong address. Both lists are re-keyed onto
        the requested URLs here, a URL named in neither becomes that URL's error entry,
        and one nobody asked for is dropped with a debug line. Same fix, same reason, as
        :func:`misaka.extensions.web.keyless.parallel_extract_keyless`.
        """
        api_key = provider_env("PARALLEL_API_KEY")
        if use_keyless("parallel", api_key):
            # The same decision :meth:`search` makes, through the same chokepoint.
            logger.info("Parallel keyless extract: %d URL(s)", len(urls))
            return await extract_with_failover("parallel", list(urls))

        # Both refusals below are configuration answers rather than outages, so each URL
        # carries the reason it failed; the dispatcher's all-entries-failed check still
        # gives the batch the one-shot rescue a raise would have earned it.
        if not api_key:
            return [
                _failed(
                    url,
                    "PARALLEL_API_KEY environment variable not set. "
                    "Get your API key at https://parallel.ai",
                )
                for url in urls
            ]

        logger.info("Parallel extract: %d URL(s)", len(urls))
        try:
            response = await _keyed_extract(api_key, list(urls))
        except ImportError as exc:
            return [_failed(url, f"Parallel SDK not installed: {exc}") for url in urls]

        by_url: dict[str, dict[str, Any]] = {}
        for result in getattr(response, "results", None) or []:
            url = str(getattr(result, "url", "") or "")
            title = str(getattr(result, "title", "") or "")
            content = str(getattr(result, "full_content", "") or "") or "\n\n".join(
                getattr(result, "excerpts", None) or []
            )
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
        for failure in getattr(response, "errors", None) or []:
            url = str(getattr(failure, "url", "") or "")
            detail = (
                getattr(failure, "content", "")
                or getattr(failure, "error_type", "")
                or "extraction failed"
            )
            by_url.setdefault(url, _failed(url, str(detail)))

        unrequested = sorted(set(by_url) - set(urls))
        if unrequested:
            logger.debug("parallel extract: reply named unrequested url(s) %s", unrequested)
        return [by_url.get(url) or _failed(url, "no content returned") for url in urls]

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "Parallel - Free (keyless)",
            "badge": "free - no key",
            "tag": (
                "Objective-tuned search and page extraction on Parallel's anonymous "
                "free tier. "
                "Rate-limited under burst load."
            ),
            "env_vars": [
                {
                    "key": "PARALLEL_API_KEY",
                    "prompt": "Parallel API key",
                    "url": "https://parallel.ai",
                },
            ],
        }
