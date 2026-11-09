"""`misaka update` and `misaka uninstall`, held to Hermes's promises (docs/plans/upgrade-install-uninstall-2026-09-29.md).

An update keeps what the install holds and never cuts running work off; it copies the home aside
first and puts a checkout back when the new code cannot start. An uninstall keeps the data unless
told otherwise, lists what lies outside the home, and never touches what a person wrote.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tarfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.cli import backup, uninstall, update
from misaka.config import home


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    root = home.home()
    root.mkdir(parents=True)
    return root


def _install(kind="git", installer="pip", path=None, commit="c0ffee"):
    return update.Install(kind, kind == "checkout", installer, path, commit, "0.18.0")


# -- update: the install method decides, and the extras survive -------------------------------

def test_uv_and_pipx_upgrade_from_their_own_records(monkeypatch):
    monkeypatch.setattr(update, "installed_extras", lambda: ["providers"])
    assert update._install_commands(_install(installer="uv tool")) == [["uv", "tool", "upgrade", "misaka"]]
    assert update._install_commands(_install(installer="pipx")) == [["pipx", "upgrade", "misaka"]]


def test_pip_reinstalls_the_package_alone_then_its_dependencies_with_the_extras(monkeypatch):
    """pip keeps a git requirement whose version string did not move (verified 2026-09-29), so
    the package is forced alone first; the extras found installed ride on the second step."""
    monkeypatch.setattr(update, "installed_extras", lambda: ["browser", "providers"])
    first, second = update._install_commands(_install(installer="pip"))
    assert first[-3:] == ["--force-reinstall", "--no-deps", f"misaka @ git+{update.REPO_URL}"]
    assert second[-1] == f"misaka[browser,providers] @ git+{update.REPO_URL}"


def test_a_checkout_syncs_with_the_extras_it_holds(tmp_path, monkeypatch):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(update, "installed_extras", lambda: ["pageindex"])
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    assert update._install_commands(_install("checkout", "uv", tmp_path)) == [["uv", "sync", "--extra=pageindex"]]


def test_the_owning_tool_is_read_from_its_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert update._manager("pip") == "pip"
    (tmp_path / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    assert update._manager("pip") == "pipx"                 # pipx installs through pip
    (tmp_path / "uv-receipt.toml").write_text("", encoding="utf-8")
    assert update._manager("uv") == "uv tool"


def _archive(tmp_path):
    """The layout of a release archive (scripts/build_release.py): its own CPython beside bin/."""
    root = tmp_path / "misaka-0.18.6-darwin-arm64"
    (root / "python" / "bin").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "BUNDLED.txt").write_text("uv git rg fd pdftotext\n", encoding="utf-8")
    return root


def test_a_release_archive_is_known_for_what_it_is(tmp_path, monkeypatch):
    """2026-10-07: an archive read as "a built package" and was told to pip install, which would
    have installed into whatever Python was on PATH and left the archive as it was."""
    root = _archive(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(root / "python"))
    install = update.describe()
    assert install.kind == "bundle" and install.path == root
    assert update._install_commands(install) is None and update.adding_extras(install, ["browser"]) is None


@pytest.mark.parametrize("latest, says", [("v0.18.6", "is the latest release"), ("v0.18.7", "v0.18.7 is out"),
                                          ("v0.18.5", "newer than the latest release (v0.18.5)")])
def test_a_release_archive_is_updated_by_the_installer(tmp_path, monkeypatch, capsys, latest, says):
    monkeypatch.setattr(sys, "prefix", str(_archive(tmp_path) / "python"))
    monkeypatch.setattr(update, "describe", lambda: update.Install("bundle", False, "", tmp_path, None, "0.18.6"))
    monkeypatch.setattr(update, "_api", lambda path: ({"tag_name": latest}, None))
    monkeypatch.setattr(update, "_run_all", lambda *a: pytest.fail("an archive is not updated in place"))
    assert update.run(apply=True) == 0
    out = capsys.readouterr().out
    assert says in out
    installer = "install.ps1 | iex" if os.name == "nt" else "install.sh | sh"
    assert (installer in out) == (latest == "v0.18.7") and "pip install" not in out
    assert "Tracking the main branch" not in out and "could not be compared" not in out


@pytest.mark.parametrize("parents, installer, upgrade, remove", [
    (("opt", "homebrew", "Cellar", "misaka", "0.18.6", "libexec"), "Homebrew", "brew upgrade misaka",
     ["brew", "uninstall", "misaka"]),
    (("AppData", "Local", "Microsoft", "WinGet", "Packages", "Luciole-Studio.Misaka_Microsoft.Winget.Source"),
     "winget", "winget upgrade Luciole-Studio.Misaka", ["winget", "uninstall", "Luciole-Studio.Misaka"]),
])
def test_an_archive_homebrew_or_winget_put_in_place_is_theirs_to_update(tmp_path, monkeypatch, capsys, parents,
                                                                          installer, upgrade, remove):
    """The installer script would have put a second copy beside it (first on PATH, on Windows)."""
    from misaka.cli import uninstall
    root = _archive(tmp_path.joinpath(*parents))
    monkeypatch.setattr(sys, "prefix", str(root / "python"))
    install = update.describe()
    assert (install.kind, install.installer) == ("bundle", installer)
    monkeypatch.setattr(update, "_api", lambda path: ({"tag_name": "v99.0.0"}, None))
    assert update.run(apply=True) == 0
    out = capsys.readouterr().out
    assert upgrade in out and "install.sh" not in out and "install.ps1" not in out
    assert uninstall._program_command(install) == remove


def test_the_users_commands_do_not_inherit_the_archives_python_flags(tmp_path, monkeypatch):
    """The launcher sets them for MISAKA's own Python; under PYTHONSAFEPATH a skill's script could
    not import the module beside it."""
    from misaka.utils.shell import getShellEnv
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    assert getShellEnv()["PYTHONSAFEPATH"] == "1", "outside an archive the user's own setting stands"
    monkeypatch.setattr(sys, "prefix", str(_archive(tmp_path) / "python"))
    env = getShellEnv()
    assert "PYTHONSAFEPATH" not in env and "PYTHONNOUSERSITE" not in env


def test_installed_extras_are_named_the_widest_way(monkeypatch):
    import importlib.metadata
    requires = ["anthropic>=0.45; extra == 'anthropic'", "openai>=1.60; extra == 'openai'",
                "anthropic>=0.45; extra == 'providers'", "openai>=1.60; extra == 'providers'",
                "fastembed; extra == 'lcm-semantic'", "pydantic>=2.10"]
    present = {"misaka", "anthropic", "openai"}

    def distribution(name):
        if name not in present:
            raise importlib.metadata.PackageNotFoundError(name)
        return SimpleNamespace(requires=requires)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    assert update.installed_extras() == ["providers"]


def test_adding_an_extra_keeps_the_ones_installed(monkeypatch):
    monkeypatch.setattr(update, "installed_extras", lambda: ["providers"])
    command = update.adding_extras(_install(installer="uv tool"), ["pageindex"])
    assert command == ["uv", "tool", "install", "--force", f"misaka[pageindex,providers] @ git+{update.REPO_URL}"]


# -- update: an install made from another branch follows that branch -------------------------

def _from_branch(installer="uv tool", revision="heretic-0.19.17"):
    return update.Install("git", False, installer, None, "c0ffee", "0.19.17", revision)


def test_the_revision_an_install_was_made_from_is_read_from_its_record(monkeypatch):
    import importlib.metadata
    import json

    record = {"url": update.REPO_URL, "vcs_info": {"vcs": "git", "commit_id": "c0ffee",
                                                   "requested_revision": "heretic-0.19.17"}}
    dist = SimpleNamespace(version="0.19.17", read_text=lambda name: (
        json.dumps(record) if name == "direct_url.json" else "uv"))
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: dist)
    found = update.describe()
    assert (found.kind, found.commit, found.revision, found.follows) == ("git", "c0ffee", "heretic-0.19.17",
                                                                        "heretic-0.19.17")
    del record["vcs_info"]["requested_revision"]
    assert update.describe().follows == update.BRANCH


def test_an_install_from_another_branch_is_compared_with_that_branch(monkeypatch, capsys):
    """2026-10-13: an install of heretic-0.19.17 -- ahead of main by 16 commits -- was compared with
    main, told "this install's commit is not an ancestor of main" and to resolve it in a checkout it
    does not have, and the check exited 1."""
    asked = []
    monkeypatch.setattr(update, "describe", lambda: _from_branch())
    monkeypatch.setattr(update, "github_token", lambda: ("t", "GITHUB_TOKEN"))
    monkeypatch.setattr(update, "_api", lambda path: asked.append(path) or ({"status": "identical", "ahead_by": 0}, None))
    assert update.run() == 0
    out = capsys.readouterr().out
    assert asked == ["/compare/c0ffee...heretic-0.19.17"]
    assert "Up to date with heretic-0.19.17." in out and "main" not in out
    monkeypatch.setattr(update, "_api", lambda path: ({"status": "ahead", "ahead_by": 2}, None))
    monkeypatch.setattr(update, "installed_extras", list)
    assert update.run() == 0
    out = capsys.readouterr().out
    assert "2 commit(s) behind heretic-0.19.17." in out and "uv tool upgrade misaka" in out


def test_a_comparison_that_fails_does_not_send_a_git_install_to_a_checkout(monkeypatch, capsys):
    monkeypatch.setattr(update, "describe", lambda: _from_branch())
    monkeypatch.setattr(update, "github_token", lambda: (None, ""))
    monkeypatch.setattr(update, "_api", lambda path: ({"status": "diverged"}, None))
    assert update.run() == 1
    out = capsys.readouterr().out
    assert "not an ancestor of heretic-0.19.17" in out and "checkout" not in out


def test_reinstalling_or_adding_an_extra_keeps_the_branch(monkeypatch):
    """A requirement without the revision installs main: adding an extra from setup would have put a
    heretic install back on main, whose code cannot open the board the branch's code migrated."""
    monkeypatch.setattr(update, "installed_extras", lambda: ["providers"])
    source = f"git+{update.REPO_URL}@heretic-0.19.17"
    assert update.adding_extras(_from_branch(), ["pageindex"]) == [
        "uv", "tool", "install", "--force", f"misaka[pageindex,providers] @ {source}"]
    first, second = update._install_commands(_from_branch("pip"))
    assert first[-1] == f"misaka @ {source}" and second[-1] == f"misaka[providers] @ {source}"
    assert update._install_commands(_from_branch("uv"))[0][-1] == f"misaka[providers] @ {source}"
    assert update._install_commands(_from_branch()) == [["uv", "tool", "upgrade", "misaka"]]   # uv's receipt has it


def test_a_checkout_follows_the_branch_of_origin_it_tracks(tmp_path):
    import subprocess

    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    origin, checkout = tmp_path / "origin.git", tmp_path / "checkout"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(checkout))
    for args in (("config", "user.email", "t@example.com"), ("config", "user.name", "t"),
                 ("commit", "--allow-empty", "-m", "one"), ("push", "origin", "HEAD:main"),
                 ("checkout", "-b", "feature/x"), ("commit", "--allow-empty", "-m", "two"),
                 ("push", "-u", "origin", "feature/x")):
        git(checkout, *args)
    install = update.Install("checkout", True, "uv", checkout, None, "0.19.17")
    state = update._checkout_state(install)
    assert (state["follows"], state["behind"], state["reason"]) == ("feature/x", 0, None)
    git(checkout, "reset", "--hard", "HEAD~1")              # one commit behind its branch, ahead of nothing
    state = update._checkout_state(install)
    assert (state["follows"], state["behind"], state["reason"]) == ("feature/x", 1, None)
    git(checkout, "branch", "--unset-upstream")             # no upstream: main, as before
    state = update._checkout_state(install)
    assert state["follows"] == "main"


# -- update: running work, the snapshot, the rollback -----------------------------------------

def _apply_setup(monkeypatch, install, *, blocking=(), panes=(), starts=None):
    calls = []
    monkeypatch.setattr(update, "describe", lambda: install)
    monkeypatch.setattr(update, "_checkout_state", lambda _install: {"head": "old", "behind": 1, "dirty": False, "reason": None})
    monkeypatch.setattr(update, "_behind", lambda _commit, _head=update.BRANCH: (1, None))
    monkeypatch.setattr(update, "installed_extras", list)
    monkeypatch.setattr(update, "running_work", lambda: (list(blocking), list(panes)))
    monkeypatch.setattr(update, "prompt_choice", lambda *_a, **_k: 1)
    monkeypatch.setattr(update, "stop_daemon", lambda: calls.append("stop"))
    monkeypatch.setattr(backup, "snapshot", lambda reason: calls.append(f"snapshot:{reason}") or Path("/snap"))
    monkeypatch.setattr(update, "_git", lambda _path, *args, check=True: calls.append("git " + " ".join(args)) or "")
    monkeypatch.setattr(update, "_run_all", lambda commands, cwd: calls.append("install") and None)
    results = iter(starts or [None])
    monkeypatch.setattr(update, "_starts", lambda: next(results))
    return calls


def test_an_update_waits_for_a_research_run_or_a_card(monkeypatch):
    calls = _apply_setup(monkeypatch, _install(), blocking=["research run r_1  ~/p"])
    assert update.run(apply=True) == 1
    assert calls == []                                       # nothing stopped, copied or installed


def test_an_update_copies_the_home_first_and_leaves_an_open_panel_running(monkeypatch):
    calls = _apply_setup(monkeypatch, _install(), panes=[{"id": "p1", "alive": True}])
    assert update.run(apply=True) == 0
    assert calls == ["snapshot:update", "install"]           # no daemon stop under an open session


def test_a_checkout_that_cannot_start_is_put_back(tmp_path, monkeypatch):
    calls = _apply_setup(monkeypatch, _install("checkout", "uv", tmp_path), starts=["ImportError: boom", None])
    assert update.run(apply=True) == 1
    assert calls == ["snapshot:update", "stop", "git pull --ff-only origin main", "install",
                     "git reset --hard old", "install"]


# -- backups ----------------------------------------------------------------------------------

def _seed_home(root: Path) -> None:
    home.path("settings").write_text('{"a": 1}', encoding="utf-8")
    home.path("credentials").mkdir(parents=True)
    home.path("auth").write_text("{}", encoding="utf-8")
    home.path("db").parent.mkdir(parents=True)
    with closing(sqlite3.connect(home.path("db"))) as con, con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES (1)")
    (root / "cache").mkdir()
    (root / "cache" / "big.bin").write_bytes(b"x" * 10)
    role = home.path("roles_root") / "last_order"
    role.mkdir(parents=True)
    (role / "settings.json").write_text("{}", encoding="utf-8")


def test_an_update_snapshot_holds_settings_credentials_boards_and_roles(fresh_home, monkeypatch):
    _seed_home(fresh_home)
    lastOrder = home.path("roles_root") / "last_order"
    home.path("mcp_config", lastOrder).write_text('{"mcpServers": {}}', encoding="utf-8")
    home.path("mcp_auth", lastOrder).parent.mkdir(parents=True)
    home.path("mcp_auth", lastOrder).write_text("{}", encoding="utf-8")
    shot = backup.snapshot("update")
    assert (shot / "settings.json").read_text(encoding="utf-8") == '{"a": 1}'
    assert (shot / "credentials" / "auth.json").exists()
    assert (shot / "profiles" / "last_order" / "settings.json").exists()
    assert (shot / "profiles" / "last_order" / "mcp.json").exists()
    assert (shot / "profiles" / "last_order" / "credentials" / "mcp-auth.json").exists()
    with closing(sqlite3.connect(shot / "state" / "board.db")) as con:
        assert con.execute("SELECT x FROM t").fetchone() == (1,)
    assert not (shot / "cache").exists()
    if os.name != "nt":                                       # POSIX modes; Windows keeps it private by ACL
        assert home.path("backups").stat().st_mode & 0o777 == 0o700
    for n in range(backup.KEEP + 2):                          # bounded, oldest first
        (home.path("backups") / f"update-2000010{n}-000000").mkdir()
    backup.snapshot("update")
    assert len(list(home.path("backups").glob("update-*"))) == backup.KEEP


def test_the_uninstall_archive_is_private_and_leaves_rebuildables_out(fresh_home, tmp_path):
    _seed_home(fresh_home)
    backup.snapshot("update")
    target = tmp_path / "out.tar.gz"
    assert backup.archive(target) == []
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    with tarfile.open(target) as tar:
        names = set(tar.getnames())
    assert f"{home.DIR_NAME}/credentials/auth.json" in names and f"{home.DIR_NAME}/state/board.db" in names
    assert not any("/cache" in name or "/backups" in name or name.endswith("-wal") for name in names)


# -- uninstall ------------------------------------------------------------------------------

def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    config = project / home.DIR_NAME
    (config / "subagents").mkdir(parents=True)
    (config / "settings.json").write_text("{}", encoding="utf-8")
    (config / "lcm").mkdir()
    (config / "lcm.gate").write_text("", encoding="utf-8")
    (project / "report.md").write_text("mine", encoding="utf-8")
    home.path("db").parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(home.path("db"))) as con, con:
        con.execute("CREATE TABLE tasks(workspace TEXT)")
        con.execute("CREATE TABLE research_runs(workspace TEXT)")
        con.execute("INSERT INTO research_runs VALUES (?)", (str(project),))
    return project


def test_only_what_misaka_derived_in_a_project_is_offered_for_removal(fresh_home, tmp_path):
    project = _project(tmp_path)
    assert uninstall._projects(str(home.path("db"))) == [str(project.resolve())]
    derived = {Path(path).name for path in uninstall._derived([str(project)])}
    assert derived == {"lcm", "lcm.gate"}


def test_links_into_the_home_are_found_and_others_are_not(fresh_home, tmp_path, monkeypatch):
    tool = fresh_home / "shared" / "tool"
    tool.parent.mkdir()
    tool.write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(tool, bin_dir / "tool")
    os.symlink(tmp_path, bin_dir / "elsewhere")
    monkeypatch.setenv("PATH", str(bin_dir))
    assert uninstall._links_into(fresh_home) == [str(bin_dir / "tool")]


def _uninstall_setup(monkeypatch, *, blocking=(), panes=()):
    ran = []
    monkeypatch.setattr(update, "describe", lambda: _install(installer="uv tool"))
    monkeypatch.setattr(update, "running_work", lambda: (list(blocking), list(panes)))
    monkeypatch.setattr(update, "stop_daemon", lambda: None)
    monkeypatch.setattr(uninstall.subprocess, "run", lambda command, check=False: ran.append(command) or SimpleNamespace(returncode=0))
    # Windows: no other process of this install, and the program goes after this one exits.
    monkeypatch.setattr(uninstall, "_holders", lambda *, daemon: [])
    monkeypatch.setattr(uninstall, "_remove_after_exit", lambda command: ran.append(command) or Path("uninstall.log"))
    return ran


def test_the_default_removes_the_program_and_keeps_the_data(fresh_home, tmp_path, monkeypatch):
    project = _project(tmp_path)
    ran = _uninstall_setup(monkeypatch)
    assert uninstall.run(assume_yes=True) == 0
    assert ran == [["uv", "tool", "uninstall", "misaka"]]
    assert fresh_home.exists() and (project / home.DIR_NAME / "lcm").exists()


def test_data_only_removes_the_home_and_derived_files_but_nothing_a_person_wrote(fresh_home, tmp_path, monkeypatch):
    project = _project(tmp_path)
    ran = _uninstall_setup(monkeypatch)
    assert uninstall.run(mode="data", assume_yes=True) == 0
    assert ran == [] and not fresh_home.exists()
    config = project / home.DIR_NAME
    assert sorted(path.name for path in config.iterdir()) == ["settings.json", "subagents"]
    assert (project / "report.md").read_text(encoding="utf-8") == "mine"


def test_uninstall_refuses_while_misaka_runs(fresh_home, monkeypatch):
    ran = _uninstall_setup(monkeypatch, panes=[{"id": "p1", "alive": True, "title": "LO"}])
    assert uninstall.run(mode="full", assume_yes=True) == 2
    assert ran == [] and fresh_home.exists()


def test_a_source_checkout_is_never_removed(fresh_home, tmp_path, monkeypatch):
    ran = _uninstall_setup(monkeypatch)
    monkeypatch.setattr(update, "describe", lambda: _install("checkout", "uv", tmp_path))
    assert uninstall.run(mode="full", assume_yes=True) == 0
    assert ran == [] and tmp_path.exists() and not fresh_home.exists()


def test_an_archive_unpacked_in_a_folder_named_cellar_is_ours(tmp_path, monkeypatch):
    """Any path part "Cellar" read as Homebrew: uninstall would have run `brew uninstall misaka`."""
    monkeypatch.setattr(sys, "prefix", str(_archive(tmp_path / "Cellar" / "downloads") / "python"))
    assert update.describe().installer == ""


def test_a_run_whose_driver_died_does_not_hold_up_an_update(tmp_path, monkeypatch):
    """Killed mid-run, its driver's lease ran out; stopping it from a window leaves it "stopping"
    with nobody to finish the stop, and every update refused until it was resumed."""
    import time
    from contextlib import closing

    from misaka.core.platform import repo, tasks
    from misaka.core.research import runs
    monkeypatch.setattr(repo, "enabled", lambda *a, **k: False)
    with closing(tasks.connect(str(home.path("db")))) as con:
        runs.init(con)
        live = runs.create(con, workspace=str(tmp_path), question="Live")
        dead = runs.create(con, workspace=str(tmp_path), question="Dead")
        con.execute("UPDATE graphs SET lease_token='d1', lease_expires=? WHERE id=?",
                    (int(time.time()) + 600, live["id"]))
        con.execute("UPDATE graphs SET lease_token='d2', lease_expires=? WHERE id=?",
                    (int(time.time()) - 600, dead["id"]))
    found = update.active_runs()
    assert [line.split()[0] for line in found] == [live["id"]]
