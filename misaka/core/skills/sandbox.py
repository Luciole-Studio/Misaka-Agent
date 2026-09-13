"""Detached skill copies for task execution.

Write bits are removed to prevent accidental edits; this is not an OS security boundary,
because the process owner can restore them.
"""
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

SIZE_CAP_MB = int(os.environ.get("MISAKA_SKILL_COPY_CAP_MB", "200"))


def _tree_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            target = os.path.join(root, f)
            if os.path.islink(target):
                continue
            try:
                total += os.path.getsize(target)
            except OSError:
                pass
    return total


def _strip_write(path):
    entries = [path]
    for root, dirs, files in os.walk(path):
        entries.extend(os.path.join(root, name) for name in files + dirs)
    for p in entries:                                  # the copy's own directory included: no new files either
        if os.path.islink(p):
            continue
        try:
            os.chmod(p, os.stat(p).st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
        except OSError:
            pass


def _path_chain(path):
    target = Path(os.path.abspath(os.fspath(path)))
    return reversed((target, *target.parents))


def _ensure_real_directory(path, *, create=True):
    """Create a directory path one component at a time, rejecting every symlink."""
    for component in _path_chain(path):
        try:
            mode = os.lstat(component).st_mode
        except FileNotFoundError:
            if not create:
                raise ValueError(f"Skill sandbox parent does not exist: {component}") from None
            os.mkdir(component, 0o700)
            mode = os.lstat(component).st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Skill sandbox path contains a symlink: {component}")
        if not stat.S_ISDIR(mode):
            raise ValueError(f"Skill sandbox parent is not a directory: {component}")


def _validate_destination(path):
    """Reject a destination or existing ancestor that could redirect writes."""
    target = Path(os.path.abspath(os.fspath(path)))
    if target == target.parent:
        raise ValueError("The filesystem root cannot be a skill sandbox.")
    _ensure_real_directory(target.parent)
    for component in _path_chain(target):
        if not os.path.lexists(component):
            continue
        mode = os.lstat(component).st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Skill sandbox path contains a symlink: {component}")
    if os.path.lexists(target) and not stat.S_ISDIR(os.lstat(target).st_mode):
        raise ValueError(f"Skill sandbox destination is not a directory: {target}")
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if directory_flag:
        fd = os.open(target.parent, os.O_RDONLY | directory_flag | getattr(os, "O_NOFOLLOW", 0))
        os.close(fd)
    return str(target)


def readonly_copies(skill_dirs, dest_root, *, bundle_records=None, category_descriptions=None):
    """Build an exact, write-bit-stripped skill stack, then replace the previous copy."""
    dest_root = _validate_destination(dest_root)
    entries = []
    for item in skill_dirs:
        if isinstance(item, dict):
            entries.append(dict(item))
        else:
            directory = str(Path(item))
            from .index import (
                _documents,
                list_skill_description,
                truncate_skill_description,
            )
            (prompt, _), (fm, body) = _documents(Path(directory) / "SKILL.md")
            name = str(fm.get("name") or Path(directory).name)
            entries.append({"dir": directory, "path": str(Path(directory) / "SKILL.md"),
                            "name": name, "runtime_name": name, "rel": Path(directory).name,
                            "frontmatter": fm, "prompt_frontmatter": prompt, "category": "general",
                            "description": truncate_skill_description(prompt.get("description")),
                            "list_description": list_skill_description(fm, body)})
    total = sum(_entry_size(e) for e in entries)
    if total > SIZE_CAP_MB * 1024 * 1024:
        raise RuntimeError(
            f"Skill copies total {total / 1_000_000:.0f} MB, exceeding the {SIZE_CAP_MB} MB limit. "
            "Reduce the skill set or raise MISAKA_SKILL_COPY_CAP_MB."
        )
    parent = os.path.dirname(dest_root)
    stage = tempfile.mkdtemp(prefix=f".{os.path.basename(dest_root)}.new-", dir=parent)
    backup = None
    copies = []
    used = set()
    skip = shutil.ignore_patterns(".git", "__pycache__")
    try:
        manifest_entries = []
        for entry in entries:
            d = entry["dir"]
            base = Path(entry["path"]).stem if entry.get("legacy") else os.path.basename(d.rstrip("/")) or "skill"
            name, n = base, 2
            while name.casefold() in used:   # two sources, one basename (macOS folds case): keep both
                name, n = f"{base}-{n}", n + 1
            used.add(name.casefold())

            def _ignore(src, names):
                # Exclude symlinks so copies cannot escape the size check or sandbox.
                return set(skip(src, names)) | {
                    item for item in names if os.path.islink(os.path.join(src, item))}

            if entry.get("legacy"):
                if Path(entry["path"]).is_symlink():
                    raise ValueError("Legacy Skill document cannot be a symlink.")
                Path(stage, name).mkdir()
                shutil.copy2(entry["path"], Path(stage, name, Path(entry["path"]).name))
            else:
                shutil.copytree(d, os.path.join(stage, name), symlinks=False, ignore=_ignore)
            copies.append(os.path.join(dest_root, name))
            copied_file = str(Path(entry["path"]).relative_to(d))
            if not Path(stage, name, copied_file).is_file():
                raise ValueError(f"Skill document absent from detached copy: {entry['path']}")
            manifest_entries.append({**{k: v for k, v in entry.items() if k not in ("frontmatter", "prompt_frontmatter")},
                "identity_path": entry.get("identity_path", os.path.realpath(entry["path"])),
                "origin_path": entry.get("origin_path", entry["path"]),
                "origin_dir": entry.get("origin_dir", entry["dir"]),
                "origin_layer": entry.get("origin_layer", entry.get("layer", "sandbox")),
                "dir": os.path.join(dest_root, name),
                "path": os.path.join(dest_root, name, copied_file),
                "copied_file": copied_file, "copied_dir": name})
        manifest = {"version": 1, "state": "prepared", "entries": manifest_entries,
                    "bundles": list(bundle_records or ()), "categories": dict(category_descriptions or {})}
        manifest["digest"] = _manifest_digest(manifest)
        (Path(stage) / ".misaka-skill-snapshot.json").write_text(json.dumps(manifest, ensure_ascii=False))
        _strip_write(stage)

        # Move the old root aside as one directory; never walk stale names through dest_root.
        _validate_destination(dest_root)
        if os.path.lexists(dest_root):
            backup = tempfile.mkdtemp(prefix=f".{os.path.basename(dest_root)}.old-", dir=parent)
            os.rmdir(backup)
            os.replace(dest_root, backup)
        os.replace(stage, dest_root)
        stage = None
    except BaseException:
        if backup and not os.path.lexists(dest_root):
            os.replace(backup, dest_root)
            backup = None
        raise
    finally:
        if stage:
            cleanup(stage)
    if backup:
        cleanup(backup)
    return copies


def cleanup(dest_root):
    """Restore write permission and remove a sandbox copy -- or whatever else sits at its place."""
    dest_root = os.path.abspath(os.fspath(dest_root))
    try:
        _ensure_real_directory(os.path.dirname(dest_root), create=False)
    except (OSError, ValueError):
        return
    if os.path.islink(dest_root) or os.path.isfile(dest_root):
        os.unlink(dest_root)
        return
    if not os.path.isdir(dest_root):
        return
    entries = [dest_root]
    for root, dirs, files in os.walk(dest_root):
        entries.extend(os.path.join(root, name) for name in files + dirs)
    for p in entries:
        if os.path.islink(p):
            continue
        try:
            os.chmod(p, os.lstat(p).st_mode | stat.S_IWUSR)
        except OSError:
            pass
    shutil.rmtree(dest_root, ignore_errors=True)


def _manifest_digest(manifest):
    payload = {k: v for k, v in manifest.items() if k != "digest"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def read_manifest(root, *, verify_files=False):
    path = Path(root) / ".misaka-skill-snapshot.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    if (not isinstance(raw, dict) or raw.get("state") not in ("prepared", "sealed")
            or not isinstance(raw.get("entries"), list)
            or raw.get("version") != 1 or raw.get("digest") != _manifest_digest(raw)):
        raise ValueError(f"Invalid Skill snapshot manifest: {path}")
    if verify_files and raw["state"] == "sealed" and _file_digests(root) != raw.get("files"):
        raise ValueError(f"Skill snapshot content changed after sealing: {root}")
    for entry in raw["entries"]:
        copied = entry["copied_dir"]
        if Path(copied).name != copied or copied in (".", ".."):
            raise ValueError("Invalid copied Skill directory.")
        from .manage import lookup_path_error
        copied_file = entry.get("copied_file", "SKILL.md")
        if lookup_path_error(copied_file) or "\\" in copied_file:
            raise ValueError("Invalid copied Skill document path.")
        entry["dir"] = str(Path(root) / copied)
        entry["path"] = str(Path(root) / copied / copied_file)
    return raw


def _file_digests(root):
    """Validate snapshot bytes at the handoff barrier, not on every prompt read."""
    root = Path(root)
    _ensure_real_directory(root, create=False)
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Skill snapshot contains a symlink: {path}")
        if path.is_file() and path != root / ".misaka-skill-snapshot.json":
            with path.open("rb") as stream:
                out[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
    return out


def seal(root, extension_roots=()):
    """The root card, after extension discovery, seals its prepared collection once.

    Base bytes come from PREPARED copies, never the live library. Descendants
    read the manifest and have no resealing path.
    """
    from . import index
    manifest = read_manifest(root)
    if manifest is not None and manifest["state"] == "sealed":
        return manifest
    base = manifest["entries"] if manifest is not None else index.all_entries([("sandbox", str(root))])
    extra = index.all_entries(extension_roots)
    readonly_copies([*base, *extra], root, bundle_records=(manifest or {}).get("bundles"),
                    category_descriptions={**index.categories(extension_roots), **(manifest or {}).get("categories", {})})
    manifest = read_manifest(root)
    manifest["state"] = "sealed"
    # The manifest binds content too, not just where the files were copied.
    manifest["files"] = _file_digests(root)
    manifest["digest"] = _manifest_digest(manifest)
    path = Path(root) / ".misaka-skill-snapshot.json"
    os.chmod(root, os.stat(root).st_mode | stat.S_IWUSR)
    from misaka.utils import atomic
    try:
        atomic.write_text(path, json.dumps(manifest, ensure_ascii=False), mode=0o444)
    finally:
        _strip_write(root)
    index.invalidate()
    return manifest


def execution_entry(entry, root):
    """Reuse a content-addressed execution copy owned by a session or task.

    Caller serializes access and owns root's lifetime. Task roots intentionally
    follow transcript retention so resumed children can still use advertised paths.
    """
    from .layers import project_skill_tree_fingerprint
    if entry.get("legacy"):
        with Path(entry["path"]).open("rb") as stream:
            fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    else:
        fingerprint = project_skill_tree_fingerprint(entry["dir"])
    key = hashlib.sha256((entry["path"] + fingerprint).encode()).hexdigest()
    root = Path(root).absolute()
    destination = root / key
    if not destination.exists():
        if _tree_size(root) + _entry_size(entry) > SIZE_CAP_MB * 1024 * 1024:
            raise ValueError(f"Skill execution copies exceed the {SIZE_CAP_MB} MB owner limit.")
        readonly_copies([entry], destination)
    manifest = read_manifest(destination)
    if not manifest or len(manifest["entries"]) != 1:
        raise ValueError(f"Invalid Skill execution copy: {destination}")
    return dict(manifest["entries"][0])


def _entry_size(entry):
    return Path(entry["path"]).stat().st_size if entry.get("legacy") else _tree_size(entry["dir"])


def snapshot_stack(profile_dir, workspace, destination):
    """Capture one scope's documents, category metadata and bundle aliases together."""
    from . import bundles, index
    from .layers import skill_roots
    roots = skill_roots(profile_dir, workspace)
    return readonly_copies(index.all_entries(roots), destination,
        bundle_records=bundles.scan(bundles.bundle_roots(profile_dir, workspace)).values(),
        category_descriptions=index.categories(roots))
