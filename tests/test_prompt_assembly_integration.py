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
    monkeypatch.setenv("MISAKA_MCP_CACHE", str(tmp_path / "mcp-cache.json"))
    monkeypatch.setenv("LCM_DB_PATH", str(tmp_path / "lcm.db"))
    monkeypatch.chdir(tmp_path)

    from misaka.cli import bootstrap
    from misaka.config import CFG
    from misaka.core import wiring
    from misaka.core.network.wiring import network
    from misaka.core.research.wiring import research

    monkeypatch.setattr(wiring, "bundled", wiring.bundled)
    bootstrap.install()
    for key, leaf in (("roles_root", "profiles"), ("profiles_root", "profiles/sisters"),
                      ("db", "board.db"), ("messages_db", "messages.db"), ("lcm_db", "lcm.db"),
                      ("tasks_root", "tasks"), ("web_config", "web.json"), ("web_cache", "cache/web")):
        monkeypatch.setitem(CFG, key, str(tmp_path / leaf))
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
    from misaka.config import identity, profiles
    from misaka.core.extensions.runner import emit_session_shutdown_event
    from misaka.core.network import worker
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import create_agent_session
    from misaka.core.session_manager import SessionManager
    from misaka.core.wiring import SessionSpec, assemble

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
    if kind == "bare":
        bare_flags, assembly, _env = worker.bare_session_setup(
            str(profile), "fixture", "fixture-model", cwd=str(workspace),
            soul=False, research_context=research)
        sections = parse_args(bare_flags).appendSystemPrompt or []
    else:
        sections = [profiles.shared_soul(), *identity.prompt_sections(str(profile), role)]
    if research and kind == "card":
        sections += worker.research_addendum_flags({"_research": {"run_id": "fixture-run"}})[1::2]
    options = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    prompt_options = {"appendSystemPrompt": sections}
    if prompt_flags is not None:
        parsed = parse_args(prompt_flags)
        prompt_options = {"systemPrompt": parsed.systemPrompt,
                          "appendSystemPrompt": parsed.appendSystemPrompt or []}
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
    return session.state.systemPrompt


@pytest.mark.parametrize(("role", "kind", "research"), [
    ("last_order", "foreground", False),
    ("last_order", "foreground", True),
    ("last_order", "bare", True),
    ("sisters/10032", "foreground", False),
    ("sisters/10032", "card", False),
    ("sisters/10032", "card", True),
])
async def test_real_role_assembly(prompt_home, role, kind, research):
    from misaka.config import identity
    from misaka.core.network.wiring.collaboration import SISTER_TOOLS
    from misaka.core.research.planner import RESEARCH_SISTER_DISCIPLINE

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

    async with assembled(prompt_home, "last_order", "bare", research=True, custom=True) as session:
        session.setActiveToolsByName(["read"])
        text = await final_prompt(session)
        assert text.startswith("CUSTOM SYSTEM: keep this exact opening.\n")
        assert text.count(identity.COMMON_CHARTER) == text.count(identity.COORDINATOR_ROLE) == 1
        assert "## Sub-agents" not in text and "## Allies" not in text
        assert "## Last Order / Sister coordination" not in text
        assert "## Sister capability profiles" in text


@pytest.mark.parametrize("soul", [False, True])
@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("custom", [False, True])
async def test_research_bare_loads_shared_file_independently_of_role_persona(prompt_home, soul, resumed, custom):
    from misaka.cli.args import parse_args
    from misaka.config import identity
    from misaka.core.network import worker

    shared = prompt_home / "profiles" / "MISAKA.md"
    content = "# Shared fixture\n\n请用中文。Keep the user's shared research conventions.\n"
    shared.write_text(content, encoding="utf-8")
    before = shared.read_bytes()
    profile = prompt_home / "profiles" / "last_order"
    options = {"session_dir": str(prompt_home / "sessions"), "continue_session": True,
               "session_file": str(prompt_home / "sessions" / "existing.jsonl")} if resumed else {}
    flags, assembly, _env = worker.bare_session_setup(
        str(profile), "fixture", "fixture-model", cwd=str(prompt_home / "workspace"),
        tools=["read"], soul=soul, research_context=True, **options)
    parsed = parse_args(flags)
    assert assembly.spec.kind == "bare" and assembly.spec.research_context
    assert (parsed.appendSystemPrompt or []).count(str(shared)) == 1
    assert parsed.tools == ["read"]
    assert ("--session" in flags) == resumed
    assert ("--no-session" in flags) != resumed
    async with assembled(prompt_home, "last_order", "bare", research=True,
                         custom=custom, prompt_flags=flags) as session:
        session.setActiveToolsByName(parsed.tools)
        text = await final_prompt(session)
        assert text.count(content.strip()) == 1
        assert text.count(identity.COMMON_CHARTER) == text.count(identity.COORDINATOR_ROLE) == 1
        assert ("Fixture voice for last_order." in text) == soul
        assert set(session.getActiveToolNames()) == {"read"}
        assert text.count("## Sister capability profiles") == 1
        assert "## Sub-agents" not in text and "## Allies" not in text
        assert await final_prompt(session) == text
    assert shared.read_bytes() == before


@pytest.mark.parametrize("soul", [False, True])
def test_nonresearch_bare_keeps_its_existing_shared_file_policy(prompt_home, soul):
    from misaka.cli.args import parse_args
    from misaka.core.network import worker

    shared = prompt_home / "profiles" / "MISAKA.md"
    flags, _assembly, _env = worker.bare_session_setup(
        str(prompt_home / "profiles" / "last_order"), "fixture", "fixture-model",
        cwd=str(prompt_home / "workspace"), soul=soul, research_context=False)
    assert str(shared) not in (parse_args(flags).appendSystemPrompt or [])
    assert not shared.exists()


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
        assert "Available tools:" in text and "## Sub-agents" in text
        assert "office" in session.getActiveToolNames()
        for guideline in office_tool_system_prompt_contribution["guidelines"]:
            assert guideline in text
        assert await final_prompt(session) == text

    generic_flags = await SubagentManager._child_flags(manager, task)
    assert "--system-prompt" in generic_flags and "--append-system-prompt" not in generic_flags
