"""Pinned pool selection, cooldown and failed-key attribution; host owns credential I/O."""
from __future__ import annotations
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from .credential_persistence import sanitize_borrowed_credential_payload
from ..host import pool as auth_mod
logger = logging.getLogger(__name__)
_TOKENS_SINGLETON_PROVIDERS = {}

STATUS_OK = "ok"


STATUS_EXHAUSTED = "exhausted"


STATUS_DEAD = "dead"


_TERMINAL_AUTH_REASONS = frozenset({
    "token_invalidated",
    "token_revoked",
    "invalid_token",
    "invalid_grant",
    "unauthorized_client",
    "refresh_token_reused",  # single-use refresh token consumed by another process
})


CREDENTIAL_PERSIST_FAILED_REASON = "credential_persist_failed"


DEAD_MANUAL_PRUNE_TTL_SECONDS = 24 * 60 * 60


AUTH_TYPE_OAUTH = "oauth"


AUTH_TYPE_API_KEY = "api_key"


SOURCE_MANUAL = "manual"


STRATEGY_FILL_FIRST = "fill_first"


STRATEGY_ROUND_ROBIN = "round_robin"


STRATEGY_RANDOM = "random"


STRATEGY_LEAST_USED = "least_used"


SUPPORTED_POOL_STRATEGIES = {
    STRATEGY_FILL_FIRST,
    STRATEGY_ROUND_ROBIN,
    STRATEGY_RANDOM,
    STRATEGY_LEAST_USED,
}


EXHAUSTED_TTL_401_SECONDS = 5 * 60


EXHAUSTED_TTL_429_SECONDS = 60 * 60


EXHAUSTED_TTL_DEFAULT_SECONDS = 60 * 60


EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS = 60


FAILURE_REASON_BILLING = "billing"


FAILURE_REASON_BILLING_UNVERIFIED = "billing_unverified"


NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS = 60.0


_EXTRA_KEYS = frozenset({
    "token_type", "scope", "client_id", "portal_base_url", "obtained_at",
    "expires_in", "agent_key_id", "agent_key_expires_in", "agent_key_reused",
    "agent_key_obtained_at", "tls", "secret_source", "secret_fingerprint",
    # Classified failure semantics for the last exhaustion (agent/error_classifier.py).
    # Providers return 403 for both an edge throttle and a spending limit, so the
    # raw status cannot size a cooldown; persisted so a restart doesn't downgrade
    # a billing bench to a 60s transient cooldown.
    "failure_reason",
})


_CLEAR_STATUS: Dict[str, Any] = {
    "last_status": None,
    "last_status_at": None,
    "last_error_code": None,
    "last_error_reason": None,
    "last_error_message": None,
    "last_error_reset_at": None,
}


_MARK_OK: Dict[str, Any] = {**_CLEAR_STATUS, "last_status": STATUS_OK}


def _normalize_pool_auth_type(provider: str, token: Any, auth_type: Any) -> str:
    """Infer pool auth metadata for token formats with one unambiguous meaning."""
    if provider == "anthropic" and isinstance(token, str) and token.startswith("sk-ant-oat"):
        return AUTH_TYPE_OAUTH
    return str(auth_type or AUTH_TYPE_API_KEY)


@dataclass
class PooledCredential:
    provider: str
    id: str
    label: str
    auth_type: str
    priority: int
    source: str
    access_token: str
    refresh_token: Optional[str] = None
    last_status: Optional[str] = None
    last_status_at: Optional[float] = None
    last_error_code: Optional[int] = None
    last_error_reason: Optional[str] = None
    last_error_message: Optional[str] = None
    last_error_reset_at: Optional[float] = None
    base_url: Optional[str] = None
    expires_at: Optional[str] = None
    expires_at_ms: Optional[int] = None
    last_refresh: Optional[str] = None
    inference_base_url: Optional[str] = None
    agent_key: Optional[str] = None
    agent_key_expires_at: Optional[str] = None
    request_count: int = 0
    extra: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}
        self.auth_type = _normalize_pool_auth_type(self.provider, self.access_token, self.auth_type)

    def __getattr__(self, name: str):
        if name in _EXTRA_KEYS:
            return self.extra.get(name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute {name!r}")

    @classmethod
    def from_dict(cls, provider: str, payload: Dict[str, Any]) -> "PooledCredential":
        field_names = {f.name for f in fields(cls) if f.name != "provider"}
        data = {k: payload.get(k) for k in field_names if k in payload}
        # Rehydrated last_status_at may be an ISO string from to_dict() — normalize to float epoch
        if isinstance(data.get("last_status_at"), str):
            data["last_status_at"] = _parse_absolute_timestamp(data["last_status_at"])
        data["extra"] = {k: payload[k] for k in _EXTRA_KEYS if payload.get(k) is not None}
        data.setdefault("id", uuid.uuid4().hex[:6])
        data.setdefault("label", payload.get("source", provider))
        data.setdefault("auth_type", AUTH_TYPE_API_KEY)
        data.setdefault("priority", 0)
        data.setdefault("source", SOURCE_MANUAL)
        data.setdefault("access_token", "")
        return cls(provider=provider, **data)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for field_def in fields(self):
            if field_def.name in {"provider", "extra"}:
                continue
            value = getattr(self, field_def.name)
            if value is not None or field_def.name in _CLEAR_STATUS:
                result[field_def.name] = value
        for k, v in self.extra.items():
            if v is not None:
                result[k] = v
        return sanitize_borrowed_credential_payload(result, self.provider)

    @property
    def runtime_api_key(self) -> str:
        if self.provider == "nous":
            # Nous stores the runtime inference credential in agent_key for
            # compatibility. It must be a NAS invoke JWT.
            for token, expires_at in (
                (self.agent_key, self.agent_key_expires_at),
                (self.access_token, self.expires_at),
            ):
                if (
                    isinstance(token, str)
                    and token.strip()
                    and auth_mod._nous_invoke_jwt_is_usable(
                        token, scope=getattr(self, "scope", None), expires_at=expires_at,
                    )
                ):
                    return token.strip()
            return ""
        return str(self.access_token or "")

    @property
    def runtime_base_url(self) -> Optional[str]:
        if self.provider == "nous":
            return self.inference_base_url or self.base_url
        return self.base_url


def _is_manual_source(source: str) -> bool:
    normalized = (source or "").strip().lower()
    return normalized == SOURCE_MANUAL or normalized.startswith(f"{SOURCE_MANUAL}:")


def _exhausted_ttl(
    error_code: Optional[int],
    *,
    sole_credential: bool = False,
    failure_reason: Optional[str] = None,
) -> int:
    """Return cooldown seconds based on the HTTP status that caused exhaustion.

    *sole_credential*: the pool has nothing to rotate to, so transient
    throttles (429 and the catch-all default covering 403/5xx/unknown) are
    capped to a brief cooldown; 401 keeps its own already-short TTL.

    *failure_reason* is the classifier verdict: an OpenRouter ``key limit
    exceeded`` and an xAI spending block both arrive as 403 but are billing,
    and a 60s retry on a spent account just re-fails. Billing keeps the full
    bench regardless of status; 402 is billing by definition.
    Unverified billing (#82154) gets the short cooldown regardless of pool
    size (the credential may be healthy), unless the status is a true 402.
    """
    if error_code == 401:
        return EXHAUSTED_TTL_401_SECONDS
    base = EXHAUSTED_TTL_429_SECONDS if error_code == 429 else EXHAUSTED_TTL_DEFAULT_SECONDS
    if failure_reason == FAILURE_REASON_BILLING_UNVERIFIED and error_code != 402:
        return min(base, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS)
    is_billing = error_code == 402 or failure_reason == FAILURE_REASON_BILLING
    if sole_credential and not is_billing:
        return min(base, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS)
    return base


def _parse_absolute_timestamp(value: Any) -> Optional[float]:
    """Best-effort parse of epoch seconds / epoch ms / ISO-8601 into epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        return numeric / 1000.0 if numeric > 1_000_000_000_000 else numeric
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            numeric = float(raw)
            return numeric / 1000.0 if numeric > 1_000_000_000_000 else numeric
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


_RETRY_DELAY_PATTERNS: Tuple[Tuple[re.Pattern, Callable[[re.Match], float]], ...] = (
    (
        re.compile(r"quotaResetDelay[:\s\"]+(\d+(?:\.\d+)?)(ms|s)", re.IGNORECASE),
        lambda m: float(m.group(1)) / 1000.0 if m.group(2).lower() == "ms" else float(m.group(1)),
    ),
    (
        re.compile(r"retry\s+(?:after\s+)?(\d+(?:\.\d+)?)\s*(?:sec|secs|seconds|s\b)", re.IGNORECASE),
        lambda m: float(m.group(1)),
    ),
    # "Resets in 4hr 5min" format used by OpenCode Go weekly usage limits
    (
        re.compile(r"resets?\s+in\s+(\d+)\s*hr\s+(\d+)\s*min", re.IGNORECASE),
        lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60,
    ),
    (re.compile(r"resets?\s+in\s+(\d+)\s*hr\b", re.IGNORECASE), lambda m: int(m.group(1)) * 3600),
    (re.compile(r"resets?\s+in\s+(\d+)\s*min\b", re.IGNORECASE), lambda m: int(m.group(1)) * 60),
)


def _extract_retry_delay_seconds(message: str) -> Optional[float]:
    if not message:
        return None
    for pattern, to_seconds in _RETRY_DELAY_PATTERNS:
        match = pattern.search(message)
        if match:
            return to_seconds(match)
    return None


def _normalize_error_context(error_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(error_context, dict):
        return {}
    normalized: Dict[str, Any] = {}
    for key in ("reason", "message"):
        value = error_context.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    reset_at = (
        error_context.get("reset_at")
        or error_context.get("resets_at")
        or error_context.get("retry_until")
    )
    parsed_reset_at = _parse_absolute_timestamp(reset_at)
    message = error_context.get("message")
    if parsed_reset_at is None and isinstance(message, str):
        retry_delay_seconds = _extract_retry_delay_seconds(message)
        if retry_delay_seconds is not None:
            parsed_reset_at = time.time() + retry_delay_seconds
    if parsed_reset_at is not None:
        normalized["reset_at"] = parsed_reset_at
    return normalized


def _exhausted_until(entry: PooledCredential, *, sole_credential: bool = False) -> Optional[float]:
    if entry.last_status != STATUS_EXHAUSTED:
        return None
    reset_at = _parse_absolute_timestamp(entry.last_error_reset_at)
    if reset_at is not None:
        return reset_at
    if entry.last_status_at:
        return entry.last_status_at + _exhausted_ttl(
            entry.last_error_code,
            sole_credential=sole_credential,
            failure_reason=entry.failure_reason,
        )
    return None


def _is_sole_credential(self) -> bool:
    """DEAD entries never re-enter rotation, so <=1 non-DEAD entry means nothing to rotate to."""
    return sum(1 for e in self._entries if e.last_status != STATUS_DEAD) <= 1


def _find(self, predicate: Callable[[PooledCredential], bool]) -> Optional[PooledCredential]:
    return next((e for e in self._entries if predicate(e)), None)


def _current_unlocked(self) -> Optional[PooledCredential]:
    if not self._current_id:
        return None
    return self._find(lambda e: e.id == self._current_id)


def current(self) -> Optional[PooledCredential]:
    with self._lock:
        return self._current_unlocked()


def entry_id_for_api_key(self, api_key_hint: Any = None) -> Optional[str]:
    """Stable id for the runtime credential in use.

    Prefer the current selection when it still supplies ``api_key_hint``;
    if the cursor was cleared, fall back to an unambiguous key match.
    """
    with self._lock:
        current = self._current_unlocked()
        if current is not None and (api_key_hint is None or current.runtime_api_key == api_key_hint):
            return current.id
        if api_key_hint is None:
            return None
        matches = [e for e in self._entries if e.runtime_api_key == api_key_hint]
        return matches[0].id if len(matches) == 1 else None


def _replace_entry(self, old: PooledCredential, new: PooledCredential) -> None:
    """Swap an entry in-place by id, preserving sort order.

    Self-locking (RLock) so the deferred refresh path — which runs outside
    the pool lock — cannot tear ``self._entries`` against a concurrent
    select()/rotation.
    """
    with self._lock:
        for idx, entry in enumerate(self._entries):
            if entry.id == old.id:
                self._entries[idx] = new
                return


def _adopt(self, entry: PooledCredential, *, persist: bool = True, **updates: Any) -> PooledCredential:
    """``replace(entry, **updates)``, swap it into the pool, optionally persist."""
    updated = replace(entry, **updates)
    self._replace_entry(entry, updated)
    if persist:
        self._persist()
    return updated


def _is_terminal_auth_failure(
    self,
    status_code: Optional[int],
    normalized_error: Dict[str, Any],
) -> bool:
    """Detect upstream-permanent OAuth failures that won't recover on TTL.

    Only 401s whose reason is a known terminal OAuth state count;
    token_expired (refreshable) and reason-less 401s (possible glitch)
    stay transient, as do 429/402. The one status-independent case is
    ``CREDENTIAL_PERSIST_FAILED_REASON``: no upstream response is involved,
    the rotated pair never became durable and only a re-auth recovers it.
    """
    raw_reason = normalized_error.get("reason")
    reason = raw_reason.strip().lower() if isinstance(raw_reason, str) else ""
    if reason == CREDENTIAL_PERSIST_FAILED_REASON:
        return True
    return status_code == 401 and reason in _TERMINAL_AUTH_REASONS


def _mark_exhausted(
    self,
    entry: PooledCredential,
    status_code: Optional[int],
    error_context: Optional[Dict[str, Any]] = None,
    *,
    persist: bool = True,
    failure_reason: Optional[str] = None,
) -> PooledCredential:
    normalized_error = _normalize_error_context(error_context)
    # Permanent OAuth failures become STATUS_DEAD, not STATUS_EXHAUSTED:
    # otherwise a revoked credential re-enters rotation every hour and
    # fails immediately until the user removes it (#32849).
    terminal = self._is_terminal_auth_failure(status_code, normalized_error)
    # Carry the classifier's verdict so the cooldown is sized by what
    # actually failed (a billing 403 must not get the sole-credential
    # transient cooldown); absent a classification, clear a stale one.
    updated_extra = dict(entry.extra)
    if failure_reason:
        updated_extra["failure_reason"] = failure_reason
    else:
        updated_extra.pop("failure_reason", None)
    return self._adopt(
        entry,
        persist=persist,
        last_status=STATUS_DEAD if terminal else STATUS_EXHAUSTED,
        last_status_at=time.time(),
        last_error_code=status_code,
        last_error_reason=normalized_error.get("reason"),
        last_error_message=normalized_error.get("message"),
        last_error_reset_at=normalized_error.get("reset_at"),
        extra=updated_extra,
    )


def _available_entries(
    self, *, clear_expired: bool = False, refresh: bool = False,
) -> Tuple[List[PooledCredential], List[PooledCredential]]:
    """Return (available, pending_refresh) for entries not in cooldown.

    *clear_expired* resets elapsed cooldowns to STATUS_OK and persists.
    *refresh* refreshes entries needing a token refresh (skipped on
    failure) — except single-use-token providers (openai-codex,
    xai-oauth), which are returned as *pending_refresh* so the caller
    refreshes them outside the lock instead of stalling every pool
    consumer during cross-process flock acquisition + OAuth network I/O.
    """
    now = time.time()
    cleared_any = False
    entries_to_prune: List[str] = []
    available: List[PooledCredential] = []
    pending_refresh: List[PooledCredential] = []
    sole_credential = self._is_sole_credential()
    for entry in self._entries:
        # Borrowed credentials persist as metadata-only references and are
        # hydrated from their live source on load; never lease an
        # unhydrated duplicate as an empty key.
        if entry.auth_type == AUTH_TYPE_API_KEY and not entry.runtime_api_key:
            continue
        synced = self._resync_stale_entry(entry)
        if synced is not entry:
            entry = synced
            cleared_any = True
        if entry.last_status == STATUS_DEAD:
            # Manual DEAD credentials are pruned after a 24h quiet window;
            # singleton-seeded ones stay (audit trail, and the seeder would
            # re-create them anyway). DEAD never re-enters via TTL — only a
            # write-side re-auth sync clears it.
            if _is_manual_source(entry.source):
                dead_at = entry.last_status_at or 0
                if dead_at and now - dead_at > DEAD_MANUAL_PRUNE_TTL_SECONDS:
                    logger.warning(
                        "credential pool: pruning DEAD manual entry %s "
                        "(reason=%s, age=%.1fh) — re-add via `hermes auth add %s`",
                        entry.label or entry.id[:8],
                        entry.last_error_reason or "unknown",
                        (now - dead_at) / 3600.0,
                        self.provider,
                    )
                    entries_to_prune.append(entry.id)  # can't mutate while iterating
                    cleared_any = True
            continue
        if entry.last_status == STATUS_EXHAUSTED:
            exhausted_until = _exhausted_until(entry, sole_credential=sole_credential)
            # Codex quota windows can reopen EARLY; a throttled live probe
            # lifts a stale cooldown (issue #43747).
            if (
                exhausted_until is not None
                and now < exhausted_until
                and not (clear_expired and self._codex_quota_restored_upstream(entry))
            ):
                continue
            if clear_expired:
                entry = self._adopt(entry, persist=False, **_MARK_OK)
                cleared_any = True
        if refresh and self._entry_needs_refresh(entry):
            if self.provider in _TOKENS_SINGLETON_PROVIDERS:
                pending_refresh.append(entry)
                continue
            refreshed = self._refresh_entry(entry, force=False)
            if refreshed is None:
                continue
            entry = refreshed
        if entry.auth_type == AUTH_TYPE_OAUTH and not (entry.access_token or "").strip():
            # A borrowed OAuth row that failed to hydrate (or a sanitized
            # row read straight off disk); leasing it would send an empty
            # bearer. The API-key guard above does not cover it.
            continue
        available.append(entry)
    if entries_to_prune:
        pruned_ids = set(entries_to_prune)
        self._entries = [e for e in self._entries if e.id not in pruned_ids]
    if cleared_any:
        self._persist(removed_ids=entries_to_prune)
    return available, pending_refresh


def _log_no_available_entries(self) -> None:
    """Emit the empty-pool INFO line at most once per throttle window."""
    now = time.monotonic()
    last = self._last_no_entries_log_at
    if last is not None and (now - last) < NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS:
        return
    self._last_no_entries_log_at = now
    logger.info("credential pool: no available entries (all exhausted or empty)")


def _select_unlocked(
    self, *, refresh: bool = True, count: bool = True,
) -> Tuple[Optional[PooledCredential], List[PooledCredential]]:
    """Select the best available entry; returns ``(entry, pending_refresh)``.

    ``count=False`` skips the ``request_count`` bump for selections that are
    not going to serve a request (a forced-refresh target lookup).
    """
    available, pending_refresh = self._available_entries(clear_expired=True, refresh=refresh)
    if not available:
        self._current_id = None
        self._log_no_available_entries()
        return None, pending_refresh

    # The pool recovered; re-arm the throttle so a later re-exhaustion
    # logs immediately.
    self._last_no_entries_log_at = None

    if self._strategy == STRATEGY_RANDOM:
        entry = random.choice(available)
    elif self._strategy == STRATEGY_LEAST_USED and len(available) > 1:
        entry = min(available, key=lambda e: e.request_count)
    else:
        entry = available[0]
    # Count the selection under every strategy. The counter is ``least_used``'s
    # baseline and reaches auth.json on the next persist (exhaustion, rotation,
    # refresh); it used to move only while ``least_used`` was active.
    if count:
        entry = self._adopt(entry, persist=False, request_count=entry.request_count + 1)
    if self._strategy == STRATEGY_ROUND_ROBIN and len(available) > 1:
        rotated = [candidate for candidate in self._entries if candidate.id != entry.id]
        rotated.append(replace(entry, priority=len(self._entries) - 1))
        self._entries = [replace(candidate, priority=idx) for idx, candidate in enumerate(rotated)]
        self._persist()
        entry = self._find(lambda candidate: candidate.id == entry.id) or entry
    self._current_id = entry.id
    return entry, pending_refresh


def _identify_failed_entry(
    self, credential_id: Optional[str], api_key_hint: Optional[str],
) -> Optional[PooledCredential]:
    """Resolve the entry that issued a failed request from its supplied identity."""
    entry = None
    if credential_id:
        entry = self._find(lambda e: e.id == credential_id)
        # #79156: when both identities disagree, trust the key that made
        # the request. A stale ``_credential_pool_entry_id`` (per-turn env
        # refresh rewrote ``api_key`` without rebinding the id) would
        # otherwise quarantine a healthy fallback for days.
        if entry is not None and api_key_hint and entry.runtime_api_key != api_key_hint:
            hint_entry = self._find(lambda e: e.runtime_api_key == api_key_hint)
            if hint_entry is not None:
                logger.info(
                    "credential pool: credential_id %s runtime key "
                    "does not match api_key_hint; attributing failure "
                    "to key-matched entry %s instead (#79156)",
                    (entry.label or entry.id[:8]),
                    (hint_entry.label or hint_entry.id[:8]),
                )
            # Otherwise the id is stale and the request key is not in the
            # pool — drop the id so we do not mark the wrong entry.
            entry = hint_entry
    if entry is None and api_key_hint:
        # Prefer the entry whose key actually failed: on a pool freshly
        # loaded from disk current() is None and _select_unlocked() would
        # return the NEXT key — the wrong one.
        entry = self._find(lambda e: e.runtime_api_key == api_key_hint)
    return entry


def _rotate_unmatched(self) -> Optional[PooledCredential]:
    """Rotate without marking anything when the failed identity matches no entry.

    Falling through to current()/_select_unlocked() would bench an
    innocent healthy key for the full TTL. But this must be BOUNDED
    (#70401): with OAuth-token auth the 401's key hint never matches any
    ``runtime_api_key``, so every retry lands here, nothing is marked, and
    the caller retries the same dead token forever (~6/sec, starving the
    event loop). Cap consecutive no-mark rotations at one lap of the
    available entries, then surface the error; no cooldown is written.
    """
    self._unmatched_rotation_streak += 1
    available_count = len(self._available_entries()[0])
    if self._unmatched_rotation_streak > max(available_count, 1):
        logger.warning(
            "credential pool: failed credential identity matched no "
            "%s entry for %d consecutive rotations (pool size %d) — "
            "surfacing the error instead of rotating again",
            self.provider, self._unmatched_rotation_streak, available_count,
        )
        self._unmatched_rotation_streak = 0
        self._current_id = None
        return None
    logger.info(
        "credential pool: failed credential identity matched no %s "
        "entry; rotating without marking any credential exhausted",
        self.provider,
    )
    self._current_id = None
    next_entry, _pending = self._select_unlocked(refresh=False)
    if next_entry is not None and len(self._available_entries()[0]) == 1:
        # A single-entry pool cannot rotate: returning its only entry would
        # report a recovery without changing the credential, and the
        # caller retries the same 401 indefinitely.
        self._unmatched_rotation_streak = 0
        self._current_id = None
        return None
    return next_entry


def mark_exhausted_and_rotate(
    self,
    *,
    status_code: Optional[int],
    error_context: Optional[Dict[str, Any]] = None,
    api_key_hint: Optional[str] = None,
    credential_id: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> Optional[PooledCredential]:
    with self._lock:
        identity_supplied = bool(credential_id or api_key_hint)
        entry = self._identify_failed_entry(credential_id, api_key_hint)
        if entry is None and identity_supplied:
            return self._rotate_unmatched()
        # A real entry was identified — any prior unmatched streak is stale.
        self._unmatched_rotation_streak = 0
        if entry is None:
            entry = self._current_unlocked() or self._select_unlocked(refresh=False)[0]
        if entry is None:
            return None
        _label = entry.label or entry.id[:8]
        self._mark_exhausted(entry, status_code, error_context, failure_reason=failure_reason)
        # A 402/429/401 is a key-level failure, and the same key can back
        # several entries (an explicit entry plus a ``model_config`` row
        # auto-seeded from ``model.api_key``). Marking only the first
        # leaves siblings OK, ``_select_unlocked()`` keeps handing back
        # the depleted key, and rotation never converges (~2.5 min hang).
        # Mark every entry sharing the failed key.
        failed_runtime_key = entry.runtime_api_key
        if identity_supplied and failed_runtime_key:
            siblings = [
                s for s in self._entries if s.id != entry.id and s.runtime_api_key == failed_runtime_key
            ]
            for sibling in siblings:
                self._mark_exhausted(
                    sibling, status_code, error_context, persist=False, failure_reason=failure_reason,
                )
            if siblings:
                self._persist()
        # Re-read the updated entry to log the correct terminal state.
        updated_entry = self._find(lambda e: e.id == entry.id) or entry
        if updated_entry.last_status == STATUS_DEAD:
            logger.warning(
                "credential pool: marking %s DEAD (status=%s, reason=%s) — "
                "permanently failed, will NOT re-enter rotation until re-auth",
                _label, status_code, updated_entry.last_error_reason or "unknown",
            )
        else:
            logger.info("credential pool: marking %s exhausted (status=%s), rotating", _label, status_code)
        self._current_id = None
        next_entry, _pending = self._select_unlocked(refresh=False)
        if next_entry:
            logger.info("credential pool: rotated to %s", next_entry.label or next_entry.id[:8])
        return next_entry
