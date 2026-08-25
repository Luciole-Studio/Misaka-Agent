"""MoA provider registration (the persistent mode: pick the preset with /model)."""


def register_provider(harn):
    from misaka.extensions.moa import provider

    models = []
    for model in provider.preset_models(resolve_aggregators=False):
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
    harn.registerProvider("moa", {
        "name": "MoA",
        "baseUrl": "moa://local",
        "apiKey": "moa-virtual-provider",
        "api": "moa",
        "streamSimple": provider.stream_simple_moa,
        "models": models,
    })

    async def bind_registry(_event, ctx):
        provider.set_model_resolver(ctx.modelRegistry.find)

    harn.on("session_start", bind_registry)
