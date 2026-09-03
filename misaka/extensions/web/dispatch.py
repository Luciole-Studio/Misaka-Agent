"""One search call: resolve the backend, run it, rescue it once if it failed.

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

Three pieces of Hermes' rescue machinery have no counterpart here, each because the thing
they guard does not exist:

* ``_rescue_extract`` and ``_policy_blocked_result`` -- the extract half. The latter's only
  caller in the whole Hermes tree is ``_rescue_extract``: it keeps a page the user's
  website policy deliberately refused from being re-fetched through the ring. There is no
  page fetch on this path and no website-policy table to refuse with.
* ``_disabled_web_plugin_for`` -- diagnoses "you configured this backend but disabled its
  plugin". MISAKA has no plugin-disable table, so the only way to name a backend that is
  not there is a typo, which is what :func:`resolve_provider` says instead.
* the ``WEB_TOOLS_DEBUG`` / ``DebugSession`` call log. A developer facility that writes
  ``logs/web_tools_debug_*.json``; it changes nothing a model sees. The two log lines that
  do carry information are kept verbatim, here and in the memo.
"""

from __future__ import annotations

import logging
from typing import Any

from misaka.extensions.web.config import (
    keyless_rescue_enabled,
    provider_env,
    use_keyless,
)
from misaka.extensions.web.keyless import KEYLESS_RING, search_with_failover
from misaka.extensions.web.provider import WebSearchProvider
from misaka.extensions.web.registry import (
    active_search_provider,
    ensure_backends_registered,
    get_provider,
    search_backend_name,
    selection_stored,
)

logger = logging.getLogger(__name__)

# Which credential decides whether a ring vendor ran keyed or keyless on this call.
_RING_KEY_VARS = {
    "exa": "EXA_API_KEY",
    "parallel": "PARALLEL_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
    "keenable": "KEENABLE_API_KEY",
}


def serves_keyless(provider: WebSearchProvider | None) -> bool:
    """Whether a call on *provider* will be dispatched through the keyless ring.

    Ring membership is the question, not "does this provider have a keyless mode".
    Tavily has one and is deliberately outside the ring, so a keyless Tavily call is a
    single vendor request that can fail and be rescued -- not a walk that already tried
    every free tier there is.
    """
    name = getattr(provider, "name", "")
    if name not in KEYLESS_RING:
        return False
    return use_keyless(name, provider_env(_RING_KEY_VARS.get(name, "")))


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
    if provider is None:
        return False
    try:
        return not serves_keyless(provider)
    except Exception as exc:  # noqa: BLE001 - rescue is best-effort, as in Hermes
        logger.debug("rescue eligibility check failed: %s", exc)
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
    provider = get_provider(backend) if backend else None
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
