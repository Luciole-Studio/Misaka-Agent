"""Pinned auxiliary recovery predicates and parameter ladder; native I/O is host-owned."""
from __future__ import annotations
import contextlib
import logging
import re
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple
from .support import _contains_any, _is_connection_error
logger = logging.getLogger(__name__)


_PAYMENT_KEYWORDS = (
    "credits", "insufficient funds", "can only afford", "billing", "payment required",
    "out of funds", "run out of funds", "balance_depleted", "no usable credits",
    "model_not_supported_on_free_tier", "not available on the free tier",
    "requires a subscription", "upgrade for access", "upgrade for higher limits",
    "reached your session usage limit", "quota exceeded", "quota_exceeded",
    "too many tokens per day", "daily limit", "tokens per day", "daily quota", "resource exhausted",
    "resource_exhausted", "resource-exhausted", "resourceexhausted",
    "weekly usage limit", "weekly limit",
)


def _is_payment_error(exc: Exception) -> bool:
    """Payment/credit/quota exhaustion: HTTP 402, or a billing/quota body on 403/404/429/no-status."""
    status = getattr(exc, "status_code", None)
    return status == 402 or (
        status in {403, 404, 429, None} and _contains_any(str(exc).lower(), _PAYMENT_KEYWORDS)
    )


_RATE_LIMIT_KEYWORDS = (
    "rate limit", "rate_limit", "too many requests", "try again", "retry after", "resets in"
)


_RATE_LIMIT_BILLING_KEYWORDS = (
    "credits", "insufficient funds", "billing", "payment required", "can only afford",
    "out of funds", "run out of funds", "balance_depleted", "no usable credits",
    "model_not_supported_on_free_tier", "not available on the free tier",
)


def _is_rate_limit_error(exc: Exception) -> bool:
    """429 rate limit (not billing/quota, which _is_payment_error owns).

    OpenAI's RateLimitError may omit .status_code — matched by class name. A generic 429 without
    billing keywords counts as a rate limit.
    """
    # (PR #8023 pattern)
    if type(exc).__name__ == "RateLimitError":
        return True
    if getattr(exc, "status_code", None) != 429:
        return False
    err_lower = str(exc).lower()
    return _contains_any(err_lower, _RATE_LIMIT_KEYWORDS) or not _contains_any(err_lower, _RATE_LIMIT_BILLING_KEYWORDS)


def _is_timeout_error(exc: Exception) -> bool:
    """Full-budget request timeout, distinct from a fast connection drop.

    A timeout burns the whole ``timeout`` budget, so a same-provider retry on the compression
    path doubles wall time; fast drops stay on the retry path.
    """
    with contextlib.suppress(ImportError):
        from openai import APITimeoutError
        if isinstance(exc, APITimeoutError):
            return True
    return "Timeout" in type(exc).__name__ or "timed out" in str(exc).lower()


def _is_transient_transport_error(exc: Exception) -> bool:
    """One-off transport blip worth retrying on the SAME provider: connection/stream-close errors plus pure 5xx/408.

    Deliberately narrow: payment/auth/rate-limit errors switch provider, refresh creds, or rotate the pool.
    """
    if _is_connection_error(exc):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and (status == 408 or 500 <= status < 600)


_DEFAULT_TRANSIENT_RETRIES = 2


_TRANSIENT_RETRY_BACKOFF_BASE = 1.0  # Backoff base (seconds); overridable so tests can zero it out.


def _transient_retry_count() -> int:
    """Same-provider retries for a transient blip: ``auxiliary.transient_retries``
    (default 2), clamped to [0, 6]; config-read failures fall back to default."""
    try:
        from ..host.config_bridge import load_auxiliary_config
        val = load_auxiliary_config().get("auxiliary", {}).get("transient_retries")
        return _DEFAULT_TRANSIENT_RETRIES if val is None else max(0, min(int(val), 6))
    except Exception:
        return _DEFAULT_TRANSIENT_RETRIES


def _is_auth_error(exc: Exception) -> bool:
    """Auth failures that should trigger provider-specific refresh."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    err_lower = str(exc).lower()
    if "error code: 401" in err_lower or "authenticationerror" in type(exc).__name__.lower():
        return True
    # xAI returns 403 "unauthenticated:bad-credentials" for expired OAuth tokens — semantically a 401.
    return "bad-credentials" in err_lower and (status == 403 or "unauthenticated" in err_lower)


def _is_unsupported_parameter_error(exc: Exception, param: str) -> bool:
    """Provider 400 for an unsupported request parameter: the parameter name plus a generic
    unsupported/unknown/unrecognized marker, so call sites can retry without the key."""
    param_lower = (param or "").lower()
    if not param_lower:
        return False
    err_lower = str(exc).lower()
    return param_lower in err_lower and _contains_any(err_lower, (
        "unsupported parameter", "unsupported_parameter", "not supported", "does not support",
        "unknown parameter", "unrecognized request argument", "unrecognized parameter", "invalid parameter",
    ))


def _is_structured_output_rejection(exc: Exception) -> bool:
    """Provider 400/422 rejecting the structured-output field, on either wire: OpenAI ``response_format``
    (incl. vLLM's ``guided_grammar``/xgrammar failures) or Anthropic ``output_config.format`` ("Extra inputs
    are not permitted"). Callers tolerate an unconstrained reply, so the reaction is one retry without it."""
    status = getattr(exc, "status_code", None)
    if status is not None and status not in {400, 422}:
        return False
    err_lower = str(exc).lower()
    # vLLM grammar-backend failures name the translated parameter, not ours.
    if _contains_any(err_lower, ("guided_grammar", "xgrammar", "compile_grammar_error")):
        return True
    if "extra inputs are not permitted" in err_lower and (
        "response_format" in err_lower or "output_config" in err_lower
    ):
        return True
    if "response_format" in err_lower and "unavailable" in err_lower:
        return True
    return _is_unsupported_parameter_error(exc, "response_format") or _is_unsupported_parameter_error(exc, "output_config")


def _without_structured_output_format(kwargs: dict) -> Optional[dict]:
    """Copy *kwargs* without ``response_format`` (top-level and ``extra_body``); None when nothing was
    removed, so call sites don't retry an unchanged request."""
    retry_kwargs = dict(kwargs)
    changed = retry_kwargs.pop("response_format", None) is not None
    extra_body = retry_kwargs.get("extra_body")
    if isinstance(extra_body, dict) and "response_format" in extra_body:
        remaining = {k: v for k, v in extra_body.items() if k != "response_format"}
        if remaining:
            retry_kwargs["extra_body"] = remaining
        else:
            retry_kwargs.pop("extra_body", None)
        changed = True
    return retry_kwargs if changed else None


_TIMEOUT_NO_RETRY_TASKS = frozenset({"compression", "vision"})


def _should_skip_same_provider_retry(task: Optional[str], exc: Exception) -> bool:
    """True when a transient error on a critical-path task should go straight to fallback.

    Carve-out: a fast first-token fail (dead stream within the no-progress window, zero output —
    see ``_timeout_message``) is cheap and keeps the same-provider retry; mid-stream stalls and
    hard-ceiling timeouts skip to fallback.
    """
    return task in _TIMEOUT_NO_RETRY_TASKS and _is_timeout_error(exc) and "no-progress timeout" not in str(exc)


def _should_retry_same_provider(task: Optional[str], exc: Exception, tag: str) -> bool:
    """True when ``exc`` is a transient transport blip worth a same-provider retry; critical-path
    tasks skip it on a full-budget timeout (``_should_skip_same_provider_retry``) and go straight
    to fallback."""
    if not _is_transient_transport_error(exc):
        return False
    if _should_skip_same_provider_retry(task, exc):
        logger.info("Auxiliary %s%s: timeout on the critical path; "
                    "skipping same-provider retry and falling back: %s", task, tag, exc)
        return False
    return True


_AFFORDABLE_TOKENS_RE = re.compile(r"can only afford\s+([0-9][0-9,]*)", re.IGNORECASE)


_AFFORDABLE_RETRY_FLOOR_TOKENS = 512


_AFFORDABLE_RETRY_MARGIN_TOKENS = 64


def _affordable_max_tokens_from_error(exc: Exception) -> Optional[int]:
    """Affordable output budget (minus margin) from an OpenRouter credit-limited 402
    ("...but can only afford 7117": credit exists, the cap was too large); ``None``
    when no count is present or the budget is too small to be useful."""
    if not _is_payment_error(exc):
        return None
    match = _AFFORDABLE_TOKENS_RE.search(str(exc))
    if not match:
        return None
    try:
        affordable = int(match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    capped = affordable - _AFFORDABLE_RETRY_MARGIN_TOKENS
    return capped if capped >= _AFFORDABLE_RETRY_FLOOR_TOKENS else None


class _LadderStep(NamedTuple):
    """A provider request the ladder asks its driver to perform. kind: "call" (client, kwargs) |
    "retry_same_provider" (provider, model) | "fallback" (fb_client, fb_model, fb_label)."""
    kind: str
    args: tuple


def _rung(step: "_LadderStep", accept: Callable[[Exception], bool]):
    """One ladder rung: perform ``step``; yields ``(response, None)`` on success,
    ``(None, exc)`` when ``accept(exc)`` lets the next rung handle it, else re-raises."""
    try:
        result = yield step
    except Exception as exc:
        if not accept(exc):
            raise
        return None, exc
    return result, None


def _param_rung_accepts(exc: Exception) -> bool:
    """After a parameter-strip retry: fall through to the max_tokens/payment/auth
    chains with the stripped kwargs; re-raise anything those chains won't handle."""
    return (_is_payment_error(exc) or _is_connection_error(exc) or _is_auth_error(exc)
            or "max_tokens" in str(exc) or "unsupported_parameter" in str(exc))


_LadderRoute = NamedTuple("_LadderRoute", [
    ("client", Any), ("task", Optional[str]), ("tag", str), ("async_mode", bool), ("base_info", str),
    ("resolved_provider", str), ("resolved_model", Optional[str]), ("resolved_base_url", Optional[str]),
    ("resolved_api_key", Optional[str]), ("resolved_api_mode", Optional[str]),
    ("final_model", Optional[str]), ("main_runtime", Optional[Dict[str, Any]]),
    ("route_info", Optional[Dict[str, str]]),
])


def _ladder_parameter_rungs(
    first_err: Exception, route: _LadderRoute, kwargs: Dict[str, Any], max_tokens: Optional[int],
):
    """Rungs 1-3: retry without temperature / structured-output format / max_tokens.
    Returns ``(response, None, kwargs)`` or ``(None, narrowed_err, stripped_kwargs)``."""
    client, task, tag = route.client, route.task, route.tag
    if "temperature" in kwargs and _is_unsupported_parameter_error(first_err, "temperature"):
        retry_kwargs = {k: v for k, v in kwargs.items() if k != "temperature"}
        logger.info("Auxiliary %s%s: provider rejected temperature; retrying once without it",
                    task or "call", tag)
        resp, first_err = yield from _rung(
            _LadderStep("call", (client, retry_kwargs)), _param_rung_accepts)
        if first_err is None:
            return resp, None, retry_kwargs
        kwargs = retry_kwargs
    if _is_structured_output_rejection(first_err):
        retry_kwargs = _without_structured_output_format(kwargs)
        if retry_kwargs is not None:
            logger.info("Auxiliary %s%s: provider rejected the structured-output "
                        "format field; retrying once without it (schema "
                        "enforcement degrades to prompt compliance): %s", task or "call", tag, first_err)
            resp, first_err = yield from _rung(
                _LadderStep("call", (client, retry_kwargs)), _param_rung_accepts)
            if first_err is None:
                return resp, None, retry_kwargs
            kwargs = retry_kwargs
    err_str = str(first_err)
    # ZAI vision models reject max_tokens with code 1210 and a message that never
    # mentions "max_tokens", so detect it explicitly.
    _is_zai_param_error = "1210" in err_str and "bigmodel" in str(getattr(client, "base_url", ""))
    if max_tokens is not None and (
        "max_tokens" in err_str or "unsupported_parameter" in err_str
        or _is_unsupported_parameter_error(first_err, "max_tokens") or _is_zai_param_error
    ):
        kwargs.pop("max_tokens", None)
        kwargs.pop("max_completion_tokens", None)
        resp, first_err = yield from _rung(
            _LadderStep("call", (client, kwargs)),
            lambda exc: _is_payment_error(exc) or _is_connection_error(exc) or _is_rate_limit_error(exc),
        )
        if first_err is None:
            return resp, None, kwargs
    return None, first_err, kwargs


async def _drive_ladder_async(ladder, perform: Callable[[_LadderStep], Any]) -> Any:
    """Async twin of :func:`_drive_ladder` (``perform`` is awaited)."""
    try:
        step = next(ladder)
        while True:
            try:
                result = await perform(step)
            except Exception as exc:
                step = ladder.throw(exc)
            else:
                step = ladder.send(result)
    except StopIteration as stop:
        return stop.value
