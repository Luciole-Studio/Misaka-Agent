from typing import Any


_HIGH_EFFORTS = {"high", "xhigh", "max", "ultra"}


def _build_gemini_thinking_config(model: str, reasoning_config: dict | None) -> dict | None:
    """Translate Hermes/OpenRouter-style reasoning config to Gemini thinkingConfig."""
    if not isinstance(reasoning_config, dict):
        return None
    normalized_model = (model or "").strip().lower().removeprefix("google/")
    # Gemini-only; Gemma/PaLM on the same provider 400 on the field even as ``{"includeThoughts": False}``.
    # ``thinking_config`` is a Gemini-only request parameter. The same ``gemini`` provider also serves Gemma
    # (and historically PaLM/Bard); those reject the field with HTTP 400 "Unknown name 'thinking_config':
    # Cannot find field" — including the polite ``{"includeThoughts": False}`` form. Omit the field entirely
    # on non-Gemini models. (#17426)
    if not normalized_model.startswith("gemini"):
        return None
    effort = str(reasoning_config.get("effort", "medium") or "medium").strip().lower()
    if reasoning_config.get("enabled") is False or effort == "none":
        return {"includeThoughts": False}
    thinking_config: dict[str, Any] = {"includeThoughts": True}
    # Gemini 2.5 takes thinkingBudget; don't guess one from coarse effort levels.
    if normalized_model.startswith("gemini-2.5-"):
        return thinking_config
    if effort not in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        effort = "medium"
    # Gemini 3 Flash documents low/medium/high thinking levels; Gemini 3 Pro
    # is stricter (low/high). Clamp Hermes' wider effort set to what each
    # family accepts so we never forward an undocumented level verbatim.
    if normalized_model.startswith("gemini-3"):
        if "flash" in normalized_model:
            thinking_config["thinkingLevel"] = (
                "low" if effort in {"minimal", "low"} else "high" if effort in _HIGH_EFFORTS else "medium"
            )
        elif "pro" in normalized_model:
            thinking_config["thinkingLevel"] = "high" if effort in _HIGH_EFFORTS else "low"
    return thinking_config


def _snake_case_gemini_thinking_config(config: dict | None) -> dict | None:
    """Convert Gemini thinking config keys to the OpenAI-compat field names."""
    if not isinstance(config, dict) or not config:
        return None
    translated: dict[str, Any] = {}
    include, level, budget = config.get("includeThoughts"), config.get("thinkingLevel"), config.get("thinkingBudget")
    if isinstance(include, bool):
        translated["include_thoughts"] = include
    if isinstance(level, str) and level.strip():
        translated["thinking_level"] = level.strip().lower()
    if isinstance(budget, (int, float)):
        translated["thinking_budget"] = int(budget)
    return translated or None


def _is_gemini_openai_compat_base_url(base_url: Any) -> bool:
    normalized = str(base_url or "").strip().rstrip("/").lower()
    return bool(normalized) and "generativelanguage.googleapis.com" in normalized and normalized.endswith("/openai")
