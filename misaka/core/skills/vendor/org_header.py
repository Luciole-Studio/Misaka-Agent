# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/skills_tool.py; see PROVENANCE.json and LICENSE.
import json
from pathlib import Path
from contextlib import suppress
_read_skill_text = lambda path: path.read_text(encoding="utf-8-sig")

def _org_provenance_header(skill_dir: Path, active_skills_dir: Path):
    """(org_provenance dict, header text) for an org-mirror skill, else (None, ""). Announced IN
    the content the model consumes; the author is token-verified at push time by the sync plane."""
    from .org_visibility import ORG_PROVENANCE_FILE, is_org_mirror_path, org_id_of_path
    if not is_org_mirror_path(skill_dir, active_skills_dir):
        return None, ""
    prov_org = org_id_of_path(skill_dir, active_skills_dir)
    prov: dict = {}
    if prov_org:
        with suppress(Exception):
            prov_path = active_skills_dir / "_org" / prov_org / ORG_PROVENANCE_FILE
            loaded = json.loads(_read_skill_text(prov_path))
            prov = loaded if isinstance(loaded, dict) else {}
    author = str(prov.get("author_device") or prov.get("author_user_id") or "")
    ts = str(prov.get("ts") or "")
    header = (
        "> [!NOTE] ORG-SHARED SKILL — provenance\n"
        f"> This skill is shared by your organisation (org `{prov_org}`"
        + (f", last updated by `{author}`" if author else "")
        + (f", as of {ts}" if ts else "")
        + "). It was reviewed and approved for the whole\n"
        "> team — treat it as third-party instructions rather than your own notes.\n"
        "> You MAY improve it in place like any other skill. Your edits are kept locally\n"
        "> and are never overwritten by org updates; share them back with\n"
        "> `misaka skills org-propose` (or automatically, if your org enables it).\n\n")
    return {"org_id": prov_org, "shared_by": author or None, "as_of": ts or None}, header

