"""Mixture-of-Agents (hermes MoA) as a core provider.

MoA runs as a persistent mode, hermes style: pick the MoA preset with /model and
every turn fans out to the reference models until you switch away. The one-shot
/moa command was removed on 2026-08-23 (user decision: the mode is the feature).

A core provider is part of every model registry, the way pi's built-in providers
are (``core/wiring.py`` ``PROVIDER_MODULES``; the registry recomputes it on every
reload). A preset is published only while its aggregator's provider is configured:
a virtual model nobody can run must not make an empty registry look populated.
"""

from misaka.core.wiring import KINDS

SESSION_KINDS = KINDS  # a configured ``provider=moa`` must resolve in every session, bare ones included


def provider_config(configured, find=None):
    """``("moa", config)`` for the registry, or None when no preset is runnable.

    ``configured(provider_id) -> bool`` is the registry's credential check for a provider,
    and ``find(provider_id, model_id)`` its lookup -- the only one that can size a preset
    whose aggregator is a custom provider from models.json.
    """
    from . import provider

    models = []
    for model in provider.preset_models(configured, find):
        models.append({
            "id": model.id,
            "name": model.name or model.id,
            "api": "moa",
            "reasoning": bool(model.reasoning),
            "input": list(model.input),
            "cost": model.cost.model_dump(),
            "contextWindow": int(model.contextWindow),
            "maxTokens": int(model.maxTokens),
        })
    if not models:
        return None
    return "moa", {
        "name": "MoA",
        "baseUrl": "moa://local",
        "apiKey": "moa-virtual-provider",
        "api": "moa",
        "streamSimple": provider.stream_simple_moa,
        "models": models,
    }


class MoaPart:
    """The session side of MoA: the registry it resolves slot models through, and the turn state it drops at the end."""

    def __init__(self):
        self.tools = []
        self.commands = []

    def attach(self, session):
        from . import provider

        provider.set_model_resolver(session.modelRegistry.find)
        provider.set_auth_resolver(session.modelRegistry.getAuth)

    async def session_shutdown(self, event, ctx):
        from . import provider

        session = getattr(getattr(ctx, "sessionManager", None), "getSessionId", None)
        provider.forget_session(session() if callable(session) else None)


def part(spec):
    return MoaPart()
