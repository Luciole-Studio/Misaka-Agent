"""In-process negative cache: stop re-fetching URLs that just refused us.

The most common waste in a research loop is retrying the site that already said no.
This is an efficiency cache and nothing more -- losing it on restart costs at most one
redundant request -- so it is a plain dict with no persistence and no file.

Time base is ``time.monotonic()``: only durations are ever compared or shown, and a
wall-clock step (NTP correction, DST) must not be able to stretch a 5-minute ban into
hours or expire an hour-long one instantly.

Keys include the URL-tool network policy and the exact URL, so a proxy/policy change
does not inherit another route's ban. A caller that follows redirects keys on the URL it asked for
rather than the one it landed on -- the asked-for URL is what the model will pick
again.

The current WebScope owns the table and its lock. Separate sessions do not inherit
one another's rejection history; snapshots of one call share their owner's table.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from misaka.core.web.network import policy_key
from misaka.core.web.scope import current_scope

# Ban windows in seconds, taken from FrontierAgent's field-tuned scrape cache
# (plugins/tools/_scrape_cache.py, constants _BAN_403/_BAN_422/_BAN_429): 403 is a
# rolling origin or proxy ban, 422 means paywalled/empty/unparseable content, 429 is a
# transient rate limit. Membership in this table is also the "tracked status" rule --
# every other status (404, 5xx, ...) is a one-off and is never cached.
_BAN_SECONDS = {403: 3600, 422: 1800, 429: 300}

# 403 and 429 are verdicts about *us* and hold for the whole window, so one is enough.
# A 422 is a verdict about the *content* and is flaky (late-rendering SPA, partial
# body), which is why FrontierAgent requires two in a row before banning
# (_MIN_FAILS_422 = 2). Statuses absent here ban on the first failure.
_MIN_FAILS = {422: 2}

_REFUSALS = {
    403: "refused the request (403)",
    422: "returned no usable content (422: paywall, empty, or unparseable)",
    429: "rate-limited the request (429)",
}

# Memory bound for a long-lived session. An entry is tiny, so this is generous.
_MAX_ENTRIES = 2048


@dataclass(slots=True)
class _Ban:
    status: int
    fails: int = 0
    until: float = 0.0  # monotonic deadline; 0.0 = failed but not banned yet


# ponytail: keyed by exact URL, like the FrontierAgent cache it is ported from.
# Host-level banning would suppress more requests but punishes a whole domain for one
# paywalled article; if per-URL proves too weak, add a parallel host table rather than
# widening this key.


def skip_reason(url: str, now: float | None = None) -> str | None:
    """Why ``url`` must not be fetched right now, or None if it may be fetched.

    The message is meant to go straight back to the model in place of page content.
    """
    scope = current_scope()
    with scope.lock:
        bans = scope.negative_bans
        key = (policy_key(), url)
        entry = bans.get(key)
        if entry is None:
            return None
        t = time.monotonic() if now is None else now
        if entry.until > t:
            minutes = max(1, round((entry.until - t) / 60))
            return (
                f"Skipped {url}: the site {_REFUSALS[entry.status]} {entry.fails}x earlier in this "
                f"session, cached for ~{minutes} more min. Use a different source or search query."
            )
        if entry.until:
            # Ban served -- forget it so the next failure starts a fresh count. An entry
            # with until == 0.0 is a bare fail count and must survive.
            del bans[key]
        return None


def record_failure(url: str, status: int, now: float | None = None) -> None:
    """Note a rejection. Untracked statuses are ignored."""
    scope = current_scope()
    with scope.lock:
        bans = scope.negative_bans
        if status not in _BAN_SECONDS:
            return
        t = time.monotonic() if now is None else now
        key = (policy_key(), url)
        entry = bans.get(key)
        if entry is not None and entry.until > t:
            # Concurrent fetches of one URL both clear the gate before either reports back,
            # so a second verdict can arrive against a ban already being served. It tells us
            # nothing new, and letting it through would rewrite the entry: a racing 429 would
            # cut an hour-long 403 ban to five minutes, and a single 422 -- which on its own
            # is not even meant to ban -- would drop the ban entirely.
            return
        if entry is None or entry.status != status:
            # A different rejection is a different story: count it from scratch.
            if entry is None and len(bans) >= _MAX_ENTRIES:
                _evict(t, bans)
            entry = bans[key] = _Ban(status)
        entry.fails += 1
        if entry.fails >= _MIN_FAILS.get(status, 1):
            entry.until = t + _BAN_SECONDS[status]


def record_success(url: str) -> None:
    """Forget any recorded failure for ``url`` -- the site is answering again."""
    scope = current_scope()
    with scope.lock:
        bans = scope.negative_bans
        bans.pop((policy_key(), url), None)


def clear() -> None:
    """Drop every entry."""
    scope = current_scope()
    with scope.lock:
        bans = scope.negative_bans
        bans.clear()


def _evict(now: float, bans: dict[str, _Ban]) -> None:
    """Make room: served bans and bare fail counts first, then the ban ending soonest."""
    for url in [u for u, e in bans.items() if e.until <= now]:
        del bans[url]
    if len(bans) >= _MAX_ENTRIES:
        del bans[min(bans, key=lambda u: bans[u].until)]
