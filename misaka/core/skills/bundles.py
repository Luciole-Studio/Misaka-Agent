"""Hermes bundles scoped to the current project, role and shared profile root.

No global cache/home override: small YAML alias files are scanned on access.
Pinned collections carry parsed bundles in their digested manifest instead.
"""

import logging
from pathlib import Path

from .layers import CFG, shared_skills_dir
from .vendor import bundles as native

logger = logging.getLogger(__name__)


def bundle_roots(profile_dir, workspace):
    roles = Path(CFG["roles_root"]).expanduser().resolve()
    project = Path(workspace).expanduser().resolve()
    roots = []
    if not project.is_relative_to(roles):
        roots.append(("project", project / "skill-bundles"))
    if profile_dir:
        roots.append(("role", Path(profile_dir) / "skill-bundles"))
    roots.append(("shared", Path(shared_skills_dir()).parent / "skill-bundles"))
    return list(dict.fromkeys((layer, str(root)) for layer, root in roots))


def scan(roots):
    out = {}
    for layer, root in roots:
        base = Path(root)
        if base.is_symlink():
            continue
        for ext in ("*.yaml", "*.yml"):  # preserve native yaml-then-yml ordering
            for path in sorted(base.glob(ext)):
                if not path.is_file() or path.is_symlink():
                    continue
                if layer == "project":
                    from .guard import _determine_verdict, scan_file
                    try:
                        if _determine_verdict(scan_file(path, path.name)) == "dangerous":
                            logger.warning("Project Skill bundle quarantined: %s", path)
                            continue
                    except Exception:
                        logger.warning("Project Skill bundle scan failed: %s", path, exc_info=True)
                        continue
                info = native._load_bundle_file(path)
                if info:
                    out.setdefault("/" + info["slug"], {**info, "layer": layer})
    return out


def save(name, skills, *, profile_dir, description="", instruction="", overwrite=False):
    from .write import mutation_lock
    root = Path(profile_dir).expanduser().absolute() / "skill-bundles"
    with mutation_lock():
        _check_target(native.bundle_path_for(name, root))
        return native.save_bundle(name, skills, description, instruction, overwrite, root=root)


def delete(name, *, profile_dir):
    from .write import mutation_lock
    root = Path(profile_dir).expanduser().absolute() / "skill-bundles"
    with mutation_lock():
        _check_target(native.bundle_path_for(name, root))
        return native.delete_bundle(name, root=root)


def _check_target(path):
    from .write import _safe_parents
    _safe_parents(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Bundle target is not a regular file: {path}")
