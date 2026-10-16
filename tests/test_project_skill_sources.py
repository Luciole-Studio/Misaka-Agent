"""Project host skills use the same discovery, review and write source identity."""
import json
from unittest.mock import AsyncMock

import pytest
from jsonschema import validate

from misaka.config import CFG
from misaka.core.skills import index, layers, manage, review
from misaka.core.skills.wiring.skills import SkillsPart

BODY = "---\nname: sample-skill\ndescription: Explain a fictional example.\n---\nOriginal body.\n"


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(layers, "external_skills_dirs", list)
    monkeypatch.setitem(CFG, "roles_root", str(tmp_path / "profiles"))
    index.invalidate()
    yield
    index.invalidate()


def skill(root):
    folder = root / "sample-skill"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(BODY, encoding="utf-8")
    return folder


def patch_args(profile, workspace, **extra):
    return dict(action="patch", name="sample-skill", profile_dir=str(profile),
                workspace=str(workspace), old_string="Original body.",
                new_string="Revised body.", **extra)


@pytest.mark.parametrize("relative", ["skills", ".agents/skills", ".claude/skills", ".codex/skills", ".grok/skills"])
def test_project_edit_updates_the_host_source_without_shadowing_it(tmp_path, relative):
    source = skill(tmp_path / relative)
    profile = tmp_path / "profiles" / "role"
    other = skill(profile / "skills")
    layers.write_skills_config({"skill_write_mode": "allow"})

    result = manage.manage(**patch_args(profile, tmp_path))

    assert result["success"], result
    assert result["written_paths"] == [str(source.resolve())]
    assert "Revised body." in (source / "SKILL.md").read_text(encoding="utf-8")
    assert (other / "SKILL.md").read_text(encoding="utf-8") == BODY


def test_explicit_source_selects_one_independent_project_copy(tmp_path):
    first = skill(tmp_path / ".claude" / "skills")
    second = skill(tmp_path / ".codex" / "skills")
    profile = tmp_path / "profiles" / "role"
    layers.write_skills_config({"skill_write_mode": "allow"})
    ambiguous = manage.manage(**patch_args(profile, tmp_path))
    assert not ambiguous["success"] and "Ambiguous" in ambiguous["error"]
    roots = layers.skill_roots(str(profile), str(tmp_path))
    assert len(index.runtime_build(roots)) == 1
    assert len(index.runtime_build(roots, include_sources=True)) == 2

    result = manage.manage(**patch_args(profile, tmp_path, source=str(second / "SKILL.md")))

    assert result["success"], result
    assert result["written_paths"] == [str(second.resolve())]
    assert (first / "SKILL.md").read_text(encoding="utf-8") == BODY
    assert "Revised body." in (second / "SKILL.md").read_text(encoding="utf-8")


def test_source_can_select_a_configured_external_copy_beside_the_role_copy(tmp_path, monkeypatch):
    external = tmp_path / "external-host" / "skills"
    source = skill(external)
    profile = tmp_path / "profiles" / "role"
    role_copy = skill(profile / "skills" / "general")
    monkeypatch.setattr(layers, "external_skills_dirs", lambda: [str(external)])
    layers.write_skills_config({"skill_write_mode": "allow"})

    result = manage.manage(**patch_args(profile, tmp_path, source=str(source / "SKILL.md")))

    assert result["success"], result
    assert result["written_paths"] == [str(source.resolve())]
    assert "Revised body." in (source / "SKILL.md").read_text(encoding="utf-8")
    assert (role_copy / "SKILL.md").read_text(encoding="utf-8") == BODY


def test_legacy_batch_source_is_not_silently_discarded(tmp_path):
    first = skill(tmp_path / ".claude" / "skills")
    second = skill(tmp_path / ".codex" / "skills")
    profile = tmp_path / "profiles" / "role"
    layers.write_skills_config({"skill_write_mode": "allow"})
    operations = [{"action": "patch", "old_string": "Original body.", "new_string": "Revised body."}]

    result = manage.manage(name="sample-skill", source=str(second / "SKILL.md"),
                           operations=operations, profile_dir=str(profile), workspace=str(tmp_path))

    assert result["success"], result
    assert (first / "SKILL.md").read_text(encoding="utf-8") == BODY
    assert "Revised body." in (second / "SKILL.md").read_text(encoding="utf-8")


def test_source_cannot_address_an_unindexed_directory_or_override_create(tmp_path):
    source = skill(tmp_path / "outside-index")
    profile = tmp_path / "profiles" / "role"
    layers.write_skills_config({"skill_write_mode": "allow"})
    result = manage.manage(**patch_args(profile, tmp_path, source=str(source / "SKILL.md")))
    assert not result["success"] and "Unknown" in result["error"]
    created = manage.manage("create", "sample-skill", profile_dir=str(profile), workspace=str(tmp_path),
                            source=str(source / "SKILL.md"), content=BODY)
    assert not created["success"] and "create destination" in created["error"]
    assert (source / "SKILL.md").read_text(encoding="utf-8") == BODY
    assert not (profile / "skills" / "sample-skill").exists()


def test_batch_never_overwrites_independent_sources_with_one_staging_copy(tmp_path):
    sources = [skill(tmp_path / host / "skills") for host in (".claude", ".grok")]
    profile = tmp_path / "profiles" / "role"
    layers.write_skills_config({"skill_write_mode": "allow"})
    operations = [{"action": "patch", "name": "sample-skill", "source": str(folder / "SKILL.md"),
                   "old_string": "Original body.", "new_string": f"Revised body {i}."}
                  for i, folder in enumerate(sources)]

    result = manage.manage(profile_dir=str(profile), workspace=str(tmp_path), operations=operations)

    assert not result["success"] and "separate skill_manage call" in result["error"]
    assert all((folder / "SKILL.md").read_text(encoding="utf-8") == BODY for folder in sources)


def test_review_and_approval_keep_the_explicit_source(tmp_path):
    first = skill(tmp_path / ".claude" / "skills")
    second = skill(tmp_path / ".codex" / "skills")
    profile = tmp_path / "profiles" / "role"
    layers.write_skills_config({})

    staged = manage.manage(**patch_args(profile, tmp_path, source=str(second / "SKILL.md")))
    assert staged["staged"], staged
    [pending] = review.pending_items(with_diff=True)
    assert pending["sources"] == [str(second.resolve())]
    assert "Revised body." in pending["diff"]
    assert (second / "SKILL.md").read_text(encoding="utf-8") == BODY

    approved = review.decide(pending["id"], "approve")

    assert approved["approved"], approved
    assert (first / "SKILL.md").read_text(encoding="utf-8") == BODY
    assert "Revised body." in (second / "SKILL.md").read_text(encoding="utf-8")


def test_host_roots_are_protected_before_creation_and_use_project_quarantine(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "role"
    protected = layers.protected_skill_roots(str(profile), str(tmp_path))
    for host in (".agents", ".claude", ".codex", ".grok"):
        assert str((tmp_path / host / "skills").resolve()) in protected
    source = skill(tmp_path / ".claude" / "skills")
    checked = []
    def quarantine(path):
        checked.append(path)
        return True
    monkeypatch.setattr(layers, "is_quarantined_project_skill", quarantine)

    roots = layers.skill_roots(str(profile), str(tmp_path))

    assert not index.candidates(roots, "sample-skill")
    assert checked == [source / "SKILL.md"]


async def test_model_tool_lists_selectable_sources_and_reports_the_written_path(tmp_path):
    first = skill(tmp_path / ".claude" / "skills")
    second = skill(tmp_path / ".codex" / "skills")
    profile = tmp_path / "profiles" / "role"
    layers.write_skills_config({"skill_write_mode": "allow"})
    part = SkillsPart(None, str(profile), str(tmp_path), kind="bare")
    part._sync_owner.schedule = AsyncMock()
    tools = {tool.name: tool for tool in part.tools}
    arguments = {"operations": [{"action": "patch", "name": "sample-skill", "source": str(second / "SKILL.md"),
                                 "old_string": "Original body.", "new_string": "Revised body."}]}
    validate(arguments, tools["skill_manage"].parameters)
    listed = await tools["skills_list"].execute("list", {}, None, None, None)
    sources = {row["source"] for row in json.loads(listed["content"][0]["text"])["skills"]}
    assert sources == {str(first / "SKILL.md"), str(second / "SKILL.md")}

    result = await tools["skill_manage"].execute("write", arguments, None, None, None)

    assert not result["isError"], result
    assert str(second.resolve()) in result["content"][0]["text"]
    assert result["details"]["written_paths"] == [str(second.resolve())]
    assert (first / "SKILL.md").read_text(encoding="utf-8") == BODY
    assert "Revised body." in (second / "SKILL.md").read_text(encoding="utf-8")
