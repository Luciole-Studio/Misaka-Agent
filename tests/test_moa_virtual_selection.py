"""MISAKA fork (B6): MoA presets read back like virtual models; advisor usage stays on the
aggregator's message, which card budgets, turn limits and subagent totals meter."""

from __future__ import annotations

import importlib

import pytest

from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    Model,
    SimpleStreamOptions,
    TranscriptContext,
    Usage,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.core.moa import provider as moa
from misaka.core.virtual_models import getBranchSelection


def _model(provider: str, id: str, api: str = "openai-completions") -> Model:
    return Model.model_validate({
        "id": id, "name": id, "api": api, "provider": provider, "baseUrl": "https://example.test",
        "reasoning": False, "input": ["text"],
        "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 100_000, "maxTokens": 1000,
    })


def _usage(tokens: int, cost: float = 0) -> Usage:
    return Usage.model_validate({
        "input": tokens, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": tokens,
        "cost": {"input": cost, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost},
    })


def _answer(model: Model, usage: Usage) -> AssistantMessage:
    return AssistantMessage.model_validate({
        "content": [{"type": "text", "text": f"from {model.id}"}], "api": model.api, "provider": model.provider,
        "model": model.id, "usage": usage.model_dump(), "stopReason": "stop", "timestamp": 1,
    })


PRESET = _model("moa", "default", api="moa")
AGGREGATOR = _model("anthropic", "claude-aggregator")
ADVISORS = [_model("openai", "gpt-advisor"), _model("google", "gemini-advisor")]
CATALOG = {(model.provider, model.id): model for model in [PRESET, AGGREGATOR, *ADVISORS]}


def test_a_moa_selection_holds_on_the_branch_while_the_aggregator_answers():
    branch = [
        {"type": "model_change", "provider": "moa", "modelId": "default"},
        {"type": "message", "message": {"role": "user", "content": "hi"}},
        {"type": "message", "message": _answer(AGGREGATOR, _usage(1)).model_dump()},
    ]

    selection = getBranchSelection(branch, lambda provider, id: CATALOG.get((provider, id)))

    assert selection == {"provider": "moa", "modelId": "default"}


async def test_advisor_usage_stays_on_the_aggregators_message(monkeypatch):
    config = moa.normalize_moa_config({"presets": {"default": {
        "reference_models": [{"provider": m.provider, "model": m.id} for m in ADVISORS],
        "aggregator": {"provider": AGGREGATOR.provider, "model": AGGREGATOR.id},
    }}})
    monkeypatch.setattr(moa, "load_moa_config", lambda: config)
    monkeypatch.setattr(moa, "_MODEL_RESOLVER", lambda provider, id: CATALOG.get((provider, id)))
    monkeypatch.setattr(moa, "_AUTH_RESOLVER", None)

    async def complete_simple(model, _context, _options=None):
        # One advisor reports its cost, the other leaves it to the catalog price.
        return _answer(model, _usage(10, cost=0.5) if model.id == "gpt-advisor" else _usage(100))

    def stream_simple(model, _context, _options=None):
        stream = AssistantMessageEventStream()
        message = _answer(model, _usage(7, cost=0.01))
        stream.push(DoneEvent(reason="stop", message=message))
        stream.end(message)
        return stream

    # The compat module is patched by name, as the MoA provider imports its functions at call time.
    stream_module = importlib.import_module("misaka.ai.compat")
    monkeypatch.setattr(stream_module, "completeSimple", complete_simple)
    monkeypatch.setattr(stream_module, "streamSimple", stream_simple)
    moa.forget_session("session-1")

    context = TranscriptContext(messages=[UserMessage(content="question", timestamp=1)])
    message = await moa.stream_simple_moa(PRESET, context, SimpleStreamOptions(sessionId="session-1")).result()

    # Aggregator 7 tokens + advisors 10 and 100; the reported cost is kept, the missing one is priced
    # at the advisor's catalog rate (1 per million input tokens).
    assert (message.provider, message.model, message.usage.input) == ("anthropic", "claude-aggregator", 117)
    assert message.usage.cost.total == pytest.approx(0.01 + 0.5 + 100 / 1_000_000)


async def test_a_moa_reply_is_stamped_and_timed_as_the_whole_round(monkeypatch):
    """MISAKA fork: the round is the request, so its reply starts when the round did and is timed through the
    advisors and the aggregator, not the aggregator alone."""
    import asyncio
    import time

    config = moa.normalize_moa_config({"presets": {"default": {
        "reference_models": [{"provider": m.provider, "model": m.id} for m in ADVISORS],
        "aggregator": {"provider": AGGREGATOR.provider, "model": AGGREGATOR.id},
    }}})
    monkeypatch.setattr(moa, "load_moa_config", lambda: config)
    monkeypatch.setattr(moa, "_MODEL_RESOLVER", lambda provider, id: CATALOG.get((provider, id)))
    monkeypatch.setattr(moa, "_AUTH_RESOLVER", None)

    async def complete_simple(model, _context, _options=None):
        await asyncio.sleep(0.05)
        return _answer(model, _usage(10))

    def stream_simple(model, _context, _options=None):
        stream = AssistantMessageEventStream()
        message = _answer(model, _usage(7)).model_copy(update={"timestamp": time.time_ns() // 1_000_000})
        stream.push(DoneEvent(reason="stop", message=message))
        stream.end(message)
        return stream

    stream_module = importlib.import_module("misaka.ai.compat")
    monkeypatch.setattr(stream_module, "completeSimple", complete_simple)
    monkeypatch.setattr(stream_module, "streamSimple", stream_simple)
    moa.forget_session("session-2")

    started = time.time_ns() // 1_000_000
    context = TranscriptContext(messages=[UserMessage(content="question", timestamp=1)])
    message = await moa.stream_simple_moa(PRESET, context, SimpleStreamOptions(sessionId="session-2")).result()

    assert started <= message.timestamp <= started + 20
    assert message.durationMs is not None and message.durationMs >= 45


async def test_a_session_resumes_on_the_moa_preset_it_selected_not_the_aggregator_that_answered(tmp_path, monkeypatch):
    from misaka.ai.models import ModelsRefreshOptions
    from misaka.ai.models_store import InMemoryModelsStore
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import createAgentSession
    from misaka.core.session_manager import SessionManager
    from misaka.core.settings_manager import SettingsManager

    config = moa.normalize_moa_config({
        "presets": {"default": {"aggregator": {"provider": "anthropic", "model": "claude-opus-4-6"}}},
    })
    monkeypatch.setattr(moa, "load_moa_config", lambda: config)
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": None, "allowModelNetwork": False})
    await runtime.setRuntimeApiKey("anthropic", "fixture")
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    preset = runtime.getModel("moa", "default")
    aggregator = runtime.getModel("anthropic", "claude-opus-4-6")
    assert preset is not None and aggregator is not None

    workspace, agentDir = tmp_path / "workspace", tmp_path / "agent"
    workspace.mkdir()
    agentDir.mkdir()
    sessionManager = SessionManager.inMemory(str(workspace))
    sessionManager.appendModelChange("moa", "default")
    sessionManager.appendMessage({"role": "user", "content": "hi", "timestamp": 1})
    sessionManager.appendMessage(_answer(aggregator, _usage(1)))
    settings = SettingsManager.inMemory({})
    options = {"cwd": str(workspace), "agentDir": str(agentDir)}
    loader = DefaultResourceLoader({**options, "settingsManager": settings})
    await loader.reload()

    result = await createAgentSession({
        **options, "resourceLoader": loader, "modelRuntime": runtime, "settingsManager": settings,
        "noTools": "builtin", "sessionManager": sessionManager,
    })
    session = result["session"]
    try:
        assert result["modelFallbackMessage"] is None
        assert (session.model.provider, session.model.id) == ("moa", "default")
        assert session.model.contextWindow == aggregator.contextWindow
    finally:
        session.dispose()
