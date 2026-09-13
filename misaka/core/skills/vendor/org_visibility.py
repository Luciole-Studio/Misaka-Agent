# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / agent/skill_utils.py; see PROVENANCE.json and LICENSE.
from pathlib import Path
from typing import Optional, Tuple

ORG_MIRROR_DIR_NAME = "_org"


ORG_ACTIVE_MARKER = ".active_org"


ORG_PROVENANCE_FILE = ".org-provenance.json"


ORG_BASELINE_FILE = ".org-baseline.json"  # upstream fingerprint; detects local edits


def read_active_org_id(skills_dir: Path) -> Optional[str]:
    """The org id whose mirror may resolve, or None (no org skills load)."""
    marker = skills_dir / ORG_MIRROR_DIR_NAME / ORG_ACTIVE_MARKER
    try:
        return (marker.read_text(encoding="utf-8").strip() or None) if marker.exists() else None
    except OSError:
        return None


def _org_rel_parts(path, skills_dir: Path) -> Tuple[str, ...]:
    """Path parts of *path* relative to *skills_dir* if it is under ``_org/``, else ``()``."""
    try:
        parts = Path(path).resolve().relative_to(Path(skills_dir).resolve()).parts
    except (OSError, ValueError):
        return ()
    return parts if parts and parts[0] == ORG_MIRROR_DIR_NAME else ()


def is_org_mirror_path(path, skills_dir: Path) -> bool:
    """True when *path* is inside the org mirror (``_org/``)."""
    return bool(_org_rel_parts(path, skills_dir))


def org_id_of_path(path, skills_dir: Path) -> Optional[str]:
    """The ``<org_id>`` segment for a path under ``_org/<org_id>/...``."""
    parts = _org_rel_parts(path, skills_dir)
    return parts[1] if len(parts) >= 2 else None

