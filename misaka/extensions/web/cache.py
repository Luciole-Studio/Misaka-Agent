"""TTL memo and single-flight coalescing for search results.

Ported from the search half of Hermes' ``tools/web_result_cache.py``. Keyed by
(provider, normalized query, bucketed limit): identical queries inside the TTL -- the
fan-out of a research run, a model re-asking the same thing two turns later -- are served
from memory instead of paid again, and concurrent identical queries share one request.

Requested limits are rounded UP to 10/20/50/100 so near-identical requests (limit=5 vs
limit=8) share one entry; the provider is asked for the bucket and the caller's own count
is sliced out of it by :func:`slice_search_response`.

Why this lives beside the tool and not in generic tool dispatch (Hermes' own note on the
same question): a dispatch-level memo would have to reason about approval gates and hooks
on a cache hit. Down here the memo sits *after* every config and safety check and
*before* the paid vendor call, so a hit skips the network request and never a control.

Disabled with ``cache_enabled: false`` in ``~/.misaka/web.json``; the TTL comes from
``cache_ttl_minutes`` there. Only successful responses are ever stored, and a response
the keyless ring rescued is never offered to :meth:`SearchMemo.store` by the tool -- see
the note there.

**Not ported:** the disk-backed extract cache that is the second half of the Hermes file
(``extract_cache_get`` / ``extract_cache_put`` / the ``cache/web`` sidecar index and its
local-dev and exempt-host rules). It caches ``web_extract`` page text, and MISAKA reaches
pages through ``web_fetch`` instead; porting it would land a module nothing calls.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from misaka.extensions.web.config import web_config

logger = logging.getLogger(__name__)

# Requested search limits are rounded UP to one of these buckets so cache keys collide on
# purpose. Callers get their requested count sliced out.
_LIMIT_BUCKETS = (10, 20, 50, 100)

DEFAULT_TTL_MINUTES = 20


def cache_enabled() -> bool:
    """The memo honours ``cache_enabled`` in ``~/.misaka/web.json`` (default: on)."""
    value = web_config().get("cache_enabled")
    if value is None:
        return True
    return bool(value)


def ttl_seconds() -> float:
    """TTL from ``cache_ttl_minutes`` (default 20, clamped to 1-1440)."""
    raw = web_config().get("cache_ttl_minutes")
    try:
        minutes = float(raw) if raw is not None else DEFAULT_TTL_MINUTES
    except (TypeError, ValueError):
        minutes = DEFAULT_TTL_MINUTES
    minutes = max(1.0, min(minutes, 1440.0))
    return minutes * 60.0


def bucket_limit(limit: int) -> int:
    """Round a requested result count up to the nearest bucket."""
    for bucket in _LIMIT_BUCKETS:
        if limit <= bucket:
            return bucket
    return _LIMIT_BUCKETS[-1]


def normalize_query(query: str) -> str:
    """Case-fold and collapse whitespace so trivial variants share an entry."""
    return re.sub(r"\s+", " ", (query or "").strip().lower())


class SearchMemo:
    """TTL memo for search responses.

    No lock, where Hermes holds a ``threading.Lock``: Hermes dispatches tools from a
    thread pool, MISAKA drives every tool from one event loop, and there is no ``await``
    between any read and its matching write below. The same argument the ring cursor and
    ``_web/single_flight.py`` already make. Coalescing of concurrent identical calls is
    not this class's job either -- that is ``single_flight`` around the whole miss path,
    which is the async equivalent of Hermes' per-key flight lock.
    """

    def __init__(self) -> None:
        self._store: dict[tuple, tuple[float, dict]] = {}

    def _key(self, provider: str, query: str, limit: int) -> tuple:
        return (provider, normalize_query(query), bucket_limit(limit))

    def lookup(self, provider: str, query: str, limit: int) -> dict | None:
        if not cache_enabled():
            return None
        key = self._key(provider, query, limit)
        hit = self._store.get(key)
        if hit is None:
            return None
        expires, response = hit
        if time.monotonic() >= expires:
            del self._store[key]
            return None
        logger.info("web_search cache hit: %r via %s", query, provider)
        return json.loads(json.dumps(response))  # defensive copy

    def store(self, provider: str, query: str, limit: int, response: dict) -> None:
        """Cache a SUCCESSFUL response under the bucketed key."""
        if not cache_enabled():
            return
        if not isinstance(response, dict) or not response.get("success"):
            return
        key = self._key(provider, query, limit)
        # Opportunistic expiry sweep to bound memory.
        now = time.monotonic()
        for expired in [k for k, (exp, _) in self._store.items() if now >= exp]:
            del self._store[expired]
        self._store[key] = (now + ttl_seconds(), json.loads(json.dumps(response)))

    def clear(self) -> None:
        """Drop every cached entry (tests; a config change)."""
        self._store.clear()


search_memo = SearchMemo()


def flight_key(provider: str, query: str, limit: int) -> str:
    """The single-flight key for one search: the memo key, as a string.

    ``_web/single_flight.py`` keys on a string, and it has to agree with the memo or two
    callers whose requests would share a cache entry would still both pay for it.
    """
    return f"web-search\x1f{provider}\x1f{bucket_limit(limit)}\x1f{normalize_query(query)}"


def slice_search_response(response: dict[str, Any], limit: int) -> dict[str, Any]:
    """Trim a bucketed response's result list down to the caller's own limit."""
    try:
        web = response.get("data", {}).get("web")
        if isinstance(web, list) and len(web) > limit:
            out = json.loads(json.dumps(response))
            out["data"]["web"] = out["data"]["web"][:limit]
            return out
    except Exception as exc:  # noqa: BLE001 - a malformed response is returned unsliced
        logger.debug("web_search response could not be sliced: %s", exc)
    return response
