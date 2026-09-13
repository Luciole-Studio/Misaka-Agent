from __future__ import annotations
import logging
from typing import Any, Dict, List
from .usage_anchor import anchored_context_tokens
from .model_metadata import estimate_request_tokens_rough
logger = logging.getLogger(__name__)


def _str_attr(agent: Any, name: str) -> str:
    """``getattr(agent, name, "") or ""`` — route facts read off partial agents/doubles."""
    return getattr(agent, name, "") or ""


def _preflight_request_tokens(
    agent: Any, messages: List[Dict[str, Any]], system_prompt: str
) -> int:
    """Token estimate for automatic preflight compression: a valid provider usage anchor,
    else the checkpoint-pruned native wire payload, else the generic estimator."""
    anchored = anchored_context_tokens(messages, getattr(agent, "_usage_anchor", None))
    agent._request_pressure_anchored = anchored is not None
    if anchored is not None:
        return anchored
    tools = getattr(agent, "tools", None) or None
    try:
        from ..host.native import estimate_native_responses_preflight_tokens

        native = estimate_native_responses_preflight_tokens(
            agent, messages, system_prompt=system_prompt or "", tools=tools
        )
        if isinstance(native, int) and not isinstance(native, bool) and native >= 0:
            return native
    except Exception:
        logger.debug(
            "native Responses preflight estimate unavailable; "
            "using generic transcript estimate",
            exc_info=True,
        )
    return estimate_request_tokens_rough(
        messages, system_prompt=system_prompt or "", tools=tools,
        charge_stale_thinking=_agent_stale_thinking_on_wire(agent),
    )


def _agent_stale_thinking_on_wire(agent: Any) -> bool:
    """Whether the active route replays stale thinking text; ``True`` (conservative full
    charge) when route facts are unavailable."""
    try:
        from .support import stale_thinking_reaches_wire

        return stale_thinking_reaches_wire(
            *(_str_attr(agent, k) for k in ("api_mode", "provider", "model", "base_url"))
        )
    except Exception:
        return True
