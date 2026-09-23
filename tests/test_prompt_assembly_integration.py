"""Real SDK + core/extension prompt folding, without a model turn or live state."""
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import pytest


@pytest.fixture
def prompt_home(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("MISAKA_", "HERMES_", "LCM_", "PI_")) or key.endswith(("_API_KEY", "_TOKEN")):
            monkeypatch.delenv(key, raising=False)
    for key, leaf in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                      ("XDG_DATA_HOME", "data"), ("HERMES_HOME", "hermes")):
        folder = tmp_path / leaf
        folder.mkdir(exist_ok=True)
        monkeypatch.setenv(key, str(folder))
    monkeypatch.setenv("LCM_DB_PATH", str(tmp_path / "lcm.db"))
    monkeypatch.chdir(tmp_path)

    from misaka.cli import bootstrap
    from misaka.config import home
    from misaka.core import wiring
    from misaka.core.network.wiring import network
    from misaka.core.research.wiring import research

    monkeypatch.setattr(wiring, "bundled", wiring.bundled)
    bootstrap.install()
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path))
    for module in (network, research):
        monkeypatch.setattr(module, "_CON", None)
    for role in ("last_order", "sisters/10032", "sisters/10033"):
        folder = tmp_path / "profiles" / role
        folder.mkdir(parents=True)
        (folder / "SOUL.md").write_text(f"Fixture voice for {role}.\n", encoding="utf-8")
        if role.startswith("sisters/"):
            (folder / "DESCRIBE.md").write_text("---\ndescription: Fixture historian\n---\n" + "研究" * 120,
                                                encoding="utf-8")

    network_attempts = []

    def no_network(*args, **_kwargs):
        network_attempts.append(args)
        raise AssertionError("Prompt integration attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    yield tmp_path
    for module in (network, research):
        connection = module._CON
        if connection is not None:
            connection.close()
            module._CON = None
    assert not network_attempts


@asynccontextmanager
async def assembled(home, role, kind, *, research=False, custom=False, prompt_flags=None):
    from misaka.cli.args import parse_args
    from misaka.config import identity
    from misaka.core.extensions.runner import emit_session_shutdown_event
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import create_agent_session
    from misaka.core.session_manager import SessionManager
    from misaka.core.wiring import SessionSpec, assemble, role_session_setup

    workspace = home / "workspace"
    workspace.mkdir(exist_ok=True)
    profile = home / "profiles" / role
    agent_dir = home / "agent" / f"{role.replace('/', '-')}-{kind}"
    agent_dir.mkdir(parents=True, exist_ok=True)
    if custom:
        (agent_dir / "SYSTEM.md").write_text("CUSTOM SYSTEM: keep this exact opening.\n", encoding="utf-8")
    spec = SessionSpec(profile_dir=str(profile), role=role, workspace=str(workspace), kind=kind,
                       sender=role.rsplit("/", 1)[-1], mcp_role=role,
                       task_id="fixture-card" if kind == "card" else None,
                       research_context=research)
    assembly = assemble(spec)
    if kind in {"foreground", "headless"}:
        role_flags, assembly, _env = role_session_setup(
            str(profile), str(workspace), research_context=research,
            receive_messages=kind == "foreground")
        sections = parse_args(role_flags).appendSystemPrompt or []
    else:
        sections = identity.base_prompt_sources(str(profile), role)
    options = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    prompt_options = {"appendSystemPrompt": sections}
    if prompt_flags is not None:
        parsed = parse_args(prompt_flags)
        prompt_options = {"systemPrompt": parsed.systemPrompt,
                          "appendSystemPrompt": parsed.appendSystemPrompt or []}
    if prompt_flags is not None:
        options.update(tools=parsed.tools, noTools=parsed.noTools)
    loader = DefaultResourceLoader({**options, "extensionFactories": assembly.extension_factories,
                                    **prompt_options})
    await loader.reload()
    result = await create_agent_session({**options, "resourceLoader": loader,
                                         "customTools": assembly.custom_tools, "parts": assembly.parts,
                                         "sessionManager": SessionManager.inMemory(str(workspace))})
    session = result["session"]

    async def no_model(*_args, **_kwargs):
        raise AssertionError("Prompt integration attempted a model turn")

    session.agent.streamFn = no_model
    try:
        yield session
    finally:
        event = {"type": "session_shutdown", "reason": "test"}
        try:
            await session.moments.session_shutdown(event)
            await emit_session_shutdown_event(session.extensionRunner, event)
        finally:
            session.dispose()


async def final_prompt(session):
    await session._prepare_agent_start([], "Offline prompt assembly check", None)
    return session.systemPrompt


@pytest.mark.parametrize(("role", "kind", "research"), [
    ("last_order", "foreground", False),
    ("last_order", "foreground", True),
    ("last_order", "headless", True),
    ("sisters/10032", "foreground", False),
    ("sisters/10032", "card", False),
    ("sisters/10032", "card", True),
])
async def test_real_role_assembly(prompt_home, role, kind, research):
    from misaka.config import identity
    from misaka.core.network.wiring.collaboration import SISTER_TOOLS
    from misaka.core.research.planner import RESEARCH_SISTER_DISCIPLINE
    from misaka.core.research.prompting import RESEARCH_LO_ORCHESTRATION

    async with assembled(prompt_home, role, kind, research=research) as session:
        text = await final_prompt(session)
        active = set(session.getActiveToolNames())
        assert text.count(identity.COMMON_CHARTER) == 1
        own, other = ((identity.COORDINATOR_ROLE, identity.SISTER_ROLE) if role == "last_order"
                      else (identity.SISTER_ROLE, identity.COORDINATOR_ROLE))
        assert text.count(own) == 1
        assert other not in text
        assert text.count("## Sister capability profiles") == 1
        assert '"id": "10033"' in text
        assert ('"id": "10032"' in text) == (role == "last_order")
        assert ("## Sub-agents" in text) == ("Agent" in active)
        assert ("## Allies" in text) == ("misaka_ally_start" in active)
        assert text.count("## Last Order / Sister coordination") == bool(active.intersection(SISTER_TOOLS))
        if kind == "card":
            assert "misaka_my_card" in active
            assert "- `misaka_my_card`:" in text
            assert "misaka_card_state" not in text
        assert text.count(RESEARCH_LO_ORCHESTRATION) == int(research and role == "last_order")
        assert (RESEARCH_SISTER_DISCIPLINE in text) == (research and kind == "card")
        assert await final_prompt(session) == text  # repeated full folds do not accumulate blocks


@pytest.mark.parametrize("role", ["last_order", "sisters/10032"])
async def test_custom_system_and_live_tool_selection(prompt_home, role):
    from misaka.config import identity

    async with assembled(prompt_home, role, "foreground", custom=True) as session:
        active = set(session.getActiveToolNames())
        text = await final_prompt(session)
        assert text.startswith("CUSTOM SYSTEM: keep this exact opening.\n")
        assert identity.COMMON_CHARTER in text
        header = "## Allies" if role == "last_order" else "## Sub-agents"
        assert header in text
        session.setActiveToolsByName(["read"])
        limited = await final_prompt(session)
        assert "## Allies" not in limited and "## Sub-agents" not in limited
        assert "## Last Order / Sister coordination" not in limited
        assert "## Sister capability profiles" in limited
        assert set(session.getActiveToolNames()) == {"read"}
        session.setActiveToolsByName(["SendMessage"])
        messages = await final_prompt(session)
        assert messages.count("## Last Order / Sister coordination") == 1
        assert "`last-order` or a registered Sister ID" in messages
        assert "- `misaka_card`:" not in messages
        assert "- `misaka_sister_message`:" not in messages
        if role != "last_order":
            session.setActiveToolsByName(["TaskOutput"])
            management = await final_prompt(session)
            assert "Only existing-task management" in management
            assert "`Agent`" not in management and "subagent_type" not in management
        session.setActiveToolsByName(sorted(active))
        restored = await final_prompt(session)
        assert restored.count(header) == 1
        assert restored.count("## Last Order / Sister coordination") == 1
        assert restored.count(identity.COMMON_CHARTER) == 1


async def test_bare_research_with_custom_system_keeps_duties_without_false_tools(prompt_home):
    from misaka.config import identity

    async with assembled(prompt_home, "last_order", "headless", research=True, custom=True) as session:
        session.setActiveToolsByName(["read"])
        text = await final_prompt(session)
        assert text.startswith("CUSTOM SYSTEM: keep this exact opening.\n")
        assert text.count(identity.COMMON_CHARTER) == text.count(identity.COORDINATOR_ROLE) == 1
        assert "## Sub-agents" not in text and "## Allies" not in text
        assert "## Last Order / Sister coordination" not in text
        assert "## Sister capability profiles" in text


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("custom", [False, True])
async def test_role_entry_keeps_shared_base_and_explicit_tool_ceiling(prompt_home, research, custom):
    from misaka.cli.args import parse_args
    from misaka.config import home, identity
    from misaka.core.wiring import role_session_setup

    shared = home.path("shared_soul")
    content = "# Shared fixture\n\n请用中文。Keep the user's shared conventions.\n"
    shared.write_text(content, encoding="utf-8")
    profile = prompt_home / "profiles" / "last_order"
    flags, assembly, _env = role_session_setup(
        str(profile), str(prompt_home / "workspace"), research_context=research)
    assert assembly.spec.kind == "foreground"
    assert (parse_args(flags).appendSystemPrompt or []).count(str(shared)) == 1
    assert parse_args(flags).tools is None and not parse_args(flags).noTools
    flags += ["--tools", "read"]
    async with assembled(prompt_home, "last_order", "headless", research=research,
                         custom=custom, prompt_flags=flags) as session:
        text = await final_prompt(session)
        assert text.count(content.strip()) == 1
        assert text.count(identity.COMMON_CHARTER) == text.count(identity.COORDINATOR_ROLE) == 1
        assert "Fixture voice for last_order." in text
        assert set(session.getActiveToolNames()) == {"read"}
        session.setActiveToolsByName(["read", "bash", "misaka_card"])
        assert set(session.getActiveToolNames()) == {"read"}  # Research never lifts CLI permission ceilings
        assert text.count("## Sister capability profiles") == 1
        assert "## Sub-agents" not in text and "## Allies" not in text
        assert await final_prompt(session) == text
    assert shared.read_text() == content


async def test_durable_sister_real_child_flags_retain_persona_and_current_tool_guidance(prompt_home):
    from misaka.cli.args import parse_args
    from misaka.config import identity
    from misaka.core.network.sister_runtime import _SisterManager
    from misaka.core.research.planner import RESEARCH_SISTER_DISCIPLINE
    from misaka.core.subagent.agents import AgentDefinition
    from misaka.core.subagent.runtime import AgentTask, RoleContext, SubagentManager
    from misaka.core.tools.office import office_tool_system_prompt_contribution

    role = "sisters/10032"
    manager = object.__new__(_SisterManager)
    manager.role_context = RoleContext(role, str(prompt_home / "profiles" / role),
                                       str(prompt_home / "workspace"), role)
    manager.research = {"run_id": "fixture-run"}
    persona = "Saved historical Sister personality; preserve this exact text.\n"
    definition = AgentDefinition("sister-10032", "Fixture Sister", persona, source="misaka-sister")
    task = AgentTask(manager=manager, id="fixture-agent", definition=definition, description="Fixture",
                     prompt="No execution", model_provider="anthropic", model_id="fixture-model",
                     cwd=manager.role_context.workspace, transcript=prompt_home / "runtime" / "agent.jsonl",
                     metadata_path=prompt_home / "runtime" / "agent.meta.json", parent_session_id="fixture")

    # Exercise the real atomic prompt-file writer, CLI parser, resource loader and SDK.
    # No subprocess, persisted card owner, or model session is started.
    flags = await manager._child_flags(task)
    parsed = parse_args(flags)
    assert parsed.systemPrompt is None and parsed.tools is None
    saved = Path(parsed.appendSystemPrompt[0])
    assert saved.read_text(encoding="utf-8") == persona == definition.prompt
    assert saved.stat().st_mode & 0o777 == 0o600
    async with assembled(prompt_home, role, "card", research=True, prompt_flags=flags) as session:
        text = await final_prompt(session)
        assert persona in text
        assert text.count(identity.COMMON_CHARTER) == text.count(identity.SISTER_ROLE) == 1
        assert text.count(RESEARCH_SISTER_DISCIPLINE) == 1
        assert "<tools>" in text and "## Sub-agents" in text
        assert "office" in session.getActiveToolNames()
        for guideline in office_tool_system_prompt_contribution["guidelines"]:
            assert guideline in text
        assert await final_prompt(session) == text

    generic_flags = await SubagentManager._child_flags(manager, task)
    assert "--system-prompt" in generic_flags and "--append-system-prompt" not in generic_flags


@pytest.mark.parametrize("tools", ["planning", "materials"])
async def test_research_scope_keeps_enabled_user_questions(prompt_home, tools):
    from types import SimpleNamespace

    from misaka.core.research import planner

    async with assembled(prompt_home, "last_order", "foreground", research=True) as session:
        question = session.getToolDefinition("AskUserQuestion")
        assert question is not None and question.parameters["properties"]["questions"]
        assert "AskUserQuestion" in session.getActiveToolNames()
        allowed = planner.RESEARCH_TOOLS if tools == "planning" else planner.MATERIAL_TOOLS
        names = planner.session_tools(SimpleNamespace(session=session), allowed)
        assert "AskUserQuestion" in names
        assert "misaka_card" not in names  # retaining questions must not reopen Board mutation
        with session.toolScope(list(names)):
            assert "AskUserQuestion" in session.getActiveToolNames()
            assert "- AskUserQuestion:" in await final_prompt(session)
            assert "Ask only when the answer materially changes" in session.systemPrompt
        session.setActiveToolsByName(["read"])
        assert "AskUserQuestion" not in planner.session_tools(SimpleNamespace(session=session), allowed)


async def test_role_questions_are_available_without_enabling_child_dialogs(prompt_home, monkeypatch):
    from misaka.core import ask_user
    from misaka.core.wiring import SessionSpec, ToolCollector

    async with assembled(prompt_home, "last_order", "headless", research=True) as session:
        assert "AskUserQuestion" in session.getActiveToolNames()
        definition = session.getToolDefinition("AskUserQuestion")
        result = await definition.execute("fixture", {"questions": [{
            "question": "Which scope?", "header": "Scope", "options": [
                {"label": "Narrow", "description": "One topic"},
                {"label": "Wide", "description": "Two topics"}]}]}, None, None, None)
        assert result["details"]["action"] == "unanswered"
        assert result["details"]["answers"] == {}
        assert "Which scope?" in result["content"][0]["text"]
        assert "do not assume approval" in result["content"][0]["text"]
    async with assembled(prompt_home, "sisters/10032", "card", research=True) as session:
        assert "AskUserQuestion" not in session.getActiveToolNames()
    monkeypatch.setenv("MISAKA_NET_PANE", "fixture")
    spec = SessionSpec(str(prompt_home / "profiles/sisters/10032"), "sisters/10032",
                       str(prompt_home / "workspace"), "card", research_context=True)
    collector = ToolCollector()
    ask_user.activate(spec)(collector)
    assert [tool.name for tool in collector.tools] == ["AskUserQuestion"]
    monkeypatch.setenv("MISAKA_SUBAGENT_ID", "fixture-child")
    assert ask_user.activate(spec) is None


@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("custom", [False, True])
async def test_lo_entrypoints_share_actual_default_tools_and_complete_prompt(prompt_home, research, custom):
    from contextlib import ExitStack
    from types import SimpleNamespace

    from misaka.core.research import planner
    from misaka.core.research.prompting import RESEARCH_LO_ORCHESTRATION

    texts, tools = [], []
    for kind in ("foreground", "headless"):
        async with assembled(prompt_home, "last_order", kind, research=research, custom=custom) as session:
            # Do not normalize both sessions to a hand-picked test list. Start from
            # the real default registry, then apply the production Research delta.
            with ExitStack() as scope:
                initial = session.getActiveToolNames()
                assert {"AskUserQuestion", "misaka_ally_start", "SendMessage"} <= set(initial)
                assert ("misaka_card" in initial) == (not research)
                if research:
                    scope.enter_context(session.toolScope(list(planner.session_tools(SimpleNamespace(session=session)))))
                    assert "misaka_card" not in session.getActiveToolNames()
                    assert "AskUserQuestion" in session.getActiveToolNames()
                tools.append(session.getActiveToolNames())
                texts.append(await final_prompt(session))
                assert texts[-1].count(RESEARCH_LO_ORCHESTRATION) == int(research)
                assert RESEARCH_LO_ORCHESTRATION.isascii()
            assert session.getActiveToolNames() == initial
    assert tools[0] == tools[1]
    assert texts[0] == texts[1]


@pytest.mark.parametrize("role,kind", [("last_order", "foreground"), ("sisters/10032", "card")])
async def test_research_overlay_is_reversible_without_losing_base(prompt_home, role, kind):
    from misaka.core.network.wiring.capabilities import SisterCapabilitiesPart
    from misaka.core.research.planner import RESEARCH_SISTER_DISCIPLINE
    from misaka.core.research.prompting import RESEARCH_LO_ORCHESTRATION

    async with assembled(prompt_home, role, kind) as session:
        session.setActiveToolsByName(["read"])
        base = await final_prompt(session)
        publisher = next(p for p in session.moments.parts if isinstance(p, SisterCapabilitiesPart))
        for _ in range(2):
            with publisher.snapshot():
                research = await final_prompt(session)
                assert await final_prompt(session) == research
                assert "Fixture voice for " + role in research
                assert research != base
                assert research.count(RESEARCH_LO_ORCHESTRATION) == int(role == "last_order")
                if kind == "card":
                    assert research.count(RESEARCH_SISTER_DISCIPLINE) == 1
            assert await final_prompt(session) == base


def test_lo_runtime_adapters_use_one_role_entry():
    import ast
    import inspect
    import textwrap

    from misaka.cli import chat
    from misaka.core.research import node, window

    for adapter in (chat.launch, node.run_interactive, window.node_session.__wrapped__):
        tree = ast.parse(textwrap.dedent(inspect.getsource(adapter)))
        calls = [n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert calls.count("role_session_setup") == 1
        assert not any(isinstance(n, ast.Constant) and n.value == "--thinking" for n in ast.walk(tree))
        assert not {"SessionSpec", "assemble", "Assembly", "build_system_prompt"}.intersection(calls)


async def test_research_role_keeps_tools_without_stealing_root_notifications(prompt_home):
    from unittest.mock import AsyncMock

    from misaka.core.network.wiring.network import NetworkPart
    from misaka.core.research.wiring.node import NodePart
    from misaka.core.research.wiring.research import ResearchPart

    async with assembled(prompt_home, "last_order", "headless", research=True) as session:
        parts = session.moments.parts
        assert not any(isinstance(p, (NodePart, ResearchPart)) for p in parts)
        network = next(p for p in parts if isinstance(p, NetworkPart))
        assert "misaka_card" not in session.getActiveToolNames()
        assert "misaka_board" in session.getActiveToolNames()
        network._resume_briefing = AsyncMock()
        network._settle_orphans = AsyncMock()
        network._collect_pending = AsyncMock()
        await network.session_start({}, None)
        await network.before_agent_start({}, None)
        network._resume_briefing.assert_not_called()
        network._settle_orphans.assert_not_called()
        network._collect_pending.assert_not_called()
        assert network._pending_watch is None


async def test_research_inherits_order_and_preserves_selection_through_lifecycle(prompt_home, monkeypatch):
    from misaka.core.platform.toolkit import tool_definition
    from misaka.core.research import planner
    from misaka.core.research.tool_policy import DRIVER_OWNED_TOOLS, research_tools
    from misaka.core.research.window import WindowLO

    async with assembled(prompt_home, "last_order", "foreground") as session:
        normal = session.getActiveToolNames()
        assert len(DRIVER_OWNED_TOOLS) == 9
        assert DRIVER_OWNED_TOOLS <= set(normal)
        # Configured permission ceiling is not permission to revive a user-disabled tool.
        monkeypatch.setattr(session, "_allowedToolNames", set(normal) | {"fixture_late", "misaka_research_assign"})
        selected = [name for name in normal if name not in {"write", "doc_verify"}]
        session.setActiveToolsByName(selected)
        owner = WindowLO(session, lambda: None, describe=dict)
        expected = list(research_tools(selected))
        assert session.getActiveToolNames() == expected
        assert list(planner.session_tools(owner)) == expected
        assert {"misaka_sister_stop", "misaka_sister_message", "skill_manage",
                "misaka_board", "misaka_ally_start", "SendMessage"} <= set(expected)
        for name in DRIVER_OWNED_TOOLS:
            result = await session.agent.beforeToolCall({"toolCall": {"name": name}, "args": {}})
            assert result["block"]
        assert not await session.agent.beforeToolCall({"toolCall": {"name": "misaka_sister_message"}, "args": {}})
        late = tool_definition(name="fixture_late", label="Late", description="Fixture", parameters={}, execute=None)
        phase = tool_definition(name="misaka_research_assign", label="Plan", description="Fixture", parameters={}, execute=None)
        try:
            with session.toolScope([*expected, phase.name]):
                session.registerCustomTools([phase])
                assert session.getActiveToolNames() == [*expected, phase.name]
                session.registerCustomTools([late])
                session.refreshTools()
                assert session.getActiveToolNames() == [*expected, phase.name]
                assert "write" not in session.getActiveToolNames()
                session.unregisterCustomTools([phase])
            # Between phases / while waiting for approval: late ordinary tools join,
            # phase commands leave, and the Research filter still applies.
            expected.append(late.name)
            assert session.getActiveToolNames() == expected
            session.refreshTools()
            assert session.getActiveToolNames() == expected
            await session.reload()
            assert session.getActiveToolNames() == expected
            assert list(planner.session_tools(owner)) == expected
        finally:
            await owner.close()
            await owner.close()  # restoration is idempotent
        assert session.getActiveToolNames() == [*selected, late.name]
        session.unregisterCustomTools([late])
        assert session.getActiveToolNames() == selected


@pytest.mark.parametrize("thinking", ["max", "medium", "off"])
@pytest.mark.parametrize("outcome", ["answer", "error", "cancelled"])
async def test_research_turn_rechecks_selection_and_keeps_mode_between_turns(prompt_home, monkeypatch, outcome, thinking):
    import asyncio
    from types import SimpleNamespace

    from misaka.core.platform.toolkit import tool_definition
    from misaka.core.research import window
    from misaka.core.research.tool_policy import research_tools

    monkeypatch.setattr(window.worker, "_reserve_usage", lambda *_: {"allowed": True, "token": None})
    monkeypatch.setattr(window.worker, "_UsageRecorder", lambda *_: SimpleNamespace(settle=lambda *_: None))
    async with assembled(prompt_home, "last_order", "foreground") as session:
        # Research must preserve the current selection, independent of model clamping.
        session.agent.state.thinkingLevel = thinking

        def unexpected_override(*_args, **_kwargs):
            raise AssertionError("Research changed the user-selected thinking level")

        monkeypatch.setattr(session, "setThinkingLevel", unexpected_override)
        normal = session.getActiveToolNames()
        # In-memory SDK session, but a stable path for WindowLO's routing check.
        session.sessionManager.sessionFile = str(prompt_home / "fixture.jsonl")
        owner = window.WindowLO(session, lambda: None, describe=dict)
        phase = tool_definition(name="misaka_research_assign", label="Plan", description="Fixture", parameters={}, execute=None)
        stale = session.getActiveToolNames()
        selected = [n for n in normal if n != "write"]
        session.setActiveToolsByName(selected)
        expected = list(research_tools(selected))
        seen = []

        async def send(*_args):
            assert session.agent.state.thinkingLevel == thinking
            # A user adjustment during a turn must also survive phase cleanup.
            session.agent.state.thinkingLevel = "low"
            seen.append(session.getActiveToolNames())
            assert seen[-1] == [*expected, phase.name]
            if outcome == "cancelled":
                raise asyncio.CancelledError
            if outcome == "error":
                raise RuntimeError("fixture failure")

        monkeypatch.setattr(session, "sendCustomMessage", send)
        try:
            call = owner._execute_turn("fixture", {"session_dir": str(prompt_home), "tools": stale,
                                                   "extra_tools": [phase], "thinking": "high"})
            if outcome == "answer":
                await call
            else:
                with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else RuntimeError):
                    await call
            assert session.agent.state.thinkingLevel == "low"
            assert len(seen) == 1
            assert session.getActiveToolNames() == expected
            assert phase.name not in {t.name for t in session.getAllTools()}
        finally:
            await owner.close()
        assert session.agent.state.thinkingLevel == "low"
        assert session.getActiveToolNames() == selected


def test_research_planning_and_report_do_not_set_thinking(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from misaka.core.research import planner, report

    worker = SimpleNamespace(run_llm_json=Mock(return_value=(None, "Fixture answer", None)))
    cfg = {"roles_root": str(tmp_path), "provider": "fixture", "default_model": "fixture"}
    planner._call(worker, cfg, "Plan", cwd=str(tmp_path), session_dir=str(tmp_path))
    monkeypatch.setattr(report, "_nodes", lambda *_: [])
    monkeypatch.setattr(report, "_boundary", lambda *_: [])
    run = {"id": "fixture", "workspace": str(tmp_path), "question": "Fixture?",
           "root_session": str(tmp_path / "session.jsonl")}
    assert report._write(None, run, cfg, worker, "Report", tools=()) == "Fixture answer"
    assert worker.run_llm_json.call_count == 2
    for call in worker.run_llm_json.call_args_list:
        assert "thinking" not in call.kwargs
