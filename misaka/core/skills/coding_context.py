"""Hermes Skill-only coding focus. No toolset changes or extra system blocks."""
from pathlib import Path

from .layers import load_skills_config
from .vendor.coding_context import (
    _MODE_ALIASES,
    _detect_profile,
)
from .vendor.coding_context import (
    _NON_CODING_SKILL_CATEGORIES as NON_CODING_SKILL_CATEGORIES,
)


def coding_mode():
    return _MODE_ALIASES.get(str(load_skills_config().get("coding_context", "auto")).strip().lower(), "auto")


def compact_skill_categories(cwd, *, platform="cli"):
    mode = coding_mode()
    if mode != "focus":
        return frozenset()
    return frozenset(_detect_profile(mode, platform, Path(cwd)).compact_skill_categories)

__all__ = ["NON_CODING_SKILL_CATEGORIES", "compact_skill_categories"]
