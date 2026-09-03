"""Firecrawl web search and scraping (keyed or self-hosted over REST, or keyless).

Ported from Hermes' ``plugins/web/firecrawl/provider.py``, minus the Nous tool-gateway,
which has no MISAKA counterpart. Both halves are here: ``/v2/search`` with the
response-shape normalizer Firecrawl needs because it answers in three different shapes
depending on whether the caller used the SDK, the cloud API or a self-hosted instance, and
the per-URL ``/v2/scrape`` loop with its policy gate, its post-redirect re-checks and its
format selection.

Firecrawl is the only backend that reads ``format``, and the only one that runs the
operator's blocklist and the SSRF gate itself. It has to: it is the one vendor that
follows redirects on its own servers and then tells you where it landed, so the URL the
tool screened is not necessarily the URL that was read.

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "firecrawl"     # explicit per-capability
    "extract_backend": "firecrawl"    # explicit per-capability
    "backend": "firecrawl"            # shared fallback (and the final default)
    "provider_tier": {"firecrawl": "free"|"paid"}

Env vars::

    FIRECRAWL_API_KEY=...            # direct cloud auth
    FIRECRAWL_API_URL=...            # self-hosted Firecrawl
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from misaka.core.tools._web.bounded import UnsafeUrlError, vet_public_url
from misaka.core.tools._web.website_policy import check_website_access
from misaka.extensions.web.config import (
    keyless_tier_enabled,
    provider_env,
    provider_tier,
    use_keyless,
)
from misaka.extensions.web.keyless import (
    FIRECRAWL_API_URL,
    extract_with_failover,
    search_with_failover,
)
from misaka.extensions.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Hermes' per-URL ceiling (``plugins/web/firecrawl/provider.py:675``). A scrape renders
# JavaScript on the vendor's servers, so a slow page is normal and a stuck one is common;
# without a wall-clock bound one bad URL holds the whole batch open.
_SCRAPE_TIMEOUT_SECONDS = 60


def _normalize_result_list(values: Any) -> list[dict[str, Any]]:
    """Normalize a mixed payload into a list of dicts."""
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def normalize_search_results(response: Any) -> list[dict[str, Any]]:
    """Extract Firecrawl search results across its response shapes, in contract form.

    Hermes hands the raw vendor dicts on unchanged, which quietly drops ``position``
    from every Firecrawl result -- the one place its own contract is not met. Numbering
    happens here instead, because the tool above renders by position and a missing key
    would silently renumber the list. Narrowing to the four contract keys is the other
    half of that: forwarding the vendor's extra fields is what let the omission hide.

    ``snippet`` is accepted beside ``description`` for the same reason the keyed and
    keyless paths share this function: a self-hosted Firecrawl is a different build from
    the cloud one, and the summary key is the field most likely to differ between them.
    """
    entries: list[dict[str, Any]] = []
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, list):
            entries = _normalize_result_list(data)
        elif isinstance(data, dict):
            entries = _normalize_result_list(data.get("web")) or _normalize_result_list(
                data.get("results")
            )
        if not entries:
            entries = _normalize_result_list(
                response.get("web")
            ) or _normalize_result_list(response.get("results"))

    return [
        {
            "title": str(entry.get("title") or ""),
            "url": str(entry.get("url") or ""),
            "description": str(entry.get("description") or entry.get("snippet") or ""),
            "position": i + 1,
        }
        for i, entry in enumerate(entries)
    ]


def _failed(
    url: str, error: str, *, title: str = "", source_url: str = ""
) -> dict[str, Any]:
    """The contract entry for a page Firecrawl did not return.

    An entry, never a hole in the list: the caller reassembles its argument list by
    position, so a dropped failure hands it the next page's text under this page's
    address. *source_url* records where the vendor actually ended up when a redirect
    carried the request somewhere the gates then refused.
    """
    return {
        "url": url,
        "title": title,
        "content": "",
        "raw_content": "",
        "error": error,
        "metadata": {"sourceURL": source_url or url, "title": title},
    }


def _blocked(
    url: str, block: dict[str, str], *, title: str = "", source_url: str = ""
) -> dict[str, Any]:
    """The entry for a page the operator's blocklist refused.

    ``blocked_by_policy`` is not decoration: it is what
    :func:`misaka.extensions.web.dispatch.policy_blocked` reads to keep the keyless rescue
    from re-fetching, through a vendor the user never configured, the very page the user
    forbade. The rule and its source travel with it because the user is owed the pattern
    and the file that stopped their fetch, or they cannot undo it.
    """
    entry = _failed(url, block["message"], title=title, source_url=source_url)
    entry["blocked_by_policy"] = {
        "host": block["host"],
        "rule": block["rule"],
        "source": block["source"],
    }
    return entry


async def _scrape(
    endpoint: str, headers: dict[str, str], url: str, formats: list[str]
) -> dict[str, Any]:
    """One ``/v2/scrape`` call, normalized to the object Firecrawl buried the page in.

    The 60-second ceiling is wall clock and belongs to the whole request. httpx's own
    timeout is per operation, so a page that dribbles one byte at a time satisfies it
    forever while holding the batch open; Hermes wraps each scrape in
    ``asyncio.wait_for(..., 60)`` for exactly that reason, and the httpx timeout stays
    alongside it so a hung connect fails at the same bound.

    The last three lines are Hermes' ``_extract_scrape_payload``: depending on which
    Firecrawl build answered -- cloud, self-hosted, or its SDK -- the scraped object is
    either the body itself or nested one level down under ``data``.
    """
    async with asyncio.timeout(_SCRAPE_TIMEOUT_SECONDS):
        async with httpx.AsyncClient(timeout=_SCRAPE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{endpoint}/v2/scrape",
                json={"url": url, "formats": formats},
                headers=headers,
            )
    if response.status_code >= 400:
        detail = (response.text or "").strip() or f"HTTP {response.status_code}"
        raise ValueError(f"Firecrawl scrape failed: {detail}")
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    return payload if isinstance(payload, dict) else {}


class FirecrawlWebSearchProvider(WebSearchProvider):
    """Firecrawl search and extract provider."""

    @property
    def name(self) -> str:
        return "firecrawl"

    @property
    def display_name(self) -> str:
        return "Firecrawl"

    def is_available(self) -> bool:
        """Return True when a key or a self-hosted instance URL is configured."""
        return bool(provider_env("FIRECRAWL_API_KEY") or provider_env("FIRECRAWL_API_URL"))

    def is_keyless_available(self) -> bool:
        """Firecrawl's public cloud API accepts anonymous rate-limited requests.

        False when the user pinned ``"provider_tier": {"firecrawl": "paid"}``.
        """
        return keyless_tier_enabled() and provider_tier("firecrawl") != "paid"

    def supports_extract(self) -> bool:
        """Firecrawl renders and scrapes pages through ``/v2/scrape``; see :meth:`extract`."""
        return True

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Firecrawl search."""
        try:
            api_key = provider_env("FIRECRAWL_API_KEY")
            api_url = provider_env("FIRECRAWL_API_URL").rstrip("/")
            # A self-hosted instance is a deliberate setup even without a key: it must
            # never be traded for the public cloud endpoint by the keyless ring.
            #
            # Divergence from Hermes, deliberate: its ``_use_keyless_ring()`` returns
            # False the moment FIRECRAWL_API_KEY is set, so a ``"firecrawl": "free"``
            # tier pin is silently ignored here and honoured by every other vendor.
            # Firecrawl is the odd one out in its own tree; ``use_keyless`` is the
            # documented chokepoint ("free forces the keyless endpoint even when the
            # vendor API key is present"), so this asks it like the other four do.
            if not api_url and use_keyless("firecrawl", api_key):
                logger.info("Firecrawl keyless search: '%s' (limit=%d)", query, limit)
                return await search_with_failover("firecrawl", query, limit)

            if not api_key and not api_url:
                return {
                    "success": False,
                    "error": (
                        "FIRECRAWL_API_KEY environment variable not set. "
                        "Get your API key at https://firecrawl.dev"
                    ),
                }

            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            logger.info("Firecrawl search: '%s' (limit=%d)", query, limit)
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{api_url or FIRECRAWL_API_URL}/v2/search",
                    json={"query": query, "limit": max(1, int(limit))},
                    headers=headers,
                )
            if response.status_code >= 400:
                detail = (response.text or "").strip() or f"HTTP {response.status_code}"
                return {"success": False, "error": f"Firecrawl search failed: {detail}"}
            return {
                "success": True,
                "data": {"web": normalize_search_results(response.json())},
            }
        except Exception as exc:  # noqa: BLE001 - surface as failure, as in Hermes
            logger.warning("Firecrawl search error: %s", exc)
            return {"success": False, "error": f"Firecrawl search failed: {exc}"}

    async def extract(
        self, urls: list[str], *, format: str | None = None
    ) -> list[dict[str, Any]]:
        """Scrape each URL through ``/v2/scrape``, one request per URL, 60 seconds each.

        The only backend that reads *format*, ported exactly: ``"markdown"`` asks for
        markdown, ``"html"`` for html, anything else (None included) for both, and the
        selection afterwards prefers markdown when it was asked for or when nothing was
        asked for and the page produced some, otherwise html.

        Three gates, in Hermes' order (``plugins/web/firecrawl/provider.py:651-745``).
        The operator's blocklist is checked BEFORE the scrape, so a refused host costs no
        vendor request. The URL Firecrawl reports having ended on is checked AFTER it,
        first for SSRF and then against the blocklist again, because this is the one
        vendor that follows redirects on its own servers: an allowed public host that
        302s to ``169.254.169.254`` would otherwise come back as a page the tool hands
        the model, having passed every check the tool could make before the fetch.

        A per-URL failure is that URL's entry, always in position -- a timeout, a scrape
        error, either block. Nothing here raises: every request stands alone, and the
        dispatcher's all-entries-failed check is what turns "every page failed" back into
        the one-shot keyless rescue.

        Divergence from Hermes, deliberate and the same one
        :func:`misaka.extensions.web.keyless.keenable_extract_keyless` records: an entry's
        ``url`` is the URL the caller asked for, not the redirect target Hermes puts
        there. The list is reassembled by position and an entry addressed elsewhere is the
        mispairing the contract exists to prevent; where the bytes actually came from
        travels in ``metadata["sourceURL"]``, which is where Firecrawl reported it and
        where Hermes reads it back from.

        Two smaller ones. The timeout message says ``web_fetch`` where Hermes says
        ``browser_navigate``, because MISAKA has no browser tool and advice naming one is
        advice the model cannot take. And an empty string, never ``None``, is what a page
        with no usable rendition contributes: Hermes' first selection branch can hand the
        contract's ``content`` a null when ``format="markdown"`` was asked of a page that
        produced no markdown.
        """
        api_key = provider_env("FIRECRAWL_API_KEY")
        api_url = provider_env("FIRECRAWL_API_URL").rstrip("/")
        # The same decision :meth:`search` makes, self-hosted guard included: a private
        # instance is a deliberate setup even without a key and must never be traded for
        # the public cloud endpoint. A vendor whose two capabilities disagree about which
        # tier they are on is the kind of bug nobody finds for months.
        if not api_url and use_keyless("firecrawl", api_key):
            logger.info("Firecrawl keyless extract: %d URL(s)", len(urls))
            return await extract_with_failover("firecrawl", list(urls))

        if not api_key and not api_url:
            # A configuration refusal rather than a raise: every URL failed for one
            # reason and each says so, and the dispatcher's all-entries-failed check
            # gives the batch the same one-shot rescue a raise would have.
            return [
                _failed(
                    url,
                    "FIRECRAWL_API_KEY environment variable not set. "
                    "Get your API key at https://firecrawl.dev",
                )
                for url in urls
            ]

        if format == "markdown":
            formats = ["markdown"]
        elif format == "html":
            formats = ["html"]
        else:
            formats = ["markdown", "html"]

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        endpoint = api_url or FIRECRAWL_API_URL

        logger.info("Firecrawl extract: %d URL(s) (formats=%s)", len(urls), formats)
        results: list[dict[str, Any]] = []
        for url in urls:
            blocked = check_website_access(url)
            if blocked:
                logger.info(
                    "Blocked web_extract for %s by rule %s",
                    blocked["host"],
                    blocked["rule"],
                )
                results.append(_blocked(url, blocked))
                continue

            try:
                payload = await _scrape(endpoint, headers, url, formats)
            except (TimeoutError, httpx.TimeoutException):
                # Both spellings: ``asyncio.timeout`` raises the builtin, httpx raises its
                # own, and neither is a subclass of the other.
                logger.warning("Firecrawl scrape timed out for %s", url)
                results.append(
                    _failed(
                        url,
                        f"Scrape timed out after {_SCRAPE_TIMEOUT_SECONDS}s -- page may "
                        "be too large or unresponsive. Try web_fetch instead.",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - per-URL error entry, as in Hermes
                logger.debug("Firecrawl scrape failed for %s: %s", url, exc)
                results.append(_failed(url, str(exc)))
                continue

            metadata = payload.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            title = str(metadata.get("title") or "")
            final_url = str(metadata.get("sourceURL") or url)

            try:
                await vet_public_url(final_url)
            except UnsafeUrlError as exc:
                logger.info(
                    "Blocked redirected web_extract for unsafe final URL: %s", final_url
                )
                results.append(
                    _failed(
                        url,
                        # The gate's own reason, not a claim on top of it: it refuses a
                        # metadata endpoint, a private answer and an unresolvable name
                        # alike, and only one of those is "private network address".
                        f"Blocked: Firecrawl reported reading {final_url}, refused by "
                        f"the outbound URL check ({exc})",
                        title=title,
                        source_url=final_url,
                    )
                )
                continue

            final_blocked = check_website_access(final_url)
            if final_blocked:
                logger.info(
                    "Blocked redirected web_extract for %s by rule %s",
                    final_blocked["host"],
                    final_blocked["rule"],
                )
                results.append(
                    _blocked(url, final_blocked, title=title, source_url=final_url)
                )
                continue

            markdown = str(payload.get("markdown") or "")
            html = str(payload.get("html") or "")
            if format == "markdown" or (format is None and markdown):
                content = markdown
            else:
                content = html or markdown
            results.append(
                {
                    "url": url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": {**metadata, "sourceURL": final_url, "title": title},
                }
            )
        return results

    def setup_hint(self) -> dict[str, Any]:
        return {
            "name": "Firecrawl",
            "badge": "free - key optional",
            "tag": "Search and page scraping via Firecrawl cloud or a self-hosted instance.",
            "env_vars": [
                {
                    "key": "FIRECRAWL_API_KEY",
                    "prompt": "Firecrawl API key (optional - keyless cloud works without it)",
                    "url": "https://firecrawl.dev",
                },
                {
                    "key": "FIRECRAWL_API_URL",
                    "prompt": "Self-hosted Firecrawl URL",
                    "url": "https://docs.firecrawl.dev/contributing/self-host",
                },
            ],
        }
