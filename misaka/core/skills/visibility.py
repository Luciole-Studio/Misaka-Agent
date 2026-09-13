"""Offer-time Hermes rules with explicit MISAKA capabilities, never tool grants.

The index stores documents, not a process-wide environment verdict. Conditions
are evaluated after each lookup, so two roles or changing active tools cannot
share a stale decision. Explicit reads skip relevance rules (not hard disables).
"""

import os

from .vendor.environment import _detect_container
from .vendor.metadata import (
    extract_skill_conditions,
    parse_config_string_list,
    skill_matches_environment,
)
from .vendor.visibility import _skill_should_show

# Only capabilities with an actual host implementation are translated. In
# particular, bash is NOT process_manage, and grep alone is NOT search_files.
_TOOL_ALIASES = {
    "terminal": ({"bash"}, {"powershell"}),
    "read_file": ({"read"},),
    "write_file": ({"write"},),
    "patch": ({"edit"},),
    "search_files": ({"grep", "find"},),
}
_TOOLSET_MEMBERS = {
    "web": {"web_search", "web_extract"},
    "terminal": {"terminal", "process_manage"},
    "file": {"read_file", "write_file", "patch", "search_files"},
    "skills": {"skills_list", "skill_view", "skill_manage"},
}


def capabilities(active):
    if active is None:
        return None, None
    tools = set(active)
    tools.update(alias for alias, choices in _TOOL_ALIASES.items() if any(c <= tools for c in choices))
    # Hermes derives toolsets from present member tools, not the configured
    # names of disabled toolsets. Do not import or initialize its registry.
    return tools, {group for group, members in _TOOLSET_MEMBERS.items() if members & tools}


def environment_detector(*, kind=None, active=None):
    def detect(tag):
        if tag == "kanban":
            # A Board card owns its task. Ordinary delegates must not inherit
            # that verdict merely by inheriting a parent's process environment.
            return kind == "card" or (kind != "child" and "misaka_board" in (active or ()))
        if tag == "docker":
            return _detect_container()
        if tag == "s6":
            return os.path.isdir("/run/s6") or os.path.isdir("/package/admin/s6-overlay")
        return True
    return detect


def offered(frontmatter, *, tools=None, toolsets=None, platform=None, detect=None, conditions=True):
    if not skill_matches_environment(frontmatter, detect or environment_detector(active=tools)):
        return False
    if not conditions:
        return True  # Hermes list/slash only filter environment and hard compatibility.
    raw = extract_skill_conditions(frontmatter)
    # A malformed null/scalar declaration must not break every Skill's prompt.
    normalized = {key: parse_config_string_list(value) for key, value in raw.items()}
    return _skill_should_show(normalized, tools, toolsets, platform)
