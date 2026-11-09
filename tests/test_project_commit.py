"""Git is the user's: nothing in MISAKA commits on its own, and a commit happens only when the user
asks -- ``/commit`` or ``project_commit`` -- and confirms the files on screen."""
import dataclasses
import subprocess
from contextlib import closing
from types import SimpleNamespace

import pytest

from misaka.core.network import dispatch
from misaka.core.platform import cards, project_commit, tasks
from misaka.core.wiring import SessionSpec


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def project(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "The User")
    _git(tmp_path, "config", "user.email", "user@example.com")
    (tmp_path / "notes.md").write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "notes.md")
    _git(tmp_path, "commit", "-q", "-m", "start")
    return tmp_path


def _ctx(cwd, *, confirm=True, has_ui=True, typed=""):
    asked, notices = [], []

    async def ask(title, message):
        asked.append((title, message))
        return confirm

    async def read(title):
        return typed

    return SimpleNamespace(cwd=str(cwd), hasUI=has_ui, asked=asked, notices=notices,
                           ui=SimpleNamespace(confirm=ask, input=read, notify=lambda text, kind: notices.append(text)))


def _commits(cwd):
    return _git(cwd, "log", "--format=%an|%s").splitlines()


async def test_a_confirmed_commit_uses_the_repository_identity(project):
    (project / "notes.md").write_text("second\n", encoding="utf-8")
    (project / "new.md").write_text("new\n", encoding="utf-8")
    ctx = _ctx(project)
    result = await project_commit.commit(ctx, "Record the findings")
    assert result.startswith("Committed ")
    assert _commits(project)[0] == "The User|Record the findings"
    assert "notes.md" in ctx.asked[0][1] and "new.md" in ctx.asked[0][1]


async def test_named_paths_are_all_that_is_committed(project):
    (project / "notes.md").write_text("second\n", encoding="utf-8")
    (project / "draft.md").write_text("not yet\n", encoding="utf-8")
    await project_commit.commit(_ctx(project), "Only the notes", ["notes.md"])
    assert _git(project, "status", "--porcelain").strip() == "?? draft.md"


@pytest.mark.parametrize("case", ["declined", "no_ui", "nothing", "not_a_repo"])
async def test_nothing_is_committed_without_a_confirmed_change(project, tmp_path_factory, case):
    where = tmp_path_factory.mktemp("plain") if case == "not_a_repo" else project
    if case != "nothing":
        (where / "notes.md").write_text("changed\n", encoding="utf-8")
    ctx = _ctx(where, confirm=case != "declined", has_ui=case != "no_ui")
    result = await project_commit.commit(ctx, "Should not land")
    assert "nothing was committed" in result or result.startswith("Nothing to commit")
    if case != "not_a_repo":
        assert _commits(project) == ["The User|start"]
    assert (ctx.asked != []) is (case == "declined")


async def test_the_command_asks_for_a_message_and_commits_nothing_without_one(project):
    (project / "notes.md").write_text("changed\n", encoding="utf-8")
    part = project_commit.ProjectCommitPart()
    command = part.commands[0].handler
    ctx = _ctx(project, typed="")
    await command("", ctx)
    assert ctx.notices == ["No commit message; nothing was committed."]
    await command("From the command", _ctx(project))
    assert _commits(project)[0] == "The User|From the command"


def test_only_the_user_s_own_windows_get_the_commit_tool(monkeypatch, tmp_path):
    monkeypatch.delenv("MISAKA_RESEARCH_NODE", raising=False)
    spec = SessionSpec(profile_dir=str(tmp_path), role="last_order", workspace=str(tmp_path), kind="foreground",
                       sender="last-order")
    assert [tool.name for tool in project_commit.part(spec).tools] == ["project_commit"]
    assert project_commit.part(dataclasses.replace(spec, workflow_context=True)) is None
    monkeypatch.setenv("MISAKA_RESEARCH_NODE", "r_x b_y key")
    assert project_commit.part(spec) is None
    assert project_commit.SESSION_KINDS == {"foreground"}


def test_cards_and_their_acceptance_never_commit(project):
    with closing(tasks.connect(str(project / "board.db"))) as con:
        cards.init_project(str(project), draft_brief=False, git=False)
        tid = cards.create(con, str(project), "A card", "## deliverable\nout.md\n", "10032")
        (project / "out.md").write_text("result\n", encoding="utf-8")
        con.execute("UPDATE tasks SET status='running', claim_lock='lock', claim_expires=9999999999 WHERE id=?", (tid,))
        row = tasks.get(con, tid)
        assert dispatch.accept(con, row, {"summary": "done", "artifacts": ["out.md"]}, generation=1, claim_lock="lock")
        assert tasks.get(con, tid)["status"] == "done"
        ok, message = cards.remove(con, str(project), tid)
        assert ok and "never committed" in message
    assert _commits(project) == ["The User|start"]


def test_a_committed_card_s_deletion_is_left_for_the_user(project):
    with closing(tasks.connect(str(project / "board.db"))) as con:
        tid = cards.create(con, str(project), "A card", "## deliverable\nout.md\n", "10032")
        _git(project, "add", "cards")
        _git(project, "commit", "-q", "-m", "the user's commit")
        ok, message = cards.remove(con, str(project), tid)
    assert ok and "left uncommitted for you to commit" in message
    assert _commits(project)[0] == "The User|the user's commit"
    assert f"cards/{tid}.md" in _git(project, "status", "--porcelain")


def test_starting_research_never_creates_a_repository(tmp_path):
    folder = tmp_path / "study"
    folder.mkdir()
    actions = cards.init_project(str(folder), draft_brief=False, git=False)
    assert not (folder / ".git").exists() and "git repository created" not in actions
    assert "git repository created" in cards.init_project(str(folder))
