"""Pinned Codex quota shape and endpoint policy; host handles cancellable HTTP."""
from typing import Any, Dict, Optional
from .nous_auth import _decode_jwt_claims
from ..host.pool import _codex_base_url

def _stripped(value: Any) -> str:
    return str(value or "").strip()


def _is_codex_rate_limit_shaped(code: Any, reason: Any, message: Any) -> bool:
    """True when persisted pool-entry error metadata describes a 429/quota stop."""
    reason_l, message_l = str(reason or "").lower(), str(message or "").lower()
    return (
        code == 429
        or any(k in reason_l for k in ("rate_limit", "usage_limit", "quota"))
        or any(k in message_l for k in ("rate limit", "usage limit", "quota")))


def _entry_is_rate_limit_exhausted(entry: Dict[str, Any]) -> bool:
    """Pool entry frozen by a 429/quota stop (as opposed to an auth failure)."""
    return entry.get("last_status") == "exhausted" and _is_codex_rate_limit_shaped(
        entry.get("last_error_code"), entry.get("last_error_reason"),
        entry.get("last_error_message"))


CODEX_QUOTA_PROBE_MIN_INTERVAL_SECONDS = 300  # 5 minutes


def _codex_usage_probe_url(base_url: Optional[str]) -> str:
    """Resolve the Codex usage endpoint for a probe.

    Mirrors the Codex CLI's PathStyle split: base URLs containing ``/backend-api`` use the ChatGPT
    ``/wham/usage`` path, everything else ``/api/codex/usage``. Kept local so this low-level auth
    module does not import the auxiliary account-usage module.
    """
    normalized = _stripped(base_url).rstrip("/") or _codex_base_url()
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    prefix = normalized + ("/wham" if "/backend-api" in normalized else "/api/codex")
    return prefix + "/usage"
