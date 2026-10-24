"""Hermes preprocessing, with MISAKA's explicit origin execution policy."""

import json
import logging
from pathlib import Path

from .vendor.preprocessing import (
    expand_inline_shell,
    load_skills_config,
    run_inline_shell,
    substitute_template_vars,
)
from .vendor.preprocessing import preprocess_skill_content as _preprocess

logger = logging.getLogger(__name__)


# Hermes bda3a363e9 / agent/skill_preprocessing.py, with the hub lock and skills dir from MISAKA's
# vendored hub (vendor/skills_hub.py): the pinned vendor copy predates it.
def _is_community_hub_skill(skill_dir: Path | None) -> bool:
    """Whether *skill_dir* is a hub-installed skill the scan gate classifies as community trust.

    The hub's INSTALL_POLICY blocks a community install on a caution/dangerous verdict — but
    ``--force`` (or a pre-scanner install) puts that skill on disk anyway, and the inline-shell
    DSL scans as high severity, so auto-executing it on view would re-arm exactly what the
    gate refused (#63307). Provenance is the hub lock entry (trusted/builtin entries expand;
    anything without one — bundled-synced, user-created, project/external — keeps the flag's
    contract). It covers every SKILL.md nested inside a community install and is read from
    the lock that owns the skills tree the dir lives in, not only the active profile's
    (``external_dirs`` can expose another profile's installs). A lock that exists but cannot
    be read fails closed for skills in its tree; no lock means no hub installs.
    """
    if skill_dir is None:
        return False
    try:
        from .vendor.skills_hub import HubLockFile, _skills_dir

        target = Path(skill_dir).resolve()
        locks = {}
        try:
            locks[HubLockFile().path.resolve()] = _skills_dir().resolve()
        except RuntimeError:  # MISAKA: no role scope here (a module-level load); the trees' own locks below
            pass
        for root in target.parents:
            if root.name == "skills" and (root / ".hub" / "lock.json").is_file():
                locks.setdefault((root / ".hub" / "lock.json").resolve(), root)
        for lock_path, root in locks.items():
            if not (target.is_relative_to(root) and lock_path.is_file()):
                continue
            try:
                installed = json.loads(lock_path.read_text(encoding="utf-8-sig"))["installed"]
                entries = [e for e in installed.values() if isinstance(e, dict)]
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                logger.debug("Unreadable hub lock %s; inline shell stays off for its tree", lock_path, exc_info=True)
                return True
            for entry in entries:
                rel = str(entry.get("install_path") or "")
                if entry.get("trust_level") == "community" and rel and target.is_relative_to((root / rel).resolve()):
                    return True
        return False
    except Exception:
        logger.debug("Could not resolve hub provenance for inline-shell trust scoping", exc_info=True)
        return True


def preprocess_skill_content(
    content, skill_dir, session_id=None, skills_cfg=None, *, layer=None
):
    from .layers import PERSONAL_LAYERS

    cfg = dict(skills_cfg if isinstance(skills_cfg, dict) else load_skills_config())
    # MISAKA has no project trust step (Hermes trusts project skill dirs explicitly), so inline
    # shell stays limited to the personal layers; Hermes' community-hub check applies inside them.
    if layer not in PERSONAL_LAYERS or (cfg.get("inline_shell", False) and _is_community_hub_skill(skill_dir)):
        cfg["inline_shell"] = False
    try:
        return _preprocess(content, skill_dir, session_id, cfg)
    except Exception:  # noqa: BLE001 - Hermes' reader falls back to RAW content, including unresolved macros
        return content


__all__ = [
    "expand_inline_shell",
    "load_skills_config",
    "preprocess_skill_content",
    "run_inline_shell",
    "substitute_template_vars",
]
