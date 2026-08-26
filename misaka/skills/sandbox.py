"""Filesystem restrictions for skill-scoped task execution."""
import os
import shutil
import stat

SIZE_CAP_MB = int(os.environ.get("MISAKA_SKILL_COPY_CAP_MB", "200"))


def _tree_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _strip_write(path):
    for root, dirs, files in os.walk(path):
        for name in files + dirs:
            p = os.path.join(root, name)
            try:
                os.chmod(p, os.stat(p).st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
            except OSError:
                pass


def readonly_copies(skill_dirs, dest_root):
    """Copy skill directories into a read-only sandbox."""
    if not skill_dirs:
        return []
    total = sum(_tree_size(d) for d in skill_dirs)
    if total > SIZE_CAP_MB * 1024 * 1024:
        raise RuntimeError(
            f"Skill copies total {total / 1_000_000:.0f} MB, exceeding the {SIZE_CAP_MB} MB limit. "
            "Reduce the skill set or raise MISAKA_SKILL_COPY_CAP_MB."
        )
    os.makedirs(dest_root, exist_ok=True)
    copies = []
    used = set()
    for d in skill_dirs:
        base = os.path.basename(d.rstrip("/")) or "skill"
        name, n = base, 2
        while name.casefold() in used:   # two sources, one basename (macOS folds case): keep both
            name, n = f"{base}-{n}", n + 1
        used.add(name.casefold())
        dst = os.path.join(dest_root, name)
        if os.path.exists(dst):
            cleanup(dst)
        def _ignore(src, names, _pat=shutil.ignore_patterns(".git", "__pycache__")):
            # Exclude symlinks so copies cannot escape the size check or sandbox.
            return set(_pat(src, names)) | {
                n for n in names if os.path.islink(os.path.join(src, n))}
        shutil.copytree(d, dst, symlinks=False, ignore=_ignore)
        _strip_write(dst)
        copies.append(dst)
    # A reused sandbox root must hold exactly this stack: copies of skills that were removed or
    # disabled since the last run would otherwise stay visible to the card.
    for stale in os.listdir(dest_root):
        if stale.casefold() not in used:
            cleanup(os.path.join(dest_root, stale))
    return copies


def cleanup(dest_root):
    """Restore write permission and remove a sandbox copy."""
    if not os.path.isdir(dest_root):
        return
    for root, dirs, files in os.walk(dest_root):
        for name in files + dirs:
            p = os.path.join(root, name)
            try:
                os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
            except OSError:
                pass
    shutil.rmtree(dest_root, ignore_errors=True)
