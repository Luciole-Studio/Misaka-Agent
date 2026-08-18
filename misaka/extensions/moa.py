"""MoA 执行面：参谋扇出＋综合官二段（hermes aggregate_moa_context 忠实移植）。

设计 docs/design/moa.md。一次性 `/moa` 路径的机器：
扇出（并行、单败→便签、全败→照实说不空转综合官）→ 综合官压简明指导
→ untrusted 包裹（宪法⑤，misaka 附加——参谋意见是外来文本）。
`/moa` 命令注册与会话注入在三期；本文件零 harn 依赖，可独测。

一期已知妥协（诚实记账，v2 挂账）：
- 参谋收到的是视图渲染成的单块文本，不是真多轮消息（run_llm_json 单 prompt 口）；
- slot 的 reasoning_effort / max_tokens 暂不生效（一次性会话口未暴露该旋钮）。
"""
import asyncio
import os

from misaka.orchestration import moa as kernel
from misaka.research.kernel import guard

ADVISOR_ROLE = "moa-advisor"   # 参谋人格：SOUL＝参谋纪律（critic.ensure_profile 先例）
SYNTH_ROLE = "moa-synth"       # 综合官：空人格目录＝裸模型（hermes 综合官无 system）
DEFAULT_REFERENCE_TIMEOUT = 600


def ensure_profiles(roles_root):
    """首跑落参谋人格（绝不覆盖）＋综合官空目录。返回 (advisor_dir, synth_dir)。"""
    root = os.path.expanduser(roles_root)
    advisor = os.path.join(root, ADVISOR_ROLE)
    soul = os.path.join(advisor, "SOUL.md")
    if not os.path.exists(soul):
        os.makedirs(advisor, exist_ok=True)
        with open(soul, "w", encoding="utf-8") as f:
            f.write(kernel.REFERENCE_SYSTEM_PROMPT)
    synth = os.path.join(root, SYNTH_ROLE)
    os.makedirs(synth, exist_ok=True)
    return advisor, synth


def view_text(view):
    """参谋视图 → 单块 prompt 文本。"""
    return "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in view)


def _reference_once(slot, prompt, *, advisor_dir, timeout, usage_kw):
    """调一个参谋。永不 raise：失败＝(label, "[failed: …]") 便签，聚合照常
    （hermes _run_reference 契约）。bare＝零工具零分身，raw＝纯文本不捞 JSON。"""
    from misaka.extensions.board import worker

    label = kernel.slot_label(slot)
    try:
        _o, text, err = worker.run_llm_json(
            advisor_dir, prompt, slot["provider"], slot["model"],
            model=slot["model"], timeout=timeout, raw=True, bare=True, **usage_kw)
        if err:
            return label, f"[failed: {err}]"
        return label, (text or "").strip() or "(空响应)"
    except Exception as exc:  # noqa: BLE001 - 单参谋失败不许炸整轮
        return label, f"[failed: {exc}]"


async def fan_out(preset, view, *, roles_root, usage_kw):
    """并行扇出全部参谋，返回 [(label, text)]，顺序与 preset 一致
    （hermes：全员派出、全员收齐，无先完成早退）。"""
    advisor_dir, _ = ensure_profiles(roles_root)
    prompt = view_text(view)
    timeout = preset.get("reference_timeout") or DEFAULT_REFERENCE_TIMEOUT
    return list(await asyncio.gather(*(
        asyncio.to_thread(_reference_once, slot, prompt, advisor_dir=advisor_dir,
                          timeout=timeout, usage_kw=usage_kw)
        for slot in preset["reference_models"])))


def synthesize(preset, user_prompt, outputs, *, roles_root, usage_kw):
    """综合官二段（hermes aggregate_moa_context 尾段）：
    全败→跳过综合直接照实说（省一次空转与超时等待）；
    综合失败/空→退回 joined 原始参谋块。返回注入文本（未包裹）。"""
    from misaka.extensions.board import worker

    joined, failed = kernel.render_references(outputs)
    degraded = ""
    if failed and preset.get("degraded_reference_policy") != "silent":
        degraded = f"[参谋不可用：{', '.join(failed)}]"
    refs_line = "、".join(kernel.slot_label(s) for s in preset["reference_models"])
    if outputs and not joined:
        return ("〔MoA 参谋上下文——全部参谋失败，本轮没有参考意见，凭你自己的判断行动。〕\n"
                f"参谋：{refs_line}\n\n" + (degraded or "[参谋全部失败]"))
    if degraded:
        joined = f"{joined}\n\n{degraded}" if joined else degraded

    _, synth_dir = ensure_profiles(roles_root)
    agg = preset["aggregator"]
    prompt = (f"{kernel.SYNTH_PROMPT}\n\n原始用户请求：\n{user_prompt}\n\n"
              f"参谋意见：\n{joined}")
    try:
        _o, synthesis, err = worker.run_llm_json(
            synth_dir, prompt, agg["provider"], agg["model"],
            model=agg["model"], timeout=DEFAULT_REFERENCE_TIMEOUT,
            raw=True, bare=True, **usage_kw)
        if err:
            synthesis = ""
    except Exception:  # noqa: BLE001 - 综合失败退回原始参谋块（hermes 同款兜底）
        synthesis = ""
    body = (synthesis or "").strip() or joined
    return ("〔MoA 参谋上下文——这是给你的私下参考，照常调用工具、继续推理或正常收尾。〕\n"
            f"综合官：{kernel.slot_label(agg)}\n参谋：{refs_line}\n\n{body}")


async def moa_guidance(profile_dir, user_prompt, messages, *, roles_root,
                       preset_name=None, usage_kw=None):
    """一次性 /moa 的整链：preset 解析→参谋视图→扇出→综合→untrusted 包裹。
    返回 (包裹好的指导文本, None) 或 (None, 人话错误)。"""
    kernel.ensure(profile_dir)
    preset, err = kernel.resolve(profile_dir, preset_name)
    if err:
        return None, err
    view = kernel.advisory_view(messages)
    if not view:
        view = [{"role": "user", "content": user_prompt}]
    outputs = await fan_out(preset, view, roles_root=roles_root,
                            usage_kw=usage_kw or {})
    guidance = await asyncio.to_thread(
        synthesize, preset, user_prompt, outputs,
        roles_root=roles_root, usage_kw=usage_kw or {})
    return guard.untrusted(f"moa:{preset['name']}", guidance), None
