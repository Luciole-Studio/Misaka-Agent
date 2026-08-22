"""Layered skill discovery: project (<folder>/skills), role, shared."""
import json
import logging
import os
from pathlib import Path

from misaka.config import CFG

logger = logging.getLogger(__name__)



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
    """Yield SKILL.md files under a project skills directory."""
    for skill_md in sorted(Path(project_dir).rglob("SKILL.md")):
        if not is_excluded_skill_path(skill_md):
            yield skill_md


def get_project_skills_dirs(cwd=None):
    """Return the project skills root: ``<folder>/skills`` when it exists.

    The folder MISAKA runs in is the project; its skills are the user's own, so they
    get the same treatment as role skills (risk warning, ``disabled`` list) and no trust gate.
    """
    root = Path(cwd or os.getcwd()).expanduser()
    try:
        cand = (root / "skills").resolve()
        roles_root = Path(os.path.expanduser(CFG["roles_root"])).resolve()
        if cand.is_dir() and roles_root not in (cand, *cand.parents):
            return [cand]
    except OSError:
        pass
    return []


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
            _add_user_skill(skill_md.parent)
    for d in (profiles.skills(profile_dir) if profile_dir else []):
        _add_user_skill(d)
    shared = shared_skills_dir()
    if os.path.isdir(shared):
        for name in sorted(os.listdir(shared)):
            cand = os.path.join(shared, name)
            if os.path.isdir(cand) and not name.startswith("."):
                _add_user_skill(cand)
    return out
