# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/skills_tool.py; see PROVENANCE.json and LICENSE.
import logging
from typing import Any, Dict, Tuple
from .readiness import SkillReadinessStatus, _get_required_environment_variables, _build_setup_note
logger = logging.getLogger(__name__)

def _skill_readiness(frontmatter: Dict[str, Any], skill_name: str, *, backend, load_env, _is_env_var_persisted, _capture_required_environment_variables, register_env_passthrough, register_credential_files, _is_remote_env_backend) -> Tuple[dict, dict]:
    """Resolve required env vars / credential files (prompting for secrets where the surface
    allows) and register what's available for sandboxes. Returns ``(fields, extras)``: fields go
    before ``_source_path`` in the skill_view result, extras after — key order is tool output."""
    required_env_vars = _get_required_environment_variables(frontmatter)
    env_snapshot = load_env()
    missing_required_env_vars = [
        e for e in required_env_vars
        if not e.get("optional") and not _is_env_var_persisted(e["name"], env_snapshot)]
    capture_result = _capture_required_environment_variables(skill_name, missing_required_env_vars)
    if missing_required_env_vars:  # re-read: a successful capture persisted into .env
        env_snapshot = load_env()
    still_missing = set(capture_result["missing_names"])
    remaining = [
        e["name"] for e in required_env_vars if not e.get("optional")
        and (e["name"] in still_missing or not _is_env_var_persisted(e["name"], env_snapshot))]
    setup_needed = bool(remaining)
    # Only vars actually set pass through to sandboxed execution (execute_code, terminal).
    if available_env_names := [e["name"] for e in required_env_vars if e["name"] not in remaining]:
        try:
            register_env_passthrough(available_env_names)
        except Exception:
            logger.debug("Could not register env passthrough for skill %s", skill_name, exc_info=True)
    # Credential files for remote sandboxes: existing host files are registered,
    # missing ones flag setup_needed.
    required_cred_files_raw = frontmatter.get("required_credential_files", [])
    missing_cred_files: list = []
    if isinstance(required_cred_files_raw, list) and required_cred_files_raw:
        try:
            missing_cred_files = register_credential_files(required_cred_files_raw)
            setup_needed = setup_needed or bool(missing_cred_files)
        except Exception:
            logger.debug("Could not register credential files for skill %s", skill_name, exc_info=True)
    status = SkillReadinessStatus.SETUP_NEEDED if setup_needed else SkillReadinessStatus.AVAILABLE
    fields = {
        "required_environment_variables": required_env_vars, "required_commands": [],
        "missing_required_environment_variables": remaining,
        "missing_credential_files": missing_cred_files, "missing_required_commands": [],
        "setup_needed": setup_needed, "setup_skipped": capture_result["setup_skipped"],
        "readiness_status": status.value}
    extras: dict = {}
    if setup_help := next((e["help"] for e in required_env_vars if e.get("help")), None):
        extras["setup_help"] = setup_help
    if capture_result["gateway_setup_hint"]:
        extras["gateway_setup_hint"] = capture_result["gateway_setup_hint"]
    missing_items = [f"env ${n}" for n in remaining] + [f"file {p}" for p in missing_cred_files]
    if setup_needed and (setup_note := _build_setup_note(status, missing_items, setup_help)):
        if _is_remote_env_backend(backend):
            setup_note = f"{setup_note} {backend.upper()}-backed skills need these requirements available inside the remote environment as well."
        extras["setup_note"] = setup_note
    return fields, extras

