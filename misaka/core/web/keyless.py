"""Keyless web search and extract: four vendors' public free tiers, in a round-robin ring.

Ported from Hermes' ``plugins/web/keyless_mcp.py``. This is the module that makes a fresh
install with **zero credentials** able to search the web and read pages at all:

- Exa       https://mcp.exa.ai/mcp          JSON-RPC ``tools/call``, formatted text payload
- Parallel  https://search.parallel.ai/mcp  JSON-RPC ``tools/call``, JSON payload
- Firecrawl https://api.firecrawl.dev       public cloud API, no auth header
- Keenable  https://api.keenable.ai         public endpoints, app-name header

Both capabilities walk the same ring, in the same order, off the same cursor
(:func:`ring_order` decides it once for either), but they fail over on different
evidence. A search either answered or it did not, so one throttled vendor advances the
walk. An extract answers per URL, so it advances only when EVERY page in the batch came
back throttled: one page failing is that page's problem, and re-running the whole batch
somewhere else would spend a second free tier on pages that already succeeded.

The tier is resolved strictly LAST -- after every keyed backend and every configured
one -- so it never pre-empts a deliberate setup. Requests carry no user identifiers.
Parallel's free tier asks for a ``session_id`` used for rate limiting; a random
per-process UUID is sent (rotates every restart, never persisted). Their optional
``model_name`` analytics field is deliberately omitted.

Tavily is deliberately **not** a ring member, matching Hermes: it serves keyless
requests through its own endpoint, but only when the user selected it
(``"backend": "tavily"``), never as one of the vendors a zero-credential install
rotates through. That keyless path lives in :mod:`misaka.core.web.backends.tavily`
next to the keyed one, because the two differ by one header.

Disable the whole tier with ``"keyless_fallback": false`` in ``~/.misaka/web.json``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from misaka.core.tools._web.website_policy import policy_blocked
from misaka.core.web.accounting import account_call
from misaka.core.web.config import config_name, provider_tier
from misaka.core.web.provider import align_documents
from misaka.core.web.runtime import api_client
from misaka.core.web.scope import current_scope
from misaka.core.web.timeouts import http_timeout

logger = logging.getLogger(__name__)

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
FIRECRAWL_API_URL = "https://api.firecrawl.dev"
KEENABLE_API_URL = "https://api.keenable.ai"

# Sent to vendors that require an application identifier. Hermes sends "hermes-agent";
# claiming to be Hermes from MISAKA would misattribute this traffic to another product.
CLIENT_NAME = "misaka-agent"

# Free-tier rate-limit correlation id for Parallel -- random per process, never
# persisted, not derived from any user or machine identifier.
_SESSION_ID = uuid.uuid4().hex

_TIMEOUT_SECONDS = 30


class KeylessError(RuntimeError):
    """A keyless call failed (transport, rate limit, or tool error)."""


_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "429",
    "quota exceeded",
    "slow down",
)


def is_rate_limitish(message: str) -> bool:
    """Heuristic: does an error message look like free-tier throttling?"""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


# ---------------------------------------------------------------------------
# MCP transport (Exa, Parallel)
# ---------------------------------------------------------------------------


def _parse_mcp_body(body: str) -> str:
    """Extract the first text content item from an MCP tools/call response.

    Handles both plain-JSON bodies and SSE (``data: {...}`` lines) -- the Exa endpoint
    answers as an event stream, Parallel as direct JSON. Raises :class:`KeylessError`
    for JSON-RPC errors and ``isError`` tool results (e.g. Exa's free-tier rate-limit
    message).
    """

    def _from_payload(payload: str) -> str | None:
        payload = payload.strip()
        if not payload.startswith("{"):
            return None
        data = json.loads(payload)
        err = data.get("error")
        if err:
            raise KeylessError(str(err.get("message") or err))
        result = data.get("result") or {}
        content = result.get("content") or []
        if result.get("isError"):
            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
            raise KeylessError(" ".join(t for t in texts if t) or "MCP tool call failed")
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                return str(item["text"])
        return None

    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            text = _from_payload(stripped)
            if text is not None:
                return text
        except json.JSONDecodeError:
            pass

    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            text = _from_payload(line[len("data: "):])
        except json.JSONDecodeError:
            continue
        if text is not None:
            return text

    raise KeylessError("Unrecognized MCP response shape")


async def mcp_call(
    url: str,
    tool: str,
    arguments: dict[str, Any],
    timeout: int = _TIMEOUT_SECONDS,
) -> str:
    """POST a JSON-RPC ``tools/call`` to *url* and return the text payload.

    Raises :class:`KeylessError` on transport failures, non-2xx statuses, JSON-RPC
    errors, and error-shaped tool results.
    """
    backend = {PARALLEL_MCP_URL: "parallel", EXA_MCP_URL: "exa"}.get(url, "mcp")
    service = {"web_search": "web_search", "web_search_exa": "web_search",
               "web_fetch": "web_extract", "web_fetch_exa": "web_extract"}.get(tool, "mcp_call")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": CLIENT_NAME,
    }
    try:
        async with (
            api_client(backend + "-mcp", url, timeout=timeout, follow_redirects=True,
                       timeout_provider=backend) as client,
            account_call(service, backend, json.dumps(arguments, sort_keys=True)),
        ):
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise KeylessError(f"request failed: {exc}") from exc
    if response.status_code >= 400:
        raise KeylessError(f"HTTP {response.status_code}: {response.text[:300]}")
    return _parse_mcp_body(response.text)


# ---------------------------------------------------------------------------
# Extract entries: one per requested URL, in the order it was asked for
# ---------------------------------------------------------------------------
#
# The four vendors below answer a page fetch in four different shapes; these two
# constructors are where they all become the list entry
# :mod:`misaka.core.web.provider` documents, so a batch that fails over mid-flight
# does not change shape halfway down.


def _extract_entry(url: str, title: str, content: str) -> dict[str, Any]:
    """A page that was read: the contract entry for *url*.

    ``raw_content`` repeats ``content`` because none of the keyless endpoints offers a
    second, untruncated rendition -- the field exists so the tool above never has to ask
    which vendor served the entry before deciding what to read.
    """
    return {
        "url": url,
        "title": title,
        "content": content,
        "raw_content": content,
        "metadata": {"sourceURL": url, "title": title},
    }


def _extract_error(url: str, error: str) -> dict[str, Any]:
    """A page that was not read: the contract entry for *url*, carrying *error*.

    An entry, never a hole in the list. The caller reassembles its argument list by
    position, so dropping the failure would hand it the next page's text under this
    page's address.
    """
    return {
        "url": url,
        "title": "",
        "content": "",
        "raw_content": "",
        "error": error,
        "metadata": {"sourceURL": url},
    }


# ---------------------------------------------------------------------------
# Parallel (search.parallel.ai) -- JSON text payloads
# ---------------------------------------------------------------------------


async def parallel_search_keyless(query: str, limit: int = 5) -> dict[str, Any]:
    """Keyless Parallel web search -> legacy search response shape."""
    try:
        text = await mcp_call(
            PARALLEL_MCP_URL,
            "web_search",
            {
                "objective": query,
                "search_queries": [query],
                "session_id": _SESSION_ID,
            },
        )
        data = json.loads(text)
        web_results = []
        for i, result in enumerate(data.get("results") or []):
            if limit and i >= limit:
                break
            excerpts = result.get("excerpts") or []
            web_results.append(
                {
                    "url": result.get("url") or "",
                    "title": result.get("title") or "",
                    "description": " ".join(excerpts) if excerpts else "",
                    "position": i + 1,
                }
            )
        return {"success": True, "data": {"web": web_results}}
    except KeylessError as exc:
        return {
            "success": False,
            "error": (
                f"Keyless Parallel search failed: {exc}. "
                "Set PARALLEL_API_KEY (https://parallel.ai) or another web "
                "backend via `~/.misaka/web.json` for reliable service."
            ),
        }
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as exc:
        return {
            "success": False,
            "error": f"Keyless Parallel search returned an unexpected payload: {exc}",
        }


async def parallel_extract_keyless(urls: list[str]) -> list[dict[str, Any]]:
    """Batch Parallel extract, preserving requested slots and unassociated material."""
    try:
        text = await mcp_call(
            PARALLEL_MCP_URL,
            "web_fetch",
            {
                "urls": list(urls),
                "objective": "Full page content",
                "session_id": _SESSION_ID,
            },
        )
        data = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError(f"expected a JSON object, got {type(data).__name__}")
    except (KeylessError, json.JSONDecodeError, TypeError) as exc:
        message = (
            f"Keyless Parallel extract failed: {exc}. "
            "Set PARALLEL_API_KEY (https://parallel.ai) or another web "
            "backend via `~/.misaka/web.json` for reliable service."
        )
        return [_extract_error(url, message) for url in urls]

    documents: list[dict[str, Any]] = []
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url") or "")
        content = (
            result.get("full_content")
            or result.get("content")
            or "\n\n".join(result.get("excerpts") or [])
        )
        entry = _extract_entry(url, str(result.get("title") or ""), content)
        entry["metadata"]["content_kind"] = "page_text" if result.get("full_content") or result.get("content") else "excerpts"
        documents.append(entry)
    for failure in data.get("errors") or []:
        if not isinstance(failure, dict):
            continue
        url = str(failure.get("url") or "")
        detail = failure.get("content") or failure.get("error_type") or "extraction failed"
        documents.append(_extract_error(url, str(detail)))

    return align_documents(urls, documents)

# ---------------------------------------------------------------------------
# Exa (mcp.exa.ai) -- formatted plain-text payloads
# ---------------------------------------------------------------------------


def _parse_exa_search_text(text: str, limit: int) -> list[dict[str, Any]]:
    """Parse Exa's formatted search text into result dicts.

    The payload is blocks separated by ``---`` lines, each shaped like::

        Title: <title>
        URL: <url>
        Published: ...
        Author: ...
        Highlights:
        <free text>
    """
    results: list[dict[str, Any]] = []
    for block in text.split("\n---\n"):
        title = ""
        url = ""
        highlight_lines: list[str] = []
        in_highlights = False
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("Title:"):
                title = stripped[len("Title:"):].strip()
                in_highlights = False
            elif stripped.startswith("URL:"):
                url = stripped[len("URL:"):].strip()
                in_highlights = False
            elif stripped.startswith("Highlights:"):
                in_highlights = True
            elif stripped.startswith(("Published:", "Author:")):
                in_highlights = False
            elif in_highlights and stripped:
                highlight_lines.append(stripped)
        if url:
            results.append(
                {
                    "url": url,
                    "title": title,
                    "description": " ".join(highlight_lines),
                    "position": len(results) + 1,
                }
            )
        if limit and len(results) >= limit:
            break
    return results


async def exa_search_keyless(query: str, limit: int = 5) -> dict[str, Any]:
    """Keyless Exa web search -> legacy search response shape."""
    try:
        text = await mcp_call(
            EXA_MCP_URL,
            "web_search_exa",
            {"query": query, "numResults": max(1, int(limit))},
        )
    except KeylessError as exc:
        return {
            "success": False,
            "error": (
                f"Keyless Exa search failed: {exc}. "
                "Set EXA_API_KEY (https://exa.ai) or another web backend "
                "via `~/.misaka/web.json` for reliable service."
            ),
        }
    return {"success": True, "data": {"web": _parse_exa_search_text(text, limit)}}


def _exa_fetch_title(text: str) -> str:
    """The title at the top of an Exa fetch payload, or ``""``.

    Whichever comes first: a Markdown ``# `` heading or a ``Title:`` line. Exa renders one
    or the other depending on the page, and nothing else in the payload names the page.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped.startswith("Title:"):
            return stripped[len("Title:"):].strip()
    return ""


async def exa_extract_keyless(urls: list[str]) -> list[dict[str, Any]]:
    """Keyless Exa web fetch -> one extract entry per requested URL.

    ``web_fetch_exa`` accepts a ``urls`` array but answers with ONE combined text payload
    and no marker saying where one page ends and the next begins, so a batched call would
    give every URL the concatenation of all of them. One call per URL is what makes each
    reply attributable -- Hermes does the same, for the same reason.
    """
    results: list[dict[str, Any]] = []
    for url in urls:
        try:
            text = await mcp_call(EXA_MCP_URL, "web_fetch_exa", {"urls": [url]})
        except KeylessError as exc:
            results.append(
                _extract_error(
                    url,
                    f"Keyless Exa extract failed: {exc}. "
                    "Set EXA_API_KEY (https://exa.ai) or another web backend "
                    "via `~/.misaka/web.json` for reliable service.",
                )
            )
            continue
        results.append(_extract_entry(url, _exa_fetch_title(text), text))
    return results


# ---------------------------------------------------------------------------
# Firecrawl keyless (public cloud API, no auth header)
# ---------------------------------------------------------------------------


async def firecrawl_search_keyless(query: str, limit: int = 5) -> dict[str, Any]:
    """Keyless Firecrawl cloud search -> legacy search response shape."""
    from misaka.core.web.backends.firecrawl import normalize_search_results
    from misaka.core.web.network import api_network_options

    try:
        async with httpx.AsyncClient(timeout=http_timeout("firecrawl", 60.0), **api_network_options(FIRECRAWL_API_URL)) as client:
            async with account_call("web_search", "firecrawl", query):
                response = await client.post(
                    f"{FIRECRAWL_API_URL}/v2/search",
                    json={"query": query, "limit": limit},
                    headers={"Content-Type": "application/json"},
                )
            response.raise_for_status()
            payload = response.json()
        return {"success": True, "data": {"web": normalize_search_results(payload)}}
    except Exception as exc:  # noqa: BLE001 - normalized below, as in Hermes
        return {
            "success": False,
            "error": (
                f"Keyless Firecrawl search failed: {exc}. "
                "Set FIRECRAWL_API_KEY (https://firecrawl.dev) or another web "
                "backend via `~/.misaka/web.json` for reliable service."
            ),
        }


async def firecrawl_extract_keyless(urls: list[str]) -> list[dict[str, Any]]:
    """Anonymous cloud scrape through the same policy-checked path as keyed calls."""
    from misaka.core.web.backends.firecrawl import scrape_urls

    return await scrape_urls(
        FIRECRAWL_API_URL, {"Content-Type": "application/json"}, urls, format="markdown"
    )


# ---------------------------------------------------------------------------
# Keenable keyless (api.keenable.ai public endpoints)
# ---------------------------------------------------------------------------


async def keenable_search_keyless(query: str, limit: int = 5) -> dict[str, Any]:
    """Keyless Keenable search -> legacy search response shape.

    POST /v1/search/public with the mandatory X-Keenable-Title app identifier (their
    keyless tier requires an app name; no user identifiers are sent). Response:
    ``{results: [{title, url, snippet}]}``.
    """
    try:
        async with (
            api_client("keenable-keyless", KEENABLE_API_URL, timeout=_TIMEOUT_SECONDS, follow_redirects=True,
                       timeout_provider="keenable") as client,
            account_call("web_search", "keenable", query),
        ):
            response = await client.post(
                f"{KEENABLE_API_URL}/v1/search/public",
                json={"query": query, "max_results": max(1, int(limit))},
                headers={
                    "Content-Type": "application/json",
                    "X-Keenable-Title": CLIENT_NAME,
                },
            )
        if response.status_code >= 400:
            raise KeylessError(
                (response.text or "").strip() or f"HTTP {response.status_code}"
            )
        data = response.json()
    except KeylessError as exc:
        return {
            "success": False,
            "error": (
                f"Keyless Keenable search failed: {exc}. "
                "Set KEENABLE_API_KEY (https://keenable.ai) or another web "
                "backend via `~/.misaka/web.json` for reliable service."
            ),
        }
    except Exception as exc:  # noqa: BLE001 - transport/JSON errors, as in Hermes
        return {"success": False, "error": f"Keyless Keenable search failed: {exc}."}
    web_results = []
    for i, result in enumerate(data.get("results") or []):
        web_results.append(
            {
                "url": result.get("url") or "",
                "title": result.get("title") or "",
                "description": result.get("snippet") or result.get("description") or "",
                "position": i + 1,
            }
        )
    return {"success": True, "data": {"web": web_results}}


async def keenable_extract_keyless(urls: list[str]) -> list[dict[str, Any]]:
    """Keyless Keenable page fetch -> one extract entry per requested URL.

    ``GET /v1/fetch/public?url=...`` answers ``{url, title, content}`` with the content in
    markdown, one URL per call, carrying the app-identifier header their keyless tier
    requires and no user identifier. A 4xx/5xx becomes that page's error entry rather than
    an exception, so one unreachable page does not cost the rest of the batch.

    Input order and the provider-reported final URL are separate identities.
    """
    results: list[dict[str, Any]] = []
    for url in urls:
        try:
            async with (
                api_client("keenable-keyless", KEENABLE_API_URL, timeout=_TIMEOUT_SECONDS, follow_redirects=True,
                           timeout_provider="keenable") as client,
                account_call("web_extract", "keenable", url),
            ):
                response = await client.get(
                    f"{KEENABLE_API_URL}/v1/fetch/public",
                    params={"url": url},
                    headers={"X-Keenable-Title": CLIENT_NAME},
                )
            if response.status_code >= 400:
                raise KeylessError(
                    (response.text or "").strip() or f"HTTP {response.status_code}"
                )
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError(f"expected a JSON object, got {type(data).__name__}")
            entry = _extract_entry(url, str(data.get("title") or ""), str(data.get("content") or ""))
            entry["metadata"]["sourceURL"] = str(data.get("url") or url)
            results.append(entry)
        except Exception as exc:  # noqa: BLE001 - per-URL error entry, as in Hermes
            results.append(
                _extract_error(
                    url,
                    f"Keyless Keenable extract failed: {exc}. "
                    "Set KEENABLE_API_KEY (https://keenable.ai) or another web "
                    "backend via `~/.misaka/web.json` for reliable service.",
                )
            )
    return results


# ---------------------------------------------------------------------------
# Round-robin ring + next-in-line failover (rate-limited free tiers)
# ---------------------------------------------------------------------------

KEYLESS_RING = ("exa", "parallel", "firecrawl", "keenable")

_KEYLESS_SEARCHERS = {
    "exa": exa_search_keyless,
    "parallel": parallel_search_keyless,
    "firecrawl": firecrawl_search_keyless,
    "keenable": keenable_search_keyless,
}

_KEYLESS_EXTRACTORS = {
    "exa": exa_extract_keyless,
    "parallel": parallel_extract_keyless,
    "firecrawl": firecrawl_extract_keyless,
    "keenable": keenable_extract_keyless,
}

# WebScope owns the cursor; each call snapshot shares its owner's locked counter.


def _vendor_pinned(name: str) -> bool:
    """True when config explicitly routes web traffic to *name*.

    A pinned vendor starts every keyless request (rotation off); the ring is only walked
    past it on throttle. Pin signals: ``backend`` / ``search_backend`` /
    ``extract_backend`` naming the vendor, or a free-tier pin in ``provider_tier``.

    All three name keys count, as in Hermes, and one function answers for both
    capabilities because :func:`ring_order` is shared: an install that named a vendor
    under only ``extract_backend`` has still chosen it, exactly as
    :func:`misaka.core.web.registry.selection_stored` reads that key. The
    cross-capability reach is the point rather than a side effect -- a pin is a statement
    about which free tier this install is willing to spend, and rotating search through
    three others while extract sits on the named one would spend the three it declined.
    """
    if provider_tier(name) == "free":
        return True
    return any(
        config_name(key) == name
        for key in ("backend", "search_backend", "extract_backend")
    )


def ring_order(name: str) -> list[str]:
    """Return the vendor walk order for a request entering via *name*.

    Pinned vendor -> start at it (its position in the ring determines the failover
    succession). Unpinned -> true round-robin: start at the next cursor position,
    advancing the cursor per request. Vendors whose tier is pinned ``paid`` are excluded
    entirely (an explicit paid selection opts that vendor's free endpoint out).
    """
    from misaka.core.web.config import provider_disabled

    scope = current_scope()
    if _vendor_pinned(name):
        start = KEYLESS_RING.index(name) if name in KEYLESS_RING else 0
    else:
        with scope.lock:
            start = scope.cursor[0]
            scope.cursor[0] = (start + 1) % len(KEYLESS_RING)
    ordered = [
        KEYLESS_RING[(start + i) % len(KEYLESS_RING)] for i in range(len(KEYLESS_RING))
    ]
    return [v for v in ordered if provider_tier(v) != "paid" and not provider_disabled(v)]


def keyless_walk_order() -> tuple[str, ...]:
    """Ring order for *resolution*, without advancing the cursor.

    The registry walks this when deciding which provider can serve keyless, and the ring
    then dispatches -- they have to agree on the entry vendor, so resolution peeks at the
    cursor rather than turning it.
    """

    scope = current_scope()
    with scope.lock:
        start = scope.cursor[0] % len(KEYLESS_RING)
    return tuple(
        KEYLESS_RING[(start + i) % len(KEYLESS_RING)] for i in range(len(KEYLESS_RING))
    )


async def search_with_failover(name: str, query: str, limit: int = 5) -> dict[str, Any]:
    """Keyless search across the vendor ring with next-in-line failover.

    Starts at *name* when the user pinned it, otherwise at the round-robin cursor.
    Rate-limit-shaped errors advance to the next ring vendor; non-throttle errors stop
    the walk (a malformed query fails everywhere). The result notes the serving vendor
    via ``data.served_by`` whenever it differs from *name*.
    """
    order = ring_order(name)
    if not order:
        return {
            "success": False,
            "error": "All keyless web providers are disabled or pinned to paid tiers.",
        }
    last: dict[str, Any] = {}
    for i, vendor in enumerate(order):
        result = await _KEYLESS_SEARCHERS[vendor](query, limit)
        if result.get("success"):
            if vendor != name:
                result.setdefault("data", {})["served_by"] = vendor
            return result
        last = result
        if not is_rate_limitish(result.get("error", "")):
            return result
        nxt = order[i + 1] if i + 1 < len(order) else None
        if nxt:
            logger.info("keyless %s search throttled; failing over to %s", vendor, nxt)
    last["error"] = (
        f"{last.get('error', '')} (all keyless vendors throttled: {', '.join(order)})"
    )
    return last


def _note_served_by(results: list[dict[str, Any]], vendor: str) -> None:
    """Record in each read page's ``metadata`` which ring vendor actually served it.

    The extract counterpart of ``data["served_by"]`` on a search response: a list has no
    envelope to hang one annotation on, so it goes per entry -- which is where Hermes puts
    its own extract-level annotations (``rescued_from`` / ``backend_error`` in
    ``_rescue_extract``). Failed entries are left alone, as Hermes leaves them: their
    error text already names the vendor that produced it.
    """
    for entry in results:
        if entry.get("error"):
            continue
        metadata = entry.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["served_by"] = vendor


async def extract_with_failover(name: str, urls: list[str]) -> list[dict[str, Any]]:
    """Keyless extract across the vendor ring, failing over per batch.

    The extract twin of :func:`search_with_failover`, sharing its walk exactly:
    :func:`ring_order` picks the same entry vendor (pinned, else the round-robin cursor)
    and drops the same paid-pinned members, so a session's two capabilities never disagree
    about which free tier is up next.

    What differs is the evidence for moving on. A search advances on one throttled reply;
    an extract advances only when EVERY URL in the batch came back rate-limit-shaped. One
    page failing is that page's problem -- a 404, a login wall, a PDF the vendor could not
    read -- and re-running the whole batch at the next vendor would spend its free tier on
    pages that already succeeded, then hand the caller the second vendor's fresh failures
    for them.

    Divergence from Hermes, deliberate: when the walk runs out, the entries say so.
    Hermes returns the last vendor's errors verbatim, which names one vendor for what was
    really every vendor, so the same "(all keyless vendors throttled: ...)" note
    :func:`search_with_failover` appends is appended here too.
    """
    order = ring_order(name)
    if not order:
        return [
            _extract_error(url, "All keyless web providers are disabled or pinned to paid tiers.")
            for url in urls
        ]
    last: list[dict[str, Any]] = []
    for i, vendor in enumerate(order):
        results = await _KEYLESS_EXTRACTORS[vendor](list(urls))
        # An empty batch is nobody's throttle: `bool(results)` returns it as it is.
        # A rule or host can itself contain "429"; explicit policy beats prose.
        if not (results and all(
            entry.get("error") and not policy_blocked(entry) and is_rate_limitish(entry["error"])
            for entry in results
        )):
            if vendor != name:
                _note_served_by(results, vendor)
            return results
        last = results
        nxt = order[i + 1] if i + 1 < len(order) else None
        if nxt:
            logger.info("keyless %s extract throttled; failing over to %s", vendor, nxt)
    suffix = f" (all keyless vendors throttled: {', '.join(order)})"
    for entry in last:
        entry["error"] = f"{entry.get('error', '')}{suffix}"
    return last


__all__ = [
    "CLIENT_NAME",
    "KEYLESS_RING",
    "KeylessError",
    "extract_with_failover",
    "is_rate_limitish",
    "keyless_walk_order",
    "mcp_call",
    "ring_order",
    "search_with_failover",
]
