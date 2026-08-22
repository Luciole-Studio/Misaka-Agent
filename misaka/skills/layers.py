"""Layered skill discovery with project trust and security scanning."""
import json
import logging
import os
from pathlib import Path

from misaka.config import CFG

logger = logging.getLogger(__name__)

PROJECT_SKILLS_SUBDIRS = (os.path.join(".misaka", "skills"),
                          os.path.join(".agents", "skills"))
_PROJECT_ROOT_MAX_DEPTH = 64
_PROJECT_SCAN_SOURCE = "project-local"
_QUARANTINE_CACHE = {}


def config_path():
    return os.path.expanduser("~/.misaka/skills.json")


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


def _write_skills_config(cfg):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def find_project_root(start=None):
    """Return the nearest Git root, excluding the user's home directory."""
    try:
        cur = Path(start if start is not None else Path.cwd()).resolve()
    except OSError:
        return None
    home = Path.home().resolve()
    for _ in range(_PROJECT_ROOT_MAX_DEPTH):
        try:
            if (cur / ".git").exists():
                return None if cur == home else cur
        except OSError:
            return None
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def trusted_project_dirs():
    raw = load_skills_config().get("trusted_project_dirs")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    out = set()
    for entry in raw:
        entry = str(entry).strip()
        if entry:
            try:
                out.add(Path(os.path.expanduser(os.path.expandvars(entry))).resolve())
            except OSError:
                continue
    return out


def is_project_root_trusted(root):
    try:
        return Path(root).resolve() in trusted_project_dirs()
    except OSError:
        return False


def trust_project_root(root):
    """Add a project root to the trusted skill-source list."""
    resolved = Path(root).resolve()
    if not resolved.is_dir():
        return False, f"Directory does not exist: {resolved}"
    cfg = load_skills_config()
    dirs = cfg.get("trusted_project_dirs")
    dirs = [dirs] if isinstance(dirs, str) else (dirs if isinstance(dirs, list) else [])
    if str(resolved) in {str(Path(os.path.expanduser(d)).resolve())
                         for d in dirs if str(d).strip()}:
        return True, f"Already trusted: {resolved}"
    dirs.append(str(resolved))
    cfg["trusted_project_dirs"] = dirs
    _write_skills_config(cfg)
    return True, (
        f"Trusted: {resolved}. Skills under .misaka/skills and .agents/skills "
        "will now be discovered."
    )


def _candidate_project_skills_dirs(root):
    """Return project skill directories that do not overlap role storage."""
    roles_root = Path(os.path.expanduser(CFG["roles_root"])).resolve()
    dirs = []
    for sub in PROJECT_SKILLS_SUBDIRS:
        cand = Path(root) / sub
        try:
            resolved = cand.resolve()
            if cand.is_dir() and roles_root not in (resolved, *resolved.parents):
                dirs.append(resolved)
        except OSError:
            continue
    return dirs


def is_quarantined_project_skill(skill_md):
    """Quarantine dangerous project skills and fail closed when scanning fails."""
    skill_dir = Path(skill_md).parent
    try:
        key = str(skill_dir.resolve())
    except OSError:
        key = str(skill_dir)
    cached = _QUARANTINE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from misaka.skills.guard import scan_skill_cached
        result, _prov = scan_skill_cached(
            skill_dir, source=_PROJECT_SCAN_SOURCE,
            cache_dir=Path(os.path.expanduser("~/.misaka/cache/project_skill_scans")))
        quarantined = result.verdict == "dangerous"
        if quarantined:
            logger.warning('Quarantined dangerous project skill: %s — %s',
                           skill_dir, result.summary)
    except Exception:  # noqa: BLE001 - unscanned project content must not load
        logger.warning('Project skill scan failed; quarantining: %s',
                       skill_dir, exc_info=True)
        quarantined = True
    _QUARANTINE_CACHE[key] = quarantined
    return quarantined


_USER_SCAN_CACHE = {}
_USER_SCAN_SOURCE = "user-local"


def warn_if_risky_user_skill(skill_dir):
    """Scan a user-owned skill and log a warning if it looks dangerous; it still loads."""
    key = str(skill_dir)
    cached = _USER_SCAN_CACHE.get(key)
    if cached is not None:
        return cached
    risky = False
    try:
        from misaka.skills.guard import scan_skill_cached
        result, _prov = scan_skill_cached(
            Path(skill_dir), source=_USER_SCAN_SOURCE,
            cache_dir=Path(os.path.expanduser("~/.misaka/cache/user_skill_scans")))
        risky = result.verdict == "dangerous"
        if risky:
            logger.warning("Risk detected in user skill; loading because it is user-owned: %s — %s",
                           skill_dir, result.summary)
    except Exception:  # noqa: BLE001 - user-owned content remains fail-open
        logger.debug('User skill scan failed; loading: %s', skill_dir, exc_info=True)
    _USER_SCAN_CACHE[key] = risky
    return risky


EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".tox", ".nox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
))
# Support directories belong to their parent skill and are not standalone skills.
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


def is_skill_support_path(path):
    """Return whether a path is inside a skill support directory."""
    parts = Path(path).parts
    for idx, part in enumerate(parts[:-1]):
        if part not in SKILL_SUPPORT_DIRS or idx == 0:
            continue
        if (Path(*parts[:idx]) / "SKILL.md").exists():
            return True
    return False


def is_excluded_skill_path(path):
    """Return whether skill discovery should skip this path."""
    return any(part in EXCLUDED_SKILL_DIRS for part in Path(path).parts) \
        or is_skill_support_path(path)


def iter_project_skill_files(project_dir):
    """Yield safe SKILL.md files under a trusted project directory."""
    for skill_md in sorted(Path(project_dir).rglob("SKILL.md")):
        if is_excluded_skill_path(skill_md):
            continue
        if not is_quarantined_project_skill(skill_md):
            yield skill_md


def get_project_skills_dirs(cwd=None):
    """Return trusted project skill roots available from the current directory."""
    root = find_project_root(cwd)
    if root is None or not is_project_root_trusted(root):
        return []
    return _candidate_project_skills_dirs(root)


def get_untrusted_project_skills_root(cwd=None):
    """Return an untrusted project root and its skill count, when present."""
    root = find_project_root(cwd)
    if root is None or is_project_root_trusted(root):
        return None
    count = sum(1 for d in _candidate_project_skills_dirs(root)
                for _ in Path(d).rglob("SKILL.md"))
    return (root, count) if count else None


def shared_skills_dir():
    return os.path.join(os.path.expanduser(CFG["roles_root"]), "skills")


def skills_stack(profile_dir, cwd=None):
    """Build the ordered project, role, and shared skill stack."""
    from misaka.config import profiles
    out, seen = [], set()

    def _add(skill_dir):
        key = os.path.realpath(str(skill_dir))
        if key not in seen:
            seen.add(key)
            out.append(str(skill_dir))

    disabled = disabled_skill_names()

    def _add_user_skill(skill_dir):
        """Scan user-owned skills for warnings, then load unless disabled."""
        if Path(skill_dir).name in disabled:
            return
        warn_if_risky_user_skill(skill_dir)
        _add(skill_dir)

    for proj_dir in get_project_skills_dirs(cwd):
        for skill_md in iter_project_skill_files(proj_dir):
            _add(skill_md.parent)
    for d in profiles.skills(profile_dir):
        _add_user_skill(d)
    shared = shared_skills_dir()
    if os.path.isdir(shared):
        for name in sorted(os.listdir(shared)):
            cand = os.path.join(shared, name)
            if os.path.isdir(cand) and not name.startswith("."):
                _add_user_skill(cand)
    return out
