"""``misaka moa configure``: pick a preset's advisors and aggregator, the way Hermes does.

Port of Hermes ``hermes_cli/moa_cmd.cmd_moa``'s ``configure`` branch (local checkout
``~/.hermes/hermes-agent``). Hermes offers the same walk twice over -- a curses radio list
when a terminal can host one, and a numbered prompt when it cannot -- and a web form on
``PUT /api/model/moa`` for its dashboard. MISAKA has no dashboard, so the numbered prompt
is the whole surface; it is also the branch Hermes itself falls back to.

The walk: one reference model at a time until you say Done, then the aggregator. Only
providers with credentials are offered, because a slot the session cannot call is a turn
that fails at fan-out. ``moa`` is never offered: an MoA preset cannot aggregate another.
"""

from __future__ import annotations

from typing import Any

from misaka.config import home
from misaka.core.moa.provider import (
    load_moa_config,
    normalize_moa_config,
    save_moa_config,
    slot_label,
)


def available_slots() -> list[tuple[str, list[str]]]:
    """``(provider, model ids)`` for every provider this install can actually call.

    The model registry's own credential check, so a provider configured in models.json
    counts exactly as one authenticated through ``/login`` does.
    """
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_registry import ModelRegistry

    registry = ModelRegistry.create(AuthStorage.create())
    by_provider: dict[str, list[str]] = {}
    for model in registry.getAvailable():
        if model.provider == "moa":
            continue          # an MoA aggregator cannot itself be an MoA preset
        by_provider.setdefault(model.provider, []).append(model.id)
    return [(provider, sorted(models)) for provider, models in sorted(by_provider.items())]


def _choose(title: str, rows: list[str], default: int, ask, out) -> int:
    """One numbered menu. Empty input keeps ``default``; so does anything unparseable."""
    for index, row in enumerate(rows, start=1):
        out(f"  {index}. {row}")
    raw = str(ask(f"{title} [{default + 1}]: ") or "").strip()
    if not raw:
        return default
    try:
        return max(0, min(len(rows) - 1, int(raw) - 1))
    except ValueError:
        return default


def _pick_slot(providers: list[tuple[str, list[str]]], current: dict[str, Any] | None, ask, out) -> dict[str, str]:
    """Pick one ``{provider, model}`` slot, seeded from ``current`` when there is one."""
    current = current or {}
    provider_rows = [f"{name}  ({len(models)} models)" for name, models in providers]
    provider_default = next(
        (index for index, (name, _models) in enumerate(providers) if name == current.get("provider")),
        0,
    )
    provider, models = providers[_choose("Select provider", provider_rows, provider_default, ask, out)]
    model_default = models.index(current["model"]) if current.get("model") in models else 0
    model = models[_choose(f"Select model for {provider}", models, model_default, ask, out)]
    return {"provider": provider, "model": model}


def describe(config: dict[str, Any], out) -> None:
    """Print every preset the way ``misaka moa list`` does."""
    out(f"MoA presets (\"moa\" in {home.display(home.path('settings'))})")
    out("Use /model to select MoA·<preset>; every turn then runs the mixture until you switch away.")
    privacy = config.get("privacy_filter") or ""
    if privacy:
        out(f"Privacy filter: {privacy} (advisor text is redacted before it is kept"
            + (" and before the aggregator reads it)" if privacy == "full" else ")"))
    for name, preset in config["presets"].items():
        mark = "*" if name == config["default_preset"] else " "
        state = "" if preset["enabled"] else " (disabled)"
        out(f"\n{mark} {name}{state}  fanout={preset['fanout']}")
        for index, slot in enumerate(preset["reference_models"], 1):
            off = "" if slot.get("enabled", True) else " (disabled)"
            out(f"    advisor{index}: {slot_label(slot)}{off}")
        out(f"    aggregator: {slot_label(preset['aggregator'])}")


def configure(name: str | None = None, *, ask=input, out=print) -> str:
    """Walk one preset's slots and write the section back. Returns the preset name."""
    config = load_moa_config()
    preset_name = (name or config["default_preset"]).strip() or config["default_preset"]
    current = config["presets"].get(preset_name) or config["presets"][config["default_preset"]]

    providers = available_slots()
    if not providers:
        raise RuntimeError(
            "No model provider has credentials, so there is nothing to build a mixture from. "
            "Add one with `/login <provider>` in a session, or define it in "
            f"{home.display(home.path('models'))}.")

    out(f"Configure MoA preset: {preset_name}")
    out("Pick at least one reference model; choose Done when finished.")
    existing = list(current.get("reference_models") or [])
    references: list[dict[str, Any]] = []
    index = 0
    while True:
        seed = existing[index] if index < len(existing) else None
        picked = _pick_slot(providers, seed, ask, out)
        picked["enabled"] = bool((seed or {}).get("enabled", True))
        references.append(picked)
        index += 1
        if _choose("Add another reference model?", ["Add another", "Done"], 1, ask, out) == 1:
            break

    out("Configure aggregator model.")
    aggregator = _pick_slot(providers, current.get("aggregator"), ask, out)

    updated = dict(current)
    updated["reference_models"] = references
    updated["aggregator"] = aggregator
    presets = dict(config["presets"])
    presets[preset_name] = updated
    written = normalize_moa_config({
        **config,
        "presets": presets,
        "default_preset": config["default_preset"] if config["default_preset"] in presets else preset_name,
    })
    save_moa_config(written)
    out(f"Saved MoA preset: {preset_name}")
    describe(written, out)
    return preset_name


__all__ = ["available_slots", "configure", "describe"]
