"""Parallel.ai web search (keyed via the optional SDK, or keyless through the ring).

Ported from Hermes' ``plugins/web/parallel/provider.py`` (search half).

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "parallel"      # explicit per-capability
    "backend": "parallel"             # shared fallback
    "provider_tier": {"parallel": "free"|"paid"}

Env vars::

    PARALLEL_API_KEY=...             # https://parallel.ai (required for the keyed path)
    PARALLEL_SEARCH_MODE=agentic     # optional: agentic|fast|one-shot

**The keyed path needs the ``parallel`` package and degrades without it.** Hermes
lazy-installs the SDK on first use; MISAKA does not install packages behind the user's
back, and Parallel's keyed search is a beta endpoint whose request shape lives in that
SDK -- hand-rolling it against a guessed URL would be a worse lie than an honest "not
installed". The keyless path needs nothing: it speaks MCP over plain HTTP, so a
credential-free install still gets Parallel through the ring.
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
from misaka.extensions.web.keyless import search_with_failover
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

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "Parallel - Free (keyless)",
            "badge": "free - no key",
            "tag": (
                "Objective-tuned search on Parallel's anonymous free tier. "
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
