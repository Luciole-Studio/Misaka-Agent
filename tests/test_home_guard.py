"""Who may write where inside the home: refused for the tools that name their target, reported
for the shell, which does not."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from misaka.config import home
from misaka.core.platform import home_guard


def test_an_agent_owns_shared_and_what_its_session_was_handed_and_nothing_else_in_the_home(tmp_path):
    inside = home.path("tasks_root") / "t_1"
    assert home.agent_may_write(tmp_path / "project" / "report.md")              # not the home's business
    assert home.agent_may_write(home.path("shared") / "corpus" / "a.pdf")
    assert home.agent_may_write(inside / "out.md", (str(inside),))
    assert not home.agent_may_write(inside / "out.md")                           # nobody handed it over
    for name in ("settings", "auth", "db", "roles_root", "log"):
        assert not home.agent_may_write(home.path(name), (str(inside),)), name
    assert not home.agent_may_write(home.home() / "skill-library" / "x")         # how the mess began
    # A workspace that merely contains the home -- a DM turn runs in $HOME -- grants nothing in it.
    assert not home.agent_may_write(home.path("settings"), (str(home.home().parent), str(home.home())))
    assert not home.agent_may_write(home.home() / "shared/../settings.json")     # resolved, not read as text


@pytest.mark.parametrize("kind", ["card", "child", "dm"])
def test_an_unattended_file_tool_is_refused_with_somewhere_to_go_instead(kind, monkeypatch):
    reason = home_guard.refusal(str(home.home() / "skill-library" / "a.md"), "/somewhere/project", kind)
    assert reason and home.display(home.path("shared")) in reason
    assert home_guard.refusal(str(home.path("shared") / "a.md"), "/somewhere/project", kind) is None
    card = home.path("tasks_root") / "t_9"
    assert home_guard.refusal(str(card / "notes.md"), str(card), kind) is None
    monkeypatch.setenv("MISAKA_SUBAGENT_MEMORY_DIR", str(home.path("agent_memory") / "explorer"))
    assert home_guard.refusal(str(home.path("agent_memory") / "explorer" / "MEMORY.md"), "/p", kind) is None


def test_a_session_with_a_person_in_it_is_not_refused():
    assert home_guard.refusal(str(home.path("settings")), "/somewhere/project", "foreground") is None


def test_the_path_guard_asks_the_homes_rule_for_write_edit_and_office(tmp_path):
    from misaka.core.skills.wiring.skills import SkillsPart

    part = SkillsPart([], None, cwd=str(tmp_path), kind="card")
    try:
        def call(tool, **arguments):
            return asyncio.run(part.tool_call({"toolName": tool, "input": arguments}, SimpleNamespace()))

        refused = call("write", path=str(home.home() / "audits" / "x.md"), content="x")
        assert refused["block"] and "MISAKA home" in refused["reason"]
        assert call("edit", path=str(home.path("settings")), oldText="a", newText="b")["block"]
        assert call("write", path=str(home.path("shared") / "x.md"), content="x") is None
        assert call("write", path=str(tmp_path / "report.md"), content="x") is None
        assert call("bash", command=f"cat {home.path('settings')}") is None      # a read is never refused
    finally:
        asyncio.run(part.session_shutdown({}, SimpleNamespace()))


def test_a_shell_command_that_litters_the_home_is_told_so_once_on_its_own_result():
    home.home().mkdir(parents=True, exist_ok=True)
    (home.home() / "left-by-an-earlier-session").mkdir()
    guard = home_guard.HomeGuard()
    result = {"toolName": "bash", "content": [{"type": "text", "text": "ok"}]}
    assert asyncio.run(guard.tool_result(result)) is None                        # nothing new
    (home.home() / "skill-library").mkdir()
    (home.path("shared")).mkdir()
    note = asyncio.run(guard.tool_result(result))
    assert note["content"][0]["text"] == "ok"                                    # the output is kept
    assert "skill-library" in note["content"][-1]["text"] and "left-by-an-earlier" not in note["content"][-1]["text"]
    assert asyncio.run(guard.tool_result(result)) is None                        # said once
    (home.home() / "another").mkdir()
    assert asyncio.run(guard.tool_result({"toolName": "read", "content": []})) is None   # only shells are checked
    for name in ("left-by-an-earlier-session", "skill-library", "another"):      # leave the home as the table has it
        (home.home() / name).rmdir()


def test_a_skill_kept_as_a_symlink_is_guarded_at_its_real_place_too(tmp_path):
    """The user keeps skills as links into a library under shared/. The guard compares real
    paths, so a write aimed at the link resolves into shared/ -- which the home's rule allows.
    The live-skill roots must therefore include where each linked skill really is."""
    from misaka.core.skills.wiring.skills import SkillsPart

    real = home.path("shared") / "library" / "pptx"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("---\nname: pptx\ndescription: d\n---\nbody\n", encoding="utf-8")
    home.path("shared_skills").mkdir(parents=True, exist_ok=True)
    (home.path("shared_skills") / "pptx").symlink_to(real)
    part = SkillsPart(None, None, cwd=str(tmp_path), kind="card")
    try:
        def call(tool, **arguments):
            return asyncio.run(part.tool_call({"toolName": tool, "input": arguments}, SimpleNamespace()))

        for target in (home.path("shared_skills") / "pptx" / "SKILL.md", real / "SKILL.md"):
            refused = call("write", path=str(target), content="x")
            assert refused and refused["block"], target
        assert call("write", path=str(home.path("shared") / "library" / "notes.md"), content="x") is None
    finally:
        asyncio.run(part.session_shutdown({}, SimpleNamespace()))
