"""Role profile layout: ~/.misaka/profiles/<role>/.

As in pi, personality (SOUL.md, config.json) and skills/MCP (skills/, mcp/,
config.yaml) are user data that share one role directory; nothing lives in the
source tree. Built-in subagent types ship with the package
(misaka/core/subagent/agents/) and can be overridden in ~/.misaka/agent/agents/.

``<role>`` is a path relative to profiles/, e.g. ``last_order`` or ``sisters/10032``.
"""
import json
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


def pinned_model(profile_dir):
    """Return the model this role runs on, or ``""`` when she has none of her own.

    One role, one model: her chat starts on it (``cli.chat.assembly``), her task cards
    run on it (``network.sister_runtime``), and the model selector writes it back
    (:func:`persist_role_default_model`). Without a pin the role falls back to the
    product-wide default, which is the global ``settings.json`` one.
    """
    if not profile_dir:
        return ""
    try:
        with open(os.path.join(profile_dir, "config.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    return str(data.get("model") or "").strip() if isinstance(data, dict) else ""


def persist_role_default_model(profile_dir, model_id):
    """Record a newly chosen default model as this role's own pin.

    Every role starts on her own pin, so a default set in her session -- Ctrl+S in the
    model selector, or adopting a provider's default right after ``/login`` -- has to land
    there or the next launch quietly ignores it and she comes back on the old model. pi's
    ``setModel(persist=True)`` writes only the global ``settings.json`` default, which is
    one value for the whole install: without this, setting a default for one Sister would
    set it for every Sister and for Last Order too.
    """
    if not profile_dir or not model_id:
        return False
    path = os.path.join(profile_dir, "config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if data.get("model") == model_id:
        return False
    data["model"] = model_id
    try:
        os.makedirs(profile_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        # The pin is a convenience, not the record: settings.json already has the default.
        return False
    return True


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
