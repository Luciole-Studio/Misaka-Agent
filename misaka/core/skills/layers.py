"""Skill discovery: the layers a role sees and the rules for walking them.

Layers, in precedence order: the project folder's ``skills/``, the role's
``skills/``, the shared ``profiles/skills/``, then the read-only external
directories listed in ``~/.misaka/core/skills.json`` (hermes ``skills.external_dirs``).
Turning them into an index (one entry per name, the system-prompt section,
lookups) is :mod:`misaka.core.skills.index`; this module only says where skills live
and which directories count, plus the project-tier quarantine chokepoint.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
from stat import S_ISLNK

from misaka.config import CFG
from misaka.utils import atomic

logger = logging.getLogger(__name__)


def home():
    """``~/.misaka`` at call time (tests move HOME)."""
    return os.path.expanduser("~/.misaka")


def config_path():
    return os.path.join(home(), "skills.json")


class SkillsConfigError(RuntimeError):
    """skills.json is on disk but cannot be parsed, so it must not be overwritten."""


def read_skills_config():
    """Read skills.json as three states: ``(cfg, reason)``.

    Missing file -> ``({}, None)``: nothing to lose, safe to write over. So is a
    file that holds no settings — zero bytes, whitespace only, a lone BOM, or a
    literal ``null``; refusing to write those would wedge every setting change
    behind a file with nothing in it to protect.
    Parsed -> ``(cfg, None)``. Present but unreadable, malformed, or not a JSON
    object -> ``({}, reason)``: read-only callers fall back to the empty mapping,
    while a read-modify-write caller must refuse to overwrite (see
    write_skills_config). ``utf-8-sig`` matches write.py's ``_config``: a BOM
    must not read as content.
    """
    path = config_path()
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as error:
        return {}, f"{path}: {error}"
    if not text.strip():
        return {}, None
    try:
        raw = json.loads(text)
    except ValueError as error:
        return {}, f"{path}: {error}"
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, f"{path}: expected a JSON object, found {type(raw).__name__}"
    return raw, None


def load_skills_config():
    """Load the skills configuration, treating invalid files as empty."""
    cfg, reason = read_skills_config()
    if reason:
        logger.warning("Ignoring unreadable skills configuration: %s", reason)
    return cfg


def disabled_skill_names():
    """Return the set of skill names the user has disabled in skills.json."""
    raw = load_skills_config().get("disabled")
    if isinstance(raw, str):
        raw = [raw]
    return {str(x).strip() for x in raw or [] if str(x).strip()}


def write_skills_config(cfg):
    """Replace skills.json, refusing when the file on disk exists and does not parse.

    Every caller reads the config, changes one key, and writes the whole file back, so
    overwriting an unparseable file would silently destroy the user's ``disabled`` list
    and ``external_dirs``.
    """
    reason = read_skills_config()[1]
    if reason:
        raise SkillsConfigError(
            f"Refusing to overwrite the skills configuration: {reason}. "
            "Fix or remove the file, then try again."
        )
    atomic.write_text(config_path(), json.dumps(cfg, ensure_ascii=False, indent=2))


EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".tox", ".nox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
))
# Support directories belong to their parent skill and are not standalone skills.
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


def _walk_skill_tree(root, *, prune_support, prune_excluded=True):
    """Walk a skill tree once, following links without revisiting a real directory."""
    seen = set()
    for here, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(here)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        has_skill = "SKILL.md" in files
        dirs[:] = sorted(
            d for d in dirs
            if (not prune_excluded or d not in EXCLUDED_SKILL_DIRS)
            and not (prune_support and has_skill and d in SKILL_SUPPORT_DIRS)
        )
        yield here, dirs, files


def walk_skill_tree(root):
    """``(directory, files)`` for every directory under a layer root, pruning dependency
    trees and a skill's support directories as it goes (hermes iter_skill_index_files).
    Symlinked directories are followed: a role's skill is often a link into a library."""
    for here, _dirs, files in _walk_skill_tree(root, prune_support=True):
        yield here, files


def iter_skill_files(root, filename="SKILL.md"):
    """Every ``filename`` under a layer root, sorted. Categories are the directories in
    between: ``<root>/finance/fmp-data/SKILL.md`` is ``finance/fmp-data``."""
    return iter(sorted(Path(here) / filename for here, files in walk_skill_tree(root)
                       if filename in files))


_PROJECT_SCAN_SOURCE = "project-local"


def is_quarantined_project_skill(skill_md):
    """Fail closed when a project skill's full directory cannot scan or is dangerous."""
    skill_dir = Path(skill_md).parent
    try:
        from misaka.core.skills import guard

        # A project skill comes out of a repository the user merely cd'd into, so
        # the party that writes `.skillignore` is the party being scanned. On this
        # path the ignore file is not consulted at all: `honor_ignore=False`.
        #
        # That costs a false positive — a legitimate skill vendoring a `.dylib`
        # quarantines on `binary_file`, and its author cannot ignore it away. The
        # alternative costs a silent bypass: honoring the file lets a one-line
        # `.skillignore` of `*` hide `scripts/*.sh` from the content scan, and a
        # payload there loads with no warning at all. Fail-closed on untrusted
        # input is the right side of that trade: quarantine is an inconvenience
        # the user can override deliberately, a bypass is not something they can
        # see. `skill_manage` keeps `honor_ignore=True` for skills the user owns.
        result = guard.scan_skill(skill_dir, source=_PROJECT_SCAN_SOURCE, honor_ignore=False)
        verdict, summary = result.verdict, result.summary
    except Exception:
        logger.warning("Project skill scan failed; quarantining: %s", skill_dir, exc_info=True)
        return True
    if verdict in ("safe", "caution"):
        # `skill_inline_shell` is medium, so on its own it leaves the verdict at safe, and
        # that is right for a project skill too: `preprocess_skill_content` expands `!`cmd``
        # only in the user's own layers, so here the snippet is inert text whatever
        # skills.json says, and quarantining inert text would only cost the user a skill.
        return False
    logger.warning("Project skill quarantined: %s - %s", skill_dir, summary)
    return True


def iter_project_skill_files(root):
    """Yield project SKILL.md files through the single quarantine chokepoint."""
    return (path for path in iter_skill_files(root) if not is_quarantined_project_skill(path))


def project_skill_tree_fingerprint(root):
    """Metadata fingerprint of every project skill bundle, including support files.

    This deliberately has no path-only cache: live edits must invalidate the index,
    while the scanner reads file contents only after the resulting cache miss.

    Both prunings are off on purpose, and it is not free: this runs on every turn,
    before ``build`` can consult its cache, so a project ``skills/`` holding a git
    checkout or a ``node_modules`` is stat-ed in full each time. Turning
    ``prune_excluded`` back on was tried and reverted (audit skills-utils-07): the
    fingerprint's job is to invalidate a *quarantine verdict*, and the scanner that
    produces that verdict walks the bundle with ``rglob("*")``, which prunes nothing.
    A payload dropped into ``node_modules/`` is scanned; if the fingerprint could not
    see it, the verdict would go stale exactly where it matters. The two scopes have
    to be the same set, and narrowing them is a security decision about where a
    payload may hide, not a cache tweak.
    """
    records = []
    for here, dirs, files in _walk_skill_tree(
        root, prune_support=False, prune_excluded=False,
    ):
        for name in (*dirs, *sorted(files)):
            path = Path(here) / name
            rel = Path(os.path.relpath(path, root)).as_posix()
            try:
                info = path.lstat()
                target = os.readlink(path) if S_ISLNK(info.st_mode) else ""
                records.append(
                    (rel, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_mode, target)
                )
            except OSError as error:
                records.append((rel, type(error).__name__, error.errno))
    payload = json.dumps(sorted(records), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


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
    from misaka.core.skills import index
    return [entry["dir"] for entry in index.build(skill_roots(profile_dir, cwd))]
