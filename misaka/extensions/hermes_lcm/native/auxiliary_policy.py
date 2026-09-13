"""Pinned auxiliary task route and timeout policy; native registry/config seams only."""
from __future__ import annotations
import contextlib
from typing import Any, Dict, Optional, Tuple
from ..host.config_bridge import _scoped_key_env
from ..host.llm import _expand_direct_api_alias, _unwrap_moa_provider


def _get_auxiliary_task_config(task: str) -> Dict[str, Any]:
    """Config dict for auxiliary.<task>, or {} when unavailable. Plugin-registered tasks get their
    declared defaults layered under user config (user wins); built-in defaults live in DEFAULT_CONFIG."""
    if not task:
        return {}
    try:
        from ..host.config_bridge import load_auxiliary_config as load_config_readonly
        config = load_config_readonly()
    except ImportError:
        return {}
    aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    task_config = aux.get(task, {}) if isinstance(aux, dict) else {}
    if not isinstance(task_config, dict):
        task_config = {}
    try:
        from ..host.config_bridge import get_plugin_auxiliary_tasks
        for _entry in get_plugin_auxiliary_tasks():
            if _entry.get("key") == task:
                _defaults = _entry.get("defaults") or {}
                if isinstance(_defaults, dict):
                    return {**_defaults, **task_config}
                break
    except Exception:
        pass  # plugin discovery failure must not break aux task config reads
    return task_config


def _preserve_provider_with_base_url(prov: Optional[str]) -> bool:
    """True when a first-class provider keeps its identity alongside an explicit base_url."""
    normalized = str(prov or "").strip().lower()
    if normalized in {"", "auto", "custom"} or normalized.startswith("custom:"):
        return False
    try:
        from ..host.llm import get_provider
        return get_provider(normalized) is not None
    except Exception:  # keep provider-backed routes safe when the catalog can't load
        return normalized in {
            "anthropic", "copilot", "copilot-acp", "minimax-oauth", "nous", "openai-codex", "qwen-oauth", "xai-oauth",
        }


def _resolve_task_provider_model(
    task: str = None, provider: str = None, model: str = None, base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Determine (provider, model, base_url, api_key, api_mode) for a call.

    Priority: explicit args > config auxiliary.{task}.* > "auto". A bare base_url means custom,
    but a first-class provider + base_url keeps the provider identity so its auth/transport
    shaping still applies. api_mode is "chat_completions", "codex_responses", or None (auto).
    """
    cfg_provider = cfg_model = cfg_base_url = cfg_api_key = resolved_api_mode = None
    if task:
        task_config = _get_auxiliary_task_config(task)
        cfg_provider = str(task_config.get("provider", "")).strip() or None
        cfg_model = str(task_config.get("model", "")).strip() or None
        cfg_base_url = str(task_config.get("base_url", "")).strip() or None
        cfg_api_key = str(task_config.get("api_key", "")).strip() or None
        if not cfg_api_key:  # key_env → env var when api_key is not set directly
            cfg_key_env = str(task_config.get("key_env") or task_config.get("api_key_env") or "").strip()
            if cfg_key_env:
                cfg_api_key = _scoped_key_env(cfg_key_env) or None
        resolved_api_mode = str(task_config.get("api_mode", "")).strip() or None
    # 'auto' is a sentinel ("inherit / auto-detect"), not a model id — leaking it to the wire
    # yields a 200 with an error-text body that consumers accept as output. The explicit `model`
    # kwarg needs the same normalization: MoA slots forward preset `model:` fields through it.
    if model and model.lower() == "auto":
        model = None
    if cfg_model and cfg_model.lower() == "auto":
        cfg_model = None
    resolved_model = model or cfg_model
    # Any moa:// facade endpoint belongs to the facade, not the aggregator's real provider —
    # drop it (mirrors _resolve_auto_route()).
    if provider and str(provider).strip().lower() == "moa":
        provider, resolved_model = _unwrap_moa_provider(provider, resolved_model)
        if provider and provider.lower() != "moa":
            base_url = None
            api_key = None
    elif cfg_provider and str(cfg_provider).strip().lower() == "moa":
        cfg_provider, cfg_model = _unwrap_moa_provider(cfg_provider, resolved_model)
        if cfg_provider and cfg_provider.lower() != "moa":
            resolved_model = cfg_model
            cfg_base_url = None
            cfg_api_key = None
    if provider:
        provider, base_url = _expand_direct_api_alias(provider, base_url)
    if cfg_provider:
        cfg_provider, cfg_base_url = _expand_direct_api_alias(cfg_provider, cfg_base_url)
    # An explicit provider without base_url adopts the task's configured endpoint (same or
    # unnamed provider) so the early return below carries it. Explicit "auto" is excluded — it
    # must keep flowing through auto-resolution.
    # See #58515.
    if provider and provider != "auto" and not base_url and cfg_base_url and cfg_provider in (None, provider):
        base_url = cfg_base_url
        if not api_key:
            api_key = cfg_api_key
    if base_url:
        kept = provider if _preserve_provider_with_base_url(provider) else "custom"
        return kept, resolved_model, base_url, api_key, resolved_api_mode
    if provider:
        return provider, resolved_model, base_url, api_key, resolved_api_mode
    if cfg_base_url and cfg_api_key:
        return "custom", resolved_model, cfg_base_url, cfg_api_key, resolved_api_mode
    if cfg_base_url and cfg_provider and cfg_provider != "auto":
        # base_url without api_key: keep the provider so it can resolve credentials from env
        # vars instead of locking into "custom".
        return cfg_provider, resolved_model, cfg_base_url, None, resolved_api_mode
    if cfg_provider and cfg_provider != "auto":
        return cfg_provider, resolved_model, cfg_base_url, cfg_api_key, resolved_api_mode
    return "auto", resolved_model, None, None, resolved_api_mode


_DEFAULT_AUX_TIMEOUT = 30.0


_COMPRESSION_TIMEOUT_FLOOR_SECONDS = 300.0


def _get_task_timeout(task: str, default: float = _DEFAULT_AUX_TIMEOUT) -> float:
    """``auxiliary.<task>.timeout`` from config, else *default*."""
    if not task:
        return default
    raw = _get_auxiliary_task_config(task).get("timeout")
    if raw is not None:
        with contextlib.suppress(ValueError, TypeError):
            return float(raw)
    return default


def _effective_aux_timeout(task: str, timeout: Optional[float]) -> float:
    """Explicit ``timeout`` wins, else config; compression gets a floor so a reasoning model
    summarising a large context isn't cut off."""
    if timeout is not None:
        return timeout
    effective = _get_task_timeout(task)
    return max(effective, _COMPRESSION_TIMEOUT_FLOOR_SECONDS) if task == "compression" else effective


_API_KEY_PROVIDER_AUX_MODELS_FALLBACK: Dict[str, str] = {
    "gemini": "gemini-3.6-flash", "zai": "glm-4.5-flash", "kimi-coding": "kimi-k2-turbo-preview",
    "stepfun": "step-3.5-flash", "kimi-coding-cn": "kimi-k2-turbo-preview",
    "gmi": "google/gemini-3.1-flash-lite-preview", "anthropic": "claude-haiku-4-5-20251001",
    "ai-gateway": "google/gemini-3-flash", "opencode-zen": "gemini-3-flash", "opencode-go": "glm-5",
    "kilocode": "google/gemini-3.6-flash", "ollama-cloud": "nemotron-3-nano:30b",
    "tencent-tokenhub": "hy4-preview", "tencent-tokenplan": "hy4-preview",
    # No "deepinfra": its aux model lives on the ProviderProfile (read first).
}
