"""Card contracts and Sister thinking defaults; all artifacts are local fixtures."""
import json
from types import SimpleNamespace as NS

import pytest
from test_agent_model_runtime import (
    local_models as local_models,  # noqa: PLC0414
)
from test_agent_model_runtime import (
    opened,
    prompt,
)
from test_subagent_native_startup import isolated as isolated  # noqa: PLC0414

from misaka.config import home
from misaka.core.network import sister_runtime, worker
from misaka.core.research import planner, report
from misaka.core.subagent.agents import AgentDefinition
from misaka.core.subagent.runtime import SubagentManager


@pytest.mark.parametrize("declaration,name", [
    ("c6_italy_germany.md：①制度编年；②文本锚点", "c6_italy_germany.md"),
    ("findings.md: Sources and counterevidence.", "findings.md"),
    ("research report.md", "research report.md"),
    ("`研究 报告.md`：证据与不确定性", "研究 报告.md"),
])
def test_contract_names_only_the_file(tmp_path, declaration, name):
    task = {"body": f"## deliverable\n{declaration}\n\n## boundaries\nnone\n",
            "output_dir": str(tmp_path)}
    assert worker.contract_deliverable(task) == name
    assert worker.missing_deliverable(task) == name
    (tmp_path / name).write_text("")
    assert worker.missing_deliverable(task) == name
    (tmp_path / name).write_text("Fixture evidence.")
    assert worker.missing_deliverable(task) is None


@pytest.mark.parametrize("declaration", [
    "../outside.md", "/tmp/outside.md", "C:\\outside.md", "sub/report.md", "sub\\report.md",
    ".", "..", "", "\n## boundaries\nnone", "Write a report about the sources.",
    "Write `critique.md` under your card's deliverable directory.", "Write findings.md.",
    "Compare `one.md` and `two.md`.", "report.md\x00", "`one.md` and `two.md`",
    "one.md\n\n## deliverable\ntwo.md",
])
def test_bad_contract_fails_closed(tmp_path, declaration):
    task = {"body": f"## deliverable\n{declaration}\n", "output_dir": str(tmp_path)}
    assert worker.missing_deliverable(task), "A malformed contract must not silently waive the artifact gate"
    with pytest.raises(ValueError, match="deliverable"):
        worker.contract_deliverable(task)


def test_missing_output_and_symlink_escape_fail_closed(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    task = {"body": "## deliverable\nreport.md\n", "output_dir": None}
    assert worker.missing_deliverable(task) == "report.md"
    task["output_dir"] = str(out)
    outside = tmp_path / "outside.md"
    outside.write_text("Not this card's deliverable")
    (out / "report.md").symlink_to(outside)
    assert worker.missing_deliverable(task) == "report.md"
    (out / "report.md").unlink()
    (out / "report.md").mkdir()
    assert worker.missing_deliverable(task) == "report.md"
    (out / "report.md").rmdir()
    (out / "report.md").write_text("Fixture evidence")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").symlink_to(out, target_is_directory=True)
    task.update(workspace=str(workspace), output_dir=str(workspace / "out"))
    assert worker.missing_deliverable(task) == "report.md"  # the output root itself escaped


def test_research_generators_separate_filename_from_requirements(monkeypatch):
    body = planner.task_body({"question": "Question", "rationale": "Rationale",
                              "deliverable": "findings.md：证据与反证"})
    assert "## deliverable\n`findings.md`\n证据与反证\n" in body
    assert worker.contract_deliverable({"body": body}) == "findings.md"
    monkeypatch.setattr(report, "materials", lambda *args: "Fixture material map")
    review = report.review_body(None, {"question": "Question"},
                                {"path": "draft.md", "id": "draft", "sha256": "fixture"})
    assert "## deliverable\n`critique.md`\n" in review
    assert worker.contract_deliverable({"body": review}) == "critique.md"


def test_plan_rejects_an_ambiguous_deliverable_before_dispatch():
    task = {"local_id": "one", "title": "One", "question": "Question", "rationale": "Rationale",
            "deliverable": "Write a report about the sources.", "assignee": "10032"}
    with pytest.raises(ValueError, match="deliverable"):
        planner._validate_task(task, ["10032"], 1)


def card_flags(root, monkeypatch, model=None):
    from misaka.config import identity, sessions
    from misaka.core import wiring

    monkeypatch.setattr(worker.task_store, "task_state_dir", lambda _: str(root / "state"))
    monkeypatch.setattr(sessions, "card_session_dir", lambda _: str(root / "cards"))
    monkeypatch.setattr(worker.skill_sandbox, "snapshot_stack", lambda *args: None)
    monkeypatch.setattr(identity, "base_prompt_sources", lambda *args: [])
    monkeypatch.setattr(wiring, "assemble", lambda spec: NS(spec=spec))
    row = {"id": "fixture", "model": model, "body": "Task"}
    return worker.card_session_setup(row, str(root / "workspace"), str(home.path("profiles_root") / "10032"),
                                     "fixture-global", "global")[0]


@pytest.mark.parametrize("preference", [None, "medium"])
async def test_card_start_uses_defaults_and_native_resume(local_models, monkeypatch, preference):
    root, requests = local_models
    models_path = home.path("models")
    models = json.loads(models_path.read_text())
    models["providers"]["fixture-a"]["models"][0].update(
        reasoning=True, compat={"forceAdaptiveThinking": True})
    models_path.write_text(json.dumps(models))
    settings_path = home.path("settings")
    settings = json.loads(settings_path.read_text())
    settings["defaultThinkingLevel"] = "high"
    if preference:
        settings["modelThinkingLevels"] = {"fixture-a/shared": preference}
    settings_path.write_text(json.dumps(settings))
    flags = card_flags(root, monkeypatch)
    assert "--thinking" not in flags
    async with opened(root, "sisters/10032", "card", flags) as (_, session):
        assert session.thinkingLevel == (preference or "high")
        session.setThinkingLevel("low")
        await prompt(session, "FIXTURE_RESUME")
        session_file = session.sessionFile
    async with opened(root, "sisters/10032", "card", [*flags, "--session", session_file]) as (_, session):
        assert session.thinkingLevel == "low"
    explicit = card_flags(root, monkeypatch, "fixture-a/shared:high")
    async with opened(root, "sisters/10032", "card", explicit) as (_, session):
        assert session.thinkingLevel == "high"
    assert len(requests) == 1


@pytest.mark.parametrize("manager_type,expected", [(SubagentManager, "off"), (sister_runtime._SisterManager, None)])
async def test_only_board_sister_drops_generic_child_thinking_override(tmp_path, monkeypatch, manager_type, expected):
    manager = object.__new__(manager_type)
    monkeypatch.setattr(manager, "_refresh_task_project_trust", lambda task: None)
    task = NS(id="fixture", metadata_path=tmp_path / "meta.json", transcript=tmp_path / "session.jsonl",
              definition=AgentDefinition(name="fixture", description="Fixture", prompt="Fixture persona", source="fixture"),
              model_provider="fixture", model_id="model", project_trusted=True)
    flags = await manager._child_flags(task)
    assert flags[flags.index("--model") + 1] == "model"
    assert flags[flags.index("--session") + 1] == str(task.transcript)
    if expected is None:
        assert "--thinking" not in flags
    else:
        assert flags[flags.index("--thinking") + 1] == expected


@pytest.mark.parametrize("override", [None, "fixture-a/shared:low"])
async def test_managed_sister_native_registration_defaults_and_resume(local_models, monkeypatch, override):
    root, requests = local_models
    models_path = home.path("models")
    models = json.loads(models_path.read_text())
    models["providers"]["fixture-a"]["models"][0].update(
        reasoning=True, compat={"forceAdaptiveThinking": True})
    models_path.write_text(json.dumps(models))
    settings_path = home.path("settings")
    settings = json.loads(settings_path.read_text())
    settings["defaultThinkingLevel"] = "high"
    settings_path.write_text(json.dumps(settings))
    card_flags(root, monkeypatch)  # isolate state/skill/identity paths, not session registration
    row = {"id": "fixture", "model": override, "assignee": "10032", "generation": 1,
           "claim_lock": "fixture", "output_dir": str(root / "workspace")}
    cfg = {"profiles_root": str(home.path("profiles_root")), "provider": "fixture-global",
           "default_model": "global", "db": str(root / "fixture.db")}
    async with opened(root, "last_order", "foreground") as (_, parent):
        assert parent.thinkingLevel == "off"  # the parent's model cannot reason
        manager = sister_runtime._SisterManager(parent, cfg, row, str(root / "workspace"))
        task = await manager.create_task(
            definition=manager.resolve_definition(None, str(root / "workspace")), description="Fixture Sister",
            prompt="Fixture", model=None, background=False, name=None, isolation=None,
            cwd=None, tool_call_id="", context=parent)
        try:
            seed = [json.loads(line) for line in task.transcript.read_text().splitlines()]
            assert [entry["type"] for entry in seed] == ["session"]
            flags = await manager._child_flags(task)
            async with opened(root, "sisters/10032", "card", flags) as (_, child):
                assert child.thinkingLevel == ("low" if override else "high")
                await prompt(child, "MANAGED_FRESH")
                child.setThinkingLevel("medium")
            flags = await manager._child_flags(task)
            async with opened(root, "sisters/10032", "card", flags) as (_, child):
                assert child.thinkingLevel == ("low" if override else "medium")
                await prompt(child, "MANAGED_RESUME")
        finally:
            task.status = "completed"  # no process was launched for this registered fixture
            await manager.close()
    expected = ["low", "low"] if override else ["high", "medium"]
    assert [payload["output_config"]["effort"] for _, payload in requests] == expected
