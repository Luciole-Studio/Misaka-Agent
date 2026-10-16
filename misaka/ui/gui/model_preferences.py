"""The GUI shortlist, independent of credentials and the runtime model catalogue."""
from __future__ import annotations

import os


def preferences() -> dict:
    from misaka.config.product import setting
    value = setting("gui", "modelSelection", {})
    return value if isinstance(value, dict) else {}


def initial_refs() -> set[str]:
    from misaka.config import current_config, profiles
    from misaka.core.network import roster
    cfg = current_config()
    refs = {f"{cfg.get('provider', '')}/{cfg.get('default_model', '')}"}
    roots = [os.path.join(cfg["roles_root"], "last_order")]
    roots.extend(os.path.join(cfg["profiles_root"], sid) for sid in roster.roster_names(root=cfg["profiles_root"]))
    refs.update(ref for root in roots if (ref := profiles.pinned_model(root)))
    return refs


def selection(provider: str, models, *, stored=None, refs=None) -> dict:
    stored = preferences() if stored is None else stored
    entry = stored.get(provider)
    if isinstance(entry, dict):
        ids = entry.get("models")
        return {"enabled": entry.get("enabled", True) is True,
                "models": [v for v in ids if isinstance(v, str)] if isinstance(ids, list) else []}
    refs = initial_refs() if refs is None else refs
    ids = [m.id for m in models if m.provider == provider and f"{provider}/{m.id}" in refs]
    return {"enabled": bool(ids), "models": ids}


def visible_models(models):
    models = list(models)
    stored, refs = preferences(), initial_refs()
    choices = {p: selection(p, models, stored=stored, refs=refs) for p in {m.provider for m in models}}
    return [m for m in models if choices[m.provider]["enabled"] and m.id in choices[m.provider]["models"]]
