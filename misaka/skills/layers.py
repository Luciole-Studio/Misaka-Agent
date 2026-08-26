"""Skill discovery: the layers a role sees and the rules for walking them.

Layers, in precedence order: the project folder's ``skills/``, the role's
``skills/``, the shared ``profiles/skills/``, then the read-only external
directories listed in ``~/.misaka/skills.json`` (hermes ``skills.external_dirs``).
Turning them into an index (one entry per name, the system-prompt section,
lookups) is :mod:`misaka.skills.index`; this module only says where skills live
and which directories count.
"""
import json
import os
from pathlib import Path

from misaka.utils import atomic
from misaka.config import CFG


def home():
    """``~/.misaka`` at call time (tests move HOME)."""
    return os.path.expanduser("~/.misaka")


def config_path():
    return os.path.join(home(), "skills.json")


def load_skills_config():
    """Load the skills configuration, treating invalid files as empty."""
    try:
        with open(config_path(), encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def disabled_skill_names():
    """Return the set of skill names the user has disabled in skills.json."""
    raw = load_skills_config().get("disabled")
    if isinstance(raw, str):
        raw = [raw]
    return {str(x).strip() for x in raw or [] if str(x).strip()}


def write_skills_config(cfg):
    atomic.write_text(config_path(), json.dumps(cfg, ensure_ascii=False, indent=2))


EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".tox", ".nox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
))
# Support directories belong to their parent skill and are not standalone skills.
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


def walk_skill_tree(root):
    """``(directory, files)`` for every directory under a layer root, pruning dependency
    trees and a skill's support directories as it goes (hermes iter_skill_index_files).
    Symlinked directories are followed: a role's skill is often a link into a library."""
    seen = set()
    for here, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(here)
        if real in seen:                    # a link back into the tree: walked already
            dirs[:] = []
            continue
        seen.add(real)
        has_skill = "SKILL.md" in files
        dirs[:] = [d for d in dirs
                   if d not in EXCLUDED_SKILL_DIRS and not (has_skill and d in SKILL_SUPPORT_DIRS)]
        yield here, files


def iter_skill_files(root, filename="SKILL.md"):
    """Every ``filename`` under a layer root, sorted. Categories are the directories in
    between: ``<root>/finance/fmp-data/SKILL.md`` is ``finance/fmp-data``."""
    return iter(sorted(Path(here) / filename for here, files in walk_skill_tree(root)
                       if filename in files))


def external_skills_dirs():
    """The read-only external directories: ``~/.agents/skills`` (the cross-harness global
    location pi mounts, always on when it exists) followed by whatever ``external_dirs``
    in skills.json lists (hermes get_external_skills_dirs): ``~`` and ``$VAR`` expanded, a
    relative path taken from ``~/.misaka``, only directories that exist, duplicates and the
    shared layer dropped. They appear in the index; new skills are always written to the
    role's own layer."""
    raw = load_skills_config().get("external_dirs") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    shared = Path(shared_skills_dir()).resolve()
    seen, out = set(), []
    for entry in ["~/.agents/skills", *raw]:
        text = str(entry).strip()
        if not text:
            continue
        path = Path(os.path.expanduser(os.path.expandvars(text)))
        path = (path if path.is_absolute() else Path(home()) / path).resolve()
        if path == shared or path in seen or not path.is_dir():
            continue
        seen.add(path)
        out.append(str(path))
    return out


def shared_skills_dir():
    return os.path.join(os.path.expanduser(CFG["roles_root"]), "skills")


PERSONAL_LAYERS = frozenset(("role", "shared"))   # the layers the user edits: snapshotted (hermes "local")


def skill_roots(profile_dir, cwd=None):
    """The layer roots a role sees, as ``(layer, root)`` in precedence order: the project
    folder's ``skills/`` (the folder MISAKA runs in is the project; never a directory under
    the profiles tree), the role's ``skills/``, the shared ``profiles/skills``, the external
    directories. Only directories that exist are listed, each once."""
    out, seen = [], set()

    def add(layer, root):
        key = Path(root).resolve()
        if key not in seen and os.path.isdir(root):
            seen.add(key)
            out.append((layer, str(root)))

    roles_root = Path(os.path.expanduser(CFG["roles_root"]))
    try:
        cand = (Path(cwd or os.getcwd()).expanduser() / "skills").resolve()
        if cand.is_dir() and roles_root.resolve() not in (cand, *cand.parents):
            add("project", cand)
    except OSError:
        pass
    if profile_dir:
        add("role", os.path.join(profile_dir, "skills"))
    add("shared", shared_skills_dir())
    for root in external_skills_dirs():
        add("external", root)
    return out


def skills_stack(profile_dir, cwd=None):
    """The skill directories a role sees, project first, one per name (the index decides
    who wins). Used where directories, not entries, are needed: the read-only copies a
    card runs against, and a sub-agent resolving the skills its definition names."""
    from misaka.skills import index
    return [entry["dir"] for entry in index.build(skill_roots(profile_dir, cwd))]
