"""Pinned pure dependency closure; source selections are recorded in HERMES_NATIVE.json."""
from __future__ import annotations

import contextlib
import logging
import re
import threading
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_BLOCKED_PROJECT_ENV_BASENAMES: set[str] = {
    ".env", ".env.local", ".env.development", ".env.production", ".env.test", ".env.staging", ".envrc",
}


class MoAPresetNotFoundError(ValueError):
    """Raised when a persisted MoA preset no longer exists in config."""


class UnscopedSecretError(RuntimeError):
    """A secret was read in multiplex mode with no scope installed.

    The fix is to wrap the call path in ``set_secret_scope(...)`` (the per-turn
    / per-adapter profile scope), not to widen the global allowlist.
    """


def drop_stale_api_content(msg: Dict[str, Any]) -> None:
    """Drop the ``api_content`` sidecar from a message whose content was rewritten
    (replaying it would resend what the rewrite removed; cost is one cache miss)."""
    msg.pop("api_content", None)


TODO_INJECTION_HEADER = "[Your active task list was preserved across context compression]"


_REASONING_TAG_NAMES = ("think", "thinking", "reasoning", "REASONING_SCRATCHPAD", "thought")


_TOOL_CALL_TAG_NAMES = ("tool_call", "tool_calls", "tool_result", "function_call", "function_calls")


_REASONING_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{name}>.*?</{name}>", re.DOTALL | re.IGNORECASE) for name in _REASONING_TAG_NAMES
)


_TOOL_CALL_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{name}\b[^>]*>.*?</{name}>", re.DOTALL | re.IGNORECASE)
    for name in _TOOL_CALL_TAG_NAMES
)


_NAMED_FUNCTION_BLOCK_PATTERN = re.compile(
    r'(?:(?<=^)|(?<=[\n\r.!?:]))[ \t]*'
    r'<function\b[^>]*\bname\s*=[^>]*>'
    r'(?:(?:(?!</function>).)*)</function>', re.DOTALL | re.IGNORECASE,
)


_UNTERMINATED_REASONING_BLOCK_PATTERN = re.compile(
    rf'(?:^|\n)[ \t]*<(?:{"|".join(_REASONING_TAG_NAMES)})\b[^>]*>.*$', re.DOTALL | re.IGNORECASE
)


_ORPHAN_REASONING_TAG_PATTERN = re.compile(
    rf'</?(?:{"|".join(_REASONING_TAG_NAMES)})>\s*', re.IGNORECASE
)


_STRAY_TOOL_CALL_CLOSER_PATTERN = re.compile(
    rf'</(?:{"|".join(_TOOL_CALL_TAG_NAMES)}|function)>\s*', re.IGNORECASE
)


_UNTERMINATED_TOOL_CALL_PATTERN = re.compile(
    rf'(?:^|\n)[ \t]*<(?:{"|".join(_TOOL_CALL_TAG_NAMES)})\b[^>]*>.*$'
    r'|(?:^|\n)[^\n<]*</?arg_(?:key|value)\b.*$',
    re.DOTALL | re.IGNORECASE,
)


def _flatten_content_text(content: Any) -> str:
    """Flatten list/dict content (e.g. Anthropic-via-OpenRouter block lists) to text: a raw list
    hitting ``re.sub`` raises TypeError and the loop retries forever. Thinking/reasoning blocks
    are dropped outright; their text key varies per provider."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else part.get("text")
            for part in content
            if isinstance(part, str) or (
                isinstance(part, dict)
                and str(part.get("type") or "").strip().lower() not in {"thinking", "reasoning", "redacted_thinking"}
                and isinstance(part.get("text"), str) and part.get("text")
            )
        )
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


_THINK_STRIP_PATTERNS = (
    *_REASONING_BLOCK_PATTERNS, *_TOOL_CALL_BLOCK_PATTERNS, _NAMED_FUNCTION_BLOCK_PATTERN,
    _UNTERMINATED_REASONING_BLOCK_PATTERN, _ORPHAN_REASONING_TAG_PATTERN,
    _STRAY_TOOL_CALL_CLOSER_PATTERN, _UNTERMINATED_TOOL_CALL_PATTERN,
)


def strip_think_blocks(agent, content: str) -> str:
    """Remove reasoning/thinking blocks from content, returning only visible text: closed tag
    pairs, unterminated open tags at a block boundary (mirrors ``gateway/stream_consumer.py``),
    stray orphan tags (all case-insensitive variants), and standalone tool-call XML blocks some
    open models emit; ``<function>`` is boundary- and ``name=``-gated so prose mentions survive."""
    content = _flatten_content_text(content) if content else ""
    for pattern in _THINK_STRIP_PATTERNS if content else ():
        content = pattern.sub('', content)
    return content


def _classify_tool_call_orphans(messages: List[Dict[str, Any]]):
    """Classify orphaned tool-call / tool-result pairs; single source of truth for GLOBAL orphan
    detection. Returns ``(surviving_call_ids, result_call_ids, orphaned_results, missing_tool_calls)``;
    every id variant of a tool_call is registered so a result matching any alias survives, and
    ``orphaned_results`` are the actual dicts (filter by ``id(msg)``). ``sanitize_api_messages``
    pairs positionally instead but shares the ``*_id_variants`` alias policy."""
    assistant_call_variants = [
        (tc, variants)
        for msg in messages if msg.get("role") == "assistant"
        for tc in msg.get("tool_calls") or []
        if (variants := tool_call_id_variants(tc))
    ]
    surviving_call_ids: set[str] = set().union(*(v for _, v in assistant_call_variants))
    result_entries = [
        (msg, tool_result_id_variants(msg.get("tool_call_id"))) for msg in messages if msg.get("role") == "tool"
    ]
    result_call_ids: set[str] = set().union(*(v for _, v in result_entries))
    orphaned_results = [msg for msg, v in result_entries if v and not (v & surviving_call_ids)]
    orphaned_ids = {id(msg) for msg in orphaned_results}
    surviving_result_variants = [v for msg, v in result_entries if v and id(msg) not in orphaned_ids]
    missing_tool_calls = [
        tc for tc, v in assistant_call_variants if not any(v & rv for rv in surviving_result_variants)
    ]
    return surviving_call_ids, result_call_ids, orphaned_results, missing_tool_calls


def _tc_field(tc: Any, key: str) -> Any:
    """Read ``key`` from a tool-call entry that may be a dict or an SDK object."""
    return tc.get(key) if isinstance(tc, dict) else getattr(tc, key, None)


def _expand_tool_id_variants(values: tuple[Any, ...]) -> frozenset[str]:
    """Every wire spelling of one tool-call identifier: Responses bridges may expose the pairing
    id and response-item id separately or as ``call_id|response_item_id``; all alias ONE call."""
    variants: set[str] = set()
    for raw in values:
        value = raw.strip() if isinstance(raw, str) else ""
        if value:
            variants.add(value)
            variants.update(p for p in (part.strip() for part in value.split("|")) if p)
    return frozenset(variants)


def tool_call_id_variants(tc: Any) -> frozenset[str]:
    """Return all pairing-id variants carried by a tool-call entry."""
    return _expand_tool_id_variants(tuple(_tc_field(tc, k) for k in ("call_id", "id", "response_item_id")))


def tool_result_id_variants(tool_call_id: Any) -> frozenset[str]:
    """Return all matching variants for a role=tool ``tool_call_id``."""
    return _expand_tool_id_variants((tool_call_id,))


_REASONING_ECHO_RULES: tuple = (
    # (family, exact providers (raw), exact providers (lowered), model substrings (lowered), hosts)
    ("kimi", frozenset({"kimi-coding", "kimi-coding-cn"}), frozenset(), (), ("api.kimi.com", "moonshot.ai", "moonshot.cn")),
    ("deepseek", frozenset(), frozenset({"deepseek"}), ("deepseek",), ("api.deepseek.com",)),
    ("mimo", frozenset(), frozenset({"xiaomi"}), ("mimo",), ("api.xiaomimimo.com", "xiaomimimo.com")),
)


_REASONING_ECHO_RULE_BY_FAMILY = {rule[0]: rule for rule in _REASONING_ECHO_RULES}


def matches_reasoning_echo_family(family: str, provider: Any, model: Any, base_url: Any) -> bool:
    """True when (provider, model, base_url) matches one echo-back family (families can overlap;
    membership is tested independently). Raises KeyError for an unknown family."""

    _, raw_providers, lowered_providers, model_subs, hosts = _REASONING_ECHO_RULE_BY_FAMILY[family]
    model_lower = (model or "").lower()
    return (
        provider in raw_providers or (provider or "").lower() in lowered_providers
        or any(sub in model_lower for sub in model_subs) or any(base_url_host_matches(base_url, host) for host in hosts)
    )


def reasoning_echo_family(provider: Any, model: Any, base_url: Any) -> "str | None":
    """``"kimi"`` / ``"deepseek"`` / ``"mimo"`` (first match in table order) when the
    endpoint enforces reasoning_content echo-back, else ``None`` (strip side)."""
    families = (rule[0] for rule in _REASONING_ECHO_RULES)
    return next((f for f in families if matches_reasoning_echo_family(f, provider, model, base_url)), None)


def needs_reasoning_echo(provider: Any, model: Any, base_url: Any) -> bool:
    """True when the endpoint requires reasoning_content echo-back."""
    return reasoning_echo_family(provider, model, base_url) is not None


def stale_thinking_reaches_wire(api_mode: Any, provider: Any, model: Any, base_url: Any) -> bool:
    """True when stale assistant reasoning text is actually replayed on the wire for the route.

    The single wire-truth predicate the compaction TRIGGER estimator and the tail-budget
    walks must share: if they disagree, a reasoning-heavy session can look over-threshold
    to preflight yet fully tail-protected to the walk — an infinite compaction loop.
    ``codex_responses`` never reads the text keys (continuity rides the encrypted sidecar).
    """
    return (api_mode or "") != "codex_responses" and needs_reasoning_echo(provider, model, base_url)


_SYNTHETIC_USER_PREFIXES = (
    "[System: Your previous response was truncated", "[System: The previous response was cut off",
    "[System: Your previous tool call", "[Your active task list was preserved across context compression]",
    "[IMPORTANT: Background process ",
)


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "") for part in content if isinstance(part, dict)
        )
    return ""


_SYNTHETIC_USER_FLAGS = (
    "_todo_snapshot_synthetic", "_empty_recovery_synthetic", "_verification_stop_synthetic", "_pre_verify_synthetic",
    "_dropped_toolcall_nudge",
)


def _is_real_user_message(message: Any) -> bool:
    """Distinguish human intent from user-role runtime scaffolding.
    A compaction summary flipped to ``role="user"`` for alternation is scaffolding and must not short-circuit
    anchor restoration."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if any(message.get(flag) for flag in _SYNTHETIC_USER_FLAGS):
        return False
    text = _message_text(message).strip()
    if not text or text.startswith(_SYNTHETIC_USER_PREFIXES):
        return False
    from .context_compressor import ContextCompressor
    return not ContextCompressor._is_synthetic_compression_user_turn(message)


_PRUNED_SKILL_RELOAD_NOTICE_HEADER = "[Skills pruned during compression — reload before acting on these tasks]"


_LENGTH_CONTINUATION_NETWORK_STUB = (
    "[System: The previous response was cut off by a network error mid-stream. Continue exactly "
    "where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
)


_LENGTH_CONTINUATION_OUTPUT_LIMIT = (
    "[System: Your previous response was truncated by the output length limit. Continue exactly "
    "where you left off. Do not restart or repeat prior text. Finish the answer directly.]"
)


_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX = "[System: Your previous tool call "


_CODEX_INCOMPLETE_NUDGE = (
    "[System: Your previous response contained only internal reasoning and never produced a "
    "visible answer or tool call. Do not keep thinking. Produce your final answer as plain text "
    "now (or make the tool call you were planning).]"
)


_CODEX_ACK_CONTINUATION_NUDGE = (
    "[System: Continue now. Execute the required tool calls and only send your final answer "
    "after completing the task.]"
)


_DROPPED_TOOLCALL_NUDGE_CONTENT = (
    "Your previous turn indicated a tool call but none was included. Do not narrate a plan or "
    "restate intent — issue the actual tool call now to continue the task."
)


_EMPTY_TOOL_RESPONSE_NUDGE = (
    "You just executed tool calls but returned an empty response. Please process the tool "
    "results above and continue with the task."
)


_REASONING_STALE_TIMEOUT_FLOORS: dict[int, tuple[str, ...]] = {
    600: (
        # NVIDIA Nemotron behind hosted NIM: documented 60-180s upstream idle kill.
        "nemotron-3-ultra", "nemotron-3-super",
        # DeepSeek R1 / V4 (reasoning_content streamed before final content).
        "deepseek-r1", "deepseek-reasoner", "deepseek-v4-flash", "deepseek-v4-pro",
        # OpenAI o-series: each variant enumerated so bare ``o1`` cannot over-match ``olmo-1``.
        "o1", "o1-mini", "o1-pro", "o1-preview", "o3", "o3-pro",
        # Mythos-class named models (claude-fable-5): 1M ctx + 128K output, a heavier thinking
        # phase than the numbered line — otherwise the stale detector trips the circuit breaker.
        "claude-fable",
    ),
    300: (
        "nemotron-3-nano", "nemotron-3.5-lightning", "qwq-32b", "o3-mini", "o4-mini",
        # xAI Grok: explicit reasoning pairs only, so bare ``grok-3``/``grok-4`` fast variants
        # don't inherit the floor.
        "grok-4-fast-reasoning", "grok-4.20-reasoning", "grok-4.5", "grok-4.6",
        # "Ox Alpha" stealth reasoning model (OpenRouter / OpenCode Zen slugs); Thinking
        # Machines Inkling (covers inkling-small and :free SKUs).
        "ox-alpha", "x-preview-f-free", "inkling",
    ),
    # Anthropic Claude 4.x+ thinking variants (anchored so 3.x never matches).
    240: ("claude-opus-4", "claude-opus-5"),
    # qwen3 family: instruct variants also match — a slightly longer wait on a hung provider
    # beats a pattern (``qwen3-.*-thinking``) that breaks on the next naming shape.
    180: ("qwen3", "claude-sonnet-5", "claude-sonnet-4.5", "claude-sonnet-4.6", "grok-4-fast-non-reasoning"),
}


_SORTED_REASONING_FLOORS: list[tuple[str, float, re.Pattern[str]]] = [
    (slug, floor, re.compile(r"^" + re.escape(slug) + r"(?:$|[\-._:])"))
    for slug, floor in sorted(
        ((slug, floor) for floor, slugs in _REASONING_STALE_TIMEOUT_FLOORS.items() for slug in slugs),
        key=lambda kv: -len(kv[0]),
    )
]


def get_reasoning_stale_timeout_floor(model: object) -> Optional[float]:
    """Stale-timeout floor (seconds) for a known reasoning model, else ``None``.

    The aggregator prefix (up to the last ``/``) is stripped and the slug matched
    start-anchored with an end-or-separator right anchor, so ``qwen3-235b`` matches ``qwen3``
    but ``some-other-qwen3`` and ``llama-4-70b-o1-preview`` do not.
    """
    if not model or not isinstance(model, str):
        return None
    name = model.strip().lower().rsplit("/", 1)[-1]
    for _slug, floor, pattern in _SORTED_REASONING_FLOORS:
        if pattern.search(name):
            return float(floor)
    return None


PERSISTENCE_ONLY_MESSAGE_FIELDS = frozenset({"timestamp", "_misaka_replay_source"})  # misaka: private ordinal is not wire content


def _parse_base_url(base_url: str):
    """``urlparse`` that tolerates a bare ``host[:port][/path]`` (no scheme)."""
    raw = (base_url or "").strip()
    return urlparse(raw if "://" in raw else f"//{raw}") if raw else None


def _hostname_of(parsed) -> str:
    return (parsed.hostname or "").lower().rstrip(".") if parsed else ""


def base_url_hostname(base_url: str) -> str:
    """Lowercased hostname for a base URL, or ``""`` if absent.

    Compare exact hostnames against provider hosts instead of substring-matching the raw URL:
    ``https://api.openai.com.example/v1`` or ``https://proxy.test/api.openai.com/v1`` would
    otherwise pass as native endpoints and mis-route api_mode and auth.
    """
    return _hostname_of(_parse_base_url(base_url))


def base_url_host_matches(base_url: str, domain: str) -> bool:
    """True when the base URL's hostname is ``domain`` or a subdomain.

    Safer than ``domain in base_url`` (``evil.com/moonshot.ai`` / ``moonshot.ai.evil`` must not
    match). Accepts bare hosts, full URLs, and URLs with paths.
    """
    hostname = base_url_hostname(base_url)
    domain = (domain or "").strip().lower().rstrip(".")
    return bool(hostname and domain) and (hostname == domain or hostname.endswith("." + domain))


logger = logging.getLogger(__name__)


_aux_interrupt_protection = threading.local()


class AuxiliaryExplicitCancellation(BaseException):
    """Frozen signal that an auxiliary attempt was explicitly hard-cancelled. ``BaseException`` so broad
    ``except Exception`` retry/fallback code never treats a host stop as a transport failure; ``cause``
    is immutable class data so nothing re-queries a mutable host Event after the transport unwound."""
    cause = "explicit_host_cancel"

    def __init__(self) -> None:
        super().__init__("auxiliary request explicitly cancelled by host")


@contextlib.contextmanager
def aux_interrupt_protection(active: bool = True, cancel_check=None, cancel_event=None):
    """Mark this thread's aux LLM call interrupt-protected (re-entrant-safe). ``cancel_check`` /
    ``cancel_event`` keep an explicit host hard-cancel path (Event preferred); nested scopes inherit both."""
    prev = getattr(_aux_interrupt_protection, "active", False)
    prev_cancel_check = getattr(_aux_interrupt_protection, "cancel_check", None)
    prev_cancel_event = getattr(_aux_interrupt_protection, "cancel_event", None)
    _aux_interrupt_protection.active = active
    if callable(cancel_check):
        _aux_interrupt_protection.cancel_check = cancel_check
    if cancel_event is not None and callable(getattr(cancel_event, "is_set", None)):
        _aux_interrupt_protection.cancel_event = cancel_event
    try:
        yield
    finally:
        _aux_interrupt_protection.active = prev
        _aux_interrupt_protection.cancel_check = prev_cancel_check
        _aux_interrupt_protection.cancel_event = prev_cancel_event


def _contains_any(text: str, needles: Tuple[str, ...]) -> bool:
    """True when any needle is a substring of ``text``."""
    return any(kw in text for kw in needles)


def _is_connection_error(exc: Exception) -> bool:
    """Connection/network errors (endpoint unreachable), as opposed to 4xx/5xx API errors."""
    with contextlib.suppress(ImportError):
        from openai import APIConnectionError, APITimeoutError
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return True
    if _contains_any(type(exc).__name__, ("Connection", "Timeout", "DNS", "SSL")):
        return True
    return _contains_any(str(exc).lower(), (
        "connection refused", "name or service not known", "no route to host",
        "network is unreachable", "timed out", "connection reset",
        # httpcore/httpx premature stream close — transient, retry/reroute.
        "incomplete chunked read", "peer closed connection", "response ended prematurely",
        "unexpected eof", "remoteprotocolerror", "localprotocolerror",
    ))


def _coerce_llm_message(response):
    """Pull a message (dict, object, or str) out of a response-or-message value: dict-shaped
    responses/bare messages (compression, proxies) and ChatCompletion objects; MagicMock
    ``reasoning_*`` attrs are deliberately not strings."""
    if response is None or isinstance(response, str):
        return response
    if isinstance(response, dict):
        if "choices" not in response:
            return response
        choices = response.get("choices") or []
    else:
        choices = getattr(response, "choices", None)
        if not choices:
            return response
    return _message_field(choices[0], "message") if choices else None


def _message_field(msg, name):
    return msg.get(name) if isinstance(msg, dict) else getattr(msg, name, None)


def extract_content_or_reasoning(response, *, max_reasoning_chars: int | None = None) -> str:
    """Extract content from an LLM response, falling back to reasoning fields.
    Order: ``content`` (inline think blocks stripped) → ``reasoning``/``reasoning_content`` →
    ``reasoning_details`` (OpenRouter array). Accepts a response or bare message;
    ``max_reasoning_chars`` bounds a reasoning fallback so unbounded chain-of-thought can't
    become the compaction summary. Returns ``""`` if nothing found."""
    msg = _coerce_llm_message(response)
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg.strip()
    raw = _message_field(msg, "content")
    if not isinstance(raw, str):
        raw = str(raw) if raw else ""
    content = raw.strip()
    if content:
        # Mirrors _strip_think_blocks
        cleaned = re.sub(
            r"<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>"
            r".*?"
            r"</(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>",
            "", content, flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        if cleaned:
            return cleaned
    # Content is empty or reasoning-only — try structured reasoning fields
    reasoning_parts: list[str] = []
    for field in ("reasoning", "reasoning_content"):
        val = _message_field(msg, field)
        if val and isinstance(val, str) and val.strip() and val not in reasoning_parts:
            reasoning_parts.append(val.strip())
    details = _message_field(msg, "reasoning_details")
    if details and isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict):
                summary = detail.get("summary") or detail.get("content") or detail.get("text")
                if summary and summary not in reasoning_parts:
                    reasoning_parts.append(summary.strip() if isinstance(summary, str) else str(summary))
    if not reasoning_parts:
        return ""
    text = "\n\n".join(reasoning_parts)
    if max_reasoning_chars is not None and len(text) > max_reasoning_chars:
        logger.warning("fell back to reasoning fields (%d chars); truncating to %d",
                       len(text), max_reasoning_chars)
        return text[:max_reasoning_chars]
    return text
