"""Fault-injection coverage for source-bound skill reviews and snapshot refreshes."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from misaka.config import CFG, home
from misaka.core.skills import index, layers, manage, operations, review, sandbox
from misaka.core.skills.scope import scope_for, using_scope
from misaka.core.skills.vendor import skill_usage

BODY = "---\nname: sample\ndescription: Explain a fictional example.\n---\nOriginal body.\n"


@pytest.fixture(autouse=True)
def isolated_skill_roots(tmp_path, monkeypatch):
    """Keep discovery and telemetry inside disposable role and project roots."""
    monkeypatch.setattr(layers, "external_skills_dirs", list)
    monkeypatch.setitem(CFG, "roles_root", str(tmp_path / "profiles"))
    index.invalidate()
    yield
    index.invalidate()


def skill(folder):
    """Create one valid skill document without executable helpers."""
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(BODY, encoding="utf-8")
    return folder


@pytest.fixture
def sealed_copy(tmp_path):
    """Supply a real sealed legacy snapshot and release its read-only files."""
    source = skill(tmp_path / "sample")
    root = home.path("tasks_root") / "fixture-card" / ".skills-ro"
    entry = {"dir": str(source), "path": str(source / "SKILL.md"), "name": "sample",
             "runtime_name": "sample", "rel": "sample", "category": "general", "layer": "external"}
    sandbox.readonly_copies([entry], root)
    sandbox.seal(root)
    yield source, root, root / source.name
    sandbox.cleanup(root)


@pytest.mark.parametrize("failure", ["copy", "seal", "publish"])
def test_failed_snapshot_refresh_preserves_the_before_image(sealed_copy, failure):
    """A partial copy, failed seal, or failed swap must leave the old seal valid."""
    source, root, destination = sealed_copy
    before = (destination / "SKILL.md").read_bytes()
    manifest = (root / ".misaka-skill-snapshot.json").read_bytes()
    (source / "SKILL.md").write_text(BODY.replace("Original", "Revised"), encoding="utf-8")
    original_copy, original_replace = shutil.copytree, os.replace

    def copy(src, dst, *args, **kwargs):
        if failure == "copy" and Path(src) == source:
            Path(dst).mkdir()
            (Path(dst) / "partial.txt").write_text("partial", encoding="utf-8")
            raise OSError("fixture copy failed")
        return original_copy(src, dst, *args, **kwargs)

    def replace(src, dst):
        if failure == "publish" and Path(src).name == "replacement" and Path(dst) == root:
            raise OSError("fixture publication failed")
        return original_replace(src, dst)

    with patch.object(sandbox.shutil, "copytree", side_effect=copy), patch.object(sandbox.os, "replace", side_effect=replace):
        with patch.object(sandbox, "_reseal_hashes", side_effect=OSError("fixture seal failed")) if failure == "seal" else patch.object(sandbox, "size_cap_mb", return_value=200):
            updated, errors = sandbox.refresh_copies(source)
    assert not updated and errors
    assert (destination / "SKILL.md").read_bytes() == before
    assert (root / ".misaka-skill-snapshot.json").read_bytes() == manifest
    sandbox.read_manifest(root, verify_files=True)
    assert not root.stat().st_mode & stat.S_IWUSR


def test_refreshed_snapshot_excludes_cache_and_repository_data(sealed_copy):
    """Refresh uses the same ignored directories and size policy as initial copies."""
    source, root, destination = sealed_copy
    for name in (".git", "__pycache__"):
        (source / name).mkdir()
        (source / name / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
    with patch.object(sandbox, "size_cap_mb", return_value=1):
        updated, errors = sandbox.refresh_copies(source)
    assert updated == [str(destination)] and not errors
    assert not (destination / ".git").exists()
    assert not (destination / "__pycache__").exists()
    sandbox.read_manifest(root, verify_files=True)


def test_refresh_restores_directory_write_permission_before_publication(sealed_copy):
    """A sealed staging root must be writable while its parent is being changed."""
    source, root, destination = sealed_copy
    replace = os.replace

    def require_writable_directory(src, dst):
        if Path(src).name == "replacement":
            assert Path(src).stat().st_mode & stat.S_IWUSR
        return replace(src, dst)

    with patch.object(sandbox.os, "replace", side_effect=require_writable_directory):
        updated, errors = sandbox.refresh_copies(source)
    assert updated == [str(destination)] and not errors
    sandbox.read_manifest(root, verify_files=True)
    assert not root.stat().st_mode & stat.S_IWUSR


def test_refresh_lock_timeout_preserves_the_committed_source_edit(sealed_copy, tmp_path, monkeypatch):
    """A busy execution copy reports a warning without undoing the source write."""
    from filelock import FileLock

    source, root, destination = sealed_copy
    before = (destination / "SKILL.md").read_bytes()
    layers.write_skills_config({"skill_write_mode": "allow"})
    monkeypatch.setattr(sandbox, "_REFRESH_LOCK_TIMEOUT", 0.02)
    with FileLock(str(root) + ".refresh.lock"):
        result = manage.manage("patch", "sample", profile_dir=str(tmp_path / "profiles" / "fixture"),
                               workspace=str(tmp_path / "project"), visible_roots=[("external", str(source.parent))],
                               source=str(source / "SKILL.md"), old_string="Original body.", new_string="Revised body.")
    assert result["success"] and result["copy_errors"], result
    assert "Revised body." in (source / "SKILL.md").read_text(encoding="utf-8")
    assert (destination / "SKILL.md").read_bytes() == before
    sandbox.read_manifest(root, verify_files=True)


def test_oversized_refresh_preserves_the_sealed_copy(sealed_copy):
    """An over-limit source cannot replace an otherwise usable snapshot."""
    source, root, destination = sealed_copy
    before = (destination / "SKILL.md").read_bytes()
    (source / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
    with patch.object(sandbox, "size_cap_mb", return_value=1):
        updated, errors = sandbox.refresh_copies(source)
    assert not updated and errors
    assert (destination / "SKILL.md").read_bytes() == before
    sandbox.read_manifest(root, verify_files=True)


def test_copy_discovery_ignores_refresh_and_replacement_images(sealed_copy):
    """Concurrent reviews only see published snapshots, never rollback images."""
    source, root, destination = sealed_copy
    temporary = []
    try:
        for suffix in ("new-fixture", "old-fixture", "refresh-fixture"):
            folder = root.parent / f".{root.name}.{suffix}"
            temporary.append(folder)
            shutil.copytree(root, folder / "replacement")
        assert sandbox.copies_of(source) == [str(destination)]
    finally:
        for folder in temporary:
            sandbox.cleanup(folder)


def test_copy_discovery_stops_at_published_snapshot_roots(sealed_copy):
    """Discovery must not traverse every copied skill after finding its manifest."""
    source, root, destination = sealed_copy
    visited = []
    walk = os.walk

    def record_walk(*args, **kwargs):
        for entry in walk(*args, **kwargs):
            visited.append(Path(entry[0]))
            yield entry

    with patch.object(sandbox.os, "walk", side_effect=record_walk):
        assert sandbox.copies_of(source) == [str(destination)]
    assert root in visited
    assert not any(folder != root and folder.is_relative_to(root) for folder in visited)


def test_pending_reviews_scan_copies_once_and_isolate_diff_failure(sealed_copy):
    """One unreadable diff must not hide another pending change or rescan copies."""
    source, _, destination = sealed_copy
    records = [{"id": "broken", "payload": {"bases": {str(source): "fixture"}}},
               {"id": "valid", "payload": {"bases": {str(source): "fixture"}}}]
    with patch.object(review, "_DIFF_LIMIT", 10), patch("misaka.core.skills.write.list_pending", return_value=records), \
         patch("misaka.core.skills.write.pending_diff", side_effect=[OSError("fixture unreadable"), "visible diff"]), \
         patch.object(sandbox, "_snapshot_roots", wraps=sandbox._snapshot_roots) as scan:
        rows = review.pending_items(with_diff=True)
    assert scan.call_count == 1
    assert rows[0]["error"] == "fixture unreadable" and rows[0]["diff"] == ""
    assert rows[1]["diff"].startswith("visible di") and not rows[1]["error"]
    assert all(row["copies"] == [str(destination)] for row in rows)


def test_project_org_name_does_not_publish_a_different_role_mirror(tmp_path):
    """Only the edited role-local directory may become an organisation proposal."""
    profile = tmp_path / "profiles" / "fixture"
    relative = Path("_org") / "fixture-team" / "sample"
    role = skill(profile / "skills" / relative)
    project = skill(tmp_path / "project" / "skills" / relative)
    changes = [{"action": "patch", "name": relative.as_posix(), "live_dir": str(project)},
               {"action": "patch", "name": "an-alias", "live_dir": str(role)}]
    with patch("misaka.core.skills.vendor.skill_manager_guards._maybe_auto_propose_org_edit", return_value="fixture proposed") as propose:
        assert operations.org_edit_notes(changes, profile, tmp_path / "project") == ["fixture proposed"]
    propose.assert_called_once_with(relative.as_posix(), role.resolve())


@pytest.mark.parametrize("layer", ["project", "shared", "external"])
def test_skill_usage_replay_keeps_same_named_sources_separate(tmp_path, layer):
    """Source edits, replay and deletion must retain the unrelated role counters."""
    profile, workspace = tmp_path / "profiles" / "fixture", tmp_path / "project"
    role = skill(profile / "skills" / "sample")
    source = skill(tmp_path / "other-source" / "sample")
    roots = [(layer, str(source.parent)), ("role", str(role.parent))]
    layers.write_skills_config({"skill_write_mode": "allow"})
    entry, error = index.resolve(roots, "sample", source=str(source / "SKILL.md"))
    assert error is None
    key = operations.usage_key(entry, profile)
    operations.observe(entry, profile, workspace)
    scope = scope_for(profile, workspace)
    with using_scope(scope):
        skill_usage.bump_view("sample")
    result = manage.manage("patch", "sample", profile_dir=str(profile), workspace=str(workspace),
                           visible_roots=roots, source=str(source / "SKILL.md"),
                           old_string="Original body.", new_string="Revised body.")
    assert result["success"], result
    operations.flush_usage(profile, workspace)
    with using_scope(scope):
        usage = skill_usage.load_usage()
    assert usage[key]["view_count"] == 1 and usage[key]["patch_count"] == 1
    assert usage["sample"]["view_count"] == 1 and usage["sample"].get("patch_count", 0) == 0
    deleted = manage.manage("delete", "sample", profile_dir=str(profile), workspace=str(workspace),
                            visible_roots=roots, source=str(source / "SKILL.md"))
    assert deleted["success"], deleted
    with using_scope(scope):
        usage = skill_usage.load_usage()
    assert key not in usage and usage["sample"]["view_count"] == 1


def test_disappearing_directory_does_not_hide_already_discovered_links(tmp_path):
    """A stat race must neither abort a tool guard nor cache an incomplete walk."""
    root, target = tmp_path / "skills", tmp_path / "library"
    root.mkdir()
    target.mkdir()
    original = os.stat

    def raced_stat(path, *args, **kwargs):
        if not kwargs and Path(path) == root:
            raise FileNotFoundError("fixture directory disappeared")
        return original(path, *args, **kwargs)

    with patch.object(layers.os, "walk", return_value=iter([(str(root), ["linked"], [])])), \
         patch.object(layers.os, "stat", side_effect=raced_stat), \
         patch.object(Path, "is_symlink", return_value=True), \
         patch.object(layers.os.path, "realpath", return_value=str(target)):
        assert layers._linked_skill_targets(root) == {str(target)}
    assert str(root) not in layers._LINKED_TARGETS
