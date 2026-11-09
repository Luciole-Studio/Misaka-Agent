"""Mixture-of-Agents (hermes MoA) as a core provider.

MoA runs as a persistent mode, hermes style: pick the MoA preset with /model and
every turn fans out to the reference models until you switch away. The one-shot
/moa command was removed on 2026-08-23 (user decision: the mode is the feature).

A core provider is part of every model runtime, the way pi's built-in providers
are (``core/wiring.py`` ``PROVIDER_MODULES``; its ``refreshModels`` recomputes the
presets on every refresh). A preset is published only while its aggregator's provider
is configured: a preset nobody can run must not make an empty catalog look populated.
"""

from misaka.core.wiring import KINDS

SESSION_KINDS = KINDS  # a configured ``provider=moa`` must resolve in every session, bare ones included


def provider_config(runtime):
    """``("moa", config)`` for the model runtime; ``refreshModels`` publishes the runnable presets.

    A preset runs when its aggregator's provider has credentials (the runtime's own check,
    models.json keys included), and is sized from the aggregator through the runtime's
    catalog -- the only one that answers for an aggregator defined in models.json.
    """
    from . import provider

    async def refreshModels(_context):
        aggregators = {
            str(preset["aggregator"].get("provider") or "")
            for preset in provider.load_moa_config()["presets"].values()
        }
        configured = {provider_id for provider_id in aggregators if await runtime.checkAuth(provider_id) is not None}
        return [
            {
                "id": model.id,
                "name": model.name or model.id,
                "api": "moa",
                "reasoning": bool(model.reasoning),
                "input": list(model.input),
                "cost": model.cost.model_dump(),
                "contextWindow": int(model.contextWindow),
                "maxTokens": int(model.maxTokens),
            }
            for model in provider.preset_models(configured.__contains__, runtime.getModel)
        ]

    return "moa", {
        "name": "MoA",
        "baseUrl": "moa://local",
        "apiKey": "moa-virtual-provider",
        "api": "moa",
        "streamSimple": provider.stream_simple_moa,
        "models": [],
        "refreshModels": refreshModels,
    }


class MoaPart:
    """The session side of MoA: the runtime it resolves slot models through, and the turn state it drops at the end."""

    def __init__(self):
        self.tools = []
        self.commands = []

    def attach(self, session):
        from . import provider

        provider.set_model_resolver(session.modelRuntime.getModel)
        provider.set_auth_resolver(session.modelRuntime.getAuth)

    async def session_shutdown(self, event, ctx):
        from . import provider

        session = getattr(getattr(ctx, "sessionManager", None), "getSessionId", None)
        provider.forget_session(session() if callable(session) else None)


def part(spec):
    return MoaPart()
