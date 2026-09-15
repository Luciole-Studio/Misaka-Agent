"""Pinned cooldown and throttled diagnostics; state belongs to the native auth owner."""
from __future__ import annotations
import logging
import time
from typing import Any, Optional
from .route_identity import normalize_route_base_url
from ..host.routing import _health_cache, _custom_health_base_url
logger = logging.getLogger(__name__)


_AUX_UNHEALTHY_TTL_SECONDS = 600  # 10 minutes


_AUX_UNHEALTHY_LABEL_ALIASES = {
    "openrouter": "openrouter", "nous": "nous", "custom": "local/custom",
    "local/custom": "local/custom", "openai-codex": "openai-codex", "codex": "openai-codex",
}


def _normalize_chain_label(provider: str) -> str:
    """resolved_provider → chain label; unknown API-key providers fall back to the lowercased input."""
    if not provider:
        return ""
    p = str(provider).strip().lower()
    return _AUX_UNHEALTHY_LABEL_ALIASES.get(p, p)


def _mark_provider_unhealthy(
    provider: str, ttl: Optional[float] = None, *, base_url: Optional[str] = None,
) -> None:
    """Hide one provider endpoint until the TTL expires after a confirmed payment error."""
    label = _normalize_chain_label(provider)
    if not label:
        return
    key = _unhealthy_cache_key(label, base_url)
    ttl = _AUX_UNHEALTHY_TTL_SECONDS if ttl is None else ttl
    expires_at = time.time() + ttl
    _health_cache('until')[key] = expires_at
    logger.warning(
        "Auxiliary: marking %s unhealthy for %ds (payment / credit error). "
        "Subsequent auxiliary calls will skip it until %s.",
        label, int(ttl), time.strftime("%H:%M:%S", time.localtime(expires_at)),
    )


def _is_provider_unhealthy(label: str, base_url: Optional[str] = None) -> bool:
    """True iff this provider endpoint is unhealthy and unexpired; lazily evicts expired entries."""
    if not label:
        return False
    key = _unhealthy_cache_key(label, base_url)
    expires_at = _health_cache('until').get(key)
    if expires_at is None:
        return False
    if time.time() >= expires_at:
        _health_cache('until').pop(key, None)
        _health_cache('logged_at').pop(key, None)
        return False
    return True


def _log_skip_unhealthy(
    label: str, task: Optional[str] = None, *, base_url: Optional[str] = None,
) -> None:
    """Log a skipped unhealthy provider at most once per minute per label."""
    now = time.time()
    key = _unhealthy_cache_key(label, base_url)
    if now - _health_cache('logged_at').get(key, 0.0) >= 60:
        _health_cache('logged_at')[key] = now
        expires_at = _health_cache('until').get(key, now)
        logger.info(
            "Auxiliary %s: skipping %s (recently returned payment error, retry in %ds)",
            task or "call", label, max(0, int(expires_at - now)),
        )


def _reset_aux_unhealthy_cache() -> None:
    """Clear the unhealthy cache (tests / explicit user reset)."""
    _health_cache('until').clear()
    _health_cache('logged_at').clear()


def _unhealthy_cache_key(provider: str, base_url: Optional[str] = None) -> Any:
    """Provider-wide key, or endpoint-specific key for an explicit custom endpoint."""
    label = _normalize_chain_label(provider)
    endpoint = normalize_route_base_url(_custom_health_base_url(provider, base_url))
    if endpoint:
        return "custom-endpoint", endpoint
    return label
