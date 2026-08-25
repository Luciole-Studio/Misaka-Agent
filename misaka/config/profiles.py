"""Role profile layout: ~/.misaka/profiles/<role>/.

As in pi, personality (SOUL.md, config.json) and skills/MCP (skills/, mcp/,
config.yaml) are user data that share one role directory; nothing lives in the
source tree. Built-in subagent types ship with the package
(misaka/extensions/subagent/agents/) and can be overridden in ~/.misaka/agent/agents/.

``<role>`` is a path relative to profiles/, e.g. ``last_order`` or ``sisters/10032``.
"""
import os


def role_of(profile_dir):
    """Return the role name (path relative to profiles/), or the basename when not under profiles/."""
    p = os.path.abspath(profile_dir or "")
    marker = os.sep + "profiles" + os.sep
    return p.split(marker, 1)[1] if marker in p else os.path.basename(p)


def is_last_order(profile_dir):
    """Return whether the profile is Last Order, the one role without sub-agents."""
    role = role_of(profile_dir).strip().casefold().replace("-", "_").replace(" ", "_")
    return role == "last_order"


def config_yaml(profile_dir):
    """Return the path of the role's config.yaml (MCP server definitions and the like)."""
    return os.path.join(profile_dir, "config.yaml")


SHARED_SOUL_TEMPLATE = """# MISAKA Network · Shared identity

- Files are the truth: conclusions go to disk as artifacts, not into the conversation.
- When something cannot be found, write "could not be verified". Never invent a source.
"""


def shared_soul():
    """Return the path of the shared soul, ~/.misaka/profiles/MISAKA.md, seeding it on first use.

    Last Order, the Sisters, and their sub-agents all load it before their own
    SOUL.md. An existing file is never overwritten. One-shot roles (planner,
    reviewing Sister, judge) do not read it, so their audit stance is unaffected by the
    shared personality.
    """
    from misaka.config import CFG
    path = os.path.join(os.path.expanduser(CFG["roles_root"]), "MISAKA.md")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(SHARED_SOUL_TEMPLATE)
    return path
