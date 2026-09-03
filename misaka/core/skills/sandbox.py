"""Detached skill copies for task execution.

Write bits are removed to prevent accidental edits; this is not an OS security boundary,
because the process owner can restore them.
"""
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


def readonly_copies(skill_dirs, dest_root):
    """Build an exact, write-bit-stripped skill stack, then replace the previous copy."""
    dest_root = _validate_destination(dest_root)
    total = sum(_tree_size(d) for d in skill_dirs)
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
        for d in skill_dirs:
            base = os.path.basename(d.rstrip("/")) or "skill"
            name, n = base, 2
            while name.casefold() in used:   # two sources, one basename (macOS folds case): keep both
                name, n = f"{base}-{n}", n + 1
            used.add(name.casefold())

            def _ignore(src, names):
                # Exclude symlinks so copies cannot escape the size check or sandbox.
                return set(skip(src, names)) | {
                    item for item in names if os.path.islink(os.path.join(src, item))}

            shutil.copytree(d, os.path.join(stage, name), symlinks=False, ignore=_ignore)
            copies.append(os.path.join(dest_root, name))
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
