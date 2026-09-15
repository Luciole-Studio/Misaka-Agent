"""One search or extract call: resolve the backend, run it, rescue it once if it failed.

This is the provider layer's entry point and the only function the tool above it needs.
Ported from the dispatch half of Hermes' ``tools/web_tools.py`` -- the provider lookup at
the top of ``web_search_tool`` plus the one-shot keyless rescue
(``_keyless_rescue_enabled`` / ``_rescue_eligible`` / ``_rescue_search``).

What is deliberately NOT here: the result memo, single-flight coalescing, the character
budget, and the ``untrusted`` fence. Those belong to the tool -- a cache has to see the
tool's own bucketing, and nothing that reshapes provider output may sit below the
contract. The one thing the tool must honour from here: a response carrying
``data["rescued_from"]`` was served by a ring vendor rather than the chosen backend and
must never be cached, or a single failure would pin the query to the free tier for a
whole TTL.

Disabled providers are diagnosed before resolution and excluded from ring rescue.
Hermes' structured Web debug trace is still tracked separately in the transplant plan.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from typing import Any

from misaka.core.tools._web.website_policy import policy_blocked
from misaka.core.web import debug
from misaka.core.web.config import (
    keyless_rescue_enabled,
    provider_disabled,
    web_config,
)
from misaka.core.web.keyless import (
    extract_with_failover,
    search_with_failover,
)
from misaka.core.web.provider import WebSearchProvider
from misaka.core.web.registry import (
    active_extract_provider,
    active_search_provider,
    ensure_backends_registered,
    extract_backend_name,
    get_provider,
    search_backend_name,
    selection_stored,
)
from misaka.utils.async_lifecycle import run_in_thread

logger = logging.getLogger(__name__)


def extract_timeout_seconds() -> float:
    """Hermes 62e5f46 provider-dispatch cap; <= 0 disables this inner cap only."""
    try:
        value = float(web_config().get("extract_timeout", 120.0))
        return value if math.isfinite(value) else 120.0
    except (TypeError, ValueError):
        return 120.0

def serves_keyless(provider: WebSearchProvider | None) -> bool:
    """Whether a call on *provider* will be dispatched through the keyless ring.

    Ring membership is the question, not "does this provider have a keyless mode".
    Tavily has one and is deliberately outside the ring, so a keyless Tavily call is a
    single vendor request that can fail and be rescued -- not a walk that already tried
    every free tier there is.
    """
    return provider is not None and provider.uses_keyless_ring()


# The cache identity shared by every vendor serving through the ring.
KEYLESS_MEMO_IDENTITY = "keyless-ring"


def memo_identity(provider: WebSearchProvider | None) -> str:
    """The name the result memo should file a call on *provider* under.

    A ring vendor serving keyless is not a backend in its own right. ``ring_order`` turns
    the round-robin cursor on every unpinned request and the resolver reads that same
    cursor, so consecutive identical searches resolve to *different* members: keying on
    the member gives one query five keys, and the fan-out the memo exists to absorb pays
    all five free tiers for an answer it already had -- the quickest possible way to get
    every one of them to throttle. The five are one logical backend and share one key.

    Divergence from Hermes, deliberate and the only one in this file. Hermes keys on
    ``provider.name`` and inherits the same interaction, but its keyless ring is the
    last-resort tier under a normally-keyed install, where the cursor rarely decides
    anything. In MISAKA the zero-credential install is the headline configuration, so what
    is a wart there is the common path here. Reverting is one line: return ``.name``.
    """
    name = getattr(provider, "name", "")
    return KEYLESS_MEMO_IDENTITY if serves_keyless(provider) else name


def rescue_eligible(provider: WebSearchProvider | None) -> bool:
    """True when a failed call on *provider* should get a one-shot rescue.

    Eligible: the call ran a keyed or configured path -- either a non-ring backend
    (searxng, brave-free, tavily, xai, an externally registered one) or a ring vendor
    operating in keyed mode. NOT eligible: the call already went through the keyless ring,
    because its failure means the ring was walked and re-walking would just repeat it.

    Best-effort, as in Hermes: a config layer that throws while answering "is this vendor
    keyless right now" must not turn a recoverable search failure into a raised exception.
    An unanswerable question means not eligible.
    """
    if not keyless_rescue_enabled():
        return False
    if getattr(provider, "name", "") == "nous":
        return False  # Explicit managed billing errors are not silently rerouted.
    if provider is None:
        return False
    try:
        return not serves_keyless(provider)
    except Exception:  # noqa: BLE001 - rescue is best-effort, as in Hermes
        return False


async def rescue_search(
    provider_name: str, original_error: str, query: str, limit: int
) -> dict[str, Any]:
    """One-shot keyless-ring rescue for a failed keyed or configured search.

    Stateless by design: this call alone routes to the free-tier ring; the NEXT search
    attempts the chosen backend again. The result is annotated with the original backend
    failure so the model -- and the user reading the transcript -- can see that the
    configured backend needs attention.
    """
    logger.warning(
        "web_search backend '%s' failed (%s); one-shot keyless rescue",
        provider_name,
        (original_error or "")[:200],
    )
    debug.event("rescue", capability="search", from_backend=provider_name)
    rescued = await search_with_failover(provider_name, query, limit)
    if rescued.get("success"):
        data = rescued.setdefault("data", {})
        data["rescued_from"] = provider_name
        data["backend_error"] = (
            f"Configured backend '{provider_name}' failed this call "
            f"({(original_error or 'unknown error')[:300]}); result served "
            "by the keyless free tier. The next call will use "
            f"'{provider_name}' again."
        )
        return rescued
    # The ring also failed: surface the ORIGINAL backend error -- it names the user's own
    # setup -- with the rescue note appended.
    return {
        "success": False,
        "error": (
            f"{original_error or 'search failed'} "
            f"(keyless rescue also failed: {rescued.get('error', 'unknown')})"
        ),
    }


def resolve_provider() -> tuple[WebSearchProvider | None, str, str]:
    """Return the provider that should serve this call, its name, and any config error.

    Split out of :func:`web_search` so the tool can name the active backend (for a cache
    key, or for a status line) without running a search.
    """
    ensure_backends_registered()
    backend = search_backend_name()
    if provider_disabled(backend):
        return None, backend, f"Web provider '{backend}' is disabled; use `misaka web enable {backend}`."
    provider = get_provider(backend) if backend else None
    if provider is not None and not provider.supports_search():
        # Hermes searches with another capable provider rather than calling an
        # extract-only implementation. Keep this separate from unknown-name errors.
        provider = active_search_provider()
        if provider is None:
            return None, backend, "No available web provider supports search."
        return provider, provider.name, ""
    if provider is not None:
        return provider, backend, ""
    if backend and selection_stored():
        return None, backend, (
            f"Web search backend is set to '{backend}', but no registered web search "
            "provider has that name. Fix the `backend` or `search_backend` entry in "
            "`~/.misaka/web.json`."
        )
    # Never-configured install: fall back to the availability-walked active provider.
    provider = active_search_provider()
    if provider is None:
        return None, backend, (
            "No web search provider configured. Set one up in `~/.misaka/web.json`."
        )
    return provider, provider.name, ""


async def web_search(query: str, limit: int = 5) -> dict[str, Any]:
    """Search the web through the active backend, returning the contract response shape.

    Success is ``{"success": True, "data": {"web": [...]}}``; failure is
    ``{"success": False, "error": str}``. Never raises for a vendor-side failure -- a
    provider that raises anyway is caught here and rescued or re-raised, which is the one
    path where an exception can still leave this function.
    """
    provider, name, config_error = resolve_provider()
    if provider is None:
        return {"success": False, "error": config_error}

    logger.info("Web search via %s: '%s' (limit: %d)", name, query, limit)
    try:
        response = await provider.search(query, limit)
    except Exception as exc:  # a provider that raises anyway: rescue it, or let it out
        if not rescue_eligible(provider):
            raise
        return await rescue_search(provider.name, str(exc), query, limit)
    if not response.get("success") and rescue_eligible(provider):
        # One-shot keyless rescue: THIS call rides the free-tier ring; the next call
        # attempts the chosen backend again.
        return await rescue_search(
            provider.name, str(response.get("error", "")), query, limit
        )
    return response


async def rescue_extract(
    provider_name: str, urls: list[str], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One-shot keyless-ring rescue for an extract whose backend failed outright.

    Fires only when EVERY page failed, which is what distinguishes a backend outage from
    pages that are simply hard to read: a partial failure is passed through untouched,
    because re-running the whole batch elsewhere would pay a second vendor for the pages
    that already worked. Stateless, like the search rescue -- the next call attempts the
    chosen backend again.

    Policy refusals are partitioned out first and their original entries preserved
    verbatim. Ported from Hermes' ``_rescue_extract`` (``tools/web_tools.py:533-578``),
    including its defensive branch for a provider that broke order parity.
    """
    if len(results) == len(urls):
        rescue_idx = [i for i, entry in enumerate(results) if not policy_blocked(entry)]
    else:  # a provider that broke order parity: nothing can be paired, rescue them all
        rescue_idx = list(range(len(results)))
    if not rescue_idx:
        return results  # every failure is an intentional policy block

    rescue_urls = [urls[i] for i in rescue_idx] if len(results) == len(urls) else list(urls)
    original_error = next(
        (results[i].get("error") for i in rescue_idx if results[i].get("error")),
        "extract failed",
    )
    logger.warning(
        "web_extract backend '%s' failed all %d URL(s) (%s); one-shot keyless rescue",
        provider_name,
        len(rescue_urls),
        (original_error or "")[:200],
    )
    debug.event("rescue", capability="extract", from_backend=provider_name)
    rescued = await extract_with_failover(provider_name, list(rescue_urls))
    if rescued and all(entry.get("error") for entry in rescued):
        return results  # the ring failed everywhere too: keep the backend's own errors
    for entry in rescued:
        if not entry.get("error"):
            meta = entry.setdefault("metadata", {})
            if isinstance(meta, dict):
                meta["rescued_from"] = provider_name
                meta["backend_error"] = (original_error or "")[:300]
    if len(rescued) == len(rescue_idx) and len(results) == len(urls):
        merged = list(results)
        for position, index in enumerate(rescue_idx):
            merged[index] = rescued[position]
        return merged
    return rescued


def resolve_extractor() -> tuple[WebSearchProvider | None, str, str]:
    """The provider that should extract, its name, and any configuration error.

    Named apart from the registry's ``resolve_extract_provider``, which it calls
    through :func:`active_extract_provider`: that one answers "which provider", this
    one answers "which provider, and if none, what do I tell the model".

    Three refusals, in Hermes' order and with its distinctions intact
    (``tools/web_tools.py:1163-1262``). A registered backend that cannot extract is named
    as search-only rather than silently swapped for one that can -- swapping would answer
    a question the user did not ask, from a vendor they did not choose. A stored name that
    is registered nowhere is a typo. Nothing stored at all falls through to the
    availability walk, as the search side does.
    """
    ensure_backends_registered()
    backend = extract_backend_name()
    if provider_disabled(backend):
        return None, backend, f"Web provider '{backend}' is disabled; use `misaka web enable {backend}`."
    provider = get_provider(backend) if backend else None
    if provider is not None and not provider.supports_extract():
        return None, backend, (
            f"{provider.display_name} is a search-only backend and cannot extract URL "
            "content. Set `extract_backend` in `~/.misaka/web.json` to firecrawl, tavily, "
            "keenable, exa, or parallel."
        )
    if provider is not None:
        return provider, backend, ""
    if backend and selection_stored():
        return None, backend, (
            f"Web extract backend is set to '{backend}', but no registered web extract "
            "provider has that name. Fix the `extract_backend` or `backend` entry in "
            "`~/.misaka/web.json`."
        )
    provider = active_extract_provider()
    if provider is None:
        return None, backend, (
            "No web extract provider configured. Set `extract_backend` in "
            "`~/.misaka/web.json` to firecrawl, tavily, keenable, exa, or parallel."
        )
    return provider, provider.name, ""


async def web_extract(
    provider: WebSearchProvider, urls: list[str], *, format: str | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Run one extract batch through *provider*, rescuing it once if the backend failed.

    Returns ``(results, rescued)``. The flag is the caller's, not decoration: a rescued
    batch came from a ring vendor rather than the chosen backend and must never be
    cached, or one bad minute would pin those pages to the free tier for a whole TTL.

    A provider that raises is a whole-backend failure and is rescued if eligible; one that
    reports every page as failed is the same event described differently, and Hermes
    rescues both.
    """
    timeout = extract_timeout_seconds()
    deadline = asyncio.timeout(timeout if timeout > 0 else None)
    try:
        async with deadline:
            if inspect.iscoroutinefunction(provider.extract):
                results = await provider.extract(list(urls), format=format)
            else:
                results = await run_in_thread(provider.extract, list(urls), format=format)
        if deadline.expired():
            raise TimeoutError  # A provider that swallowed cancellation still timed out.
    except TimeoutError:
        failed = [{"url": url, "title": "", "content": "",
                   "error": f"Extract timed out after {timeout:g}s via {provider.name}"} for url in urls]
        if not rescue_eligible(provider):
            return failed, False
        return await rescue_extract(provider.name, list(urls), failed), True
    except Exception as exc:  # a backend that raises: rescue it, or let it out
        if not rescue_eligible(provider):
            raise
        failed = [
            {"url": url, "title": "", "content": "", "error": str(exc)} for url in urls
        ]
        return await rescue_extract(provider.name, list(urls), failed), True

    if results and all(entry.get("error") for entry in results) and rescue_eligible(provider):
        return await rescue_extract(provider.name, list(urls), results), True
    return results, False
