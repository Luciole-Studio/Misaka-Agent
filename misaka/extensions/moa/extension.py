"""MoA provider registration and one-turn `/moa` command."""


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


def register_command(harn):
    from misaka.extensions.moa import provider

    async def moa_cmd(args, ctx):
        prompt = (args or "").strip()
        if not prompt:
            ctx.ui.notify("Usage: /moa <prompt> — answer one prompt with the default MoA preset, then switch back to the current model.", "info")
            return
        if not ctx.isIdle():
            ctx.ui.notify("A turn is still running; wait for it to finish before using /moa.", "error")
            return
        preset = provider.load_moa_config()["default_preset"]
        moa_model = ctx.modelRegistry.find("moa", preset)
        if moa_model is None:
            ctx.ui.notify(f"MoA preset {preset!r} is not available. Check ~/.misaka/moa.json and reload models.", "error")
            return
        prev = ctx.model
        if prev is not None and prev.provider == "moa":
            harn.sendUserMessage(prompt)
            return
        if not await harn.setModel(moa_model):
            ctx.ui.notify("Could not switch the session to the MoA model.", "error")
            return
        restore = f" to {prev.id}" if prev else ""
        ctx.ui.notify(
            f"Running this turn with MoA preset {preset!r}; switching back{restore or ' to the previous model'} when it finishes.",
            "info",
        )
        harn.sendUserMessage(prompt)
        try:
            await ctx.waitForIdle()
        finally:
            if prev is not None:
                await harn.setModel(prev)

    harn.registerCommand("moa", {
        "handler": moa_cmd,
        "description": "Answer one prompt with the default Mixture-of-Agents preset, then switch back to the current model."})


def register(harn):
    """Register the MoA provider and its interactive command."""

    register_provider(harn)
    register_command(harn)
