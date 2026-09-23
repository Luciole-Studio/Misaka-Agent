"""The two web result caches: a TTL memo for searches, a disk store for extractions.

**Search memo** -- in memory, per process, ported from the search half of Hermes'
``tools/web_result_cache.py``. Keyed by (provider, normalized query, bucketed limit):
identical queries inside the TTL -- the
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

**Extract cache** -- on disk under ``~/.misaka/cache/web``, ported from the second half of
the same Hermes file. It outlives the process and every session on the machine shares it,
which is the point: a repeat ``web_extract`` of a URL inside the TTL reads the stored clean
text back instead of paying a vendor to render the page again. Keyed by
(url, format, provider), all three -- an html extract is not a markdown one, and one
backend's rendering of a page is not another's.

Two rules narrow what it will hold, and both are about freshness rather than safety. A
local or private-address URL is a dev server the user is editing, where serving a whole
TTL of stale build output is the opposite of what the fetch was for. A host listed in
``cache_exempt_hosts`` is a staging deploy or a tunnel: public DNS, so the local-address
heuristic cannot see it, but every fetch still has to be live. The safety questions --
is this URL an SSRF target, does it carry a credential, does the operator's blocklist
refuse it -- are asked before anything reaches here, in ``misaka.core.web``.

Both halves read the same two config keys, so switching the cache off or retuning its TTL
is one edit rather than two.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
import time
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from filelock import FileLock

from misaka.config import expand_tilde_path
from misaka.config.product import CFG
from misaka.core.web.config import redact_values, web_config
from misaka.core.web.evidence import citable_url
from misaka.core.web.scope import cache_namespace
from misaka.utils import atomic

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
    """TTL memo shared only by the same profile/config/provider identity, across loops."""

    def __init__(self) -> None:
        self._store: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _key(self, provider: str, query: str, limit: int) -> tuple:
        return (cache_namespace(), provider, normalize_query(query), bucket_limit(limit))

    def lookup(self, provider: str, query: str, limit: int) -> dict | None:
        if not cache_enabled():
            return None
        key = self._key(provider, query, limit)
        with self._lock:
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
        entry = (now + ttl_seconds(), json.loads(json.dumps(response)))
        with self._lock:
            for expired in [k for k, (exp, _) in self._store.items() if now >= exp]:
                del self._store[expired]
            self._store[key] = entry

    def clear(self) -> None:
        """Drop every cached entry (tests; a config change)."""
        with self._lock:
            self._store.clear()


search_memo = SearchMemo()


def flight_key(provider: str, query: str, limit: int) -> str:
    """The single-flight key for one search: the memo key, as a string.

    ``_web/single_flight.py`` keys on a string, and it has to agree with the memo or two
    callers whose requests would share a cache entry would still both pay for it.
    """
    return f"web-search\x1f{cache_namespace()}\x1f{provider}\x1f{bucket_limit(limit)}\x1f{normalize_query(query)}"


def slice_search_response(response: dict[str, Any], limit: int) -> dict[str, Any]:
    """Trim a bucketed response's result list down to the caller's own limit."""
    try:
        web = response.get("data", {}).get("web")
        if isinstance(web, list) and len(web) > limit:
            out = json.loads(json.dumps(response))
            out["data"]["web"] = out["data"]["web"][:limit]
            return out
    except Exception:  # noqa: BLE001, S110 - a malformed response is returned unsliced
        pass
    return response


# ── Extract cache (on disk, shared by every session on the machine) ───────────

# The sidecar index inside the cache directory: digest -> {url, file, title, fetched_at}.
_INDEX_FILENAME = "extract-index.json"

# Entries kept in the index; a save past this evicts the oldest by ``fetched_at``. The
# cap bounds the JSON document that every lookup parses, not the disk the entry files sit
# on -- those are pruned by whatever cleans ``~/.misaka/cache``.
_INDEX_MAX_ENTRIES = 500

#: Longest page text one cache entry may hold. Hermes' number and Hermes' reasoning
#: (``tools/web_tools.py``: 2MB of markdown is already far more than any one read-through
#: needs, and some backends return very large pages), but a different rule on top of it:
#: Hermes' truncate-store writes a *capped* copy with a marker saying so, while a page
#: over this ceiling is not cached here at all. A capped copy served back through
#: :func:`extract_cache_get` would look like the whole page and silently lose its tail.
#: The extract tool imports this for its own stored copy, so the two agree on the number.
MAX_STORED_TEXT_CHARS = 2_000_000

# Guards the read-modify-write of the index, where the search memo above deliberately
# holds no lock. The difference is that these two functions do blocking disk I/O, so the
# tool above is expected to call them through ``asyncio.to_thread`` rather than stall the
# loop on a page write -- which puts two concurrent extractions on two real threads. The
# atomic replace in :func:`_save_index` already rules out a torn file; this rules out the
# lost insert, where two threads each load the same index and the second write drops the
# first one's entry. Writers additionally hold the directory's interprocess lock
# from body publication through index update and eviction.
_index_lock = threading.Lock()


def _cache_dir() -> Path | None:
    """``~/.misaka/cache/web``, created on demand; None when it cannot be.

    ``~/.misaka/cache/<subsystem>/`` is the established layout (``core/mcp.py``,
    ``skills/index.py``). Read from ``CFG`` on every call rather than expanded from a
    literal once at import, for two reasons: a path frozen at import time would still
    point at the developer's own cache after a test moved ``HOME``, and ``CFG`` is what
    ``tests/conftest.py`` and every web test already redirect. A cache that escapes that
    redirection writes the suite's pages into the developer's real home and then serves
    them back to the next run.

    Hermes' broad ``except`` is kept here rather than narrowed to ``OSError``, because the
    failure set is not knowable from this frame: ``expand_tilde_path`` on a ``~`` path
    reaches ``Path.home()``, which raises ``RuntimeError`` when ``HOME`` is unset and the
    uid has no passwd entry -- a container run as ``--user 1000:1000``, an ``env -i``
    subprocess. That is precisely a directory that "cannot be", and every caller below is
    written for the None. Letting it out instead turns a cache lookup nobody asked for
    into a failed ``web_extract`` of a batch of URLs that never needed the cache.
    """
    try:
        directory = Path(expand_tilde_path(str(CFG["web_cache"])))
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except Exception:  # noqa: BLE001 - no cache directory is a miss, never a raise
        return None


def _index_path() -> Path | None:
    directory = _cache_dir()
    return (directory / _INDEX_FILENAME) if directory is not None else None


def _fetched_at(entry: object) -> float:
    """An index entry's timestamp, or 0.0 for anything that is not one.

    Both the freshness check and the eviction sort come through here because the index is
    plain JSON that anything on the machine can edit: an entry whose ``fetched_at`` is a
    string, or which is not a mapping at all, has to read as infinitely old -- a miss and
    the first thing evicted -- rather than raise out of a cache lookup.
    """
    if not isinstance(entry, dict):
        return 0.0
    try:
        return float(entry.get("fetched_at", 0))
    except (TypeError, ValueError):
        return 0.0


def _load_index() -> dict:
    """The sidecar index, or ``{}``. A corrupt or unreadable one is an empty cache.

    Hermes' broad ``except`` again, and for the same reason as :func:`_cache_dir`: the
    index is a plain JSON file anything on the machine can write, so what ``json.loads``
    raises over it is not a set this frame can enumerate. A 60000-deep array of ``[``
    raises ``RecursionError``, which is not a ``ValueError`` -- and a hostile index is the
    one case this function exists to absorb.
    """
    path = _index_path()
    if path is None or not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt index is an empty cache
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_index(index: dict) -> None:
    """Publish the capped index, then remove evicted/expired cache bodies.

    Caller holds the writer lock through body publication and this update. Never
    delete a body before publishing the index, or while another writer publishes it.
    """
    path = _index_path()
    if path is None:
        return
    try:
        previous = index
        now, ttl = time.time(), ttl_seconds()
        index = {key: entry for key, entry in index.items() if now - _fetched_at(entry) < ttl}
        if len(index) > _INDEX_MAX_ENTRIES:
            newest = sorted(index.items(), key=lambda kv: _fetched_at(kv[1]), reverse=True)
            index = dict(newest[:_INDEX_MAX_ENTRIES])
        # A configured provider/extension can return account-private material and URLs.
        atomic.write_text(path, json.dumps(index), mode=0o600)
        retained = {str(entry.get("file")) for entry in index.values() if isinstance(entry, dict)}
        for key, entry in previous.items():
            if key in index or not isinstance(entry, dict):
                continue
            body = Path(str(entry.get("file", "")))
            if (str(body) not in retained and body.parent.resolve() == path.parent.resolve()
                    and re.fullmatch(r"[A-Za-z0-9._-]+-[0-9a-f]{16}\.cache\.json", body.name)
                    and not body.is_symlink()):
                body.unlink(missing_ok=True)
        # Bounded cleanup of bodies orphaned by pre-lock writers or older versions.
        # A TTL grace protects files an older, still-running process is publishing.
        with os.scandir(path.parent) as files:
            for entry in islice(files, _INDEX_MAX_ENTRIES + 128):
                if (entry.path not in retained and re.fullmatch(r"[A-Za-z0-9._-]+-[0-9a-f]{16}\.cache\.json", entry.name)
                        and entry.is_file(follow_symlinks=False) and now - entry.stat().st_mtime >= ttl):
                    Path(entry.path).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001, S110 - a cache write never breaks the caller
        pass


def _url_digest(url: str, format: str | None, provider: str = "") -> str:
    """The cache key for one extraction, as 16 hex digits.

    All three parts participate. Hermes' own review found that a URL-only key let an html
    extract and a markdown one of the same page overwrite each other, and that switching
    extract backends inside the TTL served the old backend's rendering.
    """
    raw = f"{cache_namespace()}\n{url}\n{format or 'markdown'}\n{provider or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _hostname(url: str) -> str:
    """The URL's lowercased hostname, or ``""`` when it has none.

    ``urlparse`` raises on a malformed authority (a bad IPv6 literal is the usual one),
    and each caller below has its own answer for "no host": do not cache it, do not
    exempt it, name its file ``page``.
    """
    try:
        return (urlparse(url).hostname or "").strip("[]").lower()
    except ValueError:
        return ""


def _entry_file_path(url: str, format: str | None, provider: str) -> Path | None:
    """One atomic cache envelope per (URL, format, provider), separate from evidence."""
    directory = _cache_dir()
    if directory is None:
        return None
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", _hostname(url) or "page")[:60].strip("-")
    return directory / f"{slug or 'page'}-{_url_digest(url, format, provider)}.cache.json"


def _host_matches_pattern(host: str, pattern: str) -> bool:
    """Case-insensitive host match: exact, ``*.wildcard``, or bare-domain suffix.

    ``mysite.dev`` therefore also matches ``preview.mysite.dev``, which is the shape a
    user actually writes for a staging site. Matching is on label boundaries, so it does
    not also catch ``evilmysite.dev``.
    """
    host = host.lower().strip(".")
    pattern = (pattern or "").lower().strip().strip(".")
    if not pattern:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern or host.endswith("." + pattern)


def _is_cache_exempt_host(url: str) -> bool:
    """True when the URL's host matches ``cache_exempt_hosts`` in ``~/.misaka/web.json``.

    For a site the user is developing but reaching over the public internet -- a staging
    deploy, a tunnel URL, a preview build. Public DNS, so :func:`_is_local_dev_url` cannot
    recognise it, yet every fetch has to be live. Checked at put *and* at get, so adding
    an entry takes effect on the next lookup instead of after the current TTL.

    Fails open to caching: a garbage value here means the user mistyped a config key, and
    a stale page is a smaller harm than a cache that silently switches itself off.
    """
    patterns = web_config().get("cache_exempt_hosts")
    if not isinstance(patterns, (list, tuple)) or not patterns:
        return False
    host = _hostname(url)
    if not host:
        return False
    return any(_host_matches_pattern(host, str(pattern)) for pattern in patterns)


def _is_local_dev_url(url: str) -> bool:
    """True for a loopback, private or LAN URL -- never cached.

    A page on a private address is one the user controls and is typically changing every
    few seconds: a dev server with hot reload, a LAN preview app. Freshness is the entire
    reason for fetching it, so the cache declines rather than pin a stale build for a
    whole TTL.

    Hostname heuristics only, and no DNS: this is a freshness decision, not a security
    boundary. The security boundary is :mod:`misaka.core.web.bounded`, which
    resolves the name and pins the address -- and which refuses most of these outright
    unless the operator opened the private ranges.
    """
    host = _hostname(url)
    if not host:
        return True  # unparseable, so nothing here can be reasoned about: do not cache
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    # A single-label name is a LAN hostname, not public DNS.
    if "." not in host and ":" not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # a public DNS name
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def extract_cache_get(
    url: str,
    *,
    format: str | None = None,
    provider: str = "",
) -> dict | None:
    """Return a fresh cached extraction of *url*, or None.

    The hit is shaped like one entry of the provider contract in
    :mod:`misaka.core.web.provider`, plus ``cached``: ``{"url", "title", "content",
    "error": None, "cached": True, "metadata"}``. Every failure here -- disabled, exempt, expired,
    tampered index, evicted file -- is the same None, because a caller that cannot tell
    them apart cannot do anything different about them either.

    Keyword-only where Hermes takes positionals, so a call site cannot silently pass a
    provider name into the *format* slot.
    """
    if not cache_enabled():
        return None
    if _is_local_dev_url(url) or _is_cache_exempt_host(url):
        return None
    with _index_lock:
        entry = _load_index().get(_url_digest(url, format, provider))
    if not isinstance(entry, dict):
        return None
    if time.time() - _fetched_at(entry) >= ttl_seconds():
        return None
    try:
        file_path = Path(str(entry.get("file", "")))
        cache_root = _cache_dir()
        # The index is plain JSON on disk. An entry edited to point somewhere else must
        # not turn a cache lookup into an arbitrary file read, so the resolved path has
        # to sit under the resolved cache directory -- which also settles the symlink
        # case, since resolving follows one out of the directory.
        if cache_root is None or cache_root.resolve() not in file_path.resolve().parents:
            return None
        page = json.loads(file_path.read_text(encoding="utf-8"))
        if (not isinstance(page, dict) or page.get("url") != url
                or not isinstance(page.get("content"), str)
                or not isinstance(page.get("metadata"), dict)):
            return None
    except (OSError, ValueError, RecursionError):
        return None
    logger.info("web_extract cache hit: %s", url)
    return {
        "url": url,
        "title": str(page.get("title", "") or ""),
        "content": redact_values(page["content"]),
        "error": None,
        "cached": True,
        "metadata": page["metadata"],
    }


def extract_cache_put(
    url: str,
    content: str,
    *,
    title: str = "",
    format: str | None = None,
    provider: str = "",
    metadata: dict | None = None,
) -> None:
    """Store one successful extraction's clean text for TTL reuse. Best-effort.

    Nothing here raises: a full disk, a read-only home, a cache directory somebody
    replaced with a file -- each of those costs the next lookup a paid re-extraction and
    is worth one debug line, never a failed tool call on a page that was fetched fine.
    """
    if not cache_enabled() or not content:
        return
    if _is_local_dev_url(url) or _is_cache_exempt_host(url):
        return
    if len(content) > MAX_STORED_TEXT_CHARS:
        return
    try:
        file_path = _entry_file_path(url, format, provider)
        if file_path is None:
            return
        # Publish body and provenance together. Independent file/index writes can
        # otherwise pair one writer's body with another writer's title and source.
        page = {
            "url": url, "title": title or "", "content": content,
            "metadata": {
                "sourceURL": citable_url((metadata or {}).get("sourceURL") or url),
                "served_by": (metadata or {}).get("served_by") or provider,
                **({"content_kind": metadata["content_kind"]} if metadata and "content_kind" in metadata else {}),
            },
        }
        with _index_lock, FileLock(str(file_path.parent / ".extract.lock"), timeout=5):
            atomic.write_text(file_path, json.dumps(redact_values(page)), mode=0o600)
            index = _load_index()
            index[_url_digest(url, format, provider)] = {
                "url": url,
                "file": str(file_path),
                "fetched_at": time.time(),
            }
            _save_index(index)
    except Exception:  # noqa: BLE001, S110 - a cache write never breaks the caller
        pass
