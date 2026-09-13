"""Pinned tier-aware Nous auxiliary model selection."""
from typing import Any, Optional
from ..host.catalog import fetch_nous_recommended_models, _resolve_nous_portal_url, check_nous_free_tier

def _extract_model_name(entry: Any) -> Optional[str]:
    """Pull the ``modelName`` field from a recommended-model entry, else None."""
    model_name = entry.get("modelName") if isinstance(entry, dict) else None
    return model_name.strip() if isinstance(model_name, str) and model_name.strip() else None


def get_nous_recommended_aux_model(
    *, vision: bool = False, free_tier: Optional[bool] = None, portal_base_url: str = "",
    force_refresh: bool = False) -> Optional[str]:
    """The Portal's recommended model for an auxiliary task: free tier → free pick only; paid tier →
    paid pick, falling back to the free one when the Portal returned ``null`` (staged rollouts)."""
    base = portal_base_url or _resolve_nous_portal_url()
    payload = fetch_nous_recommended_models(base, force_refresh=force_refresh)
    if not payload:
        return None
    if free_tier is None:
        try:
            free_tier = check_nous_free_tier()
        except Exception:
            free_tier = False  # assume paid on detection error — paid users see both fields anyway
    kind = "Vision" if vision else "Compaction"
    tiers = ("free",) if free_tier else ("paid", "free")
    return next((n for t in tiers if (n := _extract_model_name(payload.get(f"{t}Recommended{kind}Model")))), None)
