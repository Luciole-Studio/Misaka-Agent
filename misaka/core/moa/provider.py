"""Mixture-of-Agents virtual provider adapted from Hermes MoA."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    SimpleStreamOptions,
    StartEvent,
    TextContent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import provider_headers_to_record
from misaka.config.product import current_config
from misaka.core.moa.privacy import (
    coerce_privacy_filter,
    redact_advisor_text,
    redact_outputs,
)

MOA_CONFIG_PATH = "~/.misaka/moa.json"
DEFAULT_MOA_PRESET_NAME = "default"

def _default_slots():
    """Follow the current product model settings; override these in ~/.misaka/moa.json."""
    cfg = current_config()
    return (
        [
            {"provider": cfg["provider"], "model": cfg["lo_model"]},
            {"provider": cfg["provider"], "model": cfg["default_model"]},
        ],
        {"provider": cfg["provider"], "model": cfg["lo_model"]},
    )

# Head+tail preview budget per tool result in the advisor view.
TOOL_RESULT_BUDGET = 4000
# Context-window headroom reserved for advisor output when reference_max_tokens is unset.
REFERENCE_DEFAULT_OUTPUT_RESERVE = 8192
# Safety margin for the chars/4 token estimate.
TRIM_SAFETY_FRACTION = 0.10
MAX_REFERENCE_CONCURRENCY = 8   # hermes _MAX_REFERENCE_WORKERS

# Advisor rules, adapted from Hermes' reference-model system prompt.
REFERENCE_SYSTEM_PROMPT = """You are an advisor model in a Mixture-of-Agents (MoA) process. You are not the acting agent and you execute nothing:
you cannot call tools, run commands, browse the web, or open files, repositories, or links. Do not try, and do not apologize for it.
A separate aggregator model holds those capabilities and takes the actions.

Hard rule: never claim or imply that you performed an action (ran a command, downloaded a file, visited a link).
You may only analyze and advise based on the conversation context. For example:
- Wrong: "I ran curl and got a 404."
- Right: "Given the shape of that error, a curl against that link will most likely return a 404."

The conversation below is the current state of the task the acting agent is working on. Your job is to give your best
analysis of the situation: understand the goal, reason about the problem, and recommend the next step. Point out the best
route, concrete steps and tool strategy, likely pitfalls and risks, and anything the acting agent may have missed or gotten
wrong. Assume any files, links, or systems mentioned in the conversation exist; reason from context and do not ask for access.

Give the advice directly, with no preamble and no disclaimers. Your reply is private input for the aggregator, not an answer
shown to the user. Never claim to have executed anything."""

# Synthetic closing user turn: the advisor view must end with a user message without dropping the latest context.
ADVISORY_INSTRUCTION = (
    "[The conversation above is the current state of the task. Give your best judgment: what is happening, "
    "what should happen next, what risks or mistakes you see, and how the acting agent should proceed.]"
)


# Configuration: lenient loading, recursive MoA slots rejected.


def _coerce_float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int_or_none(value):
    if value is None or value == "":
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _coerce_fanout(value) -> str:
    """Normalize advisor cadence to per_iteration, user_turn, or every_n:N."""
    if isinstance(value, dict):
        mode = str(value.get("mode") or "").strip().lower()
        if mode == "every_n":
            n = _coerce_int_or_none(value.get("n")) or 0
            if n >= 2:
                return f"every_n:{n}"
            return "per_iteration" if n == 1 else "user_turn"
        value = mode
    mode = str(value or "").strip().lower()
    if mode in {"per_iteration", "user_turn"}:
        return mode
    if mode.startswith("every_n"):
        _, sep, rest = mode.partition(":")
        n = (_coerce_int_or_none(rest.strip()) or 0) if sep else 0
        if n >= 2:
            return f"every_n:{n}"
        if n == 1:
            return "per_iteration"
    return "user_turn"


_THINKING_LEVELS = {"minimal", "low", "medium", "high", "xhigh", "max"}


def _clean_slot(slot, *, include_enabled=False):
    """Normalize a model slot, rejecting invalid or recursive MoA slots."""
    if not isinstance(slot, dict):
        return None
    provider = str(slot.get("provider") or "").strip()
    model = str(slot.get("model") or "").strip()
    if not provider or not model or provider.lower() == "moa":
        return None
    clean: dict[str, Any] = {"provider": provider, "model": model}
    effort = str(slot.get("reasoning_effort") or "").strip().lower()
    if effort in _THINKING_LEVELS:
        clean["reasoning_effort"] = effort
    mt = _coerce_int_or_none(slot.get("max_tokens"))
    if mt is not None:
        clean["max_tokens"] = mt
    if include_enabled:
        clean["enabled"] = slot.get("enabled") is not False
    return clean


def _normalize_preset(raw) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    default_refs, default_aggregator = _default_slots()
    raw_refs = raw.get("reference_models")
    if isinstance(raw_refs, str):
        try:
            raw_refs = json.loads(raw_refs)
        except (json.JSONDecodeError, ValueError):
            raw_refs = []
    if not isinstance(raw_refs, list):
        raw_refs = [raw_refs] if isinstance(raw_refs, dict) else []
    refs = [s for s in (_clean_slot(x, include_enabled=True) for x in raw_refs) if s]
    if not refs:
        refs = [{**s, "enabled": True} for s in default_refs]
    policy = str(raw.get("degraded_reference_policy") or "loud").strip().lower()
    timeout = _coerce_float_or_none(raw.get("reference_timeout"))
    return {
        "enabled": raw.get("enabled") is not False,
        "reference_models": refs,
        "aggregator": _clean_slot(raw.get("aggregator")) or default_aggregator,
        "reference_temperature": _coerce_float_or_none(raw.get("reference_temperature")),
        "aggregator_temperature": _coerce_float_or_none(raw.get("aggregator_temperature")),
        "reference_timeout": timeout if timeout and timeout > 0 else None,
        "degraded_reference_policy": policy if policy in {"loud", "silent"} else "loud",
        "reference_max_tokens": _coerce_int_or_none(raw.get("reference_max_tokens")),
        "fanout": _coerce_fanout(raw.get("fanout")),
    }


def normalize_moa_config(raw) -> dict[str, Any]:
    """Accept named `presets` + `default_preset`; a config without presets gets one default preset."""
    if not isinstance(raw, dict):
        raw = {}
    presets: dict[str, dict[str, Any]] = {}
    presets_raw = raw.get("presets")
    if isinstance(presets_raw, dict):
        for name, preset in presets_raw.items():
            clean = str(name or "").strip()
            if clean:
                presets[clean] = _normalize_preset(preset)
    if not presets:
        presets[DEFAULT_MOA_PRESET_NAME] = _normalize_preset({})
    default = str(raw.get("default_preset") or "").strip()
    if not default or default not in presets:
        default = next(iter(presets))
    return {
        "default_preset": default,
        "presets": presets,
        # MoA-level, not per-preset, like save_traces: '' (off) | display | full.
        "privacy_filter": coerce_privacy_filter(raw.get("privacy_filter")),
        "save_traces": bool(raw.get("save_traces")),
        "trace_dir": str(raw.get("trace_dir") or "").strip() or None,
    }


def load_moa_config(path: str | None = None) -> dict[str, Any]:
    p = os.path.expanduser(path or MOA_CONFIG_PATH)
    try:
        with open(p, encoding="utf-8") as f:
            return normalize_moa_config(json.load(f))
    except (OSError, ValueError):
        return normalize_moa_config({})


def resolve_moa_preset(name: str | None = None, *, config=None) -> tuple[str, dict[str, Any]]:
    cfg = config if config is not None else load_moa_config()
    wanted = str(name or cfg["default_preset"]).strip()
    preset = cfg["presets"].get(wanted)
    if preset is None:
        wanted = cfg["default_preset"]
        preset = cfg["presets"][wanted]
    return wanted, deepcopy(preset)


def slot_label(slot) -> str:
    label = f"{slot.get('provider', '')}:{slot.get('model', '')}"
    effort = str(slot.get("reasoning_effort") or "").strip()
    return f"{label}[reasoning={effort}]" if effort else label


# Advisor view: the whole conversation flattened to plain-text user/assistant turns.


def _flatten_user_content(content) -> str:
    if isinstance(content, str):
        return content
    parts = [p.text for p in content if getattr(p, "type", "") == "text"]
    text = "\n".join(t for t in parts if t)
    if not text.strip() and content:
        return "[User supplied non-text content, such as an image attachment]"
    return text


def _truncate_tool_result(text: str, budget: int = TOOL_RESULT_BUDGET) -> str:
    if not text or len(text) <= budget:
        return text
    half = budget // 2
    return f"{text[:half]}\n[… {len(text) - 2 * half} characters omitted …]\n{text[-half:]}"


def advisory_view(messages) -> list[dict[str, str]]:
    """Convert typed session messages into the plain-text view sent to advisors."""
    rendered: list[dict[str, str]] = []
    last_user = None
    for m in messages:
        role = getattr(m, "role", None)
        if role == "user":
            text = _flatten_user_content(m.content)
            if not text.strip():
                continue
            last_user = text
            rendered.append({"role": "user", "content": text})
        elif role == "assistant":
            parts: list[str] = []
            for c in m.content:
                if c.type == "text" and c.text.strip():
                    parts.append(c.text.strip())
                elif c.type == "toolCall":
                    try:
                        args = json.dumps(c.arguments, ensure_ascii=False)
                    except (TypeError, ValueError):
                        args = str(c.arguments)
                    parts.append(f"[Tool call: {c.name}({args})]")
            if parts:
                rendered.append({"role": "assistant", "content": "\n".join(parts)})
        elif role == "toolResult":
            text = _truncate_tool_result(_flatten_user_content(m.content))
            block = f"[Tool result: {text}]"
            if rendered and rendered[-1]["role"] == "assistant":
                rendered[-1]["content"] += "\n" + block
            else:
                rendered.append({"role": "assistant", "content": block})
    if rendered and rendered[-1]["role"] == "assistant":
        rendered.append({"role": "user", "content": ADVISORY_INSTRUCTION})
    if not rendered and last_user is not None:
        rendered = [{"role": "user", "content": last_user}]
    return rendered


def trim_view_for_window(view, context_window: int, *, reserve: int | None = None):
    """Trim the oldest advisor context to fit the selected model's context window."""
    if not view or context_window <= 0:
        return view
    reserve = reserve if reserve and reserve > 0 else REFERENCE_DEFAULT_OUTPUT_RESERVE
    budget = int(context_window * (1.0 - TRIM_SAFETY_FRACTION)) - reserve
    if budget <= 0:
        return view

    def estimate(msgs):
        return (len(REFERENCE_SYSTEM_PROMPT) + sum(len(m["content"]) for m in msgs)) // 4

    body = list(view)
    while len(body) > 2 and estimate(body) > budget:
        body.pop(0)
        while len(body) > 2 and body[0]["role"] == "assistant":
            body.pop(0)
    while len(body) > 1 and body[0]["role"] == "assistant":
        body.pop(0)
    return body


# Slot-model resolution is injected by core so the AI layer never imports core.

_MODEL_RESOLVER: Callable[[str, str], Model | None] | None = None


def set_model_resolver(fn: Callable[[str, str], Model | None]) -> None:
    global _MODEL_RESOLVER
    _MODEL_RESOLVER = fn


_AUTH_RESOLVER: Any = None


def set_auth_resolver(fn) -> None:
    """Bind the session's ``modelRegistry.getAuth`` so each slot can be paid for on its own.

    A mixture calls models from providers the session itself is not using -- the session's
    model *is* ``moa``, whose api key is the virtual provider's placeholder. Sending that
    to a real endpoint is a 401, so every advisor and the aggregator resolve their own
    provider's credentials here, the way ``core/sdk``'s stream function does for a turn.
    """
    global _AUTH_RESOLVER
    _AUTH_RESOLVER = fn


async def _slot_auth(model: Model) -> tuple[Model, dict[str, Any]]:
    """``(model to call, request options)`` for one slot: its own key, headers and baseUrl."""
    if _AUTH_RESOLVER is None:
        return model, {}
    try:
        resolution = await _AUTH_RESOLVER(model)
    except Exception:  # noqa: BLE001 - an unresolvable slot fails on its own call, with the provider's message
        return model, {}
    if resolution is None:
        return model, {}
    auth = resolution.auth
    if auth.baseUrl:
        model = model.model_copy(update={"baseUrl": auth.baseUrl})
    return model, {
        "apiKey": auth.apiKey,
        "headers": provider_headers_to_record(auth.headers),
        "env": resolution.env,
    }


def _resolve_slot_model(slot) -> Model | None:
    if _MODEL_RESOLVER is None:
        return None
    try:
        return _MODEL_RESOLVER(str(slot.get("provider") or ""), str(slot.get("model") or ""))
    except Exception:  # noqa: BLE001
        return None


# ── Fan-out and aggregation ──────────────────────────────────────────────────


def _typed_view(view) -> list[Any]:
    now = time.time_ns() // 1_000_000
    out: list[Any] = []
    for m in view:
        if m["role"] == "user":
            out.append(UserMessage(content=m["content"], timestamp=now))
        else:
            out.append(AssistantMessage(
                content=[TextContent(text=m["content"])],
                api="moa", provider="moa", model="advisory-view",
                usage=_zero_usage(), stopReason="stop", timestamp=now))
    return out


def _zero_usage() -> Usage:
    return Usage(input=0, output=0, cacheRead=0, cacheWrite=0, totalTokens=0,
                 cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0))


def _message_text(message: AssistantMessage) -> str:
    return "\n".join(c.text for c in message.content if c.type == "text").strip()


async def _run_reference(slot, view, *, preset, options) -> tuple[str, str, Usage | None, list | None]:
    """Run one advisor and return its label, text, usage, and complete input trace."""
    from misaka.ai.stream import complete_simple

    label = slot_label(slot)
    m = _resolve_slot_model(slot)
    if m is None:
        return label, f"[failed: model slot is unavailable: {label}]", None, None
    m, auth = await _slot_auth(m)
    reserve = slot.get("max_tokens") or preset.get("reference_max_tokens")
    trimmed = trim_view_for_window(view, m.contextWindow, reserve=reserve)
    ctx = Context(systemPrompt=REFERENCE_SYSTEM_PROMPT, messages=_typed_view(trimmed))
    timeout = preset.get("reference_timeout")
    opts = SimpleStreamOptions(
        temperature=preset.get("reference_temperature"),
        maxTokens=reserve,
        # Advisors use only their configured reasoning depth, not the session default.
        reasoning=slot.get("reasoning_effort"),
        timeoutMs=int(timeout * 1000) if timeout else None,
        signal=getattr(options, "signal", None),
        sessionId=getattr(options, "sessionId", None),
        cacheRetention=getattr(options, "cacheRetention", None),
        **auth,
    )
    sent = [{"role": "system", "content": REFERENCE_SYSTEM_PROMPT}, *trimmed]
    try:
        msg = await complete_simple(m, ctx, opts)
    except Exception as exc:  # noqa: BLE001 - one advisor failure must not abort the turn
        return label, f"[failed: {exc}]", None, sent
    if msg.stopReason == "error":
        return label, f"[failed: {msg.errorMessage or 'unknown error'}]", msg.usage, sent
    return label, _message_text(msg) or "(empty response)", msg.usage, sent


def _is_failed(text: str) -> bool:
    s = text.lstrip().lower()
    return s.startswith(("[failed:", "[skipped:"))


def build_guidance(preset_name, preset, outputs, privacy="") -> str | None:
    """Build the private advisor block injected into the aggregator prompt.

    ``privacy="full"`` redacts the advice on the way in, which is the one mode that can
    change the answer: the aggregator reasons from redacted text (Hermes moa_loop, the
    literal ask of its issue #59959). ``display`` deliberately does not reach here.
    """
    if privacy == "full":
        outputs = redact_outputs(list(outputs))
    ok = [(label, text) for label, text, _u in outputs if not _is_failed(text)]
    failed = [label for label, text, _u in outputs if _is_failed(text)]
    degraded = ""
    if failed and preset.get("degraded_reference_policy") != "silent":
        degraded = f"[Unavailable advisors: {', '.join(failed)}]"
    agg_label = slot_label(preset["aggregator"])
    if outputs and not ok:
        if not degraded:
            return None
        return ("[MoA advisor context]\n"
                f"Preset: {preset_name}\nAggregator/action model: {agg_label}\n"
                "All advisors failed this turn. There is no reference advice; act on your own judgment.\n\n"
                + degraded)
    if not ok and not degraded:
        return None
    joined = "\n\n".join(
        f"Advisor {i} — {label}\n{text}" for i, (label, text) in enumerate(ok, 1)
    )
    if degraded:
        joined = f"{joined}\n\n{degraded}" if joined else degraded
    return ("[MoA advisor context]\n"
            f"Preset: {preset_name}\nAggregator/action model: {agg_label}\n"
            f"Advisors: {', '.join(label for label, _ in ok)}\n\n"
            "The advice below is private input for you. You are both the aggregator and the action model: "
            "answer the user or call tools as you normally would.\n\n" + joined)


def attach_guidance(messages: list[Any], guidance: str) -> list[Any]:
    """Append advisor guidance to the final user turn while preserving the stable prefix."""
    now = time.time_ns() // 1_000_000
    if messages and getattr(messages[-1], "role", None) == "user":
        last = messages[-1]
        if isinstance(last.content, str):
            merged = last.model_copy(update={"content": last.content + "\n\n" + guidance})
        else:
            merged = last.model_copy(update={
                "content": [*last.content, TextContent(text="\n\n" + guidance)]})
        return [*messages[:-1], merged]
    return [*messages, UserMessage(content=guidance, timestamp=now)]


_TURN_STATE: dict[tuple[str, str], dict[str, Any]] = {}


def forget_session(session_id: str | None) -> None:
    """Drop one session's cached advisor outputs.

    Each entry holds the advisors' full text for cadence reuse; without this the table grew
    for the life of a long-running process.
    """
    key = str(session_id or "no-session")
    for cached in [item for item in _TURN_STATE if item[0] == key]:
        del _TURN_STATE[cached]


def _hash_view(view) -> str:
    return hashlib.sha256(
        "\u0000".join(f"{m['role']}:{m['content']}" for m in view).encode("utf-8", "replace")
    ).hexdigest()


def _cadence_decision(state, preset, view) -> tuple[bool, str]:
    """Decide whether the current turn may reuse cached advisor responses."""
    fanout = preset.get("fanout") or "user_turn"
    every_n = 0
    if fanout.startswith("every_n:"):
        try:
            every_n = int(fanout.split(":", 1)[1])
        except (TypeError, ValueError):
            every_n = 0
        if every_n < 2:
            fanout = "per_iteration"

    turn_prefix = view
    if fanout == "user_turn" or every_n >= 2:
        last_user_idx = None
        for i in range(len(view) - 1, -1, -1):
            if view[i]["role"] == "user" and view[i]["content"] != ADVISORY_INSTRUCTION:
                last_user_idx = i
                break
        if last_user_idx is not None:
            turn_prefix = view[: last_user_idx + 1]

    sig = _hash_view(turn_prefix if fanout == "user_turn" else view)
    if every_n >= 2:
        turn_sig = _hash_view(turn_prefix)
        if turn_sig != state.get("turn_sig"):
            state["turn_sig"] = turn_sig
            state["iteration"] = 0
            state["last_state_sig"] = None
        state_sig = _hash_view(view)
        if state_sig != state.get("last_state_sig"):
            state["last_state_sig"] = state_sig
            state["iteration"] = state.get("iteration", 0) + 1
        on_cadence = (state["iteration"] - 1) % every_n == 0
        if not on_cadence and state.get("outputs"):
            return True, state.get("sig") or sig
        return False, sig
    reuse = sig == state.get("sig") and bool(state.get("outputs"))
    return reuse, sig


# Optional sidecar traces (opt-in file on disk; never enters the message history).


def _save_trace(cfg, session_id, preset_name, advisor_traces, agg_slot,
                agg_input_messages, agg_output) -> None:
    """Append one JSONL trace record (advisor inputs/outputs plus aggregator input) when save_traces is on."""
    if not cfg.get("save_traces"):
        return
    try:
        base = os.path.expanduser(cfg.get("trace_dir") or "~/.misaka/moa-traces")
        os.makedirs(base, exist_ok=True)
        sid = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(session_id or "unknown-session"))
        record = {
            "ts": time.time(),
            "session_id": session_id,
            "preset": preset_name,
            "references": advisor_traces,
            "aggregator": {
                "label": slot_label(agg_slot),
                "input_messages": agg_input_messages,
                "output": agg_output,
                "output_location": "inline" if agg_output is not None else "assistant_message_in_session_db",
            },
        }
        with open(os.path.join(base, f"{sid}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001, S110 - a trace is a sidecar; losing one never breaks the call
        pass


def _redact_trace_messages(messages) -> Any:
    """Redact the advisor input a trace keeps: it is the conversation, verbatim.

    Hermes ``moa_loop._redact_trace_messages``. Both string content and content-part lists
    are handled; an unknown shape passes through rather than being mangled.
    """
    if not isinstance(messages, list):
        return messages
    out: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append({**message, "content": redact_advisor_text(content)})
        elif isinstance(content, list):
            out.append({**message, "content": [
                {**part, "text": redact_advisor_text(part["text"])}
                if isinstance(part, dict) and isinstance(part.get("text"), str) else part
                for part in content
            ]})
        else:
            out.append(message)
    return out


def _serialize_messages(messages) -> list[dict[str, Any]]:
    """Serialize typed messages for an auditable MoA trace."""
    out = []
    for m in messages:
        role = getattr(m, "role", "?")
        content = getattr(m, "content", None)
        if isinstance(content, str):
            out.append({"role": role, "content": content})
        else:
            parts = []
            for c in content or []:
                ctype = getattr(c, "type", "?")
                if ctype == "text":
                    parts.append({"type": "text", "text": c.text})
                elif ctype == "thinking":
                    parts.append({"type": "thinking", "chars": len(c.thinking)})
                elif ctype == "toolCall":
                    parts.append({"type": "toolCall", "name": c.name, "arguments": c.arguments})
                else:
                    parts.append({"type": ctype})
            out.append({"role": role, "content": parts})
    return out


# Virtual provider stream (the engine entry point).


def _fold_usage(base: Usage, extras: list[Usage | None], advisor_models: list[Model | None]) -> Usage:
    """Add each advisor's usage and model-specific cost to aggregator usage."""
    from misaka.ai.models import calculate_cost

    total = base.model_copy(deep=True)
    for usage, m in zip(extras, advisor_models):
        if usage is None:
            continue
        cost = usage.cost
        if m is not None and not (cost.total or 0):
            try:
                cost = calculate_cost(m, usage.model_copy(deep=True))
            except Exception:  # noqa: BLE001 - retain provider-reported cost on calculation failure
                cost = usage.cost
        total.input += usage.input
        total.output += usage.output
        total.cacheRead += usage.cacheRead
        total.cacheWrite += usage.cacheWrite
        total.totalTokens += usage.totalTokens
        total.cost.input += cost.input
        total.cost.output += cost.output
        total.cost.cacheRead += cost.cacheRead
        total.cost.cacheWrite += cost.cacheWrite
        total.cost.total += cost.total
    return total


def stream_simple_moa(model: Model, context: Context, options: SimpleStreamOptions | None = None) -> AssistantMessageEventStream:
    """Run advisor calls, inject their guidance, and stream the aggregator response."""
    from misaka.ai.stream import stream_simple

    outer = AssistantMessageEventStream()
    opts = options or SimpleStreamOptions()

    async def run() -> None:
        # Emit exactly one start event; a nested aggregator start would duplicate history.
        now_ms = time.time_ns() // 1_000_000
        shell = AssistantMessage(content=[ThinkingContent(thinking="")], api=model.api,
                                 provider=model.provider, model=model.id,
                                 usage=_zero_usage(), stopReason="stop", timestamp=now_ms)
        outer.push(StartEvent(partial=shell))
        thinking_text = ""

        def push_thinking(delta: str) -> None:
            nonlocal thinking_text, shell
            if not thinking_text:
                outer.push(ThinkingStartEvent(contentIndex=0, partial=shell))
            thinking_text += delta
            shell = shell.model_copy(update={"content": [ThinkingContent(thinking=thinking_text)]})
            outer.push(ThinkingDeltaEvent(contentIndex=0, delta=delta, partial=shell))

        try:
            cfg = load_moa_config()
            privacy = cfg.get("privacy_filter") or ""
            preset_name, preset = resolve_moa_preset(model.id, config=cfg)
            refs = [s for s in preset["reference_models"] if s.get("enabled", True)]
            if not preset.get("enabled", True):
                # A disabled preset runs the aggregator without advisors.
                refs = []

            view = advisory_view(context.messages)
            session_key = (str(getattr(opts, "sessionId", None) or "no-session"), preset_name)
            state = _TURN_STATE.setdefault(session_key, {})
            reuse, sig = _cadence_decision(state, preset, view) if refs else (False, "")

            outputs: list[tuple[str, str, Usage | None]] = []
            advisor_models: list[Model | None] = []
            advisor_traces: list[dict[str, Any]] = []
            fresh_fanout = False
            if refs and reuse:
                # Reused advice adds no new usage and is not repeated in the trace.
                outputs = [(label, text, None) for label, text, _u in state["outputs"]]
                advisor_models = [None] * len(outputs)
            elif refs:
                fresh_fanout = True
                sem = asyncio.Semaphore(MAX_REFERENCE_CONCURRENCY)

                async def guarded(idx, slot):
                    async with sem:
                        return idx, await _run_reference(slot, view, preset=preset, options=opts)

                # Stream each advisor response as it arrives.
                push_thinking(f"MoA·{preset_name}: dispatching {len(refs)} advisors…")
                slot_results: dict[int, tuple[str, str, Usage | None]] = {}
                for done_n, fut in enumerate(asyncio.as_completed([guarded(i, s) for i, s in enumerate(refs)]), 1):
                    idx, (label, text, usage, sent) = await fut
                    slot_results[idx] = (label, text, usage)
                    # Both surfaces a person or a file keeps: the advisor block shown in
                    # the thinking stream and the trace record. `display` covers them; the
                    # aggregator still reasons from the raw text unless the mode is `full`.
                    shown = redact_advisor_text(text) if privacy else text
                    advisor_traces.append({
                        "label": label,
                        "input_messages": _redact_trace_messages(sent) if privacy else sent,
                        "output": shown,
                        "usage": usage.model_dump() if usage else None})
                    push_thinking(
                        f"\n\n── Advisor {done_n}/{len(refs)} — {label} ──\n{shown}"
                    )
                outputs = [slot_results[i] for i in range(len(refs))]
                advisor_models = [_resolve_slot_model(s) for s in refs]
                state["sig"] = sig
                state["outputs"] = list(outputs)
                push_thinking("\n\nAll advisors have replied; the aggregator takes over.")
            if thinking_text:
                outer.push(ThinkingEndEvent(contentIndex=0, content=thinking_text, partial=shell))

            guidance = build_guidance(preset_name, preset, outputs, privacy) if outputs else None

            agg_slot = preset["aggregator"]
            if str(agg_slot.get("provider", "")).lower() == "moa":
                raise RuntimeError("An MoA aggregator cannot itself use the MoA provider.")
            agg_model = _resolve_slot_model(agg_slot)
            if agg_model is None:
                raise RuntimeError(f"Aggregator model is unavailable: {slot_label(agg_slot)}")

            agg_messages = list(context.messages)
            if guidance:
                agg_messages = attach_guidance(agg_messages, guidance)
            agg_ctx = Context(systemPrompt=context.systemPrompt, messages=agg_messages,
                              tools=context.tools)
            # The session's own credentials belong to `moa`, whose key is a placeholder;
            # the aggregator is a real model at a real provider and pays its own way.
            agg_model, agg_auth = await _slot_auth(agg_model)
            agg_opts = opts.model_copy(update={
                # A preset temperature overrides the session value.
                "temperature": preset.get("aggregator_temperature")
                if preset.get("aggregator_temperature") is not None else opts.temperature,
                # A slot-specific reasoning depth overrides the session value.
                "reasoning": agg_slot.get("reasoning_effort") or opts.reasoning,
                **agg_auth,
            })

            inner = stream_simple(agg_model, agg_ctx, agg_opts)
            usages = [u for _l, _t, u in outputs]
            final_text: str | None = None
            async for event in inner:
                etype = getattr(event, "type", "")
                if etype == "start":
                    continue
                if etype == "done":
                    event = event.model_copy(update={"message": event.message.model_copy(
                        update={"usage": _fold_usage(event.message.usage, usages, advisor_models)})})
                    final_text = _message_text(event.message)
                elif etype == "error":
                    event = event.model_copy(update={"error": event.error.model_copy(
                        update={"usage": _fold_usage(event.error.usage, usages, advisor_models)})})
                outer.push(event)
            outer.end()
            if fresh_fanout:
                _save_trace(cfg, getattr(opts, "sessionId", None), preset_name,
                            advisor_traces, agg_slot,
                            _serialize_messages(agg_messages), final_text)
        except Exception as exc:  # noqa: BLE001 - surface virtual-provider failures as an engine error event
            from misaka.ai.types import ErrorEvent
            message = AssistantMessage(
                content=[], api=model.api, provider=model.provider, model=model.id,
                usage=_zero_usage(), stopReason="error", errorMessage=str(exc),
                timestamp=time.time_ns() // 1_000_000)
            outer.push(ErrorEvent(reason="error", error=message))
            outer.end(message)

    spawn_stream_task(run())   # a bare create_task can be garbage-collected mid-stream
    return outer


# Configured presets exposed as virtual registry models.


def _registry_model(find, slot) -> Model | None:
    """The model registry's own entry for a slot, when the registry is the one asking."""
    if find is None:
        return None
    try:
        return find(str(slot.get("provider") or ""), str(slot.get("model") or ""))
    except Exception:  # noqa: BLE001 - a registry mid-rebuild answers for nothing; fall through
        return None


def _catalog_model(slot) -> Model | None:
    """The builtin catalog's entry for a slot, for sizes a registry lookup cannot supply.

    ``register_provider`` runs while the extension is being registered, which is before
    any session has injected a model resolver -- so ``_resolve_slot_model`` there is not
    "usually None", it is always None, and that one call fixes what every ``moa:<preset>``
    model advertises for the life of the process. The builtin catalog is the same table
    the registry merges its dynamic and custom models onto, so reading it directly answers
    for every aggregator that is a catalogued model, resolver or no resolver.
    """
    from misaka.ai.models import get_model

    return get_model(str(slot.get("provider") or ""), str(slot.get("model") or ""))


def preset_models(configured=None, find=None) -> list[Model]:
    """Build one virtual `moa` Model per configured preset, sized from its aggregator.

    With ``configured(provider_id) -> bool`` given (the registry's own credential check,
    models.json keys included), a preset whose aggregator's provider is not configured is
    left out: the registry lists what can run, and a bare setup stays empty.

    The sizes are not decoration: ``core/agent_session`` reads ``contextWindow`` for the
    auto-compaction threshold, the ``/context`` percentage and the footer. A preset whose
    aggregator holds 128k while its virtual model claims 200k does not compact until the
    turn is already over the provider's limit, and one whose aggregator holds 1M compacts
    long before it needs to.

    ``find(provider, model_id)`` is the registry asking on its own behalf, and it is the
    only lookup that answers for an aggregator defined in models.json: the builtin catalog
    has never heard of a custom provider, and no session has bound a resolver this early.
    """
    cfg = load_moa_config()
    out: list[Model] = []
    for name, preset in cfg["presets"].items():
        if configured is not None and not configured(str(preset["aggregator"].get("provider") or "")):
            continue
        # The registry first (it holds models.json's custom providers), then a
        # session-bound resolver, then the builtin catalog. The literals below stay the
        # last resort for an aggregator none of the three has heard of.
        agg = (_registry_model(find, preset["aggregator"])
               or _resolve_slot_model(preset["aggregator"])
               or _catalog_model(preset["aggregator"]))
        out.append(Model(
            id=name, name=f"MoA·{name}", api="moa", provider="moa",
            baseUrl="moa://local", reasoning=True, input=["text", "image"],
            cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            contextWindow=agg.contextWindow if agg else 200_000,
            maxTokens=agg.maxTokens if agg else 32_000,
        ))
    return out

