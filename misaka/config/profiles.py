"""Role profile layout: ~/.misaka/profiles/<role>/.

A role directory is a partial overlay of the home: ``SOUL.md`` (voice), ``settings.json``
(what she holds of her own: her pinned model, her MCP servers, her web overrides -- see
``settings_manager.ROLE_KEYS``), ``skills/`` and ``subagents/``. Nothing lives in the source
tree. Built-in subagent types ship with the package (misaka/core/subagent/agents/).

``<role>`` is a path relative to profiles/, e.g. ``last_order`` or ``sisters/10032``.
"""
import json
import os

from misaka.utils import atomic


def role_of(profile_dir):
    """Return the role name (path relative to profiles/), or the basename when not under profiles/."""
    p = os.path.abspath(profile_dir or "")
    marker = os.sep + "profiles" + os.sep
    return p.split(marker, 1)[1] if marker in p else os.path.basename(p)


def is_last_order(profile_dir):
    """Return whether the profile is Last Order, the one role without sub-agents."""
    role = role_of(profile_dir).strip().casefold().replace("-", "_").replace(" ", "_")
    return role == "last_order"


def settings_path(profile_dir):
    """The role's own settings file; ``SettingsManager`` reads it as the ``role`` scope."""
    return os.path.join(profile_dir, "settings.json")


def role_settings(profile_dir, *, strict=False):
    """The role's own settings as written, ``{}`` when she has none (or, unless strict, none readable)."""
    if not profile_dir:
        return {}
    try:
        with open(settings_path(profile_dir), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"Expected an object in {settings_path(profile_dir)}")
        return {}
    return data


def pinned_model(profile_dir, *, strict=False):
    """Return ``provider/model`` for the model this role runs on, or ``""`` when she has none of her own.

    One role, one model: her chat starts on it (``cli.chat.assembly``), her task cards
    run on it (``network.sister_runtime``), and the model selector writes it back
    (:func:`persist_role_default_model`). Without a pin the role falls back to the
    product-wide default, which is the global ``settings.json`` one.
    """
    data = role_settings(profile_dir, strict=strict)
    provider = str(data.get("defaultProvider") or "").strip()
    model = str(data.get("defaultModel") or "").strip()
    if bool(provider) != bool(model):
        if strict:
            raise ValueError(f"{settings_path(profile_dir)}: defaultProvider and defaultModel go together")
        return ""
    return f"{provider}/{model}" if provider else ""


def resolve_model_reference(reference, registry, *, fallback_provider=None):
    """Resolve a role pin to provider/id without borrowing another role's endpoint.

    An explicit legacy provider may disambiguate a bare ID. Canonical references
    always win; raw model IDs containing slashes remain valid registry IDs.
    """
    from misaka.core.model_resolver import findExactModelReferenceMatch

    reference = str(reference or "").strip()
    models = list(registry.getAll())
    model = findExactModelReferenceMatch(reference, models)
    if model is None and fallback_provider:
        model = next((m for m in models if m.provider == fallback_provider and m.id == reference), None)
    if model is None:
        matches = [f"{m.provider}/{m.id}" for m in models if m.id.casefold() == reference.casefold()]
        if matches:
            raise ValueError(f"Ambiguous model {reference!r}; choose provider/model: {', '.join(matches)}")
        raise ValueError(f"Unknown model {reference!r}; choose a registered provider/model")
    return f"{model.provider}/{model.id}"


def explicit_model_override(profile_dir, model=None):
    """Return only an explicit override (a ``--model`` or a card's own); saved defaults and pins
    are never overrides -- the session resolves those itself."""
    return model or None


def persist_role_default_model(profile_dir, model_id, *, strict=False):
    """Record a newly chosen default model as this role's own pin.

    Callers save a canonical provider/model reference. Strict mode distinguishes a
    failed save from an unchanged value; legacy callers retain the boolean contract.
    """
    if not profile_dir or not model_id:
        if strict:
            raise ValueError("A role profile and model reference are required")
        return False
    provider, slash, model = str(model_id).partition("/")
    if not slash or not provider or not model:
        if strict:
            raise ValueError(f"A role pin is a provider/model reference, not {model_id!r}")
        return False
    try:
        from filelock import FileLock

        os.makedirs(profile_dir, exist_ok=True)
        path = settings_path(profile_dir)
        with FileLock(path + ".lock"):     # the lock SettingsManager takes for the same file
            data = role_settings(profile_dir, strict=True)
            if data.get("defaultProvider") == provider and data.get("defaultModel") == model:
                return False
            data["defaultProvider"], data["defaultModel"] = provider, model
            atomic.write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    except (OSError, ValueError):
        if strict:
            raise
        return False
    return True


SHARED_SOUL_TEMPLATE = """# MISAKA Network · Shared identity

- Keep research deliverables accessible as project artifacts, following the current task's output and saving contract.
- When something cannot be found, write "could not be verified". Never invent a source.
"""


def shared_soul():
    """Return the path of the shared soul (``MISAKA.md`` in the home), seeding it on first use.

    Last Order, the Sisters, and their sub-agents all load it before their own
    SOUL.md. An existing file is never overwritten. Research uses the same shared
    conventions and role personality as ordinary sessions.
    """
    from misaka.config import home
    path = str(home.path("shared_soul"))
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(SHARED_SOUL_TEMPLATE)
    return path
