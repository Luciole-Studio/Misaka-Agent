"""Pinned auxiliary option, concurrency and stream-budget policy."""
from __future__ import annotations
import logging
import re
import threading
from typing import Any, Callable, Dict, NamedTuple, Optional
from .auxiliary_policy import _get_auxiliary_task_config
from .reasoning import parse_reasoning_effort
logger = logging.getLogger(__name__)
_aux_sem_lock = threading.Lock()
_aux_sync_semaphores = {}
from ..host.profiles import nous_portal_tags


def _get_task_extra_body(task: str) -> Dict[str, Any]:
    """Shallow copy of ``auxiliary.<task>.extra_body`` with ``reasoning_effort`` folded into
    ``reasoning`` unless one is configured (more specific wins). MoA tasks are excluded: their
    reasoning depth is per-slot in the preset."""
    task_config = _get_auxiliary_task_config(task)
    raw = task_config.get("extra_body")
    result = dict(raw) if isinstance(raw, dict) else {}
    if "reasoning" in result:
        return result
    effort = task_config.get("reasoning_effort")
    if effort is None or effort == "":
        return result
    if task in ("moa_reference", "moa_aggregator"):
        logger.warning(
            "auxiliary.%s.reasoning_effort is not supported — MoA reasoning depth is per-slot: set reasoning_effort "
            "on the preset's reference_models entries / aggregator instead (moa.presets.<name>...). Ignoring.",
            task,
        )
        return result
    parsed = parse_reasoning_effort(effort)
    if parsed is not None:
        result["reasoning"] = parsed
    else:
        logger.warning(
            "auxiliary.%s.reasoning_effort %r is not a valid level (none, minimal, low, medium, high, xhigh, max, ultra) — ignoring",
            task, effort,
        )
    return result


def _get_task_max_concurrency(task: Optional[str]) -> Optional[int]:
    """``auxiliary.<task>.max_concurrency`` as a positive int, or None. Vision uses this key for
    its encode/resize CPU pool; its LLM calls stay concurrent."""
    if not task or task == "vision":
        return None
    try:
        value = int(_get_auxiliary_task_config(task).get("max_concurrency"))
    except (TypeError, ValueError):  # missing (None) or malformed
        return None
    return value if value > 0 else None


def _cached_semaphore(store: dict, key: Any, limit: int, factory: Callable[[int], Any]) -> Any:
    """Return the cached semaphore for ``key``, rebuilding it when the limit changed."""
    with _aux_sem_lock:
        entry = store.get(key)
        if entry is None or entry[0] != limit:
            store[key] = entry = (limit, factory(limit))
        return entry[1]


def _acquire_sync_aux_semaphore(task: Optional[str]) -> Optional[threading.BoundedSemaphore]:
    """Get a per-task sync semaphore, rebuilding it after a config change."""
    limit = _get_task_max_concurrency(task)
    return None if limit is None else _cached_semaphore(_aux_sync_semaphores, task, limit, threading.BoundedSemaphore)


_AUX_STREAM_CEILING_FLOOR_SECONDS = 600.0


_AUX_STREAM_CEILING_MULTIPLIER = 4.0


def _aux_stream_total_ceiling(effective_timeout: Optional[float]) -> float:
    """Absolute wall-clock bound for a streamed aux call; generous by design (the idle
    timeout is the real guard — this only stops a one-token-per-idle-window trickle)."""
    try:
        timeout = float(effective_timeout) if effective_timeout is not None else 0.0
    except (TypeError, ValueError):
        timeout = 0.0
    return max(_AUX_STREAM_CEILING_FLOOR_SECONDS, _AUX_STREAM_CEILING_MULTIPLIER * timeout)


def _coerce_positive_timeout(raw: Any) -> Optional[float]:
    """Coerce a config ``timeout`` to a positive float, or None (rejects bools, which are ints)."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def _fallback_chain_entry(task: Optional[str], fb_label: str) -> Optional[Dict[str, Any]]:
    """Resolve the ``fallback_chain`` entry a ``fallback_chain[<i>](<provider>)`` label points at,
    or None when the label is not a configured-chain candidate or the index no longer resolves."""
    if not task or not fb_label:
        return None
    m = re.match(r"fallback_chain\[(\d+)\]", fb_label)
    if not m:
        return None
    try:
        chain = _get_auxiliary_task_config(task).get("fallback_chain")
        entry = chain[int(m.group(1))] if isinstance(chain, list) else None
    except Exception:
        return None
    return entry if isinstance(entry, dict) else None


def _fallback_entry_timeout(task: Optional[str], fb_label: str) -> Optional[float]:
    """Per-entry ``timeout`` for a configured fallback candidate, or None (keep the task-level
    timeout). Inheriting the primary's deadline used to kill healthy-but-slower fallbacks.

    A fallback candidate previously inherited the exact timeout the primary provider was called with. When
    that deadline was tuned for the primary (or the primary simply consumed its whole budget before failing
    over), the fallback aborted on the same clock even when independently healthy — a 163k-token compression
    that needs ~90s on the fallback died at the primary's 30s deadline every turn (#62452).
    """
    entry = _fallback_chain_entry(task, fb_label)
    return _coerce_positive_timeout(entry.get("timeout") if entry else None)


def _fallback_provider_from_label(label: str) -> str:
    """Recover the provider identifier from a fallback display label."""
    match = re.match(r"(?:fallback_chain\[\d+\]|fallback_providers\[\d+\]|main-agent)\(([^)]+)\)$", label or "")
    return match.group(1).strip() if match else str(label or "").strip()


def _dedupe_tool_names(tools: list, provider: str, model: str) -> list:
    """Drop duplicate tool names (Vertex/Azure/Bedrock 400 on them) with a warning."""
    seen: set = set()
    deduped: list = []
    for tool in tools:
        name = (tool.get("function") or {}).get("name", "")
        if name and name in seen:
            logger.warning("_build_call_kwargs: duplicate tool name '%s' removed (provider=%s model=%s)", name, provider, model)
            continue
        if name:
            seen.add(name)
        deduped.append(tool)
    return deduped


class _ProfileProjection(NamedTuple):
    body: Dict[str, Any]
    reasoning_extra: Dict[str, Any]
    top_level: Dict[str, Any]
    handles_reasoning: bool


def _project_provider_profile(
    provider: str, provider_norm: str, model: str, effective_base: str, reasoning_config: Optional[dict],
) -> _ProfileProjection:
    """Provider profile's extra_body / kwargs projection; partial on failure."""
    body: Dict[str, Any] = {}
    reasoning_extra: Dict[str, Any] = {}
    top_level: Dict[str, Any] = {}
    handles_reasoning = False
    try:
        from ..host.profiles import get_provider_profile
        from .provider_profile import ProviderProfile
        profile = get_provider_profile(provider_norm)
        if profile is not None:
            body = profile.build_extra_body(model=model, base_url=effective_base, reasoning_config=reasoning_config) or {}
            reasoning_extra, top_level = profile.build_api_kwargs_extras(
                reasoning_config=reasoning_config, supports_reasoning=reasoning_config is not None,
                model=model, base_url=effective_base,
            )
            reasoning_extra = reasoning_extra or {}
            top_level = top_level or {}
            handles_reasoning = (
                type(profile).build_api_kwargs_extras is not ProviderProfile.build_api_kwargs_extras
                or _contains_profile_reasoning_fields(body)
                or _contains_profile_reasoning_fields(reasoning_extra)
                or _contains_profile_reasoning_fields(top_level)
            )
    except Exception as exc:
        logger.debug("_build_call_kwargs: provider profile projection failed for %s: %s", provider, exc)
    return _ProfileProjection(body, reasoning_extra, top_level, handles_reasoning)


def _merge_aux_extra_body(
    extra_body: Optional[dict], projection: _ProfileProjection, reasoning_config: Optional[dict], provider_norm: str,
) -> Dict[str, Any]:
    """Caller extra_body + profile body/reasoning + generic reasoning fallback + Nous tags."""
    merged_extra = dict(extra_body or {})
    merged_extra.update(projection.body)
    merged_extra.update(projection.reasoning_extra)
    if reasoning_config and isinstance(reasoning_config, dict) and not projection.handles_reasoning:
        if reasoning_config.get("enabled") is False:
            merged_extra["reasoning"] = {"enabled": False}
        else:
            merged_extra["reasoning"] = {"enabled": True, "effort": reasoning_config.get("effort") or "medium"}
    # Portal tags + sticky session_id fallback when the profile didn't supply them; session_id
    # keeps aux calls on the main turn's upstream instance (cache warmth) — tags alone are not
    # enough on /v1/messages.
    if provider_norm in _NOUS_PROVIDER_NAMES:
        if "tags" not in merged_extra:
            merged_extra["tags"] = nous_portal_tags()
        if "session_id" not in merged_extra:
            try:
                from ..host.profiles import get_conversation_context
                sticky_key = get_conversation_context()
            except Exception:
                sticky_key = None
            if sticky_key:
                merged_extra["session_id"] = sticky_key
    return merged_extra


_NOUS_PROVIDER_NAMES = frozenset({"nous", "nous-portal", "nousresearch"})


def _contains_profile_reasoning_fields(value: Any) -> bool:
    """Return whether a profile payload contains a reasoning wire control (recursive)."""
    if not isinstance(value, dict):
        return False
    return any(
        str(key).strip().lower() in _PROFILE_REASONING_KEYS or _contains_profile_reasoning_fields(nested)
        for key, nested in value.items()
    )


_PROFILE_REASONING_KEYS = {
    "reasoning", "reasoning_effort", "thinking", "thinking_config", "thinkingconfig",
    "thinking_budget", "thinkingbudget", "enable_thinking", "think", "verbosity",
}


OMIT_TEMPERATURE: object = object()


def _bare_model(model: Optional[str]) -> str:
    """Lowercased model slug with any ``vendor/`` prefix stripped."""
    return (model or "").strip().lower().rsplit("/", 1)[-1]


def _is_kimi_model(model: Optional[str]) -> bool:
    """True for any Kimi / Moonshot model that manages temperature server-side."""
    bare = _bare_model(model)
    return bare.startswith("kimi-") or bare == "kimi"


def _is_arcee_trinity_thinking(model: Optional[str]) -> bool:
    """True for Arcee Trinity Large Thinking (direct or via OpenRouter)."""
    return _bare_model(model) == "trinity-large-thinking"


def _fixed_temperature_for_model(
    model: Optional[str], base_url: Optional[str] = None
) -> "Optional[float] | object":
    """``OMIT_TEMPERATURE`` (drop the key; Kimi/Moonshot), a fixed ``float``, or ``None``."""
    if _is_kimi_model(model):
        logger.debug("Omitting temperature for Kimi model %r (server-managed)", model)
        return OMIT_TEMPERATURE
    return 0.5 if _is_arcee_trinity_thinking(model) else None


_NO_XHIGH_CLAUDE_SUBSTRINGS = ("claude-opus-4-6", "claude-opus-4.6", "claude-sonnet-4-6", "claude-sonnet-4.6")


_LEGACY_MANUAL_THINKING_CLAUDE_SUBSTRINGS = (
    "claude-3", "claude-opus-4-0", "claude-opus-4.0", "claude-opus-4-1", "claude-opus-4.1",
    "claude-sonnet-4-0", "claude-sonnet-4.0", "claude-opus-4-2025", "claude-sonnet-4-2025",
    "claude-opus-4-5", "claude-opus-4.5", "claude-sonnet-4-5", "claude-sonnet-4.5", "claude-haiku-4-5",
    "claude-haiku-4.5",
)


def _is_claude_model(model: str | None) -> bool:
    return "claude" in (model or "").lower()


def _model_matches(model: str, substrings) -> bool:
    """Case-insensitive substring match of ``model`` against a family list."""
    m = model.lower()
    return any(v in m for v in substrings)


def _forbids_sampling_params(model: str) -> bool:
    """True for models that 400 on any non-default temperature/top_p/top_k (Opus 4.7 and later;
    unknown Claude defaults to forbidding). The 4.6 family and the legacy manual-thinking families
    still accept them. Callers omit the fields entirely — the API rejects anything non-null."""
    return _is_claude_model(model) and not _model_matches(
        model, _NO_XHIGH_CLAUDE_SUBSTRINGS + _LEGACY_MANUAL_THINKING_CLAUDE_SUBSTRINGS
    )
