"""`/moa <prompt>`：MoA 一次性糖（hermes cli.py 的 /moa 形制，2026-08-27 精读移植）。

hermes 语义：/moa **不是**独立的注入路径——它把会话临时切到 moa 虚拟服务商的
默认 preset，把这条 prompt 当一轮正常对话跑（参谋扇出＋聚合官真身带工具行动
全在 provider 内，见 misaka/ai/providers/moa.py），跑完把原模型切回来。
要整个会话换过去：/model 里选 MoA·<preset>（虚拟服务商行与真模型平级在册）。

配置：全局 ~/.misaka/moa.json；管理面 `misaka moa list|delete`（cli/app.py）。
"""


def register(harn):
    async def moa_cmd(args, ctx):
        from misaka.ai.providers.moa import load_moa_config

        prompt = (args or "").strip()
        if not prompt:
            ctx.ui.notify("用法：/moa <prompt>——用默认 MoA preset 跑这一条，跑完还原你的模型。"
                          "整会话切换用 /model 选 MoA·<preset>；配置 ~/.misaka/moa.json", "info")
            return
        preset = load_moa_config()["default_preset"]
        moa_model = ctx.modelRegistry.find("moa", preset)
        if moa_model is None:
            ctx.ui.notify(f"MoA preset「{preset}」不在册——models 装载时 moa.json 可能没读到", "error")
            return
        prev = ctx.model
        if prev is not None and prev.provider == "moa":
            # 已经在 MoA 上：没有"切过去"可言，当普通消息发（hermes：/moa 是
            # 切换糖，不是第二条执行路径）
            harn.sendUserMessage(prompt)
            return
        if not await harn.setModel(moa_model):
            ctx.ui.notify("切不到 MoA 虚拟服务商（setModel 被拒）", "error")
            return
        ctx.ui.notify(f"MoA 一次性：preset「{preset}」上场，本轮跑完还原"
                      f"{('到 ' + prev.id) if prev else ''}", "info")
        harn.sendUserMessage(prompt)
        try:
            await ctx.waitForIdle()   # 等这一轮（含全部工具迭代）真正收尾
        finally:
            if prev is not None:
                await harn.setModel(prev)   # hermes _pending_moa_restore_model 同义

    harn.registerCommand("moa", {
        "handler": moa_cmd,
        "description": "MoA 一次性：这条 prompt 交给参谋团+聚合官跑，跑完还原模型"})
