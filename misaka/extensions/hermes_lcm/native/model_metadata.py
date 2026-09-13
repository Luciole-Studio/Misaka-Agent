"""Pinned estimators; model discovery belongs to MISAKA's registry, not Hermes' home."""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .support import PERSISTENCE_ONLY_MESSAGE_FIELDS
from ..host.native import get_model_context_length

CONTEXT_PROBE_TIERS = [256_000, 128_000, 64_000, 32_000, 16_000, 8_000]


DEFAULT_FALLBACK_CONTEXT = CONTEXT_PROBE_TIERS[0]


MINIMUM_CONTEXT_LENGTH = 64_000


_CJK_DENSE_RE = re.compile("[\u1100-\u11ff\u2e80-\u9fff\ua960-\ua97f\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def _is_cjk_token_dense_char(ch: str) -> bool:
    return _CJK_DENSE_RE.fullmatch(ch) is not None


def estimate_tokens_rough(text: str) -> int:
    """Rough token estimate: CJK/Hangul/Kana codepoints ~1 token each; everything else ceil(UTF-8 bytes/4).
    Ceiling keeps short texts from estimating 0. Runs on every preflight walk, so all-ASCII stays O(1).

    Byte-counting (not chars) is the corrective for non-CJK, non-ASCII text: Cyrillic/Greek/Arabic are 2
    bytes/char so count ~chars/2, matching real BPE cost (~2-3 chars/token) where chars/4 under-counted
    ~2x and let sessions ride the provider ceiling below the compaction threshold. Calibrated vs
    cl100k/o200k/Qwen2.5 (estimate/real): Russian 0.67->1.24, Arabic 0.53->0.96, Hindi 0.34->0.90,
    Greek 0.37->0.68; accented Latin barely moves (French 1.02->1.03). errors="replace": lone surrogates
    (routine in tool output; see message_sanitization) must not turn an estimate into a raise."""
    if not text:
        return 0
    text = str(text)
    if text.isascii():  # flag check on CPython; ASCII cannot contain token-dense CJK
        return (len(text) + 3) // 4
    stripped = _CJK_DENSE_RE.sub("", text)
    dense = len(text) - len(stripped)
    return dense + ((len(stripped.encode("utf-8", "replace")) + 3) // 4)


def estimate_messages_tokens_rough(messages: List[Dict[str, Any]], *, charge_stale_thinking: bool = True) -> int:
    """Rough token estimate for a message list (pre-flight only). Images cost the per-image price
    learned from provider usage (``agent.image_token_cost``; flat default before calibration)
    rather than their base64 length. ``charge_stale_thinking=False`` mirrors the tail-budget
    walk (``context_compressor._estimate_msg_budget_tokens``): on non-echo routes stale reasoning
    rides the wire only for the NEWEST assistant turn, so excluding it keeps the compaction TRIGGER
    in the same size class as the walk — otherwise reasoning-heavy sessions fire preflight forever."""
    from .image_token_cost import current_image_token_cost

    image_cost = current_image_token_cost()
    if not charge_stale_thinking:
        messages = _strip_stale_thinking_for_estimate(messages)
    return sum(_estimate_message_tokens_cached(msg, image_cost) for msg in messages)


_STALE_THINKING_ESTIMATE_KEYS = ("reasoning", "reasoning_content")


def _strip_stale_thinking_for_estimate(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copy of ``messages`` with stale thinking keys removed (newest kept). Shallow stripped copies
    share the original value objects, so the per-message memo still hits for the stripped shape."""
    def _is_assistant(m: Any) -> bool:
        return isinstance(m, dict) and m.get("role") == "assistant"
    newest = next((i for i in range(len(messages) - 1, -1, -1) if _is_assistant(messages[i])), -1)
    return [
        {k: v for k, v in m.items() if k not in _STALE_THINKING_ESTIMATE_KEYS}
        if i != newest and _is_assistant(m) and any(m.get(k) for k in _STALE_THINKING_ESTIMATE_KEYS) else m
        for i, m in enumerate(messages)
    ]


_MSG_TOKENS_CACHE: Dict[Any, Tuple[list, int, int]] = {}  # pins, text tokens, image count


_MSG_TOKENS_CACHE_MAX = 4096


def _msg_fingerprint(value: Any, pins: list) -> Any:
    if value is None or value is True or value is False:
        return value
    t = type(value)
    if t is str:
        pins.append(value)
        return ("s", id(value))
    if t is int or t is float:
        return ("n", t.__name__, value)
    if t is dict:
        return ("d", tuple((_msg_fingerprint(k, pins), _msg_fingerprint(v, pins)) for k, v in value.items()))
    if t is list or t is tuple:
        return ("l" if t is list else "t", tuple(_msg_fingerprint(v, pins) for v in value))
    raise ValueError("unfingerprintable message value")


def _estimate_message_tokens_cached(msg: Any, image_cost: int) -> int:
    """Text tokens + images x ``image_cost``; the memo holds text and image COUNT so a recalibrated
    per-image price re-prices cached rows without invalidating them."""
    def _compute() -> Tuple[int, int]:
        return _estimate_message_tokens_without_images(msg), _count_image_tokens(msg, 1)
    try:
        pins: list = []
        key = _msg_fingerprint(msg, pins)
        hash(key)
    except Exception:
        text, images = _compute()
        return text + images * image_cost
    cached = _MSG_TOKENS_CACHE.get(key)
    if cached is not None:
        return cached[1] + cached[2] * image_cost
    text, images = _compute()
    tokens = text + images * image_cost
    _MSG_TOKENS_CACHE[key] = (pins, text, images)
    while len(_MSG_TOKENS_CACHE) > _MSG_TOKENS_CACHE_MAX:
        try:
            _MSG_TOKENS_CACHE.pop(next(iter(_MSG_TOKENS_CACHE)))
        except (StopIteration, KeyError, RuntimeError):
            break
    return tokens


def _count_parts(parts: Any, types: set) -> int:
    return sum(1 for part in parts if isinstance(part, dict) and part.get("type") in types) if isinstance(parts, list) else 0


def _count_image_tokens(msg: Dict[str, Any], cost_per_image: int) -> int:
    """Count image-like content parts in a message; return their token cost."""
    if not isinstance(msg, dict):
        return 0
    content = msg.get("content")
    count = _count_parts(content, {"image", "image_url", "input_image"})
    count += _count_parts(msg.get("_anthropic_content_blocks"), {"image"})
    # Multimodal tool results that haven't been converted yet.
    if isinstance(content, dict) and content.get("_multimodal"):
        count += _count_parts(content.get("content"), {"image", "image_url"})
    return count * cost_per_image


def strip_opaque_replay_items(items: Any) -> Any:
    """``codex_reasoning_items`` with ``encrypted_content`` blanked for local token estimation.
    The ciphertext is priced by the provider's own count, never by its bytes (a compaction
    checkpoint alone can be 5M chars, #100611); only real usage prices it."""
    if not isinstance(items, list):
        return items
    return [
        {k: ("" if k == "encrypted_content" else v) for k, v in item.items()} if isinstance(item, dict) else item
        for item in items
    ]


def _wire_message_shadow(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Shadow of a message holding only what the provider actually receives.
    * ``api_content`` SUBSTITUTES ``content`` (mirrors ``turn_context.substitute_api_content`` exactly):
      only a non-empty STRING sidecar on a user/assistant row displaces content; substituting any
      other shape would UNDERcount — the dangerous direction.
    * Base64 images become a placeholder; ``_count_image_tokens`` charges them flat.
    * ``reasoning`` never ships as-is (request builds pop it after optionally promoting it into
      ``reasoning_content``); counting both inflated estimates up to +53%.
    * Opaque provider blobs (``encrypted_content`` on codex reasoning / compaction items) are
      ciphertext the provider prices by its OWN token count, never by bytes; a native compaction
      checkpoint alone can be 5M chars (#100611). They contribute 0 here: only real usage ever
      prices them, and the usage anchor carries that price forward."""
    sidecar = msg.get("api_content")
    sidecar_wins = isinstance(sidecar, str) and bool(sidecar) and msg.get("role") in ("user", "assistant")
    _rc = msg.get("reasoning_content")
    drop_reasoning_dup = isinstance(_rc, str) and bool(_rc.strip())
    shadow: Dict[str, Any] = {}
    for k, v in msg.items():
        if k in ("_anthropic_content_blocks", "reasoning_details") or k in PERSISTENCE_ONLY_MESSAGE_FIELDS or (k == "reasoning" and drop_reasoning_dup):
            continue
        if k == "api_content":
            if sidecar_wins:
                shadow["content"] = v
        elif k == "content" and sidecar_wins:
            continue
        elif k == "content" and isinstance(v, list):
            shadow[k] = [
                {"type": part.get("type"), "image": "[stripped]"}
                if isinstance(part, dict) and part.get("type") in {"image", "image_url", "input_image"} else part
                for part in v
            ]
        elif k == "content" and isinstance(v, dict) and v.get("_multimodal"):
            shadow[k] = v.get("text_summary", "")
        elif k == "codex_reasoning_items":
            shadow[k] = strip_opaque_replay_items(v)
        elif k == "encrypted_content":  # a Responses reasoning/compaction item passed as a row
            shadow[k] = ""
        else:
            shadow[k] = v
    return shadow


def _estimate_message_tokens_without_images(msg: Dict[str, Any]) -> int:
    """Token estimate for a message shadow with image payloads stripped."""
    return estimate_tokens_rough(str(_wire_message_shadow(msg) if isinstance(msg, dict) else msg))


def estimate_request_tokens_rough(
    messages: List[Dict[str, Any]], *, system_prompt: str = "", tools: Optional[List[Dict[str, Any]]] = None, charge_stale_thinking: bool = True,
) -> int:
    """Rough token estimate for a full request: system prompt + messages + tool schemas (50+ tools
    add 20-30K on their own). ``charge_stale_thinking`` is forwarded — pass False when the route
    provably strips stale thinking (``message_sanitization.stale_thinking_reaches_wire``)."""
    total = estimate_tokens_rough(system_prompt) if system_prompt else 0
    if messages:
        # Positional call: test seams and plugin engines monkeypatch estimate_messages_tokens_rough with (messages)-only signatures.
        total += estimate_messages_tokens_rough(messages) if charge_stale_thinking else estimate_messages_tokens_rough(messages, charge_stale_thinking=False)
    if tools:
        total += _estimate_tools_tokens_rough(tools)
    return total


_TOOLS_TOKENS_CACHE: dict[int, Tuple[int, str, str, int]] = {}


_TOOLS_TOKENS_CACHE_MAX = 256


def _tool_name_for_cache(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    name = fn.get("name") if isinstance(fn, dict) else None
    name = name if isinstance(name, str) else tool.get("name")
    return name if isinstance(name, str) else ""


def _estimate_tools_tokens_rough(tools: List[Dict[str, Any]]) -> int:
    if not tools:
        return 0
    key = id(tools)
    signature = (len(tools), _tool_name_for_cache(tools[0]), _tool_name_for_cache(tools[-1]))
    cached = _TOOLS_TOKENS_CACHE.get(key)
    if cached is not None and cached[:3] == signature:
        return cached[3]
    # Sum the major schema fields (descriptions + parameters dominate).
    total_chars = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        src = fn if isinstance(fn, dict) else tool
        params = src.get("parameters") or {}
        total_chars += sum(len(v) for v in (src.get("name") or "", src.get("description") or "") if isinstance(v, str))
        try:  # JSON is closer to wire size than repr()
            total_chars += len(json.dumps(params, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            total_chars += len(str(params))
    tokens = (total_chars + 3) // 4
    if len(_TOOLS_TOKENS_CACHE) >= _TOOLS_TOKENS_CACHE_MAX:
        _TOOLS_TOKENS_CACHE.pop(next(iter(_TOOLS_TOKENS_CACHE)), None)
    _TOOLS_TOKENS_CACHE[key] = (*signature, tokens)
    return tokens
