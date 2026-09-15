"""Pinned Copilot model aliases and capability interpretation."""
from typing import Any, Optional
from ..host.catalog import fetch_github_model_catalog

_COPILOT_MODEL_ALIASES = dict((
    ("openai/gpt-5", "gpt-5-mini"), ("openai/gpt-5-chat", "gpt-5-mini"), ("openai/gpt-5-mini", "gpt-5-mini"),
    ("openai/gpt-5-nano", "gpt-5-mini"), ("openai/gpt-4.1", "gpt-4.1"), ("openai/gpt-4.1-mini", "gpt-4.1"),
    ("openai/gpt-4.1-nano", "gpt-4.1"), ("openai/gpt-4o", "gpt-4o"), ("openai/gpt-4o-mini", "gpt-4o-mini"),
    ("openai/o1", "gpt-5.2"), ("openai/o1-mini", "gpt-5-mini"), ("openai/o1-preview", "gpt-5.2"),
    ("openai/o3", "gpt-5.3-codex"), ("openai/o3-mini", "gpt-5-mini"), ("openai/o4-mini", "gpt-5-mini"),
    ("anthropic/claude-opus-4.6", "claude-opus-4.6"), ("anthropic/claude-sonnet-5", "claude-sonnet-5"),
    ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"), ("anthropic/claude-sonnet-4", "claude-sonnet-4"),
    ("anthropic/claude-sonnet-4.5", "claude-sonnet-4.5"), ("anthropic/claude-haiku-4.5", "claude-haiku-4.5"),
    ("claude-sonnet-5", "claude-sonnet-5"), ("claude-opus-4-6", "claude-opus-4.6"),
    ("claude-sonnet-4-6", "claude-sonnet-4.6"), ("claude-sonnet-4-0", "claude-sonnet-4"),
    ("claude-sonnet-4-5", "claude-sonnet-4.5"), ("claude-haiku-4-5", "claude-haiku-4.5"),
    ("anthropic/claude-opus-4-6", "claude-opus-4.6"), ("anthropic/claude-sonnet-4-6", "claude-sonnet-4.6"),
    ("anthropic/claude-sonnet-4-0", "claude-sonnet-4"), ("anthropic/claude-sonnet-4-5", "claude-sonnet-4.5"),
    ("anthropic/claude-haiku-4-5", "claude-haiku-4.5"),
))


COPILOT_REASONING_EFFORTS_GPT5 = ["minimal", "low", "medium", "high"]


COPILOT_REASONING_EFFORTS_O_SERIES = ["low", "medium", "high"]


_COPILOT_CHAT_ENDPOINTS = {"/chat/completions", "/responses", "/v1/messages"}


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", []) if isinstance(payload, dict) else payload
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _copilot_catalog_item_is_text_model(
    item: dict[str, Any], *, ignore_picker_flag: bool = False) -> bool:
    if not str(item.get("id") or "").strip():
        return False
    if not ignore_picker_flag and item.get("model_picker_enabled") is False:
        return False
    capabilities = item.get("capabilities")
    if isinstance(capabilities, dict):
        model_type = str(capabilities.get("type") or "").strip().lower()
        if model_type and model_type != "chat":
            return False
    supported_endpoints = item.get("supported_endpoints")
    if isinstance(supported_endpoints, list):
        endpoints = {e for endpoint in supported_endpoints if (e := str(endpoint).strip())}
        if endpoints and not endpoints & _COPILOT_CHAT_ENDPOINTS:
            return False
    return True


def _copilot_text_models(items: list[dict[str, Any]], *, ignore_picker_flag: bool = False) -> list[dict[str, Any]]:
    """Chat-capable catalog rows, deduped by id, in catalog order."""
    models: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        model_id = str(item.get("id") or "").strip()
        if model_id in seen_ids:
            continue
        if not _copilot_catalog_item_is_text_model(item, ignore_picker_flag=ignore_picker_flag):
            continue
        seen_ids.add(model_id)
        models.append(item)
    return models


def _copilot_catalog_ids(
    catalog: Optional[list[dict[str, Any]]] = None, api_key: Optional[str] = None) -> set[str]:
    if catalog is None and api_key:
        catalog = fetch_github_model_catalog(api_key=api_key)
    return {mid for item in (catalog or []) if (mid := str(item.get("id") or "").strip())}


def normalize_copilot_model_id(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> str:
    raw = str(model_id or "").strip()
    if not raw:
        return ""

    catalog_ids = _copilot_catalog_ids(catalog=catalog, api_key=api_key)
    alias = _COPILOT_MODEL_ALIASES.get(raw)
    if alias:
        return alias

    candidates = [raw]
    if "/" in raw:
        candidates.append(raw.split("/", 1)[1].strip())
    if raw.endswith(("-mini", "-nano", "-chat")):
        candidates.append(raw[:-5])

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if candidate in _COPILOT_MODEL_ALIASES:
            return _COPILOT_MODEL_ALIASES[candidate]
        if candidate in catalog_ids:
            return candidate

    if "/" in raw:
        return raw.split("/", 1)[1].strip()
    return raw


def _github_reasoning_efforts_for_model_id(model_id: str) -> list[str]:
    raw = (model_id or "").strip().lower()
    if raw.startswith(("openai/o1", "openai/o3", "openai/o4", "o1", "o3", "o4")):
        return list(COPILOT_REASONING_EFFORTS_O_SERIES)
    normalized = normalize_copilot_model_id(model_id).lower()
    if normalized.startswith("gpt-5"):
        return list(COPILOT_REASONING_EFFORTS_GPT5)
    return []


def github_model_reasoning_efforts(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> list[str]:
    """Return supported reasoning-effort levels for a Copilot-visible model."""
    normalized = normalize_copilot_model_id(model_id, catalog=catalog, api_key=api_key)
    if not normalized:
        return []

    if catalog is None and api_key:
        catalog = fetch_github_model_catalog(api_key=api_key)
    catalog_entry = next((item for item in catalog if item.get("id") == normalized), None) if catalog else None
    if catalog_entry is not None:
        capabilities = catalog_entry.get("capabilities")
        if isinstance(capabilities, dict):
            # Structured catalog: the advertised list is authoritative (empty when absent).
            supports = capabilities.get("supports")
            efforts = supports.get("reasoning_effort") if isinstance(supports, dict) else None
            if not isinstance(efforts, list):
                return []
            return list(dict.fromkeys(e for effort in efforts if (e := str(effort).strip().lower())))
        # Legacy list-shaped capabilities: only a "reasoning" tag unlocks the pattern defaults.
        if "reasoning" not in {str(c).strip().lower() for c in catalog_entry.get("capabilities", [])}:
            return []
    return _github_reasoning_efforts_for_model_id(str(model_id or normalized))
