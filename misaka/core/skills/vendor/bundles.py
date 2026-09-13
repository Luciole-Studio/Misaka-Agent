# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / agent/skill_bundles.py; see PROVENANCE.json and LICENSE.
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List
import yaml
from misaka.utils.atomic import write_text
from .commands import _scaffold_header, slugify_skill_name as _slugify
logger = logging.getLogger(__name__)

def _load_bundle_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parse one bundle YAML; ``None`` (logged) on any error so a broken bundle can't break discovery."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not read bundle %s: %s", path, exc)
        return None
    except yaml.YAMLError as exc:
        logger.warning("Invalid YAML in bundle %s: %s", path, exc)
        return None
    def _skip(reason: str) -> None:
        logger.warning("Bundle %s %s; skipping", path, reason)
    if not isinstance(data, dict):
        return _skip("is not a mapping")
    name = str(data.get("name") or path.stem).strip()
    if not name:
        return _skip("has no name")
    raw_skills = data.get("skills") or []
    if not isinstance(raw_skills, list) or not raw_skills:
        return _skip("has no skills list")
    skills = [str(s).strip() for s in raw_skills if str(s).strip()]
    if not skills:
        return _skip("has empty skills list")
    slug = _slugify(name)
    if not slug:
        return _skip("yielded empty slug")
    return {
        "name": name, "slug": slug, "skills": skills, "path": str(path),
        "description": str(data.get("description") or "").strip() or f"Load {len(skills)} skills as a bundle",
        "instruction": str(data.get("instruction") or "").strip(),
    }


def build_bundle_invocation_message(
    cmd_key: str, user_instruction: str = "", task_id: str | None = None, platform: str | None = None,
    *, bundles, load_blocks, load_payload, disabled_names,
) -> Optional[Tuple[str, List[str], List[str]]]:
    """Build the user message for a bundle invocation: ``(message,
    loaded_skill_names, missing_skill_names)`` or ``None`` if the bundle wasn't
    found. Uninstalled members are skipped with a note; disabled ones too, since
    ``_load_skill_payload`` bypasses the scan-time filter (``platform`` scopes
    that check — gateway passes it, None resolves from env).

    Disabled skills are also skipped: bundles load members via ``_load_skill_payload`` directly, bypassing
    the scan-time disabled filter in ``get_skill_commands()``, so the disabled list must be re-applied here.
    ``platform`` scopes the check to a specific platform's ``skills.platform_disabled`` config (gateway
    dispatch passes it explicitly because the gateway handles multiple platforms in one process); when
    *None*, the platform resolves from session env vars and the global disabled list still applies. Mirrors
    the stacked-skill gate in gateway dispatch (#58888).
    """
    info = bundles.get(cmd_key)
    if not info:
        return None
    # Late import keeps skill_bundles cheap to import (no tools/* at import time).
    bundle_name = info["name"]
    loaded_names, missing, disabled, skill_blocks = load_blocks(
        [(skill_id or "").strip() for skill_id in info["skills"]],
        lambda identifier: load_payload(identifier, task_id=task_id),
        lambda _name: f'[Loaded as part of the "{bundle_name}" skill bundle.]',
        task_id,
        disabled_names=disabled_names,
    )
    if not skill_blocks:
        return None
    header = _scaffold_header(
        f'"{bundle_name}" skill bundle', loaded_names, lead_lines=[f"Bundle: {bundle_name}"], missing=missing,
        disabled=disabled, extra_instruction=info.get("instruction") or "", user_instruction=user_instruction,
    )
    return ("\n\n".join([header, *skill_blocks]), loaded_names, missing)


def bundle_path_for(name: str, root: Path) -> Path:
    """Return the canonical filesystem path for a bundle name."""
    slug = _slugify(name)
    if not slug:
        raise ValueError(f"Bundle name {name!r} normalizes to an empty slug")
    return root / f"{slug}.yaml"


def save_bundle(name: str, skills: List[str], description: str = "", instruction: str = "", overwrite: bool = False, *, root: Path) -> Path:
    """Write a bundle to disk and refresh the cache. Raises ``FileExistsError``
    if the target exists and not ``overwrite``; ``ValueError`` for unusable inputs."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Bundle name is required")
    cleaned_skills = [str(s).strip() for s in skills if str(s).strip()]
    if not cleaned_skills:
        raise ValueError("Bundle must reference at least one skill")
    path = bundle_path_for(name, root)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Bundle already exists at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {"name": name, "skills": cleaned_skills}
    payload.update({k: v for k, v in (("description", description), ("instruction", instruction)) if v})
    write_text(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return path


def delete_bundle(name: str, *, root: Path) -> Path:
    """Delete a bundle by name and return its path; ``FileNotFoundError`` if absent."""
    path = bundle_path_for(name, root)
    if not path.exists():
        raise FileNotFoundError(f"No bundle at {path}")
    path.unlink()
    return path

