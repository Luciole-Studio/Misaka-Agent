"""CCB model/agent.ts, model/bedrock.ts and explicit effort wire semantics.

Source pin: 77a7934e15d69da13879112ed7db695c9ee7a52a. MISAKA owns provider
registries and environment names; it never borrows another product's env state.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from misaka.core.prompt_templates import _ECMASCRIPT_WHITESPACE
from misaka.utils.values import maybe_await, read_field

REGION_PREFIXES = ("us", "eu", "apac", "global")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def bedrock_region_prefix(model_id: str) -> str | None:
    effective = model_id.rsplit("/", 1)[-1] if model_id.startswith("arn:") else model_id
    return next((prefix for prefix in REGION_PREFIXES if effective.startswith(f"{prefix}.anthropic.")), None)


def apply_bedrock_region_prefix(model_id: str, prefix: str) -> str:
    existing = bedrock_region_prefix(model_id)
    if existing:
        return model_id.replace(f"{existing}.", f"{prefix}.", 1)
    return f"{prefix}.{model_id}" if model_id.startswith("anthropic.") else model_id


_VERSION_PART = r"(\d{1,2})(?!\d)"


def _family_version(model_id: str, family: str) -> tuple[int, int]:
    """``(major, minor)`` of ``family`` in a model ID -- ``claude-sonnet-4-5``, ``claude-opus-5.5``,
    the older ``claude-3-7-sonnet`` -- else ``(-1, -1)``. A date is never a version."""
    lowered = model_id.lower()
    match = (re.search(rf"{family}[-.]{_VERSION_PART}(?:[-.]{_VERSION_PART})?", lowered)
             or re.search(rf"(?<![\d.]){_VERSION_PART}(?:[-.]{_VERSION_PART})?[-.]{family}", lowered))
    return (int(match[1]), int(match[2] or 0)) if match else (-1, -1)


def newest_in_family(pairs: Sequence[tuple[str, str]], family: str) -> tuple[str, str]:
    """The newest ``(provider, id)`` of ``family``. At one version an undated ID first (pi's
    ``isAlias``, which ``--model`` prefers too), then the shortest: a ``:batch`` or regional
    variant of the same model is not the model."""
    from misaka.core.model_resolver import isAlias

    return max(pairs, key=lambda pair: (_family_version(pair[1], family), isAlias(pair[1]), -len(pair[1])))


def _model_pair(model: Any) -> tuple[str, str] | None:
    provider, model_id = read_field(model, "provider"), read_field(model, "id")
    return (str(provider), str(model_id)) if provider and model_id else None


def resolve_model_spec(
    env: Mapping[str, str], call_model: str | None, definition_model: str | None,
    parent_model: Any, available_models: Sequence[Any], *, permission_mode: str | None = None,
) -> tuple[str, str]:
    """CCB env > tool > definition > inherit; a family alias resolves to the newest of the family in
    the parent provider's catalog (``newest_in_family``), not to CCB's pinned tier versions."""
    parent = _model_pair(parent_model)
    if parent is None:
        raise ValueError("The parent session has no model")
    override = env.get("MISAKA_SUBAGENT_MODEL")
    raw_spec = override or call_model or definition_model or "inherit"
    spec = raw_spec.strip("".join(_ECMASCRIPT_WHITESPACE))
    if not spec or spec.casefold() == "inherit":
        if parent[1] != "opusplan":
            return parent
        spec = "opus" if permission_mode == "plan" else "sonnet"
    available = [pair for item in available_models if (pair := _model_pair(item)) is not None]
    lowered = spec.casefold()
    if not override and raw_spec.lower() in {"opus", "sonnet", "haiku"} and raw_spec.lower() in parent[1].lower():
        return parent
    family = lowered.removesuffix("[1m]").strip()
    if family in {"opus", "sonnet", "haiku", "best", "opusplan"}:
        family = {"best": "opus", "opusplan": "sonnet"}.get(family, family)
        same_provider = [pair for pair in available if pair[0] == parent[0] and family in pair[1].lower()]
        if not same_provider:
            # CCB getProviderPrimaryModel: a third-party primary model is the
            # fallback for tier helpers, never another provider's credentials
            # or endpoint. Native parent model is that configured primary.
            if parent[0] not in {"anthropic", "amazon-bedrock", "bedrock"}:
                return parent[0], parent[1] + ("[1m]" if lowered.endswith("[1m]") and lowered != "best[1m]" else "")
            raise ValueError(f"No available model for provider {parent[0]} matches alias: {spec}")
        # The newest of the family in this provider's catalog -- MISAKA's own: models.json,
        # refreshed provider catalogs, the built-ins a release brings -- never a model it does
        # not list. CCB pins its tier versions, and on a provider that also listed 5.5 they
        # chose claude-opus-4-7 and dated 4-5 Sonnet and Haiku (2026-10-25, R19).
        resolved = newest_in_family(same_provider, family)
        if lowered.endswith("[1m]") and lowered != "best[1m]":
            resolved = resolved[0], resolved[1] + "[1m]"
    else:
        # CCB parseUserSpecifiedModel normalizes only the trailing context tag.
        if lowered.endswith("[1m]"):
            spec = spec[:-4].strip("".join(_ECMASCRIPT_WHITESPACE)) + "[1m]"
        # A Bedrock :0 suffix or ARN is not MISAKA's provider:model shortcut.
        providers = {pair[0] for pair in available} | {parent[0]}
        if "/" in spec and spec.split("/", 1)[0] in providers:
            resolved = tuple(spec.split("/", 1))
        elif ":" in spec and spec.split(":", 1)[0] in providers:
            resolved = tuple(spec.split(":", 1))
        else:
            # A bare ID stays on the parent endpoint even when another
            # provider happens to list the same ID first. provider/ID remains
            # the native explicit cross-provider selection syntax.
            resolved = parent[0], spec
    region = bedrock_region_prefix(parent[1])
    if not override and region and resolved[0] in {"amazon-bedrock", "bedrock"} and not bedrock_region_prefix(spec):
        return resolved[0], apply_bedrock_region_prefix(resolved[1], region)
    return resolved



def normalize_model_for_api(model_id: str) -> str:
    """CCB normalizeModelStringForAPI; context tags are not provider IDs.

    The native registry remains authoritative for contextWindow/capabilities;
    annotations must not create a fabricated registry model or leak to the API.
    """
    return re.sub(r"\[(1|2)m\]", "", model_id, flags=re.IGNORECASE)


def _as_js_number(value: float) -> float:
    # JSON/YAML numbers share the source's IEEE-754 Number domain.
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def parse_decimal_prefix(value: Any) -> float | None:
    """Source parseInt(String(value), 10), for persisted JSON/YAML values.

    Array String joins with commas: only the first member can contribute a
    numeric prefix. This avoids inventing a general JS String implementation.
    Empty/recursive arrays, booleans and objects yield no numeric prefix.
    Infinity is retained here; callers apply their source-specific validity test.
    """
    seen = set()
    while isinstance(value, list):
        if not value or id(value) in seen:
            return None
        seen.add(id(value))
        value = value[0]
    if type(value) in (int, float):
        number = _as_js_number(value)
        if not math.isfinite(number):
            return None  # String(Infinity/NaN) has no decimal prefix.
        if number == 0 or 1e-6 <= abs(number) < 1e21:
            return float(math.trunc(number))
        # JS String uses exponential notation outside this range; parseInt
        # reads only the leading mantissa integer (e.g. 1e-7 -> 1).
        value = repr(number)
    if not isinstance(value, str):
        return None
    match = re.match(r"[+-]?[0-9]+", value.lstrip(''.join(_ECMASCRIPT_WHITESPACE)))
    return float(match[0]) if match else None


def parse_effort(value: Any) -> str | int | None:
    """CCB effort.ts parseEffortValue/isValidNumericEffort, shared by both hosts."""
    if type(value) in (int, float):
        number = _as_js_number(value)
        if math.isfinite(number) and number.is_integer():
            return int(number)
    # String([['HIGH']]) is HIGH; multi-element arrays include a comma and
    # never match a level. Do not trim named levels: upstream does not.
    level, seen = value, set()
    while isinstance(level, list) and len(level) == 1 and id(level) not in seen:
        seen.add(id(level))
        level = level[0]
    if isinstance(level, str) and level.lower() in EFFORT_LEVELS:
        return level.lower()
    number = parse_decimal_prefix(value)
    return int(number) if number is not None and math.isfinite(number) and number.is_integer() else None


def install_effort_adapter(agent: Any, env: Mapping[str, str]) -> None:
    """Keep effort independent of thinking; adapt only known provider wires.

    Explicit output_config.effort from an existing callback retains priority
    (CCB configureEffortParams). Numeric Claude overrides replace the private
    effort_override field, matching the source object spread. Numeric values
    remain ant-only. Other MISAKA providers keep their native request contract.
    The effort goes where pi itself puts a model's effort, and only into a request
    that carries no reasoning control of pi's (``_add_effort``).
    """
    from misaka.config.product import setting

    raw = setting("subagents", "effort_level", None)          # the user's choice; "auto" = none
    if isinstance(raw, str) and raw.casefold() in {"unset", "auto"}:
        return
    if raw is None:
        try:
            raw = json.loads(env.get("MISAKA_SUBAGENT_EFFORT", "null"))   # the parent's hand-off
        except ValueError:
            return
    effort = parse_effort(raw)
    if effort is None:
        return
    previous = getattr(agent, "onPayload", None)

    async def on_payload(payload: dict[str, Any], model: Any) -> dict[str, Any]:
        if previous is not None:
            result = await maybe_await(previous(payload, model))
            if result is not None:
                payload = result
        return _add_effort(payload, model, effort, env)

    agent.onPayload = on_payload


# What pi's openai-completions request carries a model's reasoning in, by compat `thinkingFormat`: toggles
# (`thinking`, `enable_thinking`, `reasoning.enabled`, chat-template values) and effort fields (`reasoning_effort`,
# `reasoning.effort`, a string `thinking`). A request that already holds one of them gets no second.
_COMPLETIONS_REASONING_FIELDS = ("thinking", "reasoning_effort", "reasoning", "enable_thinking",
                                 "chat_template_kwargs", "chat_template_args")
# The formats whose effort pi sends as `reasoning_effort` (with `supportsReasoningEffort`), and those whose effort
# sits elsewhere. `chat-template`/`qwen-chat-template` carry it in configured template values: nothing to add there.
_REASONING_EFFORT_FORMATS = frozenset({"openai", "zai", "qwen", "baseten", "deepseek", "together"})


def _effort_level(model: Any, effort: str | int) -> str | None:
    """The sub-agent effort as one of pi's thinking levels for this model: clamped to the levels the catalog gives it
    (``clampThinkingLevel``, as pi clamps a session's level), a numeric effort as "high". None when that is "off"."""
    from misaka.ai.models import clampThinkingLevel

    level = clampThinkingLevel(model, effort if isinstance(effort, str) else "high")
    return None if level == "off" else level


def _add_effort(payload: dict[str, Any], model: Any, effort: str | int, env: Mapping[str, str]) -> dict[str, Any]:
    """Add the sub-agent's effort to a request pi built, in the field pi itself uses for this model's effort, and only
    where pi sent no reasoning control of its own: never a second control next to pi's (a thinking toggle or an
    effort), and nothing at all for a model that does not reason. pi's own control, which follows the session's
    thinking level, always wins. The one addition next to a pi control: Claude's `output_config.effort` beside
    Anthropic's `thinking: disabled`, where effort is not a reasoning switch (see the anthropic-messages branch)."""
    if not read_field(model, "reasoning", False):
        return payload
    api = read_field(model, "api", "")
    level = _effort_level(model, effort)
    if api == "anthropic-messages":
        from misaka.ai.api._common import _compat_value
        from misaka.ai.api.anthropic_messages import _mapThinkingLevelToEffort

        # pi's effort models are the adaptive-thinking ones (`forceAdaptiveThinking`): with thinking on it sends
        # `thinking: adaptive` and `output_config.effort` itself, so pi's effort stands. With thinking off it sends
        # `thinking: disabled` or nothing. Nothing: the effort is added. `thinking: disabled`: only for Claude
        # (Anthropic, and the gateways that pass Anthropic's request through), as CCB adds it next to its own
        # disabled thinking (a sub-agent always runs with thinking off): Anthropic's effort also governs text and
        # tool calls and does not turn thinking on. An Anthropic-compatible service maps both onto its own reasoning
        # switch (Fireworks: `disabled` is `reasoning_effort: "none"`, `output_config.effort` is `reasoning_effort`),
        # so there an added effort would turn the reasoning the sub-agent runs without back on. Third-party models
        # without adaptive thinking (MiniMax, most of Fireworks, gateways) get no effort from pi, so none from here.
        thinking = payload.get("thinking")
        thinking_off = isinstance(thinking, dict) and thinking.get("type") == "disabled" and _is_claude(model)
        if _compat_value(model, "forceAdaptiveThinking") is not True or (thinking is not None and not thinking_off) \
                or "effort" in (payload.get("output_config") or {}):
            return payload
        if isinstance(effort, str):
            if level is not None:
                value = _mapThinkingLevelToEffort(model, level)
                if thinking_off and value in {"xhigh", "max"} and _disabled_thinking_only_at_high(model):
                    value = "high"
                payload.setdefault("output_config", {})["effort"] = value
        elif env.get("USER_TYPE") == "ant":
            payload.setdefault("extra_body", {}).setdefault("anthropic_internal", {})["effort_override"] = effort
    elif api == "bedrock-converse-stream":
        from misaka.ai.api import bedrock_converse_stream as bedrock

        fields = payload.get("additionalModelRequestFields") or {}
        # pi sends `output_config.effort` to the adaptive-thinking Claude models only, beside their `thinking`.
        if not (bedrock._isAnthropicClaudeModel(model)
                and bedrock._supportsAdaptiveThinking(model.id, read_field(model, "name"))) \
                or "thinking" in fields or "effort" in (fields.get("output_config") or {}):
            return payload
        if isinstance(effort, str):
            if level is None:
                return payload
            addition = {"output_config": {**(fields.get("output_config") or {}),
                                          "effort": bedrock._mapThinkingLevelToEffort(model, level)}}
        elif env.get("USER_TYPE") == "ant":
            addition = {"anthropic_internal": {**(fields.get("anthropic_internal") or {}), "effort_override": effort}}
        else:
            return payload
        # pi leaves `additionalModelRequestFields` None when it has nothing for it.
        payload["additionalModelRequestFields"] = {**fields, **addition}
    elif api in {"openai-responses", "openai-codex-responses"}:
        # pi sends `reasoning` (an effort, or "none" with thinking off) to every reasoning model it can turn off.
        if "reasoning" not in payload and level is not None:
            payload["reasoning"] = {"effort": _mapped(model, level)}
    elif api == "openai-completions":
        from misaka.ai.api.openai_completions import _getCompat

        if level is None or any(field in payload for field in _COMPLETIONS_REASONING_FIELDS):
            return payload
        compat = _getCompat(model)
        thinkingFormat = compat["thinkingFormat"]
        if thinkingFormat in _REASONING_EFFORT_FORMATS:
            # kimi-for-coding-highspeed (deepseek format) and Xiaomi MiMo (pi 1.3.0 #10818) refuse it.
            if compat["supportsReasoningEffort"]:
                payload["reasoning_effort"] = _mapped(model, level)
        elif thinkingFormat == "openrouter":
            payload["reasoning"] = {"effort": _mapped(model, level)}
        elif thinkingFormat == "ant-ling":
            mapped = (read_field(model, "thinkingLevelMap") or {}).get(level)
            if isinstance(mapped, str):
                payload["reasoning"] = {"effort": mapped}
        elif thinkingFormat == "string-thinking":
            payload["thinking"] = _mapped(model, level)
    return payload


# pi's generate-models `ANTHROPIC_DISABLED_THINKING_MODELS`: the Claude models that think by default yet accept
# `thinking: {type: "disabled"}` -- Anthropic's thinking guide: "at `high` effort or below"; `xhigh` or `max` with
# disabled thinking is a 400 error. (The other 5.x models reject `disabled` outright, and pi never sends it to them;
# the 4.x models and Sonnet 5 turn thinking off at any effort.)
_DISABLED_THINKING_AT_HIGH_OR_BELOW = frozenset({"claude-opus-5", "claude-haiku-5-5", "claude-haiku-5.5"})


def _is_claude(model: Any) -> bool:
    """A Claude model, by its id: Anthropic's own ids and the gateways' (``anthropic/claude-...``)."""
    return "claude" in str(read_field(model, "id", "")).lower()


def _disabled_thinking_only_at_high(model: Any) -> bool:
    """pi's ``supportsAnthropicDisabledThinkingWithMidConvoEffort`` id test: its OpenRouter ``anthropic/`` prefix and a
    dated snapshot suffix stripped. MISAKA: also a gateway's ``-batch``/``-fast`` variant of the same model (Vercel AI
    Gateway)."""
    model_id = re.sub(r"^~?anthropic/", "", str(read_field(model, "id", "")).lower(), count=1)
    model_id = re.sub(r"-[0-9]{8}\Z", "", model_id, count=1)
    model_id = re.sub(r"-(?:batch|fast)\Z", "", model_id, count=1)
    return model_id in _DISABLED_THINKING_AT_HIGH_OR_BELOW


def _mapped(model: Any, level: str) -> Any:
    """``model.thinkingLevelMap?.[level] ?? level``, as pi's providers write a level onto the wire."""
    mapped = (read_field(model, "thinkingLevelMap") or {}).get(level)
    return level if mapped is None else mapped
