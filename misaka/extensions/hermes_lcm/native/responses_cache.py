"""Pinned Responses cache identity/retention policy; not a cache or session owner."""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any, Optional


_CRON_SESSION_ID_RE = re.compile(r"^(cron_.+)_\d{8}_\d{6}$")


def _cache_scope_from_session_id(session_id: Optional[str]) -> str:
    """Normalize a physical session_id into a stable logical cache scope."""
    sid = str(session_id or "")
    match = _CRON_SESSION_ID_RE.match(sid)
    return match.group(1) if match else sid


_EXTENDED_PROMPT_CACHE_MODELS = (
    "gpt-5.5-pro", "gpt-5.5", "gpt-5.4", "gpt-5.2",
    "gpt-5.1-codex-max", "gpt-5.1-codex-mini", "gpt-5.1-chat-latest", "gpt-5.1-codex", "gpt-5.1",
    "gpt-5-codex", "gpt-5", "gpt-4.1",
)


_EXTENDED_PROMPT_CACHE_MODEL_RE = re.compile(
    rf"(?:^|[./:])(?:{'|'.join(re.escape(name) for name in _EXTENDED_PROMPT_CACHE_MODELS)})"
    r"(?:-\d{4}-\d{2}-\d{2})?$"
)


def _default_prompt_cache_retention_for_request(model: str, base_url: Any) -> Optional[str]:
    """Return ``24h`` for supported hosts/models (Bedrock Mantle, Meta)."""
    from .support import base_url_hostname

    hostname = base_url_hostname(str(base_url or "")).lower()
    # Meta Model API: caching is opt-in via prompt_cache_retention (0% hits without).
    # Meta Model API (api.meta.ai) only achieves prompt-cache hits on the Responses API with
    # prompt_cache_retention; chat/completions stays cache-cold (0% vs 93-99% measured). Exact-hostname
    # match per #32243.
    # Meta Model API: prompt caching only on Responses API (0% on chat/completions vs 93-99% on /responses
    # with retention). See #32243.
    if hostname == "api.meta.ai":
        return "24h"
    parts = hostname.split(".")
    is_bedrock_mantle = len(parts) == 4 and parts[0] == "bedrock-mantle" and bool(parts[1]) and parts[2:] == ["api", "aws"]
    if not is_bedrock_mantle:
        return None
    normalized = str(model or "").strip().lower().replace("_", "-")
    return "24h" if _EXTENDED_PROMPT_CACHE_MODEL_RE.search(normalized) else None


def _content_cache_key(instructions: str, tools: Optional[list[dict[str, Any]]], scope_id: str = "") -> Optional[str]:
    """``pck_<sha256[:24]>`` of (scope_id, instructions, name-sorted tools), or None if nothing static.

    Routing hint only; ``scope_id`` keeps unrelated sessions off one bucket.

    ``scope_id`` (pass ``_cache_scope_from_session_id(session_id)``) keeps unrelated sessions — independent
    conversations, main vs. child/subagent, sibling children — from concentrating onto the same bucket
    merely because their static prefix matches (see #78941), while still letting recurring cron fires of one
    job share a stable key across their timestamped session_ids (the original #51395/#52295 fix this built
    on). Sorting tools by name keeps the hash insertion-order independent.
    """
    if not instructions and not tools:
        return None
    tools_part = ""
    if tools:
        sorted_tools = sorted(
            (t for t in tools if isinstance(t, dict)), key=lambda t: str(t.get("name") or t.get("type") or ""),
        )
        tools_part = json.dumps(sorted_tools, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    # \x00 separators so a boundary can't be forged by content containing the same bytes.
    content = f"{scope_id}\x00{instructions or ''}\x00{tools_part}"
    return "pck_" + hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:24]
