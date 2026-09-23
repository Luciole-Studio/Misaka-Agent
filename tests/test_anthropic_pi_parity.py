"""Anthropic reasoning/error contracts from Pi b03a367a4fbc02df81bfd96702d7a12c2d79aa45."""

import json
from unittest.mock import AsyncMock

import pytest

from misaka.ai.models import (
    clamp_thinking_level,
    get_model,
    get_supported_thinking_levels,
)
from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.providers import anthropic
from misaka.ai.types import AssistantMessage, Context, SimpleStreamOptions
from misaka.ai.utils.estimate import clamp_max_tokens_to_context
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_registry import ModelRegistry, _validate_models_config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "details", "expected", "error"),
    [
        ("refusal", {"explanation": "Provider explanation"}, "error", "Provider explanation"),
        ("refusal", None, "error", "The model refused to complete the request"),
        ("refusal", {"explanation": ""}, "error", "The model refused to complete the request"),
        ("sensitive", None, "error", "Provider stopped with: sensitive"),
        ("end_turn", None, "stop", None),
        ("max_tokens", None, "length", None),
        ("tool_use", None, "toolUse", None),
        ("pause_turn", None, "stop", None),
        ("stop_sequence", None, "stop", None),
        ("future_reason", None, "error", "Unhandled stop reason: future_reason"),
        (None, None, "error", "Anthropic stream ended without a stop reason"),
        ("", None, "error", "Anthropic stream ended without a stop reason"),
    ],
)
async def test_stream_preserves_upstream_stop_reason(monkeypatch, reason, details, expected, error):
    async def incoming():
        if reason is not None:
            yield {
                "type": "message_delta",
                "delta": {"stop_reason": reason, "stop_details": details},
            }

    monkeypatch.setattr(anthropic, "_create_raw_response", AsyncMock(return_value=incoming()))
    stream = anthropic.stream_anthropic(
        get_model("anthropic", "claude-haiku-4-5"), Context(messages=[]), {"client": object()}
    )
    initial_reason = None
    async for event in stream:
        if event.type == "start":
            initial_reason = event.partial.stopReason
    result = await stream.result()
    assert result.stopReason == expected
    assert result.errorMessage == error
    assert result.rawStopReason == (reason or None)
    restored = AssistantMessage.model_validate_json(result.model_dump_json())
    assert restored.rawStopReason == (reason or None)
    assert restored.errorMessage == error
    # Start events refer to a mutable partial; its final state is intentionally shared.
    assert initial_reason in {"pending", expected}


@pytest.mark.parametrize("off_map", [None, {}, {"off": None}, {"off": "none"}])
def test_disabled_thinking_respects_model_map(off_map):
    model = get_model("anthropic", "claude-haiku-4-5").model_copy(update={"thinkingLevelMap": off_map})
    payload = anthropic.build_params(model, Context(messages=[]), False, {"thinkingEnabled": False})
    if off_map == {"off": None}:
        assert "thinking" not in payload
    else:
        assert payload["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("window", [5000, 6500, 200000])
@pytest.mark.parametrize("level", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_thinking_budget_reclamps_to_context(monkeypatch, window, level):
    model = get_model("anthropic", "claude-haiku-4-5").model_copy(update={"contextWindow": window})
    context = Context(messages=[])
    monkeypatch.setattr(anthropic, "stream_anthropic", lambda _m, _c, options: options)
    options = anthropic.stream_simple_anthropic(
        model, context, SimpleStreamOptions(apiKey="fixture", reasoning=level, maxTokens=2048)
    )
    budget = {"minimal": 1024, "low": 2048, "medium": 8192}.get(level, 16384)
    expected_max = clamp_max_tokens_to_context(model, context, min(2048 + budget, model.maxTokens))
    assert options["maxTokens"] == expected_max
    assert options["thinkingBudgetTokens"] == min(budget, max(0, expected_max - 1024))


@pytest.mark.parametrize("value", ["max", None, 123, [], {}])
def test_max_map_schema_matches_other_levels(value):
    config = {"providers": {"fixture": {"models": [{"id": "model", "thinkingLevelMap": {"max": value}}]}}}
    assert bool(_validate_models_config(config)) == (value not in ("max", None))


def test_custom_models_load_pi_thinking_metadata(tmp_path, monkeypatch):
    ids = (
        "claude-fable-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5",
        "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5",
    )
    models = []
    for ident in ids:
        native = get_model("anthropic", ident)
        models.append({
            "id": ident,
            "reasoning": native.reasoning,
            "thinkingLevelMap": native.thinkingLevelMap,
            "compat": {"forceAdaptiveThinking": native.compat.forceAdaptiveThinking is True},
            "maxTokens": 32000,
        })
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"providers": {"fixture": {
        "api": "anthropic-messages", "apiKey": "TEST_ONLY", "baseUrl": "http://localhost:1", "models": models,
    }}}))
    registry = ModelRegistry(AuthStorage.inMemory(), str(path), InMemoryModelsStore())
    monkeypatch.setattr(
        anthropic, "stream_anthropic", lambda m, c, o: anthropic.build_params(m, c, False, o)
    )
    for ident in ids:
        model = registry.find("fixture", ident)
        native = get_model("anthropic", ident)
        assert get_supported_thinking_levels(model) == get_supported_thinking_levels(native)
        assert clamp_thinking_level(model, "max") == ("high" if "haiku" in ident else "max")
        for level in get_supported_thinking_levels(model):
            payload = anthropic.stream_simple_anthropic(
                model, Context(messages=[]),
                SimpleStreamOptions(apiKey="fixture", reasoning=None if level == "off" else level),
            )
            if level == "off":
                assert payload["thinking"] == {"type": "disabled"}
            elif "haiku" in ident:
                assert payload["thinking"]["type"] == "enabled"
                assert "output_config" not in payload
            else:
                assert payload["thinking"]["type"] == "adaptive"
                assert payload["output_config"]["effort"] == ("low" if level == "minimal" else level)


def test_thinking_selector_uses_pi_descriptions():
    from misaka.ui.tui.interactive.components.settings_selector import (
        THINKING_DESCRIPTIONS,
    )
    from misaka.ui.tui.interactive.components.thinking_selector import (
        LEVEL_DESCRIPTIONS,
    )

    assert LEVEL_DESCRIPTIONS == THINKING_DESCRIPTIONS
    assert LEVEL_DESCRIPTIONS["xhigh"] == "Extra-high reasoning (~32k tokens)"
    assert LEVEL_DESCRIPTIONS["max"] == "Maximum reasoning"


@pytest.mark.parametrize("managed", [True, False])
@pytest.mark.parametrize("level", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_managed_and_proxy_effort_keep_requested_level(monkeypatch, managed, level):
    native = get_model("anthropic", "claude-fable-5-1")
    model = native.model_copy(update={
        "compat": native.compat.model_copy(update={"supportsMidConvoEffort": managed}),
    })
    monkeypatch.setattr(
        anthropic, "stream_anthropic", lambda m, c, o: anthropic.build_params(m, c, False, o)
    )
    payload = anthropic.stream_simple_anthropic(
        model, Context(messages=[]), SimpleStreamOptions(apiKey="fixture", reasoning=level)
    )
    effort = "low" if level == "minimal" else level
    if managed:
        assert payload["output_config"] == {"effort": "high"}
        assert payload["messages"][-1] == {"role": "system", "content": [], "output_config": {"effort": effort}}
        assert payload["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    else:
        assert payload["output_config"] == {"effort": effort}
        assert "block_binding" not in payload["thinking"]
        assert all("output_config" not in message for message in payload["messages"])
