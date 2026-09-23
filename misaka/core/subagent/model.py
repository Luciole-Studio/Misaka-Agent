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


def _model_pair(model: Any) -> tuple[str, str] | None:
    provider, model_id = read_field(model, "provider"), read_field(model, "id")
    return (str(provider), str(model_id)) if provider and model_id else None


def resolve_model_spec(
    env: Mapping[str, str], call_model: str | None, definition_model: str | None,
    parent_model: Any, available_models: Sequence[Any], *, permission_mode: str | None = None,
) -> tuple[str, str]:
    """CCB env > tool > definition > inherit; host registry resolves tier IDs."""
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
        # CCB's pinned tier defaults, if present in the host registry. Do not
        # invent a model absent from a custom MISAKA provider's configuration.
        version = {"opus": "4-7", "sonnet": "4-6" if parent[0] == "anthropic" else "4-5", "haiku": "4-5"}[family]
        pool = same_provider
        resolved = next((pair for pair in pool if f"{family}-{version}" in pair[1]), pool[0])
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
        api = read_field(model, "api", "")
        model_id = str(read_field(model, "id", "")).lower()
        if api in {"anthropic-messages", "bedrock-converse-stream"}:
            supported = any(name in model_id for name in ("opus-4-7", "opus-4-6", "sonnet-4-6", "deepseek-v4-pro"))
            known_legacy = any(name in model_id for name in ("haiku", "sonnet", "opus"))
            if not supported and (known_legacy or api != "anthropic-messages"):
                return payload
            target = payload if api == "anthropic-messages" else payload.setdefault("additionalModelRequestFields", {})
            if "effort" in target.get("output_config", {}):
                return payload
            if isinstance(effort, str):
                target.setdefault("output_config", {})["effort"] = effort
                if api == "anthropic-messages":
                    payload.setdefault("betas", []).append("effort-2025-11-24")
            elif env.get("USER_TYPE") == "ant":
                internal_target = target.setdefault("extra_body", {}) if api == "anthropic-messages" else target
                internal_target.setdefault("anthropic_internal", {})["effort_override"] = effort
        elif api in {"openai-responses", "openai-codex-responses"}:
            payload.setdefault("reasoning", {}).setdefault("effort", effort if isinstance(effort, str) else "high")
        elif api == "openai-completions":
            payload.setdefault("reasoning_effort", effort if isinstance(effort, str) else "high")
        return payload

    agent.onPayload = on_payload
