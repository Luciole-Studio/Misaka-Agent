# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / hermes_cli/oneshot.py; see PROVENANCE.json and LICENSE.

def _normalize_toolsets(toolsets: object = None) -> list[str] | None:
    """Split repeated/comma-separated toolset flags into a clean list (``None`` when empty)."""
    if not toolsets:
        return None
    items = toolsets if isinstance(toolsets, (list, tuple)) else [toolsets]
    parts = [str(item).split(",") if isinstance(item, str) else [str(item)] for item in items]
    return [p.strip() for chunk in parts for p in chunk if p.strip()] or None


def _normalize_skills(skills: object = None) -> list[str]:
    """Normalize repeated/comma-separated skill flags and preserve order."""
    return list(dict.fromkeys(_normalize_toolsets(skills) or []))

