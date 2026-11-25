import json
import os
from pathlib import Path

import pytest
from test_research_node_routine import RED, RESEARCHER, lab  # noqa: F401 - the fixture

from misaka.cli.args import parseArgs
from misaka.core.documents import workspace as doc_workspace
from misaka.core.network import dispatch, worker
from misaka.core.platform import cards, home_guard, tasks
from misaka.core.research import bundle, commands, planner, runs, workflow
from misaka.core.web import evidence


def plan(local_work=None):
    task = {
        "local_id": "local",
        "title": "Count the records",
        "question": "How many records are included?",
        "rationale": "Produce the count for the comparison.",
        "deliverable": "evidence.md\nState the exact count.",
        "assignee": RESEARCHER,
        "assignee_reason": "Independent reader.",
    }
    if local_work is not None:
        task["local_work"] = local_work
    return {"status": "ready", "plan_markdown": "Count the records.", "tasks": [task],
            "red_team": {"assignee": RED, "reason": "Check the count."}}


def validate(payload, workspace):
    return planner.validate_plan(commands.Plan.model_validate(payload).model_dump(), [RESEARCHER, RED],
                                 workspace=workspace)


@pytest.mark.parametrize("scope", [
    {"input_paths": ["../outside.txt"], "tools": ["read", "write"]},
    {"input_paths": ["C:\\secrets.txt"], "tools": ["read", "write"]},
    {"input_paths": ["missing.txt"], "tools": ["read", "write"]},
    {"input_paths": ["."], "tools": ["read", "write"]},
    {"input_paths": ["input.txt"], "tools": ["read", "write", "web_fetch"]},
    {"input_paths": ["input.txt"], "tools": ["read"]},
])
def test_local_work_rejects_unusable_paths_and_tools(tmp_path, scope):
    (tmp_path / "input.txt").write_text("input", encoding="utf-8")
    with pytest.raises(ValueError, match="local_work|Local work"):
        validate(plan(scope), str(tmp_path))


async def test_research_dispatch_preserves_opt_in_and_renders_a_reusable_output(lab):  # noqa: F811
    project = lab.tmp
    source = project / "input.txt"
    source.write_text("fixture", encoding="utf-8")
    scope = {"input_paths": ["input.txt"], "tools": ["read", "write"]}
    bounded = validate(plan(scope), str(project))["tasks"][0]
    ordinary = {**validate(plan(), str(project))["tasks"][0], "local_id": "ordinary"}
    ids = await workflow._submit_tasks(lab.con, lab.run, lab.root, [bounded, ordinary], kind="research")
    assert cards.read(cards.card_path(str(project), ids["local"]))["fields"]["local_work"] == scope

    def setup(row, *, beast=False):
        task = {**dict(row), "beast": beast}
        task.update(worker.card_extras(lab.con, row))
        return worker.card_session_setup(task, str(project), str(project / "profiles" / RESEARCHER), "p", "m")

    row = tasks.get(lab.con, ids["local"])
    expected = Path(row["output_dir"]) / "evidence.md"
    flags, assembly, prompt, _ = setup(row)
    assert assembly.spec.card_file_scope is not None
    assert set(parseArgs(flags).tools) == {"read", "write", "misaka_card_note", "misaka_card_complete"}
    assert parseArgs(flags).noMcp
    assert str(source.resolve()) in prompt and str(expected.resolve()) in prompt
    assert "already exists" not in prompt
    guard = home_guard.part(assembly.spec)
    unlisted = project / "other.txt"
    unlisted.write_text("Other card's material", encoding="utf-8")
    assert await guard.tool_call({"toolName": "read", "input": {"path": "input.txt"}}) is None
    assert (await guard.tool_call({"toolName": "read", "input": {"path": str(unlisted)}}))["block"]
    assert (await guard.tool_call({"toolName": "edit", "input": {"path": str(source)}}))["block"]
    assert (await guard.tool_call({"toolName": "bash", "input": {}}))["block"]
    assert (await guard.tool_call({"toolName": "write", "input": {"path": str(expected.with_name("extra.md"))}}))["block"]
    assert await guard.tool_call({"toolName": "write", "input": {"path": str(expected)}}) is None

    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_text("2 records", encoding="utf-8")
    flags_again, resumed_assembly, prompt_again, _ = setup(tasks.get(lab.con, ids["local"]))
    assert parseArgs(flags_again).tools == parseArgs(flags).tools
    assert resumed_assembly.spec.card_file_scope == assembly.spec.card_file_scope
    assert "already exists" in prompt_again
    assert await home_guard.part(resumed_assembly.spec).tool_call(
        {"toolName": "read", "input": {"path": str(expected)}}) is None

    near_cap_flags, near_cap_assembly, _, _ = setup(row, beast=True)
    assert near_cap_assembly.spec.card_file_scope == assembly.spec.card_file_scope
    assert parseArgs(near_cap_flags).tools == parseArgs(flags).tools

    ordinary_flags, ordinary_assembly, _, _ = setup(tasks.get(lab.con, ids["ordinary"]))
    assert ordinary_assembly.spec.card_file_scope is None
    assert parseArgs(ordinary_flags).tools is None


def test_local_work_rejects_a_link_to_an_input_outside_the_project(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="existing file inside the project"):
        validate(plan({"input_paths": ["linked.txt"]}), str(workspace))


def create_bounded(con, workspace, *, deliverable="evidence.md", tools=None):
    (workspace / "input.txt").write_text("fixture", encoding="utf-8")
    return cards.create(con, str(workspace), "Count", f"## Deliverable\n`{deliverable}`", RESEARCHER,
                        local_work={"input_paths": ["input.txt"], "tools": tools or ["read", "write"]})


@pytest.mark.parametrize("name", ["memo.docx", "data.xlsx", "slides.pptx", "CON.md", "bad?.md", "SOURCES.md"])
def test_local_work_rejects_unwritable_deliverable_in_plan_and_direct_create(lab, name):  # noqa: F811
    project = lab.tmp
    (project / "input.txt").write_text("fixture", encoding="utf-8")
    payload = plan({"input_paths": ["input.txt"]})
    payload["tasks"][0]["deliverable"] = f"`{name}`"
    with pytest.raises(ValueError, match="deliverable|cannot write"):
        validate(payload, str(project))
    before = lab.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    with pytest.raises(ValueError, match="deliverable|cannot write"):
        create_bounded(lab.con, project, deliverable=name)
    assert lab.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


@pytest.mark.parametrize("changed", [None, {"input_paths": ["other.txt"]}, "unreadable"])
def test_created_event_guards_against_changed_scope_mirror(lab, changed):  # noqa: F811
    task_id = create_bounded(lab.con, lab.tmp)
    row = tasks.get(lab.con, task_id)
    created = json.loads(tasks.latest_payload(lab.con, task_id, "created"))
    assert created["local_work"] == {"input_paths": ["input.txt"], "tools": ["read", "write"]}
    if changed == "unreadable":
        Path(cards.card_path(row["workspace"], task_id)).write_bytes(b"\xff")
    else:
        (lab.tmp / "other.txt").write_text("other", encoding="utf-8")
        cards.set_fields(row["workspace"], task_id, local_work=changed)
    task = {**dict(row), **worker.card_extras(lab.con, row, include_materials=False)}
    with pytest.raises(ValueError, match="local work|Local work"):
        worker.card_session_setup(task, row["workspace"], str(lab.tmp / RESEARCHER), "p", "m")


async def test_local_work_write_and_edit_replace_only_the_output_hardlink(lab):  # noqa: F811
    from misaka.core.tools.edit import createEditToolDefinition
    from misaka.core.tools.write import createWriteToolDefinition

    task_id = create_bounded(lab.con, lab.tmp, tools=["read", "write", "edit"])
    row = tasks.get(lab.con, task_id)
    task = {**dict(row), **worker.card_extras(lab.con, row, include_materials=False)}
    _, assembly, _, _ = worker.card_session_setup(task, row["workspace"], str(lab.tmp / RESEARCHER), "p", "m")
    out = Path(row["output_dir"], "evidence.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    original = lab.tmp / "input.txt"
    os.link(original, out)
    guard = home_guard.part(assembly.spec)
    def event(name, target):
        return {"toolName": name, "input": {"path": str(target)}}

    assert await guard.tool_call(event("read", out)) is None
    assert await guard.tool_call(event("write", out)) is None
    options = {}
    guard.configure_tool_options(options)
    write = createWriteToolDefinition(row["workspace"], options["write"])
    edit = createEditToolDefinition(row["workspace"], options["edit"])
    await edit.execute("edit", {"path": str(out), "edits": [{"oldText": "fixture", "newText": "edited"}]})
    assert original.read_text(encoding="utf-8") == "fixture"
    assert out.read_text(encoding="utf-8") == "edited"
    frozen = out.with_name("frozen.md")
    os.link(out, frozen)
    await write.execute("write", {"path": str(out), "content": "updated"})
    assert out.read_text(encoding="utf-8") == "updated"
    assert frozen.read_text(encoding="utf-8") == "edited"
    with pytest.raises(ValueError, match="deliverable"):
        await write.execute("outside", {"path": str(frozen), "content": "denied"})


@pytest.mark.parametrize(("input_name", "allowed"), [
    ("input.txt", True), ("./input.txt", True), ("./~notes.txt", False), ("./ notes.txt", False),
])
def test_readable_legacy_yaml_still_supplies_its_local_work_scope(lab, input_name, allowed):  # noqa: F811
    task_id = create_bounded(lab.con, lab.tmp)
    lab.con.execute("UPDATE events SET payload=json_remove(payload, '$.local_work', '$.local_deliverable') "
                    "WHERE task_id=? AND kind='created'", (task_id,))
    path = lab.tmp / input_name
    path.write_text("fixture", encoding="utf-8")
    row = tasks.get(lab.con, task_id)
    cards.set_fields(row["workspace"], task_id, local_work={"input_paths": [input_name]})
    if allowed:
        assert worker.load_card_file_scope(lab.con, row).inputs == (str(path),)
    else:
        with pytest.raises(worker.InvalidLocalWork, match="project-relative file path"):
            worker.load_card_file_scope(lab.con, row)


async def test_legacy_scope_requires_the_card_file_when_its_owner_says_so(lab):  # noqa: F811
    project = lab.tmp
    (project / "input.txt").write_text("fixture", encoding="utf-8")
    bounded = validate(plan({"input_paths": ["input.txt"]}), str(project))["tasks"][0]
    ids = await workflow._submit_tasks(lab.con, lab.run, lab.root, [bounded], kind="research")
    task_id = ids["local"]
    lab.con.execute("UPDATE events SET payload=json_remove(payload, '$.local_work', '$.local_deliverable') "
                    "WHERE task_id=? AND kind='created'", (task_id,))
    row = tasks.get(lab.con, task_id)
    card_path = Path(cards.card_path(row["workspace"], task_id))
    card_path.rename(card_path.with_suffix(".bak"))
    with pytest.raises(worker.InvalidLocalWork, match="mirror could not be read"):
        worker.load_card_file_scope(lab.con, row)


async def test_local_work_limits_evidence_submission_index_and_bundle_following(lab):  # noqa: F811
    project = lab.tmp
    ordinary = {**validate(plan(), str(project))["tasks"][0], "local_id": "ordinary"}
    ids = await workflow._submit_tasks(lab.con, lab.run, lab.root, [ordinary], kind="research")
    other = tasks.get(lab.con, ids["ordinary"])
    relay = Path(other["output_dir"]) / "evidence.md"
    pages = project / "downloads" / "pages"
    pages.mkdir(parents=True)
    allowed_page = pages / "allowed.md"
    unrelated_page = pages / "unrelated.md"
    allowed_url, unrelated_url = "https://example.org/allowed", "https://example.org/unrelated"
    allowed_page.write_text(f'---\nsource_url: "{allowed_url}"\n---\nAllowed evidence.', encoding="utf-8")
    unrelated_page.write_text(f'---\nsource_url: "{unrelated_url}"\n---\nOther evidence.', encoding="utf-8")
    relay.write_text(f"The other card cited {unrelated_url}.", encoding="utf-8")
    runs.register_file(lab.con, lab.run["id"], "task_output", "Relay", str(relay),
                       sha256=bundle.corpus.sha256_file(relay), task_id=other["id"])
    allowed_doc, _ = bundle.corpus.ingest(str(allowed_page), workspace=str(project))
    unrelated_doc, _ = bundle.corpus.ingest(str(unrelated_page), workspace=str(project))
    read_provenance = evidence.read_provenance

    def read_declared_provenance(path):
        assert Path(path) != unrelated_page
        return read_provenance(path)

    lab.monkeypatch.setattr(evidence, "read_provenance", read_declared_provenance)

    bounded = validate(plan({"input_paths": [relay.relative_to(project).as_posix(),
                                             allowed_page.relative_to(project).as_posix()]}), str(project))["tasks"][0]
    ids = await workflow._submit_tasks(lab.con, lab.run, lab.root, [bounded], kind="research")
    task_id = ids["local"]
    assert tasks.claim(lab.con, task_id, "test-owner")
    row = tasks.get(lab.con, task_id)
    output = Path(row["output_dir"]) / "evidence.md"
    extra = output.with_name("extra.md")
    extra.write_text("Spawned output", encoding="utf-8")
    worker.record_output_baseline(lab.con, row)
    assert json.loads(tasks.latest_payload(lab.con, task_id, "artifact_baseline")) == {"files": {}}
    output.write_text(f"Sources: `{relay.relative_to(project).as_posix()}` and {allowed_url}", encoding="utf-8")
    relative = output.relative_to(project).as_posix()
    extra_relative = extra.relative_to(project).as_posix()
    assert tasks.add_event(lab.con, task_id, "artifact_written", {"path": extra_relative})

    checked = worker.scoped_findings(lab.con, row, [{"text": "Known fact", "source_file": str(allowed_page),
                                                     "doc_id": allowed_doc, "page": 1},
                                                    {"text": "Known URL", "source_file": allowed_url}])
    assert checked[0]["source_file"] == allowed_page.relative_to(project).as_posix()
    assert checked[1]["source_file"] == allowed_url
    for locator in ({"source_file": str(unrelated_page)}, {"source_file": unrelated_url},
                    {"doc_id": unrelated_doc}, {"source_file": f"doc:{unrelated_doc}#p1"}):
        with pytest.raises(ValueError, match="declared local input"):
            worker.scoped_findings(lab.con, row, [{"text": "Outside claim", **locator}])
    assert tasks.add_event(lab.con, task_id, "card_evidence", {"findings": checked})
    submitted = worker.build_submission(lab.con, row, "Counted")
    assert submitted["artifacts"] == [relative]
    assert len(submitted["findings"]) == 2
    indexed = doc_workspace.ingest_artifacts(lab.con, row, [extra_relative, relative])
    assert [path for _, path in indexed] == [relative]

    runs.register_file(lab.con, lab.run["id"], "task_output", "Deliverable", str(output),
                       sha256=bundle.corpus.sha256_file(output), task_id=task_id)
    runs.register_file(lab.con, lab.run["id"], "task_output", "Unexpected sibling", str(extra),
                       sha256=bundle.corpus.sha256_file(extra), task_id=task_id)
    manifest = Path(bundle.card_bundle(lab.con, lab.run, row)).read_text(encoding="utf-8")
    assert "Unexpected sibling" not in manifest
    bundled = {path.read_text(encoding="utf-8") for path in (output.parent / "sources").rglob("*.md")}
    assert bundled == {allowed_page.read_text(encoding="utf-8"), relay.read_text(encoding="utf-8")}


async def test_scope_drift_reopens_only_its_own_research_card(lab):  # noqa: F811
    project = lab.tmp
    (project / "input.txt").write_text("fixture", encoding="utf-8")
    bounded = validate(plan({"input_paths": ["input.txt"]}), str(project))["tasks"][0]
    ordinary = {**validate(plan(), str(project))["tasks"][0], "local_id": "ordinary"}
    ids = await workflow._submit_tasks(lab.con, lab.run, lab.root, [bounded, ordinary], kind="research")
    for local_id, task_id in ids.items():
        lock = "test-claim"
        assert tasks.claim(lab.con, task_id, lock)
        row = tasks.get(lab.con, task_id)
        output = Path(row["output_dir"]) / "evidence.md"
        output.write_text("A verified result.", encoding="utf-8")
        finding = {"text": "A finding", "source_file": "input.txt" if local_id == "ordinary" else "outside.md"}
        assert dispatch.accept_state(lab.con, row, {"summary": "Done", "findings": [finding],
                                                    "artifacts": [output.relative_to(project).as_posix()]},
                                     generation=row["generation"], claim_lock=lock)
    workflow.settle_done_tasks(lab.con, run_id=lab.run["id"])
    scoped = tasks.get(lab.con, ids["local"])
    assert scoped["status"] == "ready" and scoped["generation"] == 2
    assert "declared local input" in tasks.latest_payload(lab.con, scoped["id"], "research_artifact_drift")
    assert tasks.latest_payload(lab.con, ids["ordinary"], "research_v2_settled") is not None


@pytest.mark.parametrize("input_name", ["./~notes.txt", "./ notes.txt", "notes.txt /."])
def test_local_work_rejects_normalized_tilde_and_space_paths_at_plan_and_create(lab, input_name):  # noqa: F811
    project = lab.tmp
    (project / "~notes.txt").write_text("fixture", encoding="utf-8")
    (project / " notes.txt").write_text("fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="project-relative file path"):
        validate(plan({"input_paths": [input_name]}), str(project))
    with pytest.raises(ValueError, match="project-relative file path"):
        cards.create(
            lab.con, str(project), "Bounded", "## Deliverable\nevidence.md\n\nDo work.", "10032",
            local_work={"input_paths": [input_name], "tools": ["read", "write"]},
        )


def test_project_file_blocks_git_inside_symlinked_workspace(directory_alias):
    real_ws, link_ws = directory_alias
    workspaces = (real_ws,) if link_ws.samefile(real_ws) else (real_ws, link_ws)
    for workspace in workspaces:
        (workspace / ".git").mkdir()
        (workspace / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (workspace / "notes.txt").write_text("ok", encoding="utf-8")

    assert doc_workspace.project_file(str(link_ws / ".git" / "config"), str(link_ws)) is None
    assert doc_workspace.project_file(str(link_ws / "notes.txt"), str(link_ws)) == str(real_ws / "notes.txt")
