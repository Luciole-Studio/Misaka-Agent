"""Skills run where they live, as in Hermes (F1, 2026-10-24).

A Sister's card could not run the script its SKILL.md names: MISAKA served skills from
temporary copies, refused every shell command that named a skill folder, and in unattended
sessions every `$NAME` expansion. Hermes does none of that -- `skill_view` returns the skill's
own directory (tools/skills_tool.py), `${HERMES_SKILL_DIR}` and `[Skill directory:]` name it
(agent/skill_preprocessing.py, agent/skill_commands.py) -- and its file tools refuse only the
skills hub cache (agent/file_safety.py)."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from misaka.core.skills.wiring.skills import SkillsPart


async def published(part, base="BASE", ctx=None):
    """What a part adds to the run's prompt: its sections, as a pi extension adds them, joined as the
    prompt renders them. A part never returns a whole prompt, which would be forced."""
    options = {"sections": {}}
    assert await part.before_agent_start({"systemPrompt": base, "systemPromptOptions": options}, ctx) is None
    return "\n\n".join(options["sections"].values())


@pytest.fixture
def skills(tmp_path, monkeypatch):
    from misaka.config import CFG
    from misaka.core.extensions import startup_sections

    monkeypatch.setitem(CFG, "roles_root", str(tmp_path / "profiles"))
    monkeypatch.setitem(CFG, "profiles_root", str(tmp_path / "profiles" / "sisters"))
    monkeypatch.setattr(startup_sections, "register", lambda *a, **k: None)
    profile = tmp_path / "profile"
    root = profile / "skills"
    folder = root / "runner"
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: runner\ndescription: Fixture skill.\n---\nRun `${HERMES_SKILL_DIR}/scripts/run.sh`.\n",
        encoding="utf-8")
    (folder / "scripts" / "run.sh").write_text("echo ok\n", encoding="utf-8")
    return SimpleNamespace(tmp=tmp_path, profile=profile, root=root, folder=folder)


def _part(skills, kind):
    """A session that discovers the role's layers itself, as every session does in production
    (explicit roots never took the old copy path, so they would prove nothing)."""
    return SkillsPart(None, str(skills.profile), cwd=str(skills.tmp), kind=kind)


@pytest.mark.parametrize("kind", ["foreground", "dm", "card", "child"])
def test_skill_view_returns_the_skills_own_directory(skills, kind):
    part = _part(skills, kind)
    try:
        view = next(tool for tool in part.tools if tool.name == "skill_view")
        result = asyncio.run(view.execute("call", {"name": "runner"}, None, None, SimpleNamespace()))
        payload = json.loads(result["content"][0]["text"])
        assert payload["success"]
        assert os.path.realpath(payload["skill_dir"]) == os.path.realpath(skills.folder)
        assert "misaka-skill-execution-" not in payload["content"]
        assert os.path.realpath(skills.folder / "scripts" / "run.sh") in {
            os.path.realpath(word.strip("`.")) for word in payload["content"].split() if "run.sh" in word}
    finally:
        asyncio.run(part.session_shutdown({}, SimpleNamespace()))


@pytest.mark.parametrize("kind", ["foreground", "card"])
def test_an_invoked_skill_names_its_own_directory(skills, kind):
    from misaka.core.skills import index as skill_index

    part = _part(skills, kind)
    try:
        entry, error = skill_index.resolve(part.resolution_roots(), "runner")
        assert error is None
        message = asyncio.run(part._run_activation(part._single_message, entry, "", None))
        assert f"[Skill directory: {skills.folder}]" in message
        assert f"{skills.folder}/scripts/run.sh" in message and "${HERMES_SKILL_DIR}" not in message
    finally:
        asyncio.run(part.session_shutdown({}, SimpleNamespace()))


def test_a_child_preloads_skills_from_where_they_live(skills):
    """Hermes preloads a child's skills through the live loader (build_preloaded_skills_prompt)."""
    from misaka.core.subagent.runtime import SubagentManager

    manager = SimpleNamespace(role_context=SimpleNamespace(profile_dir=str(skills.profile)))
    task = SimpleNamespace(id="a1", cwd=str(skills.tmp),
                           definition=SimpleNamespace(skills=["runner"], base_dir=None))
    loaded = asyncio.run(SubagentManager._preloaded_skills(manager, task))
    assert len(loaded) == 1 and f"[Skill directory: {skills.folder}]" in loaded[0]


@pytest.mark.parametrize("kind", ["card", "child"])
def test_a_cards_skill_index_no_longer_calls_skills_read_only(skills, kind):
    part = _part(skills, kind)
    try:
        prompt = asyncio.run(published(part, "base", SimpleNamespace()))
        assert "runner" in prompt and "Skills are read-only in this session" not in prompt
    finally:
        asyncio.run(part.session_shutdown({}, SimpleNamespace()))


def test_read_only_agent_types_still_do_not_write_skills():
    """Explore, Plan and verification promise not to modify anything; skill_manage, which cards
    and children now have as in Hermes, is withheld from them like edit and write."""
    from pathlib import Path

    from misaka.core.subagent import agents

    folder = Path(agents.__file__).parent / "agents"
    for name in ("Explore", "Plan", "verification"):
        definition = agents.parse(folder / f"{name}.md")
        assert "skill_manage" in definition.disallowed_tools, name


def test_the_misaka_template_aliases_are_gone_as_in_hermes():
    from misaka.core.skills.vendor.preprocessing import substitute_template_vars

    assert substitute_template_vars("${MISAKA_SKILL_DIR}", "/s", "id") == "${MISAKA_SKILL_DIR}"
    assert substitute_template_vars("${HERMES_SKILL_DIR}/${HERMES_SESSION_ID}", "/s", "id") == "/s/id"


@pytest.mark.parametrize("kind", ["card", "child"])
def test_a_card_and_its_children_manage_skills_as_hermes_workers_do(skills, kind):
    part = _part(skills, kind)
    try:
        assert "skill_manage" in {tool.name for tool in part.tools}
        assert "learn" in {command.name for command in part._commands}
    finally:
        asyncio.run(part.session_shutdown({}, SimpleNamespace()))


def test_the_file_tools_do_not_read_a_skills_hub_cache(tmp_path, monkeypatch):
    from misaka.config import home
    from misaka.core.web.evidence import check_material_read, is_skills_hub_cache

    shared_hub = home.path("shared_skills") / ".hub"
    shared_hub.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match=r"^Access denied: .* is an internal MISAKA cache file .* prompt injection"):
        check_material_read(str(shared_hub / "lock.json"))
    role = tmp_path / "role"
    (role / "skills" / ".hub").mkdir(parents=True)
    monkeypatch.setenv("MISAKA_PROFILE_DIR", str(role))
    assert is_skills_hub_cache(str(role / "skills" / ".hub" / "taps.json"))
    assert not is_skills_hub_cache(str(tmp_path / "other-role" / "skills" / ".hub" / "taps.json"))
    check_material_read(str(home.path("shared_skills") / "pptx" / "SKILL.md"))   # a skill itself is readable


@pytest.mark.parametrize("trust,expanded", [("community", False), ("trusted", True)])
def test_inline_shell_stays_off_for_a_community_hub_skill(tmp_path, trust, expanded):
    from misaka.core.skills.preprocessing import preprocess_skill_content

    root = tmp_path / "skills"
    skill = root / "hubbed"
    skill.mkdir(parents=True)
    (root / ".hub").mkdir()
    lock = {"installed": {"hubbed": {"install_path": "hubbed", "trust_level": trust}}}
    (root / ".hub" / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    out = preprocess_skill_content("x !`echo hi` y", skill, None, {"inline_shell": True}, layer="role")
    assert ("!`echo hi`" not in out and "hi" in out) is expanded


@pytest.mark.parametrize("kind,last_order,guarded,reports", [
    ("foreground", True, True, True), ("dm", False, True, True), ("card", False, True, True),
    ("child", False, True, True), ("beast", False, False, True), ("bare", False, False, False),
    ("bare", True, True, False), ("beast", True, True, False),
])
def test_the_home_guard_checks_the_kinds_the_skill_guard_used_to(monkeypatch, kind, last_order, guarded, reports):
    """The file-tool checks moved here from the skills extension, with the same kinds: every kind
    but a Sister's one-shot and beast wrap-up, and Last Order's one-shots (her beast one included).
    Asked through parts_for, so the module's SESSION_KINDS filter is part of what is checked."""
    from misaka.config import profiles
    from misaka.core import wiring
    from misaka.core.platform.home_guard import HomeGuard

    monkeypatch.setattr(profiles, "is_last_order", lambda _profile: last_order)
    spec = wiring.SessionSpec(profile_dir="/p", role="r", workspace="/w", kind=kind)
    found = [part for part in wiring.parts_for(spec) if isinstance(part, HomeGuard)]
    if not (guarded or reports):
        assert found == []
    else:
        assert len(found) == 1 and (found[0]._guard_paths, found[0]._report_strays) == (guarded, reports)


@pytest.mark.parametrize("kind", ["foreground", "dm", "card", "child"])
def test_no_part_of_a_session_refuses_a_skill_script_run_the_skill_md_way(monkeypatch, kind):
    """The whole session stack, as parts_for assembles it, lets the command a SKILL.md documents through."""
    from misaka.config import profiles
    from misaka.core import wiring
    from misaka.core.moments import Moments

    monkeypatch.setattr(profiles, "is_last_order", lambda _profile: kind == "foreground")
    # A session started without a parent's CCB permission mode (that policy is its own layer).
    for key in [key for key in os.environ if key.startswith("MISAKA_SUBAGENT_")]:
        monkeypatch.delenv(key)
    spec = wiring.SessionSpec(profile_dir="/tmp/profiles/sisters/x", role="x", workspace="/tmp/ws", kind=kind)
    session = SimpleNamespace(extensionRunner=SimpleNamespace(createContext=lambda: SimpleNamespace()))
    moments = Moments(session, [])   # the runner's dispatch over the parts, without a live session to attach
    moments.parts = wiring.parts_for(spec)
    from misaka.config import home

    # The home's own skills folder and a `$NAME` expansion: the two things the old guard refused.
    command = f'SKILLS={home.path("shared_skills")}; python3 -B "$SKILLS/cnki/scripts/run.py" search "$Q"'
    try:
        assert asyncio.run(moments.tool_call({"toolName": "bash", "input": {"command": command}})) is None
    finally:
        for part in moments.parts:
            shutdown = getattr(part, "session_shutdown", None)
            if shutdown is not None:
                try:
                    asyncio.run(shutdown({}, SimpleNamespace()))
                except Exception:  # noqa: BLE001, S110 - a fixture part without a live session may not close cleanly
                    pass


@pytest.mark.parametrize("backend", ["fd", "glob"])
def test_find_passes_a_hub_cache_and_the_credentials_by(tmp_path, backend):
    """Hermes' search_files drops read-blocked paths from its file list (_filter_read_blocked_search_results)."""
    import shutil

    from misaka.config import home
    from misaka.core.tools.find import createFindToolDefinition

    if backend == "fd" and shutil.which("fd") is None:
        pytest.skip("fd is not installed")
    skills_root = home.path("shared_skills")
    for path in (skills_root / ".hub" / "lock.json", skills_root / "pptx" / "notes.json",
                 home.path("credentials") / "auth.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    ops = None
    if backend == "glob":
        listed = [str(p) for p in home.home().rglob("*.json")]
        ops = SimpleNamespace(exists=lambda _path: True, glob=lambda *_a, **_k: listed)
    tool = createFindToolDefinition(str(home.home()), {"operations": ops} if ops else None)
    result = asyncio.run(tool.execute("call", {"pattern": "*.json"}, None, None, SimpleNamespace(cwd=str(home.home()))))
    text = result.content[0].text
    assert "notes.json" in text and "lock.json" not in text and "auth.json" not in text
