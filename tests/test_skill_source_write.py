"""A skill edit lands in the directory MISAKA loaded, and in the startup copy."""
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

from misaka.core.skills import write as skill_write

from misaka.config import home
from misaka.core.skills import layers, manage, review, sandbox
from misaka.core.skills.vendor.skill_provenance import (
    reset_current_write_origin,
    set_current_write_origin,
)

BODY = """---
name: {name}
description: Explain one test skill.
---
Original body.
"""
REVISED = BODY.replace("Original body.", "Revised body.")


def _skill(root, name="mw-source-skill"):
    _guard_available()
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(BODY.format(name=name), encoding="utf-8")
    return folder


def _guard_available():
    """The scanner module is the real one when this tree can read it."""
    name = "misaka.core.skills.guard"
    if name in sys.modules:
        return
    try:
        __import__(name)
    except (ImportError, OSError):
        module = types.ModuleType(name)
        module.MAX_SINGLE_FILE_KB = 1024
        module.scan_skill = lambda *_args, **_kwargs: types.SimpleNamespace()
        module.format_scan_report = lambda *_args, **_kwargs: ""
        module.should_allow_install = lambda *_args, **_kwargs: (True, "")
        sys.modules[name] = module


def _allow(external):
    layers.write_skills_config({"skill_write_mode": "allow", "external_dirs": [str(external)]})


def _place():
    return Path(tempfile.mkdtemp(prefix="skill-src-"))


def test_a_nested_skill_snapshot_survives_windows_path_spelling():
    root = _place()
    skill = root / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("top", encoding="utf-8")
    (skill / "references" / "note.md").write_text("nested", encoding="utf-8")
    image = skill_write.tree_image(skill)
    dest = root / "restored"
    skill_write._materialize(dest, image)
    assert (dest / "references" / "note.md").read_text(encoding="utf-8") == "nested"
    with pytest.raises(ValueError, match="Duplicate or invalid path"):
        skill_write._checked_snapshot([{"path": "references\\note.md", "sha256": "a" * 64, "mode": 0o644}])
    shutil.rmtree(root, ignore_errors=True)


def test_an_edit_returns_to_the_loaded_directory_and_the_startup_copy():
    root = _place()
    external = root / "agents-skills"
    source = _skill(external)
    profile = root / "role"
    (profile / "skills").mkdir(parents=True)
    _allow(external)
    entry = {"dir": str(source), "path": str(source / "SKILL.md"), "name": "mw-source-skill",
             "runtime_name": "mw-source-skill", "rel": "mw-source-skill", "category": "general",
             "layer": "external"}
    sandbox_root = Path(home.path("tasks_root")) / "card-1" / ".skills-ro"
    sandbox.readonly_copies([entry], sandbox_root)
    sandbox.seal(sandbox_root)

    result = manage.manage("patch", "mw-source-skill", profile_dir=str(profile), content=REVISED.format(name="mw-source-skill"),
                           workspace=str(root / "project"), visible_roots=[("external", str(external))])
    assert result["success"], result
    assert "Revised body." in (source / "SKILL.md").read_text(encoding="utf-8")
    assert not (profile / "skills" / "mw-source-skill").exists()
    assert Path(result["written_paths"][0]).resolve() == source.resolve()
    copy = Path(result["updated_copies"][0]) / "SKILL.md"
    assert "Revised body." in copy.read_text(encoding="utf-8")
    sandbox.read_manifest(sandbox_root, verify_files=True)
    shutil.rmtree(root, ignore_errors=True)


def test_a_staged_review_names_the_source_and_the_copy_before_approval():
    root = _place()
    external = root / "agents-skills"
    source = _skill(external)
    profile = root / "role"
    (profile / "skills").mkdir(parents=True)
    layers.write_skills_config({"external_dirs": [str(external)]})
    entry = {"dir": str(source), "path": str(source / "SKILL.md"), "name": "mw-source-skill",
             "runtime_name": "mw-source-skill", "rel": "mw-source-skill", "category": "general"}
    sandbox.readonly_copies([entry], Path(home.path("tasks_root")) / "card-2" / ".skills-ro")

    staged = manage.manage("patch", "mw-source-skill", profile_dir=str(profile), content=REVISED.format(name="mw-source-skill"),
                           workspace=str(root / "project"), visible_roots=[("external", str(external))])
    assert staged["staged"], staged
    assert "Original body." in (source / "SKILL.md").read_text(encoding="utf-8")
    rows = review.pending_items(with_diff=True)
    assert rows and rows[0]["sources"] == [str(source.resolve())]
    assert rows[0]["copies"]
    assert "Revised body." in rows[0]["diff"]
    decided = review.decide(rows[0]["id"], "approve")
    assert decided["approved"], decided
    assert "Revised body." in (source / "SKILL.md").read_text(encoding="utf-8")
    assert "Revised body." in Path(decided["copies"][0], "SKILL.md").read_text(encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)


def test_a_skill_that_ships_with_misaka_is_not_edited_in_place():
    root = _place()
    bundled = root / "bundled"
    _skill(bundled, "mw-bundled-skill")
    profile = root / "role"
    (profile / "skills").mkdir(parents=True)
    _allow(root / "unused")
    result = manage.manage("patch", "mw-bundled-skill", profile_dir=str(profile), content=REVISED.format(name="mw-bundled-skill"),
                           workspace=str(root / "project"), visible_roots=[("vendor", str(bundled))])
    assert not result["success"]
    assert "ships with MISAKA" in result["error"]
    assert "Original body." in (bundled / "mw-bundled-skill" / "SKILL.md").read_text(encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)


def test_background_review_does_not_write_the_loaded_source():
    root = _place()
    external = root / "agents-skills"
    source = _skill(external)
    profile = root / "role"
    (profile / "skills").mkdir(parents=True)
    _allow(external)
    token = set_current_write_origin("background_review")
    try:
        result = manage.manage("patch", "mw-source-skill", profile_dir=str(profile), content=REVISED.format(name="mw-source-skill"),
                               workspace=str(root / "project"), visible_roots=[("external", str(external))])
    finally:
        reset_current_write_origin(token)
    assert not result["success"]
    assert "own skills" in result["error"]
    assert "Original body." in (source / "SKILL.md").read_text(encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)
