"""Pinned Nous stale-model recovery policy."""
import logging
from typing import Optional
from .auxiliary_recovery import _is_payment_error
from .support import _contains_any
logger = logging.getLogger(__name__)

_NOUS_MODEL = "google/gemini-3.6-flash"


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


def _refresh_nous_recommended_model(*, vision: bool, stale_model: Optional[str]) -> Optional[str]:
    """Fresh Portal recommended model after a stale-model 404 (long-lived processes pin dropped models).

    Returns the fresh recommendation, else ``_NOUS_MODEL``, whichever differs from ``stale_model``; None if neither.
    """
    stale = (stale_model or "").strip().lower()
    fresh: Optional[str] = None
    try:
        from .recommended_models import get_nous_recommended_aux_model
        fresh = get_nous_recommended_aux_model(vision=vision, force_refresh=True)
    except Exception as exc:
        logger.debug("Nous recommended-model refresh failed (%s); using default %s", exc, _NOUS_MODEL)
    if fresh and fresh.strip().lower() != stale:
        return fresh
    return _NOUS_MODEL if _NOUS_MODEL.strip().lower() != stale else None


def nous_api_mode(model: str = "") -> str:
    """Wire protocol for a Nous Portal model. Portal serves its ``anthropic/*`` catalog on a native
    Messages route alongside OpenAI-compatible chat/completions for everything else.

    ``anthropic/*`` rides chat/completions by default for now (``nous.anthropic_wire``). Measured
    2026-09-06, 20 concurrent sessions x 6 tool calls on Fable 5.1, same account and hour: the
    native route re-wrote the previous turn on 14-20% of consecutive calls (4 runs; the cache read
    stopped at the prior breakpoint with byte-identical prefixes), chat/completions 0 of 320 pairs.
    That is 15-20% of a fan-out's cache-write bill. The cause is inside the portal's native route
    (NousResearch/api#227 carries the diagnostics); flip the default back to ``native`` when it is
    fixed. Cost of ``chat``: prior-turn thinking travels as OpenAI-style reasoning fields instead of
    signed native blocks, and cache_control scopes are translated by the portal's adapter.
    Empty/unknown model defaults to ``chat_completions`` (the historical Nous transport)."""
    if str(model or "").strip().lower().startswith("anthropic/"):
        # ``auto`` starts on chat too: it is safe on every upstream, and ``agent/nous_wire.py``
        # promotes the session to native from the first response when the upstream allows it.
        return "anthropic_messages" if _nous_anthropic_wire() == "native" else "chat_completions"
    return "chat_completions"


def _nous_anthropic_wire() -> str:
    """``nous.anthropic_wire``: ``"chat"`` (default), ``"native"``, or ``"auto"`` (chat, then per-session
    promotion decided from the first response; see ``agent/nous_wire.py``). Anything else reads as ``chat``."""
    try:
        from ..host.config_bridge import load_auxiliary_config as load_config_readonly
        value = str(((load_config_readonly().get("nous") or {}).get("anthropic_wire")) or "chat").strip().lower()
    except Exception:
        return "chat"
    return value if value in ("native", "auto") else "chat"
