"""Skill discovery: the layers a role sees and the rules for walking them.

Layers, in precedence order: the project folder's ``skills/``, the role's
``skills/``, the home's shared ``skills/``, then the read-only external
directories listed under ``skills.external_dirs`` in settings.json (hermes).
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

from misaka.config import CFG, home

logger = logging.getLogger(__name__)


def config_path():
    """Where the skills settings live: the ``skills`` section of the global settings.json."""
    return str(home.path("settings"))


class SkillsConfigError(RuntimeError):
    """settings.json is on disk but cannot be parsed, so it must not be overwritten."""


def _settings():
    from misaka.core.settings_manager import SettingsManager

    return SettingsManager.forRole(None)


def read_skills_config():
    """The ``skills`` section as ``(cfg, reason)``: ``reason`` names an unreadable settings.json,
    in which case read-only callers get ``{}`` and a read-modify-write caller must refuse."""
    manager = _settings()
    if manager.globalSettingsLoadError is not None:
        return {}, f"{config_path()}: {manager.globalSettingsLoadError}"
    return manager.getScopedSection("global", "skills"), None


def load_skills_config():
    """Load the skills configuration, treating an unreadable settings.json as empty."""
    cfg, reason = read_skills_config()
    if reason:
        logger.warning("Ignoring unreadable skills configuration: %s", reason)
    return cfg


def write_skills_config(cfg):
    """Replace the ``skills`` section, refusing when settings.json exists and does not parse:
    overwriting it would silently destroy every other setting in the file."""
    manager = _settings()
    if manager.globalSettingsLoadError is not None:
        raise SkillsConfigError(
            f"Refusing to overwrite the settings: {config_path()}: {manager.globalSettingsLoadError}. "
            "Fix or remove the file, then try again."
        )

    def replace(section):
        section.clear()
        section.update(cfg)

    manager.updateSection("skills", replace)


def disabled_skill_names(platform=None):
    """Hermes scalar/list/serialized-list normalization, scoped to this surface.

    MISAKA has no unconditionally advertised ``hermes-agent`` manual: no name is
    exempt from the user's disable list here.
    """
    from .vendor.metadata import _normalize_string_set
    cfg = load_skills_config()
    disabled = _normalize_string_set(cfg.get("disabled"))
    platforms = cfg.get("platform_disabled")
    if platform and isinstance(platforms, dict):
        disabled |= _normalize_string_set(platforms.get(platform))
    return disabled



EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".curator_backups", ".misaka-skill-transactions", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".tox", ".nox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
))
# Support directories belong to their parent skill and are not standalone skills.
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


def _walk_skill_tree(root, *, prune_support, prune_excluded=True):
    """Walk a skill tree once, following links without revisiting a real directory."""
    from .vendor.org_visibility import read_active_org_id
    active_org = read_active_org_id(Path(root))
    seen = set()
    for here, dirs, files in os.walk(root, followlinks=True):
        if Path(here) == Path(root) and active_org is None:
            dirs[:] = [d for d in dirs if d != "_org"]
        elif Path(here) == Path(root) / "_org":
            dirs[:] = [d for d in dirs if d == active_org]
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
        # the skills settings say, and quarantining inert text would only cost the user a skill.
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
    in the ``skills`` section of settings.json lists (hermes get_external_skills_dirs): ``~`` and ``$VAR`` expanded, a
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
        path = (path if path.is_absolute() else home.home() / path).resolve()
        if path == shared or path in seen or not path.is_dir():
            continue
        seen.add(path)
        out.append(str(path))
    return out


def shared_skills_dir():
    return str(home.path("shared_skills"))


PERSONAL_LAYERS = frozenset(("role", "shared"))   # the layers the user edits: snapshotted (hermes "local")


def skill_roots(profile_dir, cwd=None, *, extension_paths=()):
    """The layer roots a role sees, as ``(layer, root)`` in precedence order: the project
    folder's ``skills/`` (the folder MISAKA runs in is the project; never a directory under
    the profiles tree), the role's ``skills/``, the home's shared ``skills/``, the external
    directories. Only directories that exist are listed, each once."""
    out, seen = [], set()

    def add(layer, root):
        key = (layer if layer.startswith("extension:") else "", Path(root).resolve())
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
    for layer, root in extension_roots(extension_paths):
        add(layer, root)
    return out


def protected_skill_roots(profile_dir, cwd=None, *, extension_paths=(), linked=True):
    """Workflow protection includes absent and other-role Skill and bundle roots.

    With ``linked`` the real locations of symlinked skills are included too: the file tools
    resolve their target and would otherwise write a live skill through its link. The shell
    guard passes ``linked=False``: it can only refuse a command that *names* a root, reads
    included, and a skill kept in a library is used from there (its runtime, its documented
    entry) far more often than written."""
    roles = Path(os.path.expanduser(CFG["roles_root"]))
    roots = [Path(shared_skills_dir())]
    if profile_dir:
        roots.append(Path(profile_dir) / "skills")
    # Profiles may be nested (Sisters); the nearest existing role ancestors are
    # covered by their expected skills root, regardless of whether it exists yet.
    if roles.is_dir():
        for here, dirs, files in os.walk(roles):
            dirs[:] = [d for d in dirs if d not in {"skills", "skill-bundles", ".git", "sessions", "cache"}]
            roots.extend((Path(here) / "skills", Path(here) / "skill-bundles"))
    workspace = Path(cwd or os.getcwd()).expanduser().absolute()
    if not (workspace / "skills").resolve().is_relative_to(roles.resolve()):
        roots.append(workspace / "skills")
    roots.extend(Path(root) for _, root in skill_roots(profile_dir, cwd, extension_paths=extension_paths))
    protected = {str(p) for root in roots for p in (root.absolute(), root.resolve())}
    # A skill kept as a symlink (a library elsewhere, linked into a root) really lives where the
    # link points; the file tools compare real paths, so that place must be protected too.
    if linked:
        for root in roots:
            protected.update(_linked_skill_targets(root))
    return protected


_LINKED_TARGETS = {}     # root -> (directory mtimes seen, real locations): the guard asks on every tool call


def _linked_skill_targets(root):
    """Real locations of the symlinked directories inside a skills root (any nesting level;
    the walk does not enter the links themselves). Resolving a few hundred links costs more
    than a tool call should, so the answer is kept until a directory in the root changes."""
    if not root.is_dir():
        return set()
    stamps, links = [], []
    for here, dirs, _files in os.walk(root):
        stamps.append((here, os.stat(here).st_mtime_ns))
        links.extend(Path(here) / name for name in dirs if (Path(here) / name).is_symlink())
    key = str(root)
    cached = _LINKED_TARGETS.get(key)
    if cached is not None and cached[0] == stamps:
        return cached[1]
    targets = {os.path.realpath(link) for link in links}
    _LINKED_TARGETS[key] = (stamps, targets)
    return targets


def extension_resources(loader):
    """Keep provider metadata when available; old resource loaders remain usable."""
    getter = getattr(loader, "getExtensionSkillResources", None)
    if getter is not None:
        return getter()
    getter = getattr(loader, "getExtensionSkillPaths", None)
    return getter() if getter is not None else ()


def extension_roots(resources):
    """Namespaced layer labels keep the serializable (layer, path) root protocol.

    Provider namespaces use the loader's source label, never SKILL frontmatter.
    Duplicate provider names stay ambiguous; callers can choose an exact source.
    """
    from .vendor.commands import slugify_skill_name
    for resource in resources:
        if isinstance(resource, str):
            yield "extension", resource
            continue
        metadata = resource.get("metadata", {})
        if metadata.get("enabled") is False or metadata.get("available") is False or metadata.get("state") in ("disabled", "unavailable", "withdrawn"):
            continue
        label = metadata.get("source", "").removeprefix("extension:")
        if label.startswith("inline:"):
            namespace = f"<{label}>"
        else:
            if label.endswith((".py", ".js", ".ts")):
                label = label.rsplit(".", 1)[0]
            namespace = slugify_skill_name(label)
        yield (f"extension:{namespace}" if namespace else "extension"), resource["path"]


def parse_skill_name(name):
    """Keep the loader's inline source label intact when splitting a qualified name."""
    from .vendor.metadata import parse_qualified_name
    if name.startswith("<inline:"):
        namespace, separator, bare = name.partition(">:")
        if separator:
            return namespace + ">", bare
    return parse_qualified_name(name)
