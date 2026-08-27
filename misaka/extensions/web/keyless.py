"""Keyless web search: five vendors' public free tiers, in a round-robin ring.

Ported from Hermes' ``plugins/web/keyless_mcp.py`` (search half; the extract half is not
ported, see :mod:`misaka.extensions.web.provider`). This is the module that makes a fresh
install with **zero credentials** able to search at all:

- Exa       https://mcp.exa.ai/mcp          JSON-RPC ``tools/call``, formatted text payload
- Parallel  https://search.parallel.ai/mcp  JSON-RPC ``tools/call``, JSON payload
- Tavily    https://api.tavily.com          keyless access-mode header
- Firecrawl https://api.firecrawl.dev       public cloud API, no auth header
- Keenable  https://api.keenable.ai         public endpoints, app-name header

The tier is resolved strictly LAST -- after every keyed backend and every configured
one -- so it never pre-empts a deliberate setup. Requests carry no user identifiers.
Parallel's free tier asks for a ``session_id`` used for rate limiting; a random
per-process UUID is sent (rotates every restart, never persisted). Their optional
``model_name`` analytics field is deliberately omitted.

Disable the whole tier with ``"keyless_fallback": false`` in ``~/.misaka/web.json``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from misaka.extensions.web.config import config_name, provider_tier

logger = logging.getLogger(__name__)

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
TAVILY_API_URL = "https://api.tavily.com"
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
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise KeylessError(f"request failed: {exc}") from exc
    if response.status_code >= 400:
        raise KeylessError(f"HTTP {response.status_code}: {response.text[:300]}")
    return _parse_mcp_body(response.text)


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


# ---------------------------------------------------------------------------
# Tavily keyless (api.tavily.com -- X-Tavily-Access-Mode: keyless)
# ---------------------------------------------------------------------------


async def _tavily_keyless_post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to Tavily with keyless headers; raise KeylessError on failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{TAVILY_API_URL}/{endpoint.lstrip('/')}",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Client-Name": CLIENT_NAME,
                    "X-Tavily-Access-Mode": "keyless",
                },
            )
    except httpx.HTTPError as exc:
        raise KeylessError(f"request failed: {exc}") from exc
    if response.status_code >= 400:
        raise KeylessError((response.text or "").strip() or f"HTTP {response.status_code}")
    return response.json()


async def tavily_search_keyless(query: str, limit: int = 5) -> dict[str, Any]:
    """Keyless Tavily search -> legacy search response shape."""
    try:
        data = await _tavily_keyless_post(
            "search", {"query": query, "max_results": max(1, int(limit))}
        )
    except KeylessError as exc:
        return {
            "success": False,
            "error": (
                f"Keyless Tavily search failed: {exc}. "
                "Set TAVILY_API_KEY (https://app.tavily.com) or another web "
                "backend via `~/.misaka/web.json` for reliable service."
            ),
        }
    except ValueError as exc:  # a 2xx body that was not JSON
        return {"success": False, "error": f"Keyless Tavily search failed: {exc}."}
    web_results = []
    for i, result in enumerate(data.get("results") or []):
        web_results.append(
            {
                "url": result.get("url") or "",
                "title": result.get("title") or "",
                "description": result.get("content") or "",
                "position": i + 1,
            }
        )
    return {"success": True, "data": {"web": web_results}}


# ---------------------------------------------------------------------------
# Firecrawl keyless (public cloud API, no auth header)
# ---------------------------------------------------------------------------


async def firecrawl_search_keyless(query: str, limit: int = 5) -> dict[str, Any]:
    """Keyless Firecrawl cloud search -> legacy search response shape."""
    from misaka.extensions.web.backends.firecrawl import normalize_search_results

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
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
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
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


# ---------------------------------------------------------------------------
# Round-robin ring + next-in-line failover (rate-limited free tiers)
# ---------------------------------------------------------------------------

KEYLESS_RING = ("exa", "parallel", "tavily", "firecrawl", "keenable")

_KEYLESS_SEARCHERS = {
    "exa": exa_search_keyless,
    "parallel": parallel_search_keyless,
    "tavily": tavily_search_keyless,
    "firecrawl": firecrawl_search_keyless,
    "keenable": keenable_search_keyless,
}

# Per-process round-robin cursor, seeded by the random session id so a fleet spreads
# evenly across all five free tiers; advances once per unpinned keyless request so a
# single process also rotates. No lock: MISAKA drives tools from one event loop and the
# read-modify-write below has no await in it.
_ring_cursor = int(_SESSION_ID, 16) % len(KEYLESS_RING)


def _vendor_pinned(name: str) -> bool:
    """True when config explicitly routes web traffic to *name*.

    A pinned vendor starts every keyless request (rotation off); the ring is only walked
    past it on throttle. Pin signals: ``backend`` / ``search_backend`` naming the vendor,
    or a free-tier pin in ``provider_tier``.
    """
    if provider_tier(name) == "free":
        return True
    return any(config_name(key) == name for key in ("backend", "search_backend"))


def ring_order(name: str) -> list[str]:
    """Return the vendor walk order for a request entering via *name*.

    Pinned vendor -> start at it (its position in the ring determines the failover
    succession). Unpinned -> true round-robin: start at the next cursor position,
    advancing the cursor per request. Vendors whose tier is pinned ``paid`` are excluded
    entirely (an explicit paid selection opts that vendor's free endpoint out).
    """
    global _ring_cursor
    if _vendor_pinned(name):
        start = KEYLESS_RING.index(name) if name in KEYLESS_RING else 0
    else:
        start = _ring_cursor
        _ring_cursor = (_ring_cursor + 1) % len(KEYLESS_RING)
    ordered = [
        KEYLESS_RING[(start + i) % len(KEYLESS_RING)] for i in range(len(KEYLESS_RING))
    ]
    return [v for v in ordered if provider_tier(v) != "paid"]


def keyless_walk_order() -> tuple[str, ...]:
    """Ring order for *resolution*, without advancing the cursor.

    The registry walks this when deciding which provider can serve keyless, and the ring
    then dispatches -- they have to agree on the entry vendor, so resolution peeks at the
    cursor rather than turning it.
    """
    start = _ring_cursor % len(KEYLESS_RING)
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
            "error": "All keyless web providers are pinned to paid tiers.",
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


__all__ = [
    "CLIENT_NAME",
    "KEYLESS_RING",
    "KeylessError",
    "is_rate_limitish",
    "keyless_walk_order",
    "mcp_call",
    "ring_order",
    "search_with_failover",
]
