"""Pinned fallback selection and failure scopes on native registry boundaries."""
from __future__ import annotations
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from .auxiliary_recovery import (_drive_ladder_async, _LadderStep, _is_auth_error, _is_payment_error, _is_rate_limit_error)
from .support import _contains_any, _is_connection_error
from .auxiliary_policy import _get_auxiliary_task_config
from .auxiliary_options import _fallback_provider_from_label
from .model_metadata import MINIMUM_CONTEXT_LENGTH
from ..host.native import get_model_context_length
from ..host.routing import (_resolve_fallback_entry, _custom_health_base_url,
    _is_provider_unhealthy, _mark_provider_unhealthy, _log_skip_unhealthy,
    _read_main_provider, _read_main_model, _get_provider_chain, _normalize_chain_label,
    _recoverable_pool_provider, _record_route_info, resolve_provider_client)
logger = logging.getLogger(__name__)
from ..host.routing import _fallback_entry_api_key
from .auxiliary_recovery import _LadderRoute


def _is_model_not_found_error(exc: Exception) -> bool:
    """"Requested model doesn't exist" (404 / invalid model) — typically a long-lived process pinned a
    since-dropped model. Excludes billing keywords, which :func:`_is_payment_error` owns."""
    status = getattr(exc, "status_code", None)
    err_lower = str(exc).lower()
    if _contains_any(err_lower, (
        "credits", "insufficient funds", "billing", "out of funds", "balance_depleted",
        "no usable credits", "free tier", "free-tier", "not available on the free tier",
    )):
        return False
    if status not in {404, 400, None}:
        return False
    return _contains_any(err_lower, (
        "model does not exist", "does not exist in our configuration", "openrouter catalog",
        "is not a valid model", "no such model", "model not found",
        "the model `",            # OpenAI-style: "The model `X` does not exist"
        "model_not_found", "unknown model",
    ))


def _is_model_incompatible_error(exc: Exception) -> bool:
    """"This route cannot serve this model" 400 (capability mismatch, e.g. a Codex/ChatGPT-account
    fallback asked to run a non-OpenAI model). Auth/payment predicates don't fire, so this keeps the
    chain going instead of aborting. Excludes billing 400s and not-found 400s."""
    status = getattr(exc, "status_code", None)
    if status not in {400, None}:
        return False
    err_lower = str(exc).lower()
    if _is_model_not_found_error(exc):
        return False
    # Billing keywords checked directly: _is_payment_error is status-gated and misses 400-coded billing bodies.
    if _contains_any(err_lower, (
        "credits", "insufficient funds", "billing", "out of funds", "balance_depleted",
        "no usable credits", "payment required", "free tier", "free-tier",
        "not available on the free tier", "model_not_supported_on_free_tier", "quota",
    )):
        return False
    return _contains_any(err_lower, (
        "is not supported when using",   # codex/ChatGPT-account model gating
        "model is not supported", "not supported with this", "not supported for this account",
        "model_not_supported", "does not support this model", "unsupported model",
    ))


def _is_invalid_aux_response_error(exc: Exception) -> bool:
    """HTTP-200 empty/malformed ChatCompletions — a capability failure routed like model incompatibility."""
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc).lower()
    return "auxiliary " in msg and "llm returned invalid response" in msg and "choices[0].message" in msg


_RERAISE_ORIGINAL = object()


_FALLBACK_REASONS: Tuple[Tuple[Callable[[Exception], bool], str], ...] = (
    (_is_auth_error, "auth error"), (_is_payment_error, "payment error"),
    (_is_rate_limit_error, "rate limit"), (_is_model_incompatible_error, "model incompatible with route"),
    (_is_invalid_aux_response_error, "invalid provider response"), (_is_connection_error, "connection error"),
)


def _credential_rung_accepts(exc: Exception) -> bool:
    return _is_auth_error(exc) or _is_payment_error(exc) or _is_rate_limit_error(exc)


def _failed_backend_skip(
    failed_provider: str, failed_model: Optional[str], *, failed_base_url: str = "",
    failure_scope: Any = None,
) -> Callable[..., bool]:
    """Predicate ``skip(provider, model, base_url="")`` → True when a candidate must be skipped for the failed
    route. Scope: ``failed_model`` → model-scoped (only that deployment; timeout/connection/rate-limit);
    None → credential-wide (whole provider; auth/payment)."""
    from .backend_identity import BackendIdentity, FailureScope, should_skip_candidate
    skip_model = (failed_model or "").strip().lower() or None
    failed_ident = BackendIdentity.build(
        provider=(_read_main_provider() if failed_provider in {"auto", "", None} else failed_provider), model=skip_model, base_url=failed_base_url)
    failure_scope = failure_scope or (FailureScope.MODEL if skip_model else FailureScope.CREDENTIAL)

    def _skip(provider: str, model: Optional[str], base_url: str = "") -> bool:
        return should_skip_candidate(
            BackendIdentity.build(provider=provider, model=model, base_url=base_url), failed_ident, failure_scope,
        )
    return _skip


def _task_minimum_context_length(task: Optional[str]) -> Optional[int]:
    """Minimum context length for an auxiliary task; None = no floor (only ``compression`` has one)."""
    return MINIMUM_CONTEXT_LENGTH if task == "compression" else None


def _candidate_context_window(provider: str, model: str, base_url: str = "", api_key: str = "") -> Optional[int]:
    """Best-effort context window for a fallback candidate; ``None`` = unknown (never raises; callers pass it through)."""
    if not model:
        return None
    try:
        ctx = get_model_context_length(model, base_url=base_url, api_key=api_key, provider=provider)
    except Exception as exc:
        logger.debug("Auxiliary fallback: could not resolve context window for %s/%s: %s", provider, model, exc)
        return None
    return ctx if isinstance(ctx, int) and ctx > 0 else None


def _context_too_small(
    entry: Dict[str, Any], provider: str, model: str, min_ctx: Optional[int], *,
    task: Optional[str], label: str, name_model: bool = False,
) -> Optional[str]:
    """Screen one fallback candidate by context window; returns the ``tried`` note when it is too small."""
    if min_ctx is None:
        return None
    fb_ctx = _candidate_context_window(
        provider, model, base_url=str(entry.get("base_url") or ""), api_key=_fallback_entry_api_key(entry) or "")
    if fb_ctx is None or fb_ctx >= min_ctx:
        return None
    if name_model:
        logger.info("Auxiliary %s: skipping %s (%s context=%d < min=%d), continuing chain",
                    task, label, model, fb_ctx, min_ctx)
    else:
        logger.info("Auxiliary %s: skipping %s (context=%d < min=%d), continuing chain",
                    task or "call", label, fb_ctx, min_ctx)
    return f"{label} (context too small: {fb_ctx}<{min_ctx})"


def _try_configured_fallback_chain(
    task: str, failed_provider: str, reason: str = "error", failed_model: Optional[str] = None, *,
    failed_base_url: str = "", failure_scope: Any = None,
) -> Tuple[Optional[Any], Optional[str], str]:
    """Try auxiliary.<task>.fallback_chain entries in order (each needs ``provider``; model/base_url/api_key optional).
    ``failed_model`` scoping per ``_failed_backend_skip`` (sibling models on the same provider still
    run after a model-scoped failure). Returns (client, model, provider_label) or (None, None, "")."""
    if not task:
        return None, None, ""
    chain = _get_auxiliary_task_config(task).get("fallback_chain")
    if not chain or not isinstance(chain, list):
        return None, None, ""
    skip = _failed_backend_skip(
        failed_provider, failed_model, failed_base_url=failed_base_url, failure_scope=failure_scope)
    tried = []
    min_ctx = _task_minimum_context_length(task)
    for i, entry in enumerate(chain):
        if not isinstance(entry, dict):
            continue
        fb_provider = str(entry.get("provider", "")).strip()
        if not fb_provider:
            continue
        fb_model_raw = str(entry.get("model", "")).strip()
        fb_base_url = _custom_health_base_url(fb_provider, entry.get("base_url"))
        if skip(fb_provider, fb_model_raw, fb_base_url):
            continue
        if _is_provider_unhealthy(fb_provider, fb_base_url):
            _log_skip_unhealthy(fb_provider, task, base_url=fb_base_url)
            tried.append(f"fallback_chain[{i}]({fb_provider}) (unhealthy)")
            continue
        fb_model = fb_model_raw or None
        label = f"fallback_chain[{i}]({fb_provider})"
        try:
            fb_client, resolved_model = _resolve_fallback_entry(entry)
        except Exception:
            fb_client, resolved_model = None, None
        if fb_client is not None:
            too_small = _context_too_small(
                entry, fb_provider, resolved_model, min_ctx, task=task, label=label, name_model=True,
            ) if resolved_model else None
            if too_small:
                tried.append(too_small)
                continue
            logger.info("Auxiliary %s: %s on %s — configured fallback to %s (%s)",
                        task, reason, failed_provider, label, resolved_model or fb_model or "default")
            return fb_client, resolved_model or fb_model, label
        tried.append(label)
    if tried:
        logger.debug("Auxiliary %s: configured fallback_chain exhausted (tried: %s)", task, ", ".join(tried))
    return None, None, ""


def _try_main_fallback_chain(
    task: Optional[str], failed_provider: str = "", reason: str = "error", *,
    failed_model: Optional[str] = None, failed_base_url: str = "", failure_scope: Any = None,
) -> Tuple[Optional[Any], Optional[str], str]:
    """Top-level main-agent fallback chain for a ``provider: auto`` auxiliary call: auto tasks honour the
    user's main fallback policy before the built-in discovery chain; read via ``get_fallback_chain`` so
    ``fallback_providers`` and legacy ``fallback_model`` keep the main agent's order."""
    try:
        from ..host.config_bridge import load_auxiliary_config as load_config_readonly
        from ..host.routing import get_fallback_chain
        chain = get_fallback_chain(load_config_readonly())
    except Exception as exc:
        logger.debug("Auxiliary %s: could not load main fallback chain: %s", task or "call", exc)
        return None, None, ""
    if not chain:
        return None, None, ""
    skip = _failed_backend_skip(
        failed_provider, failed_model, failed_base_url=failed_base_url, failure_scope=failure_scope)
    tried: List[str] = []
    min_ctx = _task_minimum_context_length(task)
    for i, entry in enumerate(chain):
        if not isinstance(entry, dict):
            continue
        fb_provider = str(entry.get("provider") or "").strip()
        fb_model = str(entry.get("model") or "").strip()
        if not fb_provider or not fb_model:
            continue
        fb_norm = fb_provider.lower()
        label = f"fallback_providers[{i}]({fb_provider})"
        fb_base_url = _custom_health_base_url(fb_provider, entry.get("base_url"))
        if fb_norm == "auto" or skip(fb_provider, fb_model, fb_base_url):
            tried.append(f"{label} (skipped)")
            continue
        if _is_provider_unhealthy(fb_norm, fb_base_url):
            _log_skip_unhealthy(fb_norm, task, base_url=fb_base_url)
            tried.append(f"{label} (unhealthy)")
            continue
        try:
            fb_client, resolved_model = _resolve_fallback_entry(entry)
        except Exception as exc:
            logger.debug("Auxiliary %s: main fallback %s failed to resolve: %s", task or "call", label, exc)
            fb_client, resolved_model = None, None
        if fb_client is not None:
            too_small = _context_too_small(
                entry, fb_provider, resolved_model or fb_model, min_ctx, task=task, label=label,
            )
            if too_small:
                tried.append(too_small)
                continue
            logger.info("Auxiliary %s: %s on %s — main fallback chain to %s (%s)",
                        task or "call", reason, failed_provider or "auto", label, resolved_model or fb_model)
            return fb_client, resolved_model or fb_model, fb_provider
        tried.append(label)
    if tried:
        logger.debug("Auxiliary %s: main fallback chain exhausted (tried: %s)", task or "call", ", ".join(tried))
    return None, None, ""


def _try_payment_fallback(
    failed_provider: str, task: str = None, reason: str = "payment error", *,
    failed_base_url: str = "", failure_scope: Any = None,
) -> Tuple[Optional[Any], Optional[str], str]:
    """Try the auto-detection chain after a payment/credit or connection error, skipping the failed
    provider (and the main-provider path when it maps to the same backend). Returns (client, model, label) or (None, None, "")."""
    skip = failed_provider.lower().strip()
    main_provider = _read_main_provider()
    skip_labels = {skip}
    if main_provider and main_provider.lower() in skip:
        skip_labels.add(main_provider.lower())
    skip_chain_labels = {_normalize_chain_label(s) for s in skip_labels}
    skip_backend = _failed_backend_skip(
        failed_provider, None, failed_base_url=failed_base_url, failure_scope=failure_scope)
    tried = []
    for label, try_fn in _get_provider_chain():
        candidate_base_url = _custom_health_base_url(label)
        if (not failed_base_url and label in skip_chain_labels) or skip_backend(
                label, None, candidate_base_url):
            continue
        if _is_provider_unhealthy(label, candidate_base_url):
            _log_skip_unhealthy(label, task, base_url=candidate_base_url)
            tried.append(f"{label} (unhealthy)")
            continue
        client, model = try_fn()
        if client is not None:
            logger.info("Auxiliary %s: %s on %s — falling back to %s (%s)",
                        task or "call", reason, failed_provider, label, model or "default")
            return client, model, label
        tried.append(label)
    logger.warning("Auxiliary %s: %s on %s and no fallback available (tried: %s)",
                   task or "call", reason, failed_provider, ", ".join(tried))
    return None, None, ""


def _try_main_agent_model_fallback(
    failed_provider: str, task: str = None, reason: str = "error",
    failed_model: Optional[str] = None, failed_base_url: str = "", failure_scope: Any = None,
) -> Tuple[Optional[Any], Optional[str], str]:
    """Last-resort fallback to the main agent provider + model after the configured chain is exhausted.
    ``failed_model`` scoping per ``_failed_backend_skip``; same-URL custom endpoints serve many models,
    so a hung aux model says nothing about the main model's health. Returns (client, model, label) or (None, None, "")."""
    main_provider = (_read_main_provider() or "").strip()
    main_model = (_read_main_model() or "").strip()
    if main_provider.lower() == "moa":
        # MoA virtual provider: fall back to the preset's aggregator (the acting model).
        _agg_provider, _agg_model = ("", "")
        if not _agg_provider or not _agg_model:
            return None, None, ""
        main_provider, main_model = _agg_provider, _agg_model
    if not main_provider or not main_model or main_provider.lower() in {"auto", ""}:
        return None, None, ""
    main_base_url = _custom_health_base_url(main_provider)
    if _failed_backend_skip(
            failed_provider, failed_model, failed_base_url=failed_base_url,
            failure_scope=failure_scope)(main_provider, main_model, main_base_url):
        return None, None, ""
    if _is_provider_unhealthy(main_provider, main_base_url):
        _log_skip_unhealthy(main_provider, task, base_url=main_base_url)
        return None, None, ""
    try:
        client, resolved_model = resolve_provider_client(provider=main_provider, model=main_model)
    except Exception:
        client, resolved_model = None, None
    if client is None:
        return None, None, ""
    label = f"main-agent({main_provider})"
    logger.info("Auxiliary %s: %s on %s — falling back to main agent model %s (%s)",
                task or "call", reason, failed_provider, label, resolved_model or main_model)
    return client, resolved_model or main_model, label


def _ladder_provider_fallback(first_err: Exception, route: _LadderRoute):
    """Last rung: other providers (per-task chain; then auto: main fallback chain + discovery
    chain, explicit: main-agent-model net). Returns the response or None.
    Capacity errors (payment/quota, connection, exhausted 429, model incompatible, malformed
    response) bypass the explicit-provider gate — the provider cannot serve this request
    regardless of user intent. Auth errors only fall back in auto mode."""
    task, tag, resolved_provider = route.task, route.tag, route.resolved_provider
    # Respect explicit provider choice for transient errors (auth, request validation, etc.) but allow
    # fallback when the provider clearly cannot serve the request due to capacity: payment/quota exhaustion
    # and connection failures are capacity problems, not request constraints. See #26803: daily token quota
    # (429 + "too many tokens per day") must fall back just like a 402 credit error.
    # Rate limits are included: after retries are exhausted, a 429 means the provider is at capacity. See
    # #52228. See #26803: daily token quota must fall back like a 402 credit error.
    is_auto = resolved_provider in {"auto", "", None}
    reason = next((label for predicate, label in _FALLBACK_REASONS if predicate(first_err)), None)
    is_capacity_error = any(
        predicate(first_err) for predicate, label in _FALLBACK_REASONS if label != "auth error")
    if reason is None or not (is_auto or is_capacity_error):
        return None
    if reason == "payment error":
        # Mark the concrete backend (not the "auto" label) unhealthy so later aux calls skip
        # it instead of paying another doomed RTT.
        _mark_provider_unhealthy(
            _recoverable_pool_provider(resolved_provider, route.client, main_runtime=route.main_runtime)
            or resolved_provider, base_url=route.base_info)
    logger.info("Auxiliary %s%s: %s on %s (%s), trying fallback",
                task or "call", tag, reason, resolved_provider, first_err)
    # Skip only the failed model for model-specific failures; 401/402 are provider-wide, so
    # auth keeps skipping the credential surface, while billing is scoped to the endpoint:
    # separate custom URLs can carry separate credentials (or no billing relationship at all).
    _chain_failed_model = None if reason in ("auth error", "payment error") else route.final_model
    from .backend_identity import FailureScope
    _chain_failure_scope = (
        FailureScope.ENDPOINT
        if reason == "payment error" and _custom_health_base_url(resolved_provider, route.base_info)
        else None
    )
    fb_client, fb_model, fb_label = _try_configured_fallback_chain(
        task, resolved_provider or "auto", reason=reason, failed_model=_chain_failed_model,
        failed_base_url=route.base_info, failure_scope=_chain_failure_scope)
    if fb_client is None and is_auto:
        fb_client, fb_model, fb_label = _try_main_fallback_chain(
            task, resolved_provider or "auto", reason=reason, failed_model=_chain_failed_model,
            failed_base_url=route.base_info, failure_scope=_chain_failure_scope)
        if fb_client is None:
            fb_client, fb_model, fb_label = _try_payment_fallback(
                resolved_provider, task, reason=reason, failed_base_url=route.base_info,
                failure_scope=_chain_failure_scope)
    elif fb_client is None:
        fb_client, fb_model, fb_label = _try_main_agent_model_fallback(
            resolved_provider, task, reason=reason, failed_model=_chain_failed_model,
            failed_base_url=route.base_info, failure_scope=_chain_failure_scope)
    if fb_client is not None:
        # Second pass: the candidate credential was stale and quarantined — walk the discovery
        # chain once more (unhealthy entries are skipped).
        for _pass in range(2):
            _record_route_info(route.route_info, _fallback_provider_from_label(fb_label), fb_model)
            fb_resp = yield _LadderStep("fallback", (fb_client, fb_model, fb_label))
            if fb_resp is not None:
                return fb_resp
            if _pass == 0:
                fb_client, fb_model, fb_label = _try_payment_fallback(
                    resolved_provider, task, reason="stale fallback credential",
                    failed_base_url=route.base_info, failure_scope=_chain_failure_scope)
                if fb_client is None:
                    break
    # All fallback layers exhausted — one user-visible warning, then re-raise.
    logger.warning("Auxiliary %s%s: %s on %s and all fallbacks exhausted "
                   # All fallback layers exhausted — emit a single user-visible warning so the operator
                   # knows aux task is about to fail. (#26882) The error itself is re-raised below.
                   # (#26882)
                   "(fallback_chain + main agent model). Raising original error.",
                   task or "call", tag, reason, resolved_provider)
    return None
