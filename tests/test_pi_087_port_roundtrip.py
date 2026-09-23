"""Transcript stability across a restart and the seams that hand a transcript on.

After the pi 0.87 port the prompt and the tool declarations ride the session file as
system messages. A restarted session must recognise them as current: re-declaring the
tools or re-patching the prompt on every run would grow every transcript and break the
provider cache. The other seams here copy or rebuild a transcript for a second model
(fork workers, verifier agents, the MoA aggregator); each must keep exactly one head.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from misaka.ai.types import AssistantMessage, DoneEvent, TextContent
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.utils.values import read_field
from tests.test_prompt_assembly_integration import prompt_home  # noqa: F401 - fixture


def _reply_stream(replies: list[str], seen: list):
    def stream(model, context, options=None):
        seen.append(context)
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


@asynccontextmanager
async def _opened(home, session_manager):
    from misaka.ai.models import get_models
    from misaka.cli.args import parse_args
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.extensions.runner import emit_session_shutdown_event
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import create_agent_session
    from misaka.core.settings_manager import SettingsManager
    from misaka.core.wiring import SessionSpec, assemble, role_session_setup

    role = "last_order"
    workspace = home / "workspace"
    workspace.mkdir(exist_ok=True)
    profile = home / "profiles" / role
    agent_dir = home / "agent" / "last_order-foreground"
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = SessionSpec(profile_dir=str(profile), role=role, workspace=str(workspace), kind="foreground",
                       sender=role, mcp_role=role, task_id=None, research_context=False)
    assembly = assemble(spec)
    role_flags, assembly, _env = role_session_setup(str(profile), str(workspace), research_context=False,
                                                    receive_messages=True)
    sections = parse_args(role_flags).appendSystemPrompt or []
    options = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    loader = DefaultResourceLoader({**options, "extensionFactories": assembly.extension_factories,
                                    "appendSystemPrompt": sections})
    await loader.reload()
    model = next(m for m in get_models("anthropic") if m.id == "claude-opus-5")
    auth = AuthStorage.inMemory()
    auth.setRuntimeApiKey(model.provider, "fixture")
    result = await create_agent_session({**options, "resourceLoader": loader,
                                         "customTools": assembly.custom_tools, "parts": assembly.parts,
                                         "model": model, "authStorage": auth,
                                         "settingsManager": SettingsManager.inMemory(
                                             {"compaction": {"enabled": False}, "retry": {"enabled": False}}),
                                         "sessionManager": session_manager})
    session = result["session"]
    try:
        yield session
    finally:
        event = {"type": "session_shutdown", "reason": "test"}
        try:
            await session.moments.session_shutdown(event)
            await emit_session_shutdown_event(session.extensionRunner, event)
        finally:
            session.dispose()


def _system_entries(manager) -> list[dict]:
    """The persisted system messages, as plain dicts, in transcript order."""
    return [
        message if isinstance(message, dict) else message.model_dump(exclude_none=True)
        for entry in manager.getEntries()
        if entry.get("type") == "message"
        for message in [entry["message"]]
        if read_field(message, "role") == "system"
    ]


async def test_a_restarted_session_neither_redeclares_tools_nor_repatches_the_prompt(prompt_home):  # noqa: F811
    from misaka.core.session_manager import SessionManager

    session_dir = prompt_home / "sessions"
    seen: list = []
    async with _opened(prompt_home, SessionManager.create(str(prompt_home / "workspace"), str(session_dir))) as first:
        first.agent.streamFn = _reply_stream(["one"], seen)
        await first.prompt("first run")
        await first.waitForIdle()
        path = first.sessionManager.getSessionFile()
        heads = _system_entries(first.sessionManager)
        assert len(heads) == 1, [head.keys() for head in heads]
        assert heads[0].get("toolsAdded") and heads[0].get("sections")
        declared = [read_field(tool, "name") for tool in heads[0]["toolsAdded"]]
        assert set(declared) == set(first.getActiveToolNames())

    async with _opened(prompt_home, SessionManager.open(path, str(session_dir))) as second:
        second.agent.streamFn = _reply_stream(["two"], seen)
        await second.prompt("second run")
        await second.waitForIdle()
        heads = _system_entries(second.sessionManager)
        # One head only: the restarted session recognised the persisted prompt sections and
        # tool declarations as current instead of patching or re-declaring them.
        assert heads[1:] == [], heads[1:]
        assert [getattr(m, "role", None) for m in second.agent.state.messages].count("system") == 1

    # Both requests carried the same head to the provider (the cache prefix survives a restart).
    first_head, second_head = seen[0].messages[0], seen[1].messages[0]
    assert first_head.model_dump(exclude_none=True) == second_head.model_dump(exclude_none=True)


async def test_a_pre_port_session_file_gains_one_head_on_its_next_run(prompt_home):  # noqa: F811
    """A session written before the port has no leading system message. Its next run must
    declare the prompt sections and the tools once, ahead of the new user message, and the
    replayed prompt must equal the session's effective prompt."""
    from misaka.ai.utils.transcript import get_current_system_prompt, get_current_tools
    from misaka.core.session_manager import SessionManager

    session_dir = prompt_home / "sessions"
    legacy = SessionManager.create(str(prompt_home / "workspace"), str(session_dir))
    legacy.appendMessage({"role": "user", "content": "old question", "timestamp": 1})
    legacy.appendMessage({
        "role": "assistant", "content": [{"type": "text", "text": "old answer"}], "api": "anthropic-messages",
        "provider": "anthropic", "model": "claude-opus-5", "stopReason": "stop", "timestamp": 2,
        "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
    })
    legacy._rewriteFile()
    path = legacy.getSessionFile()
    assert _system_entries(legacy) == []

    seen: list = []
    effective: list[str] = []
    async with _opened(prompt_home, SessionManager.open(path, str(session_dir))) as session:
        reply = _reply_stream(["new answer"], seen)

        def stream(model, context, options=None):
            # The run's effective prompt: parts may force extra text for the run's duration.
            effective.append(session.systemPrompt)
            return reply(model, context, options)

        session.agent.streamFn = stream
        await session.prompt("new question")
        await session.waitForIdle()
        roles = [read_field(m, "role") for m in session.agent.state.messages]
        assert roles == ["user", "assistant", "system", "user", "assistant"], roles
        heads = _system_entries(session.sessionManager)
        assert len(heads) == 1 and heads[0].get("sections") and heads[0].get("toolsAdded")
        sent = seen[0].messages
        assert get_current_system_prompt(sent) == effective[0]
        # The persisted head carries the structured sections; the forced run-time text is
        # projected onto the request only.
        assert get_current_system_prompt(session.sessionManager.buildSessionContext().messages) == session.systemPrompt
        assert {tool.name for tool in get_current_tools(sent)} == set(session.getActiveToolNames())


# --- seams that hand a transcript to a second model ------------------------------------


def _model(api: str, provider: str = "fixture"):
    from misaka.ai.types import Model

    return Model(
        id=f"{provider}-model", name="fixture", api=api, provider=provider, baseUrl="", reasoning=False,
        input=["text"], cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        contextWindow=100_000, maxTokens=1024,
    )


def _capturing_provider(api: str, seen: list):
    """An API provider that records the transcript it is handed and answers with one word."""
    from misaka.ai.api_registry import ApiProvider, register_api_provider

    def stream_simple(model, context, options=None):
        seen.append(context)
        return _reply_stream(["ok"], [])(model, context, options)

    register_api_provider(ApiProvider(api=api, stream=stream_simple, streamSimple=stream_simple), source_id="test")


async def test_moa_aggregator_receives_the_normalized_transcript_with_one_head(monkeypatch):
    """The MoA provider is handed a `TranscriptContext`; the aggregator must be called with
    the same prompt and tool declarations, not a rebuilt `Context`."""
    from misaka.ai.api_registry import unregister_api_providers
    from misaka.ai.types import Context, Tool, UserMessage
    from misaka.ai.utils.transcript import (
        get_current_system_prompt,
        get_current_tools,
        normalize_context,
    )
    from misaka.core.moa import provider as moa

    seen: list = []
    _capturing_provider("fixture-api", seen)
    aggregator = _model("fixture-api")
    monkeypatch.setattr(moa, "load_moa_config", lambda: moa.normalize_moa_config({
        "presets": {"solo": {"enabled": False, "aggregator": {"provider": "fixture", "model": aggregator.id}}},
        "default_preset": "solo",
    }))
    monkeypatch.setattr(moa, "_MODEL_RESOLVER", lambda _provider, _id: aggregator)
    monkeypatch.setattr(moa, "_AUTH_RESOLVER", None)
    tool = Tool(name="read", description="read a file", parameters={"type": "object", "properties": {}})
    context = normalize_context(Context(
        systemPrompt="mixture prompt", messages=[UserMessage(content="question", timestamp=1)], tools=[tool]))
    try:
        result = await moa.stream_simple_moa(_model("moa", "moa"), context).result()
    finally:
        unregister_api_providers("test")
    assert result.content[-1].text == "ok"
    assert len(seen) == 1
    sent = seen[0].messages
    assert [read_field(m, "role") for m in sent] == ["system", "user"]
    assert get_current_system_prompt(sent) == "mixture prompt"
    assert [t.name for t in get_current_tools(sent)] == ["read"]


async def test_exact_fork_replaces_every_system_message_with_the_parent_head():
    """An exact fork sends the parent's rendered prompt and tool declarations as the one
    head of the transcript, dropping the prompt patches and tool deltas this worker added."""
    from types import SimpleNamespace

    from misaka.ai.types import Context, SystemMessage, Tool, UserMessage
    from misaka.ai.utils.transcript import (
        get_current_system_prompt,
        get_current_tools,
        normalize_context,
    )
    from misaka.core.subagent.fork import install

    read = Tool(name="read", description="worker wording", parameters={"type": "object", "properties": {}})
    active_names: list[str] = []
    session = SimpleNamespace(
        agent=SimpleNamespace(state=SimpleNamespace(tools=[read]), streamFn=None),
        setActiveToolsByName=active_names.extend,
    )
    seen: list = []

    def previous(model, context, options=None):
        seen.append(context)
        return "stream"

    session.agent.streamFn = previous
    install(session, {"systemPrompt": "PARENT PROMPT", "tools": [
        {"name": "read", "description": "parent wording", "parameters": read.parameters_json_schema(),
         "constrainedSampling": None}]})
    assert active_names == ["read"]
    transcript = normalize_context(Context(
        systemPrompt="worker prompt", messages=[
            UserMessage(content="copied question", timestamp=5),
            SystemMessage(content="", sections={"rules": "<rules>\nworker rules\n</rules>"}, timestamp=6),
            UserMessage(content="directive", timestamp=7),
        ], tools=[read]))
    assert session.agent.streamFn(_model("fixture-api"), transcript, None) == "stream"
    sent = seen[0].messages
    assert [read_field(m, "role") for m in sent] == ["system", "user", "user"]
    assert sent[0].timestamp == transcript.messages[0].timestamp
    assert get_current_system_prompt(sent) == "PARENT PROMPT"
    assert [(t.name, t.description) for t in get_current_tools(sent)] == [("read", "parent wording")]


def test_an_agent_seeded_with_a_parents_transcript_keeps_its_own_prompt_only_without_the_parents_head():
    """`AgentState` seeds its `systemPrompt` only when the copied messages do not already
    start with a system message, which a parent transcript now always does: the verifier
    in `subagent/child.py` strips those before seeding."""
    from misaka.agent.agent import Agent
    from misaka.ai.types import SystemMessage, UserMessage

    parent = [SystemMessage(content="PARENT", toolsAdded=[], timestamp=1), UserMessage(content="q", timestamp=2)]
    inherited = Agent(initialState={"systemPrompt": "VERIFIER", "messages": list(parent), "tools": []})
    assert inherited.state.systemPrompt == "PARENT"
    stripped = Agent(initialState={
        "systemPrompt": "VERIFIER", "tools": [],
        "messages": [m for m in parent if str(read_field(m, "role", "")) != "system"]})
    assert stripped.state.systemPrompt == "VERIFIER"
    assert [read_field(m, "role") for m in stripped.state.messages] == ["system", "user"]
