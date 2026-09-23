"""Tavily web search and page extraction (keyed, or opt-in keyless).

Ported from Hermes' ``plugins/web/tavily/provider.py``.

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "tavily"     # explicit per-capability
    "extract_backend": "tavily"    # explicit per-capability
    "backend": "tavily"            # shared fallback
    "provider_tier": {"tavily": "free"|"paid"}

Env vars::

    TAVILY_API_KEY=...           # https://app.tavily.com/home (optional)
    TAVILY_BASE_URL=...          # optional override of https://api.tavily.com

Auth is header-based. A key uses ``Authorization: Bearer``; without a key the request is
keyless (``X-Tavily-Access-Mode: keyless``).

Tavily is **not** a member of the zero-config keyless ring
(:data:`misaka.core.web.keyless.KEYLESS_RING`), matching Hermes. Keyless access is
opt-in: it serves an explicit ``"backend": "tavily"`` without a key, but a fresh install
with no web credentials rotates across Exa / Parallel / Firecrawl / Keenable instead and
never lands here. A ring vendor's failure is walked past to the next vendor; Tavily's is
returned to the caller, which is what makes it eligible for the one-shot keyless rescue.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from misaka.core.web.accounting import account_call
from misaka.core.web.config import (
    config_label,
    keyless_tier_enabled,
    provider_env,
    provider_tier,
    use_keyless,
)
from misaka.core.web.keyless import CLIENT_NAME
from misaka.core.web.network import api_network_options
from misaka.core.web.provider import (
    WebSearchProvider,
    align_documents,
    check_response,
    extraction_error,
)
from misaka.core.web.timeouts import http_timeout

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
        f"opt-in keyless {action} in `{config_label()}`: unpin `provider_tier.tavily` "
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

    async with (
        httpx.AsyncClient(timeout=http_timeout("tavily", 60), **api_network_options(url)) as client,
        account_call(f"web_{endpoint.lstrip('/')}", "tavily", payload.get("query") or "\n".join(payload.get("urls") or [])),
    ):
        response = await client.post(url, json=payload, headers=_tavily_headers(api_key))
    if response.status_code >= 400:
        body = (response.text or "").strip()
        detail = body or f"HTTP {response.status_code}"
        raise ValueError(detail)
    return check_response(response.json())



def normalize_extract_documents(
    response: dict[str, Any], urls: list[str]
) -> list[dict[str, Any]]:
    """Map a Tavily ``/extract`` response onto *urls*: one entry each, in order.

    Three lists carry the answer and all three have to be walked, because a URL named in
    none of them is a URL Tavily dropped without saying so. ``results`` are the pages it
    read -- ``raw_content`` preferred over ``content``, the same page untruncated;
    ``failed_results`` carry their own error text, which is the only place the vendor says
    *why*; and ``failed_urls`` is a bare list of strings with no reason attached at all,
    hence the literal "extraction failed" Hermes uses for them.

    Preserve canonical response URLs and unassociated material; the shared association
    helper gives requested slots their exact URL/ID match or an explicit error.
    """
    check_response(response)
    fallback = urls[0] if len(urls) == 1 else ""
    documents: list[dict[str, Any]] = []

    for result in response.get("results") or []:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url") or fallback)
        title = str(result.get("title") or "")
        content = str(result.get("raw_content") or result.get("content") or "")
        documents.append(
            {
                "url": url,
                "title": title,
                "content": content,
                "raw_content": content,
                "metadata": {"sourceURL": url, "title": title},
            },
        )
    for failure in response.get("failed_results") or []:
        if not isinstance(failure, dict):
            continue
        url = str(failure.get("url") or fallback)
        documents.append(extraction_error(url, str(failure.get("error") or "extraction failed")))
    for failed_url in response.get("failed_urls") or []:
        url = failed_url if isinstance(failed_url, str) else str(failed_url)
        documents.append(extraction_error(url, "extraction failed"))

    return align_documents(urls, documents)


def normalize_search_results(response: dict[str, Any]) -> dict[str, Any]:
    """Map a Tavily ``/search`` response to ``{success, data: {web: [...]}}``."""
    check_response(response)
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

    def supports_extract(self) -> bool:
        """Tavily reads whole pages through ``/extract``; see :meth:`extract`."""
        return True

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

    async def extract(
        self, urls: list[str], *, format: str | None = None
    ) -> list[dict[str, Any]]:
        """Read every URL through Tavily's ``/extract``, in one batched request.

        *format* is ignored: Tavily returns one rendition per page, so ``content`` and
        ``raw_content`` differ only in truncation, not in markup.

        Keyless goes to Tavily's OWN endpoint, never the ring -- Tavily is deliberately
        not a ring member (see the module docstring), and a keyless extract is one
        request that can fail and be rescued rather than a walk that already spent every
        free tier there is. That is the same routing :meth:`search` uses, decided by the
        same ``use_keyless`` call.

        Divergence from Hermes, deliberate: it catches the non-2xx ``ValueError`` from the
        request helper and turns it into one error entry per URL. A rejected key or an
        unreachable endpoint is a whole-backend failure, and raising is how the contract
        says to report one -- the dispatcher catches it and may route the batch through
        the keyless ring once. The missing-credential refusal below is the one that stays
        per-entry, because it is a configuration answer rather than an outage.
        """
        api_key = provider_env("TAVILY_API_KEY")
        force_keyless = use_keyless("tavily", api_key)
        if not force_keyless and not api_key:
            error = _missing_key_error("extract")
            return [extraction_error(url, error) for url in urls]

        logger.info(
            "Tavily %sextract: %d URL(s)",
            "keyless " if force_keyless else "",
            len(urls),
        )
        raw = await tavily_request(
            "extract",
            {"urls": list(urls), "include_images": False},
            api_key="" if force_keyless else api_key,
        )
        return normalize_extract_documents(raw, list(urls))

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Tavily",
            "badge": "free - key optional",
            "tag": (
                "Search and page extraction. Works keyless; set TAVILY_API_KEY "
                "for higher limits."
            ),
            "env_vars": [
                {
                    "key": "TAVILY_API_KEY",
                    "prompt": "Tavily API key (optional - keyless works without it)",
                    "url": "https://app.tavily.com/home",
                },
            ],
        }
