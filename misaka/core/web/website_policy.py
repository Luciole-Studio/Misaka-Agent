"""The user's website blocklist: hosts every URL-capable tool must refuse to fetch.

Ported from Hermes' ``tools/website_policy.py``. This is a *policy* table, not a safety
control -- ``_web/bounded.py`` is what stops a URL reaching a private address. This is
the user saying "never fetch this domain on my behalf", which means it is theirs to edit
and ours to obey exactly: a rule that silently stops matching is worse than no rule, so
nothing here widens or narrows a pattern beyond what was written.

Configuration source is the one structural change from Hermes, which reads
``security.website_blocklist`` out of ``~/.hermes/config.yaml`` with PyYAML. MISAKA has
no ``security`` section and no YAML config at all; web behaviour is the ``web`` section of
``settings.json`` (global, with a role's own overlay), and the policy is read from a
``website_blocklist`` key there::

    {"website_blocklist": {"enabled": true,
                           "domains": ["ads.example", "*.cdn.example"],
                           "shared_files": ["blocked.txt"]}}

Default reads use core.web.config's strict shared/profile view. Explicit paths still
load only that document and report malformed policy to the caller. Relative shared
files are resolved against the layer declaring them, never the process directory.

``enabled`` is opt-in with no implicit default: a ``domains`` list without
``"enabled": true`` blocks nothing. Hermes behaves the same way (its defaults carry
``enabled: False`` and the user's mapping only ever overrides it), and the behaviour is
kept rather than "fixed" because the alternative -- inferring intent from a non-empty list
-- turns a half-written config into a wall the user never asked for.

Divergence from Hermes worth recording. Hermes *enforces* this table at six call sites:
browser navigation (``tools/browser_tool.py:4179`` and ``:4270``, which answer with a
``blocked_by_policy`` record), image and video fetches (``tools/vision_tools.py:545``,
``:615``, ``:2026``, ``:2119``, which raise ``PermissionError``), skill-bundle downloads
(``tools/skills_hub.py:422``), the image resolver's pre-flight
(``tools/image_source.py:189``) and the Firecrawl provider's per-URL loop
(``plugins/web/firecrawl/provider.py:651``). Only ``tools/web_tools.py:1279`` uses it for
something other than refusal -- deciding a URL is ineligible for the extract cache. The
genuine gap is that the extract gate lives *inside* the Firecrawl provider, so a Tavily or
Exa extract is never checked at all; MISAKA screens at the tool layer instead
(:mod:`misaka.core.web.screening`, above every backend and over its own direct
fetches -- ``web_fetch``, ``download_file``), so a newly added backend cannot quietly opt
out of the user's blocklist.

Initial screening is followed by per-hop checks in
:func:`misaka.core.web.bounded.open_checked_stream`. Firecrawl checks its reported
final source on both paid and free paths; ``web_extract`` checks that source again on a
cache hit. A vendor-side post-check rejects returned content, not an already-made remote
request. Policy refusals remain terminal through both rescue and free-tier failover.

The 30-second cache is not a micro-optimisation for one URL: it is what keeps a 50-URL
extract from re-reading and re-parsing the same document 51 times. Its lock is real
rather than defensive -- MISAKA runs several sessions in one process.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from misaka.config import home
from misaka.core.web.config import _config_path, web_config
from misaka.core.web.scope import current_scope

logger = logging.getLogger(__name__)

# The shape a missing file, an absent key, or a null stands in for. ``enabled: False`` is
# the load-bearing entry: absent configuration must never block anything.
_DEFAULT_WEBSITE_BLOCKLIST: dict[str, Any] = {
    "enabled": False,
    "domains": [],
    "shared_files": [],
}

# Long enough that a multi-URL extract reads the file once, short enough that a user who
# has just added a domain sees it take effect without restarting the session.
_CACHE_TTL_SECONDS = 30.0
_cache_lock = threading.Lock()
_cached_policy: dict[str, Any] | None = None
_cached_policy_path: str | None = None
_cached_policy_time: float = 0.0


class WebsitePolicyError(Exception):
    """Raised when the website policy configuration is malformed."""


def policy_blocked(result: dict[str, Any]) -> bool:
    """A website-policy refusal is terminal for both rescue and free-tier failover."""
    if result.get("blocked_by_policy"):
        return True
    return "blocked by website policy" in str(result.get("error") or "").lower()


def _default_config_path() -> Path:
    return Path(_config_path())


def _policy_cache_key() -> str:
    # Standalone checks retain Hermes' TTL; tool calls use their consistent config view.
    if current_scope().config is not None:
        return f"{_default_config_path()}|{json.dumps(web_config().get('website_blocklist'), sort_keys=True)}"
    return f"{home.path('settings')}|{_default_config_path()}"


def _normalize_host(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if not host.isascii():
        try:
            return httpx.URL(host=host).raw_host.decode("ascii").rstrip(".")
        except httpx.InvalidURL:
            pass  # An invalid host is left for the outbound URL gate to reject.
    return host


def _normalize_rule(rule: Any) -> str | None:
    """Reduce one written rule to the bare host it means, or ``None`` if it means nothing.

    Users write rules the way they write bookmarks, so a full URL, a trailing path, a
    ``www.`` prefix and a stray comment line all have to survive contact with the matcher.
    Dropping ``www.`` here rather than at match time is what makes the rule ``www.x.com``
    and the rule ``x.com`` the same rule -- the host side keeps its ``www.`` and matches
    by dot-suffix instead.
    """
    if not isinstance(rule, str):
        return None
    value = rule.strip().lower()
    if not value or value.startswith("#"):
        return None
    if "://" in value:
        try:
            parsed = urlparse(value)
            value = parsed.hostname or ""
        except ValueError:
            # An authority urlparse refuses outright ("http://[::1") names no host, so the
            # rule means nothing. Raising here would put a ValueError through
            # load_website_blocklist, which only ever contracts to raise WebsitePolicyError.
            return None
    value = value.split("/", 1)[0].strip().rstrip(".")
    # Encode literal IDN labels but preserve the pattern's existing ASCII glob syntax.
    value = ".".join(label if label.isascii() else _normalize_host(label) for label in value.split("."))
    # Hermes slices `value[4:]` behind a `startswith` here; `removeprefix` is the same
    # operation and is what this repo's ruff insists on (FURB188).
    value = value.removeprefix("www.")
    return value or None


def _iter_blocklist_file_rules(path: Path) -> list[str]:
    """Rules from one shared blocklist file.

    A missing or unreadable file logs a warning and contributes nothing rather than
    raising: shared lists are typically checked out from somewhere else, and a stale path
    in a config must not be able to take every web tool offline.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Shared blocklist file not found (skipping): %s", path)
        return []
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read shared blocklist file %s (skipping): %s", path, exc)
        return []

    rules: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        normalized = _normalize_rule(stripped)
        if normalized:
            rules.append(normalized)
    return rules


def _load_policy_config(config_path: Path | None) -> dict[str, Any]:
    """The raw ``website_blocklist`` mapping, merged over the defaults.

    Every failure that is *not* "there is no config" raises: a typo in the one file that
    says what to block is the user's problem to see, and :func:`check_website_access`
    decides separately whether this call may fail open on it.
    """
    if config_path is not None and not config_path.exists():
        return dict(_DEFAULT_WEBSITE_BLOCKLIST)

    try:
        config = (json.loads(config_path.read_text(encoding="utf-8")) or {}
                  if config_path is not None else web_config(strict=True))
    except json.JSONDecodeError as exc:
        raise WebsitePolicyError(f"Invalid config JSON at {config_path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise WebsitePolicyError(f"Failed to read config file {config_path}: {exc}") from exc
    except ValueError as exc:
        raise WebsitePolicyError(str(exc)) from exc
    if not isinstance(config, dict):
        raise WebsitePolicyError("web.json root must be a mapping")

    website_blocklist = config.get("website_blocklist", {})
    if website_blocklist is None:
        website_blocklist = {}
    if not isinstance(website_blocklist, dict):
        raise WebsitePolicyError("website_blocklist must be a mapping")

    policy = dict(_DEFAULT_WEBSITE_BLOCKLIST)
    policy.update(website_blocklist)
    return policy


def load_website_blocklist(config_path: Path | None = None) -> dict[str, Any]:
    """The parsed policy: ``{"enabled": bool, "rules": [{"pattern", "source"}]}``.

    ``source`` is either the literal ``"config"`` or the shared file's path, and it exists
    so a block message can tell the user *where* to go and delete the rule.

    Results for the default path are cached for :data:`_CACHE_TTL_SECONDS`. Passing an
    explicit ``config_path`` bypasses the cache in both directions -- it is neither read
    nor written -- which is what lets a test observe an edit without racing the TTL.
    """
    global _cached_policy, _cached_policy_path, _cached_policy_time

    default_path = _default_config_path()
    use_default = config_path is None
    resolved_path = _policy_cache_key() if use_default else str(config_path)
    now = time.monotonic()

    # Cached policy, if it is still fresh and was read from this same path. The path is
    # part of the test because the settings file is redirectable at runtime (``MISAKA_HOME``).
    if config_path is None:
        with _cache_lock:
            if (
                _cached_policy is not None
                and _cached_policy_path == resolved_path
                and (now - _cached_policy_time) < _CACHE_TTL_SECONDS
            ):
                return _cached_policy

    policy = _load_policy_config(config_path)
    config_path = config_path or default_path

    raw_domains = policy.get("domains", []) or []
    if not isinstance(raw_domains, list):
        raise WebsitePolicyError("website_blocklist.domains must be a list")

    raw_shared_files = policy.get("shared_files", []) or []
    if not isinstance(raw_shared_files, list):
        raise WebsitePolicyError("website_blocklist.shared_files must be a list")

    enabled = policy.get("enabled", True)
    if not isinstance(enabled, bool):
        raise WebsitePolicyError("website_blocklist.enabled must be a boolean")

    rules: list[dict[str, str]] = []
    # (source, pattern): the same host written in the config and in a shared file are two
    # rules, because they are two places the user would have to edit to unblock it.
    seen: set[tuple[str, str]] = set()

    for raw_rule in raw_domains:
        normalized = _normalize_rule(raw_rule)
        if normalized and ("config", normalized) not in seen:
            rules.append({"pattern": normalized, "source": "config"})
            seen.add(("config", normalized))

    # A relative shared file is relative to the config directory (``~/.misaka``, the
    # parent of web.json) -- never to the process working directory, which for an agent
    # is whatever repository it happens to have been started in.
    config_dir = config_path.parent
    for shared_file in raw_shared_files:
        if not isinstance(shared_file, str) or not shared_file.strip():
            continue
        path = Path(shared_file).expanduser()
        if not path.is_absolute():
            path = (config_dir / path).resolve()
        for normalized in _iter_blocklist_file_rules(path):
            key = (str(path), normalized)
            if key in seen:
                continue
            rules.append({"pattern": normalized, "source": str(path)})
            seen.add(key)

    result: dict[str, Any] = {"enabled": enabled, "rules": rules}

    if use_default:
        with _cache_lock:
            _cached_policy = result
            _cached_policy_path = resolved_path
            _cached_policy_time = now

    return result


def invalidate_cache() -> None:
    """Force the next :func:`check_website_access` to re-read the config."""
    global _cached_policy
    with _cache_lock:
        _cached_policy = None


def _match_host_against_rule(host: str, pattern: str) -> bool:
    """Whether one host is covered by one rule.

    Two shapes, deliberately: an explicit ``*.`` pattern is a glob and covers only
    subdomains, while a bare host covers itself *and* every subdomain of it. The
    dot-suffix half is the part that earns its keep -- a user who blocks ``mysite.dev``
    means ``preview.mysite.dev`` too -- and the leading dot is what keeps
    ``notexample.com`` from matching ``example.com``.
    """
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        return fnmatch.fnmatch(host, pattern)
    return host == pattern or host.endswith(f".{pattern}")


def _extract_host_from_urlish(url: str) -> str:
    """The host of something the model called a URL.

    A bare ``example.com/path`` has no scheme, so ``urlparse`` files the whole thing under
    ``path`` and reports no host at all; re-parsing it as ``//example.com/path`` is what
    recovers the host. Returning ``""`` means "nothing to check", which is an allow --
    this is a blocklist, and a string with no host reaches no site.

    An authority ``urlparse`` cannot parse at all -- an unbalanced bracket, ``https://[::1``
    -- raises ``ValueError`` instead of reporting no host, and that has to land in the same
    ``""``. This function runs *before* the try/except in :func:`check_website_access`, so
    an escaping ValueError would be the one way this module can break a caller: web_fetch
    turns any schemeless string into ``https://<string>``, so a model typing ``[::1``
    arrives here as ``https://[::1``, and :func:`screening.screen_url` above it promises
    never to raise.
    """
    try:
        parsed = urlparse(url)
        host = _normalize_host(parsed.hostname or parsed.netloc)
        if not host and "://" not in url:
            schemeless = urlparse(f"//{url}")
            host = _normalize_host(schemeless.hostname or schemeless.netloc)
    except ValueError:
        return ""

    return host


def check_website_access(url: str, config_path: Path | None = None) -> dict[str, str] | None:
    """``None`` when *url* is allowed, otherwise the block record for it.

    A block carries ``url``, ``host``, ``rule``, ``source`` and a ``message`` written for
    the model to relay: the user is owed the pattern and the file that stopped their
    fetch, or they cannot undo it.

    Policy errors fail open -- logged and treated as "allowed" -- when no explicit
    ``config_path`` was given, so a typo in web.json cannot take every web tool offline.
    Pass ``config_path`` (which is what tests do) and errors propagate instead: the
    asymmetry is deliberate, because a test that asserts on a malformed config must see
    the exception rather than the silence a session gets.
    """
    # Fast path: with the cached policy disabled there is nothing any URL could match, so
    # skip the file read and the host parse entirely. This is the state on almost every
    # installation, and it sits in front of every fetch MISAKA makes.
    #
    # It carries the same freshness and same-path tests as the cache read inside
    # load_website_blocklist, and must: ``enabled`` is opt-in, so *every* installation
    # starts out cached-disabled, and the user's first act on this feature is the
    # false -> true edit. A fast path that never expired would swallow exactly that
    # transition and hold it for the life of the process -- invalidate_cache() has no
    # production caller, so nothing in a running session could clear it.
    if config_path is None:
        with _cache_lock:
            if (
                _cached_policy is not None
                and not _cached_policy.get("enabled")
                and (time.monotonic() - _cached_policy_time) < _CACHE_TTL_SECONDS
                and _cached_policy_path == _policy_cache_key()
            ):
                return None

    host = _extract_host_from_urlish(url)
    if not host:
        return None

    try:
        policy = load_website_blocklist(config_path)
    except WebsitePolicyError as exc:
        if config_path is not None:
            raise  # An explicit path is a caller that wants the error, not a session.
        logger.warning("Website policy config error (failing open): %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - fail open: no config fault may break web tools
        logger.warning("Unexpected error loading website policy (failing open): %s", exc)
        return None

    if not policy.get("enabled"):
        return None

    for rule in policy.get("rules", []):
        pattern = rule.get("pattern", "")
        if _match_host_against_rule(host, pattern):
            source = rule.get("source", "config")
            logger.info("Blocked URL %s -- matched rule '%s' from %s", url, pattern, source)
            return {
                "url": url,
                "host": host,
                "rule": pattern,
                "source": source,
                "message": (
                    f"Blocked by website policy: '{host}' matched rule '{pattern}'"
                    f" from {source}"
                ),
            }
    return None


__all__ = [
    "WebsitePolicyError",
    "check_website_access",
    "invalidate_cache",
    "load_website_blocklist",
    "policy_blocked",
]
