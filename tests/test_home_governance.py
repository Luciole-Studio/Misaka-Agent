"""The home has one root and one table, and nothing outside ``misaka/config`` goes around them.

These are the rules of docs/plans/home-governance-2026-09-21.md as assertions. The source scans
are why a new hardcoded path fails here instead of surfacing months later as a file nobody can
account for in a user's home.
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

from misaka.config import CFG, current_config, home

PACKAGE = Path(__file__).resolve().parents[1] / "misaka"
# Shipped skill assets and vendored upstream code are not MISAKA's own source.
_NOT_OURS = ("/core/skills/assets/", "/vendor/")
# ``~/.misaka`` or a path component that is exactly ``.misaka`` -- not ``.misaka-lcm-cache``,
# not the dotted module name ``misaka.extensions.misaka_lcm``.
_SPELLS_THE_HOME = re.compile(r"(^|[/~\s`'\"(])\.misaka(/|$|[\s`'\").,;:])")
# The layout knobs that existed before the home did. One knob replaced them all.
_RETIRED = ("MISAKA_CODING_AGENT_DIR", "MISAKA_CODING_AGENT_SESSION_DIR", "MISAKA_PROFILES", "MISAKA_SESSIONS",
            "MISAKA_DB", "MISAKA_MESSAGES", "MISAKA_TASKS", "MISAKA_WEB_CONFIG", "MISAKA_WEB_CACHE",
            "MISAKA_OFFICE_CACHE", "MISAKA_OFFICE_INTENT", "MISAKA_NET_SOCK", "MISAKA_NET_SNAPSHOT",
            "MISAKA_MCP_CACHE", "MISAKA_WORKTREE_DIR",
            # Knobs that became settings.json sections (2026-09-22): a MISAKA_* name in the
            # environment is a parent's hand-off to a child, never a setting.
            "MISAKA_PROVIDER", "MISAKA_MODEL", "MISAKA_LO_MODEL", "MISAKA_FORCE_MODEL", "MISAKA_JUDGE_TIMEOUT",
            "MISAKA_TOKEN_CAP", "MISAKA_BEAST_AT", "MISAKA_RESEARCH_PLAN_APPROVAL", "MISAKA_MAX_CONCURRENT_SISTERS",
            "MISAKA_MAX_CONCURRENT_PER_SISTER", "MISAKA_MCP_CALL_TIMEOUT", "MISAKA_MCP_INIT_TIMEOUT",
            "MISAKA_MCP_REQUIRED_WAIT", "MISAKA_OCR_LANGS", "MISAKA_PANEL_PREFIX", "MISAKA_SKILL_COPY_CAP_MB",
            "MISAKA_SMALL_FAST_MODEL", "MISAKA_MAX_CONCURRENT_SUBAGENTS", "MISAKA_AUTO_BACKGROUND_TASKS",
            "MISAKA_DISABLE_BACKGROUND_TASKS", "MISAKA_DISABLE_AUTO_MEMORY", "MISAKA_SIMPLE", "MISAKA_TASK_MAX_OUTPUT",
            "MISAKA_VERIFICATION_AGENT", "MISAKA_AGENT_SDK_DISABLE_BUILTIN_AGENTS", "MISAKA_MANAGED_AGENTS_DIR",
            "MISAKA_AGENT_LIST_IN_MESSAGES", "MISAKA_COORDINATOR_MODE", "MISAKA_AGENT_MEMORY_HOME",
            "MISAKA_ALLOW_PRIVATE_URLS", "MISAKA_EFFORT_LEVEL", "MISAKA_AGENT_MEMORY_SNAPSHOT",
            "MISAKA_INHERIT_PROCESS_GROUP", "MISAKA_TUI_ESC_TIMEOUT")


def _sources():
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = "/" + path.relative_to(PACKAGE).as_posix()
        if rel.startswith("/config/") or any(part in rel for part in _NOT_OURS):
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _docstrings(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def test_no_code_outside_config_spells_the_home_or_a_project_dir():
    offenders = []
    for path, tree in _sources():
        prose = _docstrings(tree)
        offenders += [f"{path.relative_to(PACKAGE.parent)}:{node.lineno}" for node in ast.walk(tree)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and id(node) not in prose and _SPELLS_THE_HOME.search(node.value)]
    assert not offenders, ("ask misaka.config.home (path / display / project_dir) instead of spelling it:\n  "
                           + "\n  ".join(offenders))


def test_no_code_outside_config_names_the_project_dir_constant():
    offenders = [f"{path.relative_to(PACKAGE.parent)}:{node.lineno}" for path, tree in _sources()
                 for node in ast.walk(tree)
                 if (isinstance(node, ast.Name) and node.id == "CONFIG_DIR_NAME")
                 or (isinstance(node, ast.alias) and node.name == "CONFIG_DIR_NAME")]
    assert not offenders, "derive a project's config directory with home.project_dir():\n  " + "\n  ".join(offenders)


def test_the_retired_layout_knobs_are_gone_everywhere():
    offenders = [f"{path.relative_to(PACKAGE.parent)}: {name}"
                 for path in sorted(PACKAGE.rglob("*.py")) if not any(part in path.as_posix() for part in _NOT_OURS)
                 for name in _RETIRED if re.search(rf"\b{name}\b", path.read_text(encoding="utf-8"))]
    assert not offenders, f"{home.ENV_HOME} is the only layout knob:\n  " + "\n  ".join(offenders)


def test_every_layout_row_has_a_known_kind_and_stays_inside_the_home():
    for name, entry in home.LAYOUT.items():
        assert entry.kind in home.KINDS, name
        assert not os.path.isabs(entry.rel) and ".." not in Path(entry.rel).parts, name
        assert home.path(name).is_relative_to(home.home()), name


def test_paths_follow_the_home_at_each_lookup_and_a_test_override_is_undone(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "one"))
    assert CFG["db"] == str(home.path("db")) and CFG["db"].startswith(str((tmp_path / "one").resolve()))
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "two"))
    assert CFG["db"].startswith(str((tmp_path / "two").resolve()))          # nothing was cached
    assert current_config()["tasks_root"] == str(home.path("tasks_root"))   # the materialised view has them too
    with monkeypatch.context() as scoped:
        scoped.setitem(CFG, "db", "/elsewhere/board.db")
        assert CFG["db"] == "/elsewhere/board.db"
    assert CFG["db"] == str(home.path("db"))
    with pytest.raises(KeyError):
        CFG["no_such_setting"]


def test_the_home_is_never_a_project(tmp_path, monkeypatch):
    real = tmp_path / "user" / home.DIR_NAME
    real.mkdir(parents=True)
    monkeypatch.setenv(home.ENV_HOME, str(real))
    assert home.project_dir(tmp_path / "user") is None                      # run from $HOME
    assert home.project_dir(real / "tasks" / "t_1") == real / "tasks" / "t_1" / home.DIR_NAME
    assert home.project_dir(tmp_path / "repo") == tmp_path / "repo" / home.DIR_NAME

    # The home reached through a symlink is still the home.
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / home.DIR_NAME).symlink_to(real)
    assert home.project_dir(tmp_path / "linked") is None

    # A home that is not called .misaka is only a project's config directory by coincidence.
    custom = tmp_path / "custom-home"
    custom.mkdir()
    monkeypatch.setenv(home.ENV_HOME, str(custom))
    assert home.project_dir(tmp_path / "user") == tmp_path / "user" / home.DIR_NAME


def test_project_scope_is_absent_when_the_working_directory_is_the_users_home(tmp_path, monkeypatch):
    from misaka.core import project_trust
    from misaka.core.settings_manager import SettingsManager
    from misaka.core.subagent.agents import _project_agent_dirs
    from misaka.core.subagent.configuration import project_settings

    user = tmp_path / "user"
    real = user / home.DIR_NAME
    (real / home.SUBAGENTS_DIR).mkdir(parents=True)
    (real / "prompts").mkdir()
    (real / "settings.json").write_text('{"theme": "from-the-home"}')
    monkeypatch.setenv(home.ENV_HOME, str(real))
    monkeypatch.setenv("HOME", str(user))

    assert not project_trust.has_trust_requiring_project_resources(str(user))
    assert not project_trust.has_trust_requiring_project_resources(str(real / "tasks" / "t_1"))
    assert _project_agent_dirs(real / "tasks") == []
    assert project_settings(str(user)) == {}
    manager = SettingsManager.create(str(user), str(real), {"projectTrusted": True})
    assert manager.getProjectSettings() == {}
    assert manager.getGlobalSettings() == {"theme": "from-the-home"}

    # An ordinary project beside it still has its scope.
    repo = tmp_path / "repo"
    (repo / home.DIR_NAME / home.SUBAGENTS_DIR).mkdir(parents=True)
    assert project_trust.has_trust_requiring_project_resources(str(repo))
    assert _project_agent_dirs(repo) == [(repo / home.DIR_NAME / home.SUBAGENTS_DIR).resolve()]


def test_a_pointer_into_the_home_is_stored_relative_and_comes_back(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    inside = home.path("sessions") / "cards" / "t_1" / "a.jsonl"
    assert home.stored(inside) == (Path(home.LAYOUT["sessions"].rel) / "cards/t_1/a.jsonl").as_posix()
    assert home.from_stored(home.stored(inside)) == inside
    outside = tmp_path / "project" / "notes.md"
    assert home.stored(outside) == str(outside.resolve())
    assert home.from_stored(home.stored(outside)) == outside.resolve()
    # Moving the home moves the pointer with it.
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "moved"))
    assert home.from_stored("sessions/cards/t_1/a.jsonl").is_relative_to((tmp_path / "moved").resolve())


def test_display_is_what_a_user_would_type(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(home.ENV_HOME, raising=False)
    assert home.display() == f"~/{home.DIR_NAME}"
    assert home.display(home.path("auth")) == f"~/{home.DIR_NAME}/{home.LAYOUT['auth'].rel}"
    monkeypatch.setenv(home.ENV_HOME, "/srv/misaka")
    assert home.display(home.path("db")).startswith("/")


def test_a_socket_too_deep_for_the_kernel_moves_to_a_short_private_directory(tmp_path, monkeypatch):
    shallow = tmp_path / "h"
    monkeypatch.setenv(home.ENV_HOME, str(shallow))
    if len(os.fsencode(shallow.resolve() / home.LAYOUT["net_sock"].rel)) <= 103:
        assert home.path("net_sock").is_relative_to(home.home())

    deep = tmp_path / ("d" * 60) / ("e" * 60)
    monkeypatch.setenv(home.ENV_HOME, str(deep))
    moved = home.path("net_sock")
    assert not moved.is_relative_to(home.home()) and len(os.fsencode(moved)) < 100
    assert moved == home.path("net_sock")                                   # a daemon and its clients agree
    assert home.path("net_snapshot").is_relative_to(home.home())            # only the socket has the limit
    monkeypatch.setenv(home.ENV_HOME, str(deep / "other"))
    assert home.path("net_sock") != moved                                   # one directory per home


def test_a_socket_directory_that_is_not_ours_alone_is_refused_or_closed(tmp_path):
    ours = tmp_path / "run"
    ours.mkdir(mode=0o755)
    home.private_dir(ours)
    assert ours.stat().st_mode & 0o077 == 0
    (tmp_path / "file").write_text("not a directory")
    with pytest.raises((RuntimeError, OSError)):
        home.private_dir(tmp_path / "file")


def test_the_board_stores_session_pointers_relative_and_reads_them_back_as_paths(tmp_path, monkeypatch):
    import sqlite3

    from misaka.core.platform import tasks

    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    con = tasks.connect(CFG["db"])
    try:
        card = tasks.create_task(con, "a card", "body", "10032", workspace=str(tmp_path / "project"))
        session = home.path("sessions") / "cards" / card / "one.jsonl"
        assert tasks.set_runtime(con, card, "agent-1", str(session))
        row = tasks.get(con, card)
        assert row["session_file"] == str(session) and row["session_dir"] == str(session.parent)
        assert dict(row)["session_file"] == str(session)
        # A run row copies the card's pointer: read decoded through Row, it must be stored relative again.
        tasks._start_run(con, card, int(row["generation"]), "worker", "lock-1", 1, "id-1")
    finally:
        con.close()

    # On disk there is no trace of where the home was.
    raw = sqlite3.connect(CFG["db"])
    try:
        stored = raw.execute("SELECT session_file, session_dir FROM tasks WHERE id=?", (card,)).fetchone()
        assert stored == (home.stored(session), home.stored(session.parent))
        assert raw.execute("SELECT session_file FROM task_runs WHERE task_id=?", (card,)).fetchone() == (home.stored(session),)
        dump = "\n".join(str(value) for table, in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
                         for record in raw.execute(f'SELECT * FROM "{table}"') for value in record)
        assert str(home.home()) not in dump
    finally:
        raw.close()

    # The same board under a moved home resolves against the new one.
    moved = tmp_path / "moved"
    (tmp_path / "home").rename(moved)
    monkeypatch.setenv(home.ENV_HOME, str(moved))
    con = tasks.connect(CFG["db"])
    try:
        assert tasks.get(con, card)["session_file"] == str(home.path("sessions") / "cards" / card / "one.jsonl")
    finally:
        con.close()


def test_ensure_makes_the_home_and_its_credentials_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    home.ensure()
    home.ensure()                                                            # idempotent
    for name in ("agent", "credentials"):
        assert home.path(name).stat().st_mode & 0o777 == 0o700, name
    home.path("credentials").chmod(0o755)
    home.ensure()
    assert home.path("credentials").stat().st_mode & 0o777 == 0o700         # a widened one is closed again


def test_the_home_root_holds_only_what_a_user_edits_and_one_directory_per_kind():
    top = {}
    for entry in home.LAYOUT.values():
        top.setdefault(entry.rel.split("/", 1)[0], set()).add(entry.kind)
    mixed = {name: kinds for name, kinds in top.items() if name and len(kinds) > 1}
    assert not mixed, f"one kind per top-level entry: {mixed}"
    assert {name for name, kinds in top.items() if kinds != {"edit"}} == {
        "credentials", "state", "shared", "cache", "logs", "run"}


def test_ensure_narrows_a_credential_file_that_was_left_readable(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    home.ensure()
    assert not home.path("auth").exists()                                    # never created here
    home.path("auth").write_text("{}")
    home.path("auth").chmod(0o644)
    home.ensure()
    assert home.path("auth").stat().st_mode & 0o777 == 0o600


def test_a_board_written_by_another_build_is_refused_by_version_and_left_alone(tmp_path, monkeypatch):
    import sqlite3

    from misaka.core.platform import tasks
    from misaka.core.research import runs

    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    con = tasks.connect(CFG["db"])
    try:
        assert con.execute("SELECT version FROM schema_migrations WHERE component='tasks'").fetchone()[0] == tasks.TASK_SCHEMA_VERSION
        runs.init(con)
        assert con.execute("SELECT version FROM schema_migrations WHERE component='research'").fetchone()[0] == runs.RESEARCH_SCHEMA_VERSION
        runs.init(con)                                                              # current: a no-op
    finally:
        con.close()
    raw = sqlite3.connect(CFG["db"])
    raw.execute("UPDATE schema_migrations SET version=version-1 WHERE component='tasks'")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match=f"another MISAKA .v{tasks.TASK_SCHEMA_VERSION - 1}"):
        tasks.connect(CFG["db"])
    raw = sqlite3.connect(CFG["db"])                                                # nothing was reshaped
    assert raw.execute("SELECT version FROM schema_migrations WHERE component='tasks'").fetchone()[0] == tasks.TASK_SCHEMA_VERSION - 1
    raw.close()

    # A populated board with no marker at all is another build's too.
    other = tmp_path / "unmarked.db"
    raw = sqlite3.connect(other)
    raw.executescript(tasks.SCHEMA)
    raw.execute("INSERT INTO tasks (id,title,assignee,workspace,created_at) VALUES ('t_1','x','10032','/p',1)")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="no version marker"):
        tasks.connect(str(other))


def test_a_role_session_writes_its_own_keys_to_its_file_and_everything_else_globally(tmp_path, monkeypatch):
    from misaka.core.settings_manager import ROLE_KEYS, SettingsManager

    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    home.path("settings").parent.mkdir(parents=True)
    home.path("settings").write_text(json.dumps({"theme": "dark", "web": {"search_backend": "exa", "cache_ttl_minutes": 5}}))
    role = home.path("profiles_root") / "10032"
    role.mkdir(parents=True)
    manager = SettingsManager.forRole(str(role))

    manager.updateSection("web", lambda section: section.update({"search_backend": "tavily"}))   # a role key
    manager.setValue("theme", "light")                                                         # not one
    assert json.loads((role / "settings.json").read_text()) == {"web": {"search_backend": "tavily"}}
    assert json.loads(home.path("settings").read_text())["theme"] == "light"
    assert json.loads(home.path("settings").read_text())["web"] == {"search_backend": "exa", "cache_ttl_minutes": 5}

    # Reads: the role's value over the home's, the home's where the role says nothing.
    assert manager.getSection("web") == {"search_backend": "tavily", "cache_ttl_minutes": 5}
    assert manager.getScopedSection("role", "web") == {"search_backend": "tavily"}
    assert SettingsManager.forRole(None).getSection("web") == {"search_backend": "exa", "cache_ttl_minutes": 5}

    # Another role sees none of it, and the table is what decides.
    other = home.path("profiles_root") / "10036"
    other.mkdir()
    assert SettingsManager.forRole(str(other)).getSection("web") == {"search_backend": "exa", "cache_ttl_minutes": 5}
    assert {"defaultProvider", "defaultModel", "mcpServers", "web"} == set(ROLE_KEYS)


def test_the_role_scope_survives_the_manager_and_the_home_alike(tmp_path, monkeypatch):
    from misaka.config import profiles
    from misaka.core.settings_manager import SettingsManager

    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    role = home.path("roles_root") / "last_order"
    role.mkdir(parents=True)
    assert profiles.persist_role_default_model(str(role), "anthropic/claude-sonnet-5", strict=True)
    assert profiles.pinned_model(str(role)) == "anthropic/claude-sonnet-5"
    manager = SettingsManager.forRole(str(role))
    assert manager.getDefaultModelPair() == ("anthropic", "claude-sonnet-5")
    manager.updateSection("mcpServers", lambda s: s.update({"camofox": {"command": "npx"}}))
    assert json.loads((role / "settings.json").read_text()) == {
        "defaultProvider": "anthropic", "defaultModel": "claude-sonnet-5", "mcpServers": {"camofox": {"command": "npx"}}}


def test_a_section_write_edits_the_file_as_it_is_now_not_this_managers_stale_copy(tmp_path, monkeypatch):
    """Two processes hold their own SettingsManager. When one writes the ``web`` section after
    the other loaded, the second's later edit of the same section must build on the file, not
    on its own older copy -- otherwise the first write silently disappears."""
    from misaka.core.settings_manager import SettingsManager

    first = SettingsManager.forRole(None, str(tmp_path))
    first.updateSection("web", lambda section: section.update({"a": 1}))
    second = SettingsManager.forRole(None, str(tmp_path))          # loaded now: sees a=1
    first.updateSection("web", lambda section: section.update({"b": 2}))
    second.updateSection("web", lambda section: section.update({"c": 3}))
    assert SettingsManager.forRole(None, str(tmp_path)).getSection("web") == {"a": 1, "b": 2, "c": 3}
    assert second.getSection("web") == {"a": 1, "b": 2, "c": 3}   # its copy caught up with the file
