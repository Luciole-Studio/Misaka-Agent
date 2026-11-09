"""A session's permission mode and dialog, as CCB's (77a7934), 2026-10-24.

MISAKA ported CCB's sub-agent policy but not where a mode comes from: Last Order and the
Sisters' own sessions never asked (pi has no approvals) yet told their sub-agents "default", so
a background child refused what its parent did freely. Now every session starts in the settings'
``permissions.defaultMode`` (``initialPermissionModeFromCLI``), bypassPermissions when unset (user
ruling), its sub-agents inherit it (``agentGetAppState``), and what asks shows CCB's dialog:
Yes / Yes, and don't ask again / No."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from misaka.core.subagent import configuration
from misaka.core.subagent import permission_suggestions as offers


class _Settings:
    def __init__(self, tmp_path, global_=None, project=None, trusted=True):
        self.globalSettings, self.projectSettings, self.trusted = global_ or {}, project or {}, trusted
        self.storage = SimpleNamespace(projectSettingsPath=str(tmp_path / ".misaka" / "settings.json"))
        self.saved = []

    def getGlobalSettings(self):
        return self.globalSettings

    def getProjectSettings(self):
        return json.loads(json.dumps(self.projectSettings))

    def getManagedSettings(self):
        return {}

    def isProjectTrusted(self):
        return self.trusted

    def markProjectModified(self, field, nested=None):
        pass

    def saveProjectSettings(self, settings):
        self.projectSettings = settings
        self.saved.append(settings)


def _session(tmp_path, **settings):
    return SimpleNamespace(settingsManager=_Settings(tmp_path, **settings), cwd=str(tmp_path),
                           moments=SimpleNamespace(parts=[]), getAllTools=list)


@pytest.mark.parametrize("permissions,mode", [
    ({}, "bypassPermissions"),                                       # unset: nothing asks, as before
    ({"disableBypassPermissionsMode": "disable"}, "default"),
    ({"defaultMode": "default"}, "default"),
    ({"defaultMode": "acceptEdits"}, "acceptEdits"),
    ({"defaultMode": "bypassPermissions", "disableBypassPermissionsMode": "disable"}, "default"),
    ({"defaultMode": "auto"}, "default"),     # no transcript classifier at the top: CCB's circuit breaker
    ({"defaultMode": "bubble"}, "default"),
])
def test_a_top_level_session_starts_in_the_settings_mode(tmp_path, permissions, mode):
    session = _session(tmp_path, global_={"permissions": permissions})
    assert configuration.initial_permission_mode(session) == mode


def test_project_settings_override_the_users(tmp_path):
    session = _session(tmp_path, global_={"permissions": {"defaultMode": "acceptEdits"}},
                       project={"permissions": {"defaultMode": "plan"}})
    assert configuration.initial_permission_mode(session) == "plan"


@pytest.mark.parametrize("command,prefix", [
    ('git commit -m "fix typo"', "git commit"), ("NODE_ENV=prod npm run build", "npm run"),
    ("MY_VAR=val npm run build", None), ("ls -la", None), ("cat file.txt", None), ("chmod 755 file", None),
])
def test_the_command_prefix_is_ccbs(command, prefix):
    assert offers.get_simple_command_prefix(command) == prefix


def _part(tmp_path, monkeypatch, *, mode=None, settings=None):
    from misaka.core.subagent.extension import part_for

    for key in ("MISAKA_SUBAGENT_ID", "MISAKA_SUBAGENT_PERMISSION_MODE", "MISAKA_SUBAGENT_PERMISSION_SETTINGS"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    part = part_for(str(tmp_path / "profile"), "10032", str(workspace))
    permissions = {"defaultMode": mode} if mode else {}
    session = SimpleNamespace(settingsManager=_Settings(workspace, project=settings or {"permissions": permissions}),
                              cwd=str(workspace), getAllTools=lambda: [SimpleNamespace(name="doc_read")])
    session.moments = SimpleNamespace(parts=[part])
    session.getPermissionMode = part.get_permission_mode
    part.attach(session)
    return part, session, workspace


def _ui(*answers):
    asked = []

    async def select(title, options):
        asked.append((title, options))
        return options[answers[len(asked) - 1]]

    return SimpleNamespace(hasUI=True, ui=SimpleNamespace(select=select)), asked


def _bash(part, command, ctx):
    return asyncio.run(part.tool_call({"toolName": "bash", "input": {"command": command}}, ctx))


def test_by_default_nothing_asks_and_a_child_inherits_that(tmp_path, monkeypatch):
    part, _session, _workspace = _part(tmp_path, monkeypatch)
    assert part.get_permission_mode() == "bypassPermissions" and part.policy.top_level
    assert _bash(part, 'SKILLS=/x; python3 "$SKILLS/run.py"', None) is None
    # CCB agentGetAppState: a parent in bypassPermissions keeps it over the agent's own mode.
    assert configuration.child_permission_mode(part.get_permission_mode(), "plan") == "bypassPermissions"


def test_a_child_does_not_take_the_top_level_mode(tmp_path, monkeypatch):
    from misaka.core.subagent.extension import part_for

    monkeypatch.setenv("MISAKA_SUBAGENT_ID", "a1")
    monkeypatch.setenv("MISAKA_SUBAGENT_PERMISSION_MODE", "default")
    part = part_for(str(tmp_path), "10032", str(tmp_path))
    part.attach(SimpleNamespace(settingsManager=_Settings(tmp_path), cwd=str(tmp_path)))
    assert part.get_permission_mode() == "default" and not part.policy.top_level


def test_in_default_mode_a_top_level_session_asks_its_person(tmp_path, monkeypatch):
    part, _session, _workspace = _part(tmp_path, monkeypatch, mode="default")
    assert _bash(part, "ls", None) is None                     # read-only: no question
    assert asyncio.run(part.tool_call({"toolName": "doc_read", "input": {}}, None)) is None   # its own tool
    ctx, asked = _ui(0)
    assert _bash(part, "python3 run.py", ctx) is None           # Yes
    assert asked[0][1][0] == "Yes" and asked[0][1][-1] == "No"
    ctx, _asked = _ui(2)
    assert _bash(part, "python3 run.py", ctx)["block"]           # No
    blocked = _bash(part, "python3 run.py", None)                # nobody to ask: a card, a contact turn
    assert blocked["block"] and "cannot display a permission prompt" in blocked["reason"]


def test_dont_ask_again_saves_a_prefix_rule_in_the_project(tmp_path, monkeypatch):
    part, session, workspace = _part(tmp_path, monkeypatch, mode="default")
    ctx, asked = _ui(1)
    assert _bash(part, "npm run build", ctx) is None
    assert asked[0][1][1] == f"Yes, and don't ask again for npm run commands in {workspace}"
    assert session.settingsManager.projectSettings["permissions"]["allow"] == ["Bash(npm run:*)"]
    assert _bash(part, "npm run test", None) is None             # not asked again


def test_dont_ask_again_stays_in_the_session_when_the_project_is_not_trusted(tmp_path, monkeypatch):
    part, session, _workspace = _part(tmp_path, monkeypatch, mode="default")
    session.settingsManager.trusted = False
    assert _bash(part, "npm run build", _ui(1)[0]) is None
    assert session.settingsManager.saved == [] and part.policy.session_rules == ["Bash(npm run:*)"]
    assert _bash(part, "npm run test", None) is None


# Paths go into the commands with forward slashes: the shell is bash on Windows too (Git Bash), where a backslash is an
# escape and `C:\Users\x` reads as `C:Usersx`.
def test_dont_ask_again_for_a_path_outside_grants_its_directory_for_this_session(tmp_path, monkeypatch):
    part, _session, _workspace = _part(tmp_path, monkeypatch, mode="default")
    outside = tmp_path / "skills" / "cnki" / "scripts"
    outside.mkdir(parents=True)
    (outside / "run.py").write_text("print(1)\n", encoding="utf-8")
    ctx, asked = _ui(1)
    assert _bash(part, f"cat {(outside / 'run.py').as_posix()}", ctx) is None
    assert asked[0][1][1] == "Yes, allow reading from scripts/ from this project"
    assert part.policy.session_rules == [f"Read({outside.as_posix()}/**)"]
    assert _bash(part, f"cat {(outside / 'other.txt').as_posix()}", None) is None
    ctx, asked = _ui(1)                    # the folder is readable now: what still asks is the command itself
    assert _bash(part, f"python3 {(outside / 'run.py').as_posix()} search x", ctx) is None
    assert asked[0][1][1].startswith("Yes, and don't ask again for similar commands in ")


def test_dont_ask_again_for_a_script_outside_grants_its_folder_and_the_command(tmp_path, monkeypatch):
    part, session, _workspace = _part(tmp_path, monkeypatch, mode="default")
    outside = tmp_path / "skills" / "cnki" / "scripts"
    outside.mkdir(parents=True)
    (outside / "run.py").write_text("print(1)\n", encoding="utf-8")
    ctx, asked = _ui(1)
    assert _bash(part, f"python3 {(outside / 'run.py').as_posix()} search x", ctx) is None
    assert asked[0][1][1] == "Yes, and allow access to scripts/ and similar commands"
    assert _bash(part, f"python3 {(outside / 'run.py').as_posix()} search x", None) is None
    assert configuration.permission_settings(session)[-1]["additionalDirectories"] == [str(outside.resolve())]


def test_a_protected_or_ambiguous_ask_offers_no_dont_ask_again(tmp_path, monkeypatch):
    part, _session, _workspace = _part(tmp_path, monkeypatch, mode="default")
    ctx, asked = _ui(0)
    assert _bash(part, 'python3 "$HOME/run.py"', ctx) is None
    assert asked[0][1] == ["Yes", "No"]


def test_a_childs_ask_shows_the_dialog_in_the_parent_and_saves_there(tmp_path, monkeypatch):
    from misaka.core.subagent.runtime import SubagentManager

    saved = []
    ctx, asked = _ui(1)
    manager = SimpleNamespace(session=None, permission_updates=saved.extend)
    task = SimpleNamespace(id="a1", permission_context=ctx)
    suggestions = offers.suggestion_for_exact_command("git commit -m x")
    event = {"toolName": "bash", "toolInput": {"command": "git commit -m x"}, "suggestions": suggestions,
             "suggestionLabel": "Yes, and don't ask again for git commit commands in /p"}
    assert asyncio.run(SubagentManager._resolve_permission_request(manager, task, event)) is True
    assert asked[0][1] == ["Yes", event["suggestionLabel"], "No"] and saved == suggestions


def test_a_mode_chosen_live_survives_a_session_switch(tmp_path, monkeypatch):
    part, session, _workspace = _part(tmp_path, monkeypatch)
    part.set_permission_mode("default")
    part.attach(session)                       # /new, /resume: the part is attached again
    assert part.get_permission_mode() == "default"


def test_a_top_level_session_cannot_switch_to_auto(tmp_path, monkeypatch):
    part, _session, _workspace = _part(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="transcript classifier"):
        part.set_permission_mode("auto")


def test_last_order_has_the_policy_but_starts_no_sub_agents(monkeypatch):
    from misaka.config import profiles
    from misaka.core import wiring
    from misaka.core.subagent.extension import SubagentPart

    monkeypatch.setattr(profiles, "is_last_order", lambda _profile: True)
    spec = wiring.SessionSpec(profile_dir="/p", role="last-order", workspace="/w", kind="foreground")
    found = [part for part in wiring.parts_for(spec) if isinstance(part, SubagentPart)]
    assert len(found) == 1 and found[0].manager is None and found[0].tools == []
