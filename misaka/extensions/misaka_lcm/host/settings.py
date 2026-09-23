"""Plugin-owned preferences and a native extension dialog; never conversation storage."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from filelock import FileLock

from misaka.utils.atomic import write_text
from misaka.utils.values import read_field

from ..vendor.config import ENV_FIELD_SPECS
from . import execution, storage

_FIELDS = {spec.name: spec for spec in ENV_FIELD_SPECS if spec.name in {
    "proactive_recall_enabled", "embeddings_enabled", "embedding_provider", "embedding_model"}}
_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"


def path() -> Path:
    return storage.plugin_home() / "settings.json"


def read() -> dict:
    try:
        saved = json.loads(path().read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(saved, dict):
        raise TypeError(f"MISAKA LCM settings must be an object: {path()}")
    for name, value in saved.items():
        spec = _FIELDS.get(name)
        if spec is None or type(value) is not spec.py_type:
            raise ValueError(f"Invalid MISAKA LCM setting {name!r}: {path()}")
    return saved


def apply(config, saved=None):
    """Explicit LCM_* env overrides remain authoritative, including false/empty."""
    for name, value in (read() if saved is None else saved).items():
        spec = _FIELDS[name]
        if spec.env_key not in os.environ:
            setattr(config, name, value)
            config.config_sources[name] = "plugin.settings"
    return config


def refresh(built, ctx):
    # Load preferences at the existing serialized operation boundary. Do not
    # restart the engine, replay messages, or touch its cursor/DAG to flip a flag.
    from .config_bridge import load_config
    current = load_config(ctx=ctx)
    for name, spec in _FIELDS.items():
        setattr(built._config, name, getattr(current, name))
        built._config.config_sources[name] = (
            "env" if spec.env_key in os.environ else current.config_sources.get(name, "default"))


def check(config) -> int:
    """Use the original provider's query path: local FastEmbed never downloads here."""
    from ..vendor.embedding_provider import resolve_provider
    provider = resolve_provider(config)
    if provider is None:
        raise ValueError("Configure an embedding provider and model before enabling automatic recall")
    vector = provider.embed_query("MISAKA LCM readiness check")
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValueError("Embedding readiness check returned an invalid vector")
    return len(vector)


def configure(enabled: bool):
    from .config_bridge import load_config
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(target) + ".lock", timeout=5):
        saved = read()
        config = load_config()
        changes = {"proactive_recall_enabled": enabled}
        if enabled:
            changes["embeddings_enabled"] = True
            if not config.embedding_provider and not config.embedding_model:
                changes.update(embedding_provider="fastembed", embedding_model=_LOCAL_MODEL)
        proposed = {**saved, **changes}
        apply(config, proposed)
        if config.proactive_recall_enabled != enabled or (enabled and not config.embeddings_enabled):
            raise ValueError("LCM_* environment overrides conflict with this change; adjust them first")
        if enabled:
            check(config)  # A failed check leaves the saved preference untouched.
        execution.check_cancelled()
        write_text(target, json.dumps(proposed, indent=2) + "\n", mode=0o600)
    return config


def describe(config) -> str:
    state = "On" if config.proactive_recall_enabled else "Off"
    embeddings = "On" if config.embeddings_enabled else "Off"
    return (f"MISAKA LCM settings — automatic recall: {state}\n"
            f"Embeddings: {embeddings} · {config.embedding_provider or '(unset)'} / "
            f"{config.embedding_model or '(unset)'}\n"
            f"Recall budget: {config.proactive_recall_budget_tokens} tokens\n"
            "Only already-loaded project history is searched; the final owner exit clears the content cache.\n"
            f"Preferences only (no conversation content): {path()}")


def register(harn, *, workspace):
    async def command(args, ctx):
        from .config_bridge import load_config
        ctx = storage.context(ctx, workspace)
        action = (args or "").strip().lower()
        try:
            if action not in {"", "on", "off", "status", "check"}:
                raise ValueError("Usage: /lcm-settings [on|off|status|check]")
            config = await execution.off_loop(load_config, ctx=ctx)
            if not action and read_field(ctx, "hasUI", False) and read_field(ctx, "mode") == "tui":
                toggle = "Disable automatic recall" if config.proactive_recall_enabled else "Enable automatic recall"
                choice = await ctx.ui.select(describe(config), [toggle, "Check embeddings", "Close"])
                if choice == toggle:
                    action = "off" if config.proactive_recall_enabled else "on"
                elif choice == "Check embeddings":
                    action = "check"
                else:
                    return
            if action in {"on", "off"}:
                config = await execution.off_loop(configure, action == "on", ctx=ctx,
                                                  signal=read_field(ctx, "signal"))
                ctx.ui.notify(describe(config) + "\nSaved; active plugin runtimes reload this at their next operation.", "info")
            elif action == "check":
                dimensions = await execution.off_loop(check, config, ctx=ctx, signal=read_field(ctx, "signal"))
                ctx.ui.notify(f"Embedding check passed: {dimensions} dimensions.\n" + describe(config), "info")
            else:
                ctx.ui.notify(describe(config), "info")
        except Exception as error:  # noqa: BLE001 - extension UI boundary reports failures without changing history
            ctx.ui.notify(f"MISAKA LCM settings: {error}", "error")
    harn.registerCommand("lcm-settings", {
        "description": "MISAKA LCM settings: automatic recall, embeddings and project-cache scope.",
        "handler": command,
    })
