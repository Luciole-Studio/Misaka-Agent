"""The pi 0.86–0.87 kernel port (2026-09-23): the prompt and tool declarations ride the
transcript as system messages, prompts are sections, edits are `context_edit` entries, and
turns end through `finishTurn`. Each test pins one behaviour the upstream changelog names."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from misaka.agent.agent import Agent
from misaka.agent.agent_loop import _declareToolChanges
from misaka.agent.guards import finish_turn_from_stop_predicate
from misaka.agent.types import (
    AgentContext,
    AgentTool,
    AgentToolResult,
    AgentTurnDecision,
)
from misaka.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    Model,
    SystemMessage,
    TextContent,
    Tool,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import (
    collapseSystemMessages,
    getCurrentSystemPrompt,
    getCurrentTools,
    getToolStateChanges,
    normalizeContext,
    resolveTranscriptTools,
)
from misaka.core.cache_warmer import (
    CacheWarmer,
    getCacheWarmingDelayMs,
    getPromptCacheTtlMs,
    isReplayable,
)
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import InMemorySettingsStorage, SettingsManager
from misaka.core.system_prompt import (
    buildSystemPrompt,
    buildSystemPromptSections,
    diffSystemPromptSections,
)

MODEL = Model(
    id="fixture", name="fixture", api="anthropic-messages", provider="fixture", baseUrl="", reasoning=False,
    input=["text"], cost={"input": 3, "output": 15, "cacheRead": 0.3, "cacheWrite": 3.75},
    contextWindow=200_000, maxTokens=8192, promptCache={"short": 300, "long": 3600},
)


def _tool(name: str, description: str = "d") -> Tool:
    return Tool(name=name, description=description, parameters={"type": "object", "properties": {}})


def _user(text: str, timestamp: int = 1) -> UserMessage:
    return UserMessage(content=text, timestamp=timestamp)


def _with_compat(compat: dict) -> Model:
    return Model.model_validate({**MODEL.model_dump(), "compat": compat})


# --- ai: the transcript carries the prompt and the tools --------------------------------


def test_normalize_context_folds_prompt_and_tools_into_a_leading_system_message():
    transcript = normalizeContext(Context(systemPrompt="be brief", messages=[_user("q")], tools=[_tool("a")]))
    assert [m.role for m in transcript.messages] == ["system", "user"]
    assert getCurrentSystemPrompt(transcript.messages) == "be brief"
    assert [t.name for t in getCurrentTools(transcript.messages)] == ["a"]
    # Nothing to fold keeps an empty transcript empty; a second normalization is a no-op.
    assert normalizeContext(Context(messages=[])).messages == []
    assert normalizeContext(transcript) is transcript


def test_later_system_messages_patch_sections_and_change_tools():
    messages = [
        SystemMessage(content="base", sections={"rules": "<rules>\nA\n</rules>"}, toolsAdded=[_tool("a"), _tool("b")], timestamp=0),
        _user("q"),
        SystemMessage(content="", sections={"rules": None, "extra": "<extra>\nE\n</extra>"}, toolsRemoved=[{"name": "a"}], timestamp=2),
    ]
    assert getCurrentSystemPrompt(messages) == "base\n\n<extra>\nE\n</extra>"
    assert [t.name for t in getCurrentTools(messages)] == ["b"]
    collapsed = collapseSystemMessages(normalizeContext(Context(messages=messages)))
    assert [m.role for m in collapsed.messages] == ["system", "user"]
    assert collapsed.messages[0].sections == {"extra": "<extra>\nE\n</extra>"}


def test_tool_state_changes_treat_a_redefinition_as_removal_plus_addition():
    changes = getToolStateChanges([_tool("a", "old"), _tool("b")], [_tool("a", "new"), _tool("c")])
    assert [t.name for t in changes.toolsAdded] == ["a", "c"]
    assert [t.name for t in changes.toolsRemoved] == ["a", "b"]
    # A transport that anchors additions cannot replay a removal: it sends the current list.
    messages = [SystemMessage(content="", toolsAdded=[_tool("a")], timestamp=0), SystemMessage(content="", toolsRemoved=[{"name": "a"}], toolsAdded=[_tool("c")], timestamp=1)]
    assert resolveTranscriptTools(messages, True).anchorsAdditions is False
    assert [t.name for t in resolveTranscriptTools(messages, True).requestTools] == ["c"]
    additive = [SystemMessage(content="", toolsAdded=[_tool("a")], timestamp=0), SystemMessage(content="", toolsAdded=[_tool("c")], timestamp=1)]
    resolved = resolveTranscriptTools(additive, True)
    assert resolved.anchorsAdditions is True and [t.name for t in resolved.requestTools] == ["a"]


# --- agent: the loop declares tool changes and ends turns through finishTurn --------------


def _agent_tool(name: str) -> AgentTool:
    async def execute(_id, _args, _signal, _update):
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    return AgentTool(name=name, label=name, description="d", parameters={"type": "object", "properties": {}}, execute=execute)


def test_a_stop_verdict_takes_the_tools_off_the_wrap_up_turn():
    """2026-09-29: the 0.87 port took ``systemPrompt`` off AgentContext, and the guards' wrap-up
    turn still passed it -- the first stop verdict of a headless run (a loop, no progress, the wall
    clock) raised TypeError instead of letting the model write its answer. The stripped context
    reaches the model as every tool declared removed."""
    from misaka.agent.guards import _SessionGuards
    from misaka.utils.values import read_field

    context = AgentContext(messages=[], tools=[_agent_tool("a")])
    context.messages.extend(_declareToolChanges(context, [_user("q")]))
    guards = _SessionGuards(SimpleNamespace())
    guards._forcing = True
    update = guards.next_turn({"context": context})
    assert update.context.tools == [] and update.context.messages == context.messages
    declared = _declareToolChanges(update.context, [_user("wrap up", 2)])
    assert [m.role for m in declared] == ["system", "user"]
    assert [read_field(tool, "name") for tool in declared[0].toolsRemoved] == ["a"]


def test_declare_tool_changes_inserts_a_system_message_before_the_first_prompt():
    context = AgentContext(messages=[], tools=[_agent_tool("a")])
    declared = _declareToolChanges(context, [_user("q")])
    assert [m.role for m in declared] == ["system", "user"]
    assert [t.name for t in declared[0].toolsAdded] == ["a"]
    # Once declared, an unchanged loadout adds nothing.
    context.messages.extend(declared)
    assert _declareToolChanges(context, [_user("again", 2)]) == [_user("again", 2)]
    # A pending system message carries the delta itself instead of a second one.
    context.tools = [_agent_tool("a"), _agent_tool("b")]
    pending = SystemMessage(content="", sections={"x": "y"}, timestamp=3)
    declared = _declareToolChanges(context, [pending, _user("more", 4)])
    assert len(declared) == 2 and [t.name for t in declared[0].toolsAdded] == ["b"] and declared[0].sections == {"x": "y"}


def _stream_fn(replies: list[str]):
    def stream(model, context, options=None):
        stream = AssistantMessageEventStream()
        message = AssistantMessage(
            content=[TextContent(text=replies.pop(0))], api=model.api, provider=model.provider, model=model.id,
            usage={"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
                   "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
            stopReason="stop", timestamp=1,
        )
        stream.push(DoneEvent(reason="stop", message=message))
        stream.end()
        return stream

    return stream


async def test_finish_turn_continue_runs_one_more_request_and_end_stops():
    seen: list[str] = []
    replies = ["one", "two", "three"]

    async def finish_turn(turn, _signal=None):
        seen.append(turn.message.content[0].text)
        return AgentTurnDecision(action="continue") if len(seen) == 1 else AgentTurnDecision(action="end")

    agent = Agent(initialState={"systemPrompt": "p", "model": MODEL, "tools": []}, streamFn=_stream_fn(replies), finishTurn=finish_turn)
    await agent.prompt("go")
    assert seen == ["one", "two"] and replies == ["three"]
    assert agent.state.messages[0].role == "system" and agent.state.systemPrompt == "p"


async def test_a_stop_predicate_becomes_a_finish_turn_that_ignores_hard_exits():
    calls: list[str] = []
    finish = finish_turn_from_stop_predicate(lambda turn, _s=None: calls.append("asked") or True)
    errored = SimpleNamespace(message=SimpleNamespace(stopReason="error"))
    assert await finish(errored) is None and calls == []
    normal = SimpleNamespace(message=SimpleNamespace(stopReason="stop"))
    assert (await finish(normal)).action == "end"


# --- core: sections, context edits, settings, cache warming -----------------------------


def test_system_prompt_is_sections_and_a_change_is_a_named_patch():
    options = {"cwd": "/w", "selectedTools": ["bash"], "toolSnippets": {"bash": "Shell"}, "toolGuidelines": {"bash": ["Be careful"]}}
    sections = buildSystemPromptSections(options)
    assert list(sections) == ["preamble", "addendum", "tools", "rules", "cwd"] or list(sections) == ["preamble", "tools", "rules", "cwd"]
    assert sections["tools"].startswith("<tools>\n- bash: Shell") and "- Be careful" in sections["rules"]
    assert buildSystemPrompt(options).endswith("<cwd>\n/w\n</cwd>")
    later = buildSystemPromptSections({**options, "selectedTools": []})
    patch = diffSystemPromptSections(sections, later)
    assert set(patch) == {"tools", "rules"} and "(none)" in patch["tools"]
    assert diffSystemPromptSections(sections, sections) is None
    with pytest.raises(ValueError):
        buildSystemPromptSections({**options, "sections": {"Preamble": "x"}})
    assert buildSystemPrompt({**options, "forceSystemPrompt": "forced"}) == "forced"


def test_context_edit_omits_or_replaces_a_message_in_the_projection_only():
    manager = SessionManager.inMemory("/tmp")
    first = manager.appendMessage(_user("keep"))
    manager.appendMessage(AssistantMessage(
        content=[TextContent(text="failed")], api="a", provider="p", model="m", stopReason="error",
        usage={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
               "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}, timestamp=2))
    failed = manager.getLeafId()
    manager.appendContextEdit(failed, None)
    manager.appendContextEdit(first, {"content": "kept, edited"})
    projection = manager.buildSessionProjection()
    assert [m.role for m in projection.messages] == ["user"]
    assert projection.messages[0].content == "kept, edited"
    # Raw history is untouched; the edit rides its own entry.
    assert [e["type"] for e in manager.getBranch()] == ["message", "message", "context_edit", "context_edit"]
    with pytest.raises(ValueError):
        manager.appendContextEdit(failed, {"nope": 1})


def test_compaction_records_the_system_head_and_retain_none_keeps_its_own_id():
    manager = SessionManager.inMemory("/tmp")
    manager.appendMessage(SystemMessage(content="prompt", toolsAdded=[_tool("a")], timestamp=0))
    manager.appendMessage(_user("old"))
    compaction_id = manager.appendCompaction("summary", None, 10)
    entry = manager.getEntry(compaction_id)
    assert entry["firstKeptEntryId"] == compaction_id
    assert entry["systemMessage"].content == "prompt"
    messages = manager.buildSessionContext().messages
    assert [getattr(m, "role", None) for m in messages] == ["system", "compactionSummary"]
    assert getCurrentSystemPrompt(messages) == "prompt"


def _storage(settings: dict) -> InMemorySettingsStorage:
    storage = InMemorySettingsStorage()
    storage.global_value = json.dumps(settings)
    return storage


def test_compaction_budgets_resolve_through_model_overrides_and_retry_delay_is_capped():
    from misaka.ai.utils.retry import RetryPolicy, retryDelayMs

    settings = SettingsManager.fromStorage(_storage({
        "compaction": {"reserveTokens": 100, "modelOverrides": {"fixture/fixture": {"keepRecentTokens": 7}}},
        "retry": {"maxAgentDelayMs": 5000},
    }))
    assert settings.getCompactionSettings(MODEL) == {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 7}
    assert settings.getCompactionSettings() == {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 20000}
    assert settings.getRetrySettings()["maxAgentDelayMs"] == 5000
    policy = RetryPolicy(enabled=True, maxRetries=9, baseDelayMs=2000, maxAgentDelayMs=5000)
    assert retryDelayMs(policy, 1) == 2000 and retryDelayMs(policy, 9) == 5000
    assert settings.getCacheWarmingMode() == "streaming"
    bad = SettingsManager.fromStorage(_storage({"compaction": {"reserveTokens": -1}}))
    with pytest.raises(ValueError):
        bad.getCompactionSettings()


def test_cache_warming_economics():
    assert getCacheWarmingDelayMs(300_000) == 270_000 and getCacheWarmingDelayMs(5_000) is None
    assert getPromptCacheTtlMs(MODEL, {"cacheRetention": "long"}) == 3_600_000
    assert getPromptCacheTtlMs(MODEL, {"cacheRetention": "none"}) is None
    assert isReplayable(MODEL, {"reasoning": "high"}) is False  # budget-based thinking changes the cache key
    assert isReplayable(_with_compat({"forceAdaptiveThinking": True}), {"reasoning": "high"}) is True
    manager = SessionManager.inMemory("/tmp")
    manager.appendMessage(AssistantMessage(
        content=[TextContent(text="x")], api="a", provider="fixture", model="fixture", stopReason="stop",
        usage={"input": 100_000, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 100_001,
               "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}, timestamp=1))
    warmer = CacheWarmer(SimpleNamespace(), manager, lambda: "streaming")
    decision = warmer.evaluate(SimpleNamespace(model=MODEL, phase="streaming"))
    assert decision.economicsAvailable and decision.action == "warm"
    assert warmer.evaluate(SimpleNamespace(model=MODEL, phase="idle")).continuationProbability == 0.15
    assert warmer.status.reason == "waiting for first request"
