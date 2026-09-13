# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/skills_tool.py; see PROVENANCE.json and LICENSE.
import json
from typing import Any, Dict, List
def _json(payload):
    return json.dumps(payload, ensure_ascii=False)

def _parse_tags(tags_value) -> List[str]:
    """Tags from frontmatter: a parsed list, "[a, b]", or "a, b"."""
    if not tags_value:
        return []
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]
    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]


def _sort_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep every skill listing path ordered the same way."""
    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))


def skills_list(all_skills: List[Dict[str, Any]], category: str = None, task_id: str = None) -> str:
    """Tier 1 listing: name + description (+ category) only; ``task_id`` is handler parity."""
    try:
        if not all_skills:
            return _json({"success": True, "skills": [], "categories": [],
                          "message": "No skills found in skills/ directory."})
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]
        all_skills = _sort_skills(all_skills)
        categories = sorted({s.get("category") for s in all_skills if s.get("category")})
        return _json({
            "success": True, "skills": all_skills, "categories": categories,
            "count": len(all_skills),
            "hint": "Use skill_view(name) to see full content, tags, and linked files"})
    except Exception as e:
        return _json({"success": False, "error": str(e)})

