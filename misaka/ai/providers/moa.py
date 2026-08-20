"""MoA 虚拟服务商（hermes 忠实移植：moa_config.py＋moa_loop.py，2026-08-27 全文精读）。

hermes 形制：MoA 不是工具也不是独立注入路径，而是名为 "moa" 的**虚拟服务商**——
preset 就是它的"型号"。会话把模型切到 moa/<preset> 后，每次模型调用变成：
参谋（reference）扇出 → 意见附在聚合官 prompt 末尾 → **聚合官以真身行动**
（带工具、带流式，它就是行动模型）。/moa <prompt> 只是糖：临时切过来跑一轮再还原。

pi 形制落地的忠实性映射（与 hermes 的差异逐条记账）：
- hermes 的 MoAClient 是 OpenAI 客户端假面；这里是 `register_api_provider(api="moa")`
  的流函数——引擎里与 anthropic-messages 平级的同一插口。
- hermes 用线程池扇出（call_llm 阻塞）；这里原生 asyncio.gather，语义同款
  （全员派出全员收齐、单败成便签、全败跳过包装照实说）。
- hermes 手工给参谋请求打 cache_control 标（Anthropic 缓存 opt-in）；pi 系
  provider 在各自内部按 cacheRetention 自动装饰，这里只透传，不重复实现。
- hermes 的 peel/rebase（压缩闸重打标）不需要：pi 引擎每次调用把 context 全量
  交给 provider，guidance 附在**本次请求的副本**上，从不落进持久转录。
- 参谋意见的实时上屏（hermes moa.reference 事件）一期不做：pi 事件流的每个
  事件都会进正在拼装的 assistant 消息，伪造 thinking 块会污染回放；记档二期。
- privacy_filter 不移：依赖 hermes 中央脱敏器（agent.redact），misaka 没有对应物。

配置：全局 ~/.misaka/moa.json（hermes config.yaml 的 moa 块同构；具名 presets＋
default_preset）。宽容读（手编坏值降级默认，不炸会话）；递归槽（provider=moa）
在清洗时即拒。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from copy import deepcopy
from typing import Any, Callable

from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream

logger = logging.getLogger(__name__)

MOA_CONFIG_PATH = "~/.misaka/moa.json"
DEFAULT_MOA_PRESET_NAME = "default"

# 默认 preset 槽位本地化：hermes 硬编码 openai-codex/openrouter 的默认槽，在本机
# 是死槽；这里按本仓 models.json 的实际服务商写（同为"硬编码默认"，只是能跑）。
DEFAULT_MOA_REFERENCE_MODELS = [
    {"provider": "sub2api-claude", "model": "claude-opus-5"},
    {"provider": "sub2api-claude", "model": "claude-sonnet-5"},
]
DEFAULT_MOA_AGGREGATOR = {"provider": "sub2api-claude", "model": "claude-opus-5"}

# 每条工具结果在参谋视图里的 head+tail 预览预算（hermes _REFERENCE_TOOL_RESULT_BUDGET）
TOOL_RESULT_BUDGET = 4000
# preset 未设 reference_max_tokens 时，为参谋输出预留的窗内余量（hermes 同值）
REFERENCE_DEFAULT_OUTPUT_RESERVE = 8192
# chars/4 估算的安全余量（hermes _REFERENCE_TRIM_SAFETY_FRACTION）
TRIM_SAFETY_FRACTION = 0.10
MAX_REFERENCE_CONCURRENCY = 8   # hermes _MAX_REFERENCE_WORKERS

# 参谋纪律（hermes _REFERENCE_SYSTEM_PROMPT 中文化，二〇二六-〇八 已审）
REFERENCE_SYSTEM_PROMPT = """你是 MoA（Mixture of Agents）流程中的参谋模型。你**不是**行动者，也不执行任何东西：
你不能调用工具、跑命令、浏览网页、访问文件/仓库/链接——不要尝试，也不要为此道歉。
真正持有这些能力并采取行动的是另一个聚合/编排模型。

铁律：你**绝不许**声称或暗示自己执行过任何操作（跑过命令、下载过文件、访问过链接）。
你只能基于对话上下文分析与建议。示例：
- 错：「我跑了 curl，得到 404。」
- 对：「按这个报错形态，对该链接发 curl 大概率返回 404。」

下面的对话是行动者正在处理的任务现状。你的工作：给出你对局面的最高水平分析——
理解目标、推理问题、建议下一步。指出最佳路线、具体步骤与工具使用策略、
可能的坑与风险、以及行动者可能遗漏或搞错的地方。对话里提到的文件/链接/系统
一律假定存在，基于上下文推理即可，不要索要访问权限。

直接给建议——不要开场白，不要免责声明。你的回答是交给聚合者的私下参考，
不是给用户看的答案。绝不声称执行过任何东西。"""

# 视图收尾的合成 user 轮（hermes _ADVISORY_INSTRUCTION）：满足"以 user 结尾"
# 且不删行动者的最新上下文
ADVISORY_INSTRUCTION = ("[以上对话是任务现状。给出你最高水平的判断：发生了什么、"
                        "下一步该做什么、你看到哪些风险或错误、行动者该怎么走。]")


# ── 配置（hermes moa_config.py 精简移植：宽容读＋递归拒） ─────────────────


def _coerce_float_or_none(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int_or_none(value):
    if value is None or value == "":
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _coerce_fanout(value) -> str:
    """节奏档：per_iteration｜user_turn（默认，最省——hermes #67199）｜every_n:<N>。"""
    if isinstance(value, dict):
        mode = str(value.get("mode") or "").strip().lower()
        if mode == "every_n":
            n = _coerce_int_or_none(value.get("n")) or 0
            if n >= 2:
                return f"every_n:{n}"
            return "per_iteration" if n == 1 else "user_turn"
        value = mode
    mode = str(value or "").strip().lower()
    if mode in {"per_iteration", "user_turn"}:
        return mode
    if mode.startswith("every_n"):
        _, sep, rest = mode.partition(":")
        n = (_coerce_int_or_none(rest.strip()) or 0) if sep else 0
        if n >= 2:
            return f"every_n:{n}"
        if n == 1:
            return "per_iteration"
    return "user_turn"


_THINKING_LEVELS = {"minimal", "low", "medium", "high", "xhigh"}


def _clean_slot(slot, *, include_enabled=False):
    """清洗一个模型槽；残缺/递归（provider=moa）槽＝None 丢弃（hermes _clean_slot）。"""
    if not isinstance(slot, dict):
        return None
    provider = str(slot.get("provider") or "").strip()
    model = str(slot.get("model") or "").strip()
    if not provider or not model or provider.lower() == "moa":
        return None
    clean: dict[str, Any] = {"provider": provider, "model": model}
    effort = str(slot.get("reasoning_effort") or "").strip().lower()
    if effort in _THINKING_LEVELS:
        clean["reasoning_effort"] = effort
    mt = _coerce_int_or_none(slot.get("max_tokens"))
    if mt is not None:
        clean["max_tokens"] = mt
    if include_enabled:
        clean["enabled"] = slot.get("enabled") is not False
    return clean


def _default_preset() -> dict[str, Any]:
    return {
        "enabled": True,
        "reference_models": [{**s, "enabled": True} for s in deepcopy(DEFAULT_MOA_REFERENCE_MODELS)],
        "aggregator": deepcopy(DEFAULT_MOA_AGGREGATOR),
        "reference_temperature": None,
        "aggregator_temperature": None,
        "reference_timeout": None,
        "degraded_reference_policy": "loud",
        "reference_max_tokens": None,
        "fanout": "user_turn",
    }


def _normalize_preset(raw) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    raw_refs = raw.get("reference_models")
    if isinstance(raw_refs, str):
        try:
            raw_refs = json.loads(raw_refs)
        except (json.JSONDecodeError, ValueError):
            raw_refs = []
    if not isinstance(raw_refs, list):
        raw_refs = [raw_refs] if isinstance(raw_refs, dict) else []
    refs = [s for s in (_clean_slot(x, include_enabled=True) for x in raw_refs) if s]
    if not refs:
        refs = [{**s, "enabled": True} for s in deepcopy(DEFAULT_MOA_REFERENCE_MODELS)]
    policy = str(raw.get("degraded_reference_policy") or "loud").strip().lower()
    timeout = _coerce_float_or_none(raw.get("reference_timeout"))
    return {
        "enabled": raw.get("enabled") is not False,
        "reference_models": refs,
        "aggregator": _clean_slot(raw.get("aggregator")) or deepcopy(DEFAULT_MOA_AGGREGATOR),
        "reference_temperature": _coerce_float_or_none(raw.get("reference_temperature")),
        "aggregator_temperature": _coerce_float_or_none(raw.get("aggregator_temperature")),
        "reference_timeout": timeout if timeout and timeout > 0 else None,
        "degraded_reference_policy": policy if policy in {"loud", "silent"} else "loud",
        "reference_max_tokens": _coerce_int_or_none(raw.get("reference_max_tokens")),
        "fanout": _coerce_fanout(raw.get("fanout")),
    }


def normalize_moa_config(raw) -> dict[str, Any]:
    """具名 presets＋default_preset；老平铺形（顶层直接放槽位）折成 default。"""
    if not isinstance(raw, dict):
        raw = {}
    presets: dict[str, dict[str, Any]] = {}
    presets_raw = raw.get("presets")
    if isinstance(presets_raw, dict):
        for name, preset in presets_raw.items():
            clean = str(name or "").strip()
            if clean:
                presets[clean] = _normalize_preset(preset)
    if not presets:
        presets[DEFAULT_MOA_PRESET_NAME] = _normalize_preset(raw)
    default = str(raw.get("default_preset") or "").strip()
    if not default or default not in presets:
        default = next(iter(presets))
    return {
        "default_preset": default,
        "presets": presets,
        "save_traces": bool(raw.get("save_traces")),
        "trace_dir": str(raw.get("trace_dir") or "").strip() or None,
    }


def load_moa_config(path: str | None = None) -> dict[str, Any]:
    p = os.path.expanduser(path or MOA_CONFIG_PATH)
    try:
        with open(p, encoding="utf-8") as f:
            return normalize_moa_config(json.load(f))
    except (OSError, ValueError):
        return normalize_moa_config({})


def resolve_moa_preset(name: str | None = None, *, config=None) -> tuple[str, dict[str, Any]]:
    cfg = config if config is not None else load_moa_config()
    wanted = str(name or cfg["default_preset"]).strip()
    preset = cfg["presets"].get(wanted)
    if preset is None:
        wanted = cfg["default_preset"]
        preset = cfg["presets"][wanted]
    return wanted, deepcopy(preset)


def slot_label(slot) -> str:
    label = f"{slot.get('provider', '')}:{slot.get('model', '')}"
    effort = str(slot.get("reasoning_effort") or "").strip()
    return f"{label}[reasoning={effort}]" if effort else label


# ── 参谋视图（hermes _reference_messages：全流程压平成纯文本 user/assistant 轮）──


def _flatten_user_content(content) -> str:
    if isinstance(content, str):
        return content
    parts = [p.text for p in content if getattr(p, "type", "") == "text"]
    text = "\n".join(t for t in parts if t)
    if not text.strip() and content:
        return "[用户发来非文本内容（如图片附件）]"
    return text


def _truncate_tool_result(text: str, budget: int = TOOL_RESULT_BUDGET) -> str:
    if not text or len(text) <= budget:
        return text
    half = budget // 2
    return f"{text[:half]}\n[... 省略 {len(text) - 2 * half} 字符 ...]\n{text[-half:]}"


def advisory_view(messages) -> list[dict[str, str]]:
    """typed Message 序列 → 纯文本 user/assistant 视图（零 tool 轮零 tool_calls）。

    hermes 不变量：动作（工具调用）全保留成 `[调用工具: …]` 行；工具结果 head+tail
    预览折进**前一条** assistant；thinking 丢弃；空轮丢弃；必以 user 收尾
    （不够就补合成审题轮，绝不删行动者最新上下文）。
    """
    rendered: list[dict[str, str]] = []
    last_user = None
    for m in messages:
        role = getattr(m, "role", None)
        if role == "user":
            text = _flatten_user_content(m.content)
            if not text.strip():
                continue
            last_user = text
            rendered.append({"role": "user", "content": text})
        elif role == "assistant":
            parts: list[str] = []
            for c in m.content:
                if c.type == "text" and c.text.strip():
                    parts.append(c.text.strip())
                elif c.type == "toolCall":
                    try:
                        args = json.dumps(c.arguments, ensure_ascii=False)
                    except (TypeError, ValueError):
                        args = str(c.arguments)
                    parts.append(f"[调用工具: {c.name}({args})]")
            if parts:
                rendered.append({"role": "assistant", "content": "\n".join(parts)})
        elif role == "toolResult":
            text = _truncate_tool_result(_flatten_user_content(m.content))
            block = f"[工具结果: {text}]"
            if rendered and rendered[-1]["role"] == "assistant":
                rendered[-1]["content"] += "\n" + block
            else:
                rendered.append({"role": "assistant", "content": block})
    if rendered and rendered[-1]["role"] == "assistant":
        rendered.append({"role": "user", "content": ADVISORY_INSTRUCTION})
    if not rendered and last_user is not None:
        rendered = [{"role": "user", "content": last_user}]
    return rendered


def trim_view_for_window(view, context_window: int, *, reserve: int | None = None):
    """按参谋自己的上下文窗修剪（hermes _trim_messages_for_reference）。

    misaka 的窗就在 Model.contextWindow 上，无须 hermes 那套元数据探测/缓存。
    不变量：估算 chars/4；丢最老；剩余首轮必须是 user；尾轮＋至少一前轮必留。
    """
    if not view or context_window <= 0:
        return view
    reserve = reserve if reserve and reserve > 0 else REFERENCE_DEFAULT_OUTPUT_RESERVE
    budget = int(context_window * (1.0 - TRIM_SAFETY_FRACTION)) - reserve
    if budget <= 0:
        return view

    def estimate(msgs):
        return (len(REFERENCE_SYSTEM_PROMPT) + sum(len(m["content"]) for m in msgs)) // 4

    body = list(view)
    while len(body) > 2 and estimate(body) > budget:
        body.pop(0)
        while len(body) > 2 and body[0]["role"] == "assistant":
            body.pop(0)
    while len(body) > 1 and body[0]["role"] == "assistant":
        body.pop(0)
    return body


# ── 槽位模型解析（core 注入，尊重分层：ai 不许 import core） ─────────────────

_MODEL_RESOLVER: Callable[[str, str], Model | None] | None = None


def set_model_resolver(fn: Callable[[str, str], Model | None]) -> None:
    global _MODEL_RESOLVER
    _MODEL_RESOLVER = fn


def _resolve_slot_model(slot) -> Model | None:
    if _MODEL_RESOLVER is None:
        return None
    try:
        return _MODEL_RESOLVER(str(slot.get("provider") or ""), str(slot.get("model") or ""))
    except Exception:  # noqa: BLE001 - 解析失败＝槽位便签，不炸整轮
        return None


# ── 扇出与聚合 ───────────────────────────────────────────────────────────


def _typed_view(view) -> list[Any]:
    now = time.time_ns() // 1_000_000
    out: list[Any] = []
    for m in view:
        if m["role"] == "user":
            out.append(UserMessage(content=m["content"], timestamp=now))
        else:
            out.append(AssistantMessage(
                content=[TextContent(text=m["content"])],
                api="moa", provider="moa", model="advisory-view",
                usage=_zero_usage(), stopReason="stop", timestamp=now))
    return out


def _zero_usage() -> Usage:
    return Usage(input=0, output=0, cacheRead=0, cacheWrite=0, totalTokens=0,
                 cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0))


def _message_text(message: AssistantMessage) -> str:
    return "\n".join(c.text for c in message.content if c.type == "text").strip()


async def _run_reference(slot, view, *, preset, options) -> tuple[str, str, Usage | None]:
    """调一个参谋：(label, text, usage)。永不 raise——失败＝[failed: …] 便签
    （hermes _run_reference 契约：聚合官拿部分意见照样行动）。"""
    from misaka.ai.stream import complete_simple

    label = slot_label(slot)
    m = _resolve_slot_model(slot)
    if m is None:
        return label, f"[failed: 槽位模型不在册 {label}]", None
    reserve = slot.get("max_tokens") or preset.get("reference_max_tokens")
    trimmed = trim_view_for_window(view, m.contextWindow, reserve=reserve)
    ctx = Context(systemPrompt=REFERENCE_SYSTEM_PROMPT, messages=_typed_view(trimmed))
    timeout = preset.get("reference_timeout")
    opts = SimpleStreamOptions(
        temperature=preset.get("reference_temperature"),
        maxTokens=reserve,
        # 参谋思考深度只认槽位钉的档，不继承会话全局（hermes：全局 xhigh 继承进
        # 每个参谋会静默放大开销）
        reasoning=slot.get("reasoning_effort"),
        timeoutMs=int(timeout * 1000) if timeout else None,
        signal=getattr(options, "signal", None),
        sessionId=getattr(options, "sessionId", None),
        cacheRetention=getattr(options, "cacheRetention", None),
        apiKey=None,
    )
    try:
        msg = await complete_simple(m, ctx, opts)
    except Exception as exc:  # noqa: BLE001 - 单参谋失败不许炸整轮
        return label, f"[failed: {exc}]", None
    if msg.stopReason == "error":
        return label, f"[failed: {msg.errorMessage or '未知错误'}]", msg.usage
    return label, _message_text(msg) or "(空响应)", msg.usage


def _is_failed(text: str) -> bool:
    s = text.lstrip().lower()
    return s.startswith("[failed:") or s.startswith("[skipped:")


def build_guidance(preset_name, preset, outputs) -> str | None:
    """参谋产出 → 注入聚合官的指导块（hermes create() 中段的忠实版）。"""
    ok = [(label, text) for label, text, _u in outputs if not _is_failed(text)]
    failed = [label for label, text, _u in outputs if _is_failed(text)]
    degraded = ""
    if failed and preset.get("degraded_reference_policy") != "silent":
        degraded = f"[参谋不可用：{', '.join(failed)}]"
    agg_label = slot_label(preset["aggregator"])
    if outputs and not ok:
        if not degraded:
            return None
        return ("〔MoA 参谋上下文〕\n"
                f"Preset：{preset_name}\n聚合官/行动模型：{agg_label}\n\n"
                "本轮全部参谋失败——没有参考意见，凭你自己的判断行动。\n\n" + degraded)
    if not ok and not degraded:
        return None
    joined = "\n\n".join(f"参谋 {i} — {label}:\n{text}" for i, (label, text) in enumerate(ok, 1))
    if degraded:
        joined = f"{joined}\n\n{degraded}" if joined else degraded
    return ("〔MoA 参谋上下文〕\n"
            f"Preset：{preset_name}\n聚合官/行动模型：{agg_label}\n"
            f"参谋：{', '.join(label for label, _ in ok)}\n\n"
            "下面的参谋意见是给你的私下参考。你是聚合官也是行动模型：照常直接回答"
            "用户或调用工具。\n\n" + joined)


def attach_guidance(messages: list[Any], guidance: str) -> list[Any]:
    """指导块附在聚合官 prompt **末尾**（hermes _attach_reference_guidance：
    并进尾部 user 轮——字符串/分段两种形都并；不然单开一条 user 轮）。
    附在末尾＝前缀稳定＝KV 缓存可复用；返回新列表不动原件。"""
    now = time.time_ns() // 1_000_000
    if messages and getattr(messages[-1], "role", None) == "user":
        last = messages[-1]
        if isinstance(last.content, str):
            merged = last.model_copy(update={"content": last.content + "\n\n" + guidance})
        else:
            merged = last.model_copy(update={
                "content": [*last.content, TextContent(text="\n\n" + guidance)]})
        return [*messages[:-1], merged]
    return [*messages, UserMessage(content=guidance, timestamp=now)]


# ── 节奏档缓存（hermes create() 的 turn-scoped cache＋every_n 记拍） ────────

_TURN_STATE: dict[tuple[str, str], dict[str, Any]] = {}


def _hash_view(view) -> str:
    return hashlib.sha256(
        "\u0000".join(f"{m['role']}:{m['content']}" for m in view).encode("utf-8", "replace")
    ).hexdigest()


def _cadence_decision(state, preset, view) -> tuple[bool, str]:
    """返回 (是否复用缓存意见, 本次签名)。user_turn：签名只算到最后一条**真** user
    轮（合成审题轮不算），同轮工具迭代全命中；per_iteration：全量签名，逢变必跑；
    every_n:N：每轮第 1 拍在拍上，之后每 N 拍在拍上，拍间复用上次意见。"""
    fanout = preset.get("fanout") or "user_turn"
    every_n = 0
    if fanout.startswith("every_n:"):
        try:
            every_n = int(fanout.split(":", 1)[1])
        except (TypeError, ValueError):
            every_n = 0
        if every_n < 2:
            fanout = "per_iteration"

    turn_prefix = view
    if fanout == "user_turn" or every_n >= 2:
        last_user_idx = None
        for i in range(len(view) - 1, -1, -1):
            if view[i]["role"] == "user" and view[i]["content"] != ADVISORY_INSTRUCTION:
                last_user_idx = i
                break
        if last_user_idx is not None:
            turn_prefix = view[: last_user_idx + 1]

    sig = _hash_view(turn_prefix if fanout == "user_turn" else view)
    if every_n >= 2:
        turn_sig = _hash_view(turn_prefix)
        if turn_sig != state.get("turn_sig"):
            state["turn_sig"] = turn_sig
            state["iteration"] = 0
            state["last_state_sig"] = None
        state_sig = _hash_view(view)
        if state_sig != state.get("last_state_sig"):
            state["last_state_sig"] = state_sig
            state["iteration"] = state.get("iteration", 0) + 1
        on_cadence = (state["iteration"] - 1) % every_n == 0
        if not on_cadence and state.get("outputs"):
            return True, state.get("sig") or sig
        return False, sig
    reuse = sig == state.get("sig") and bool(state.get("outputs"))
    return reuse, sig


# ── trace 落盘（hermes moa_trace.py：opt-in，旁路文件，绝不进消息史） ────────


def _save_trace(cfg, session_id, preset_name, outputs, agg_slot, guidance) -> None:
    if not cfg.get("save_traces"):
        return
    try:
        base = os.path.expanduser(cfg.get("trace_dir") or "~/.misaka/moa-traces")
        os.makedirs(base, exist_ok=True)
        sid = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(session_id or "unknown-session"))
        record = {
            "ts": time.time(),
            "session_id": session_id,
            "preset": preset_name,
            "references": [
                {"label": label, "output": text,
                 "usage": usage.model_dump() if usage else None}
                for label, text, usage in outputs
            ],
            "aggregator": {"label": slot_label(agg_slot), "guidance_attached": bool(guidance)},
        }
        with open(os.path.join(base, f"{sid}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 - trace 绝不许弄断一轮
        logger.debug("MoA trace write failed: %s", exc)


# ── 虚拟服务商流函数（引擎插口） ─────────────────────────────────────────


def _fold_usage(base: Usage, extras: list[Usage | None], advisor_models: list[Model | None]) -> Usage:
    """参谋花费叠进聚合官消息的 usage——参谋按**各自模型**的价计费后加总
    （hermes：把参谋 token 折进聚合官价目会算错每一个参谋）。"""
    from misaka.ai.models import calculate_cost

    total = base.model_copy(deep=True)
    for usage, m in zip(extras, advisor_models):
        if usage is None:
            continue
        cost = usage.cost
        if m is not None and not (cost.total or 0):
            try:
                cost = calculate_cost(m, usage.model_copy(deep=True))
            except Exception:  # noqa: BLE001 - 计价失败不拦记账
                cost = usage.cost
        total.input += usage.input
        total.output += usage.output
        total.cacheRead += usage.cacheRead
        total.cacheWrite += usage.cacheWrite
        total.totalTokens += usage.totalTokens
        total.cost.input += cost.input
        total.cost.output += cost.output
        total.cost.cacheRead += cost.cacheRead
        total.cost.cacheWrite += cost.cacheWrite
        total.cost.total += cost.total
    return total


def stream_simple_moa(model: Model, context: Context, options: SimpleStreamOptions | None = None) -> AssistantMessageEventStream:
    """api="moa" 的 streamSimple：扇出参谋 → 意见附尾 → 聚合官真身行动。"""
    from misaka.ai.stream import stream_simple

    outer = AssistantMessageEventStream()
    opts = options or SimpleStreamOptions()

    async def run() -> None:
        try:
            cfg = load_moa_config()
            preset_name, preset = resolve_moa_preset(model.id, config=cfg)
            refs = [s for s in preset["reference_models"] if s.get("enabled", True)]
            if not preset.get("enabled", True):
                # 禁用的 preset＝直接用聚合官（hermes：disabled 就是"聚合官单干"）
                refs = []

            view = advisory_view(context.messages)
            session_key = (str(getattr(opts, "sessionId", None) or "no-session"), preset_name)
            state = _TURN_STATE.setdefault(session_key, {})
            reuse, sig = _cadence_decision(state, preset, view) if refs else (False, "")

            outputs: list[tuple[str, str, Usage | None]] = []
            advisor_models: list[Model | None] = []
            if refs and reuse:
                # 命中轮复用意见但**零入账**（hermes：cache HIT 不再折参谋花费，
                # 否则参谋开销会按工具迭代数翻倍）
                outputs = [(label, text, None) for label, text, _u in state["outputs"]]
                advisor_models = [None] * len(outputs)
            elif refs:
                sem = asyncio.Semaphore(MAX_REFERENCE_CONCURRENCY)

                async def guarded(slot):
                    async with sem:
                        return await _run_reference(slot, view, preset=preset, options=opts)

                outputs = list(await asyncio.gather(*(guarded(s) for s in refs)))
                advisor_models = [_resolve_slot_model(s) for s in refs]
                state["sig"] = sig
                state["outputs"] = list(outputs)
                _save_trace(cfg, getattr(opts, "sessionId", None), preset_name,
                            outputs, preset["aggregator"], True)

            guidance = build_guidance(preset_name, preset, outputs) if outputs else None

            agg_slot = preset["aggregator"]
            if str(agg_slot.get("provider", "")).lower() == "moa":
                raise RuntimeError("MoA 聚合官不能又是一个 MoA preset（递归）")
            agg_model = _resolve_slot_model(agg_slot)
            if agg_model is None:
                raise RuntimeError(f"聚合官模型不在册：{slot_label(agg_slot)}")

            agg_messages = list(context.messages)
            if guidance:
                agg_messages = attach_guidance(agg_messages, guidance)
            agg_ctx = Context(systemPrompt=context.systemPrompt, messages=agg_messages,
                              tools=context.tools)
            agg_opts = opts.model_copy(update={
                # 聚合官是行动模型：会话温度照常适用；preset 钉了值则以 preset 为准
                "temperature": preset.get("aggregator_temperature")
                if preset.get("aggregator_temperature") is not None else opts.temperature,
                # 思考深度：槽位钉的档优先，否则像普通行动模型一样继承会话档（#64187 语义）
                "reasoning": agg_slot.get("reasoning_effort") or opts.reasoning,
            })

            inner = stream_simple(agg_model, agg_ctx, agg_opts)
            usages = [u for _l, _t, u in outputs]
            async for event in inner:
                etype = getattr(event, "type", "")
                if etype == "done":
                    event = event.model_copy(update={"message": event.message.model_copy(
                        update={"usage": _fold_usage(event.message.usage, usages, advisor_models)})})
                elif etype == "error":
                    event = event.model_copy(update={"error": event.error.model_copy(
                        update={"usage": _fold_usage(event.error.usage, usages, advisor_models)})})
                outer.push(event)
            outer.end()
        except Exception as exc:  # noqa: BLE001 - 虚拟服务商的失败以引擎错误消息形态上交
            from misaka.ai.types import ErrorEvent
            message = AssistantMessage(
                content=[], api=model.api, provider=model.provider, model=model.id,
                usage=_zero_usage(), stopReason="error", errorMessage=str(exc),
                timestamp=time.time_ns() // 1_000_000)
            outer.push(ErrorEvent(reason="error", error=message))
            outer.end(message)

    asyncio.create_task(run())
    return outer


def stream_moa(model: Model, context: Context, options: StreamOptions | None = None) -> AssistantMessageEventStream:
    simple = SimpleStreamOptions(**options.model_dump()) if options is not None else None
    return stream_simple_moa(model, context, simple)


# ── preset → 注册表模型（core 在 refresh 时调用） ─────────────────────────


def preset_models() -> list[Model]:
    """把 moa.json 的 presets 合成 provider="moa" 的模型行（hermes
    _moa_provider_row 的 pi 形制版：进的是 model registry 不是 picker payload）。
    上下文窗/输出上限抄各自聚合官真身；价目全零——真实花费在流内按真身计入。"""
    cfg = load_moa_config()
    out: list[Model] = []
    for name, preset in cfg["presets"].items():
        agg = _resolve_slot_model(preset["aggregator"])
        out.append(Model(
            id=name, name=f"MoA·{name}", api="moa", provider="moa",
            baseUrl="moa://local", reasoning=True, input=["text", "image"],
            cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            contextWindow=agg.contextWindow if agg else 200_000,
            maxTokens=agg.maxTokens if agg else 32_000,
        ))
    return out


streamMoa = stream_moa
streamSimpleMoa = stream_simple_moa


if __name__ == "__main__":
    # 配置面：递归拒、老平铺折默认、fanout 归一
    cfg = normalize_moa_config({"presets": {
        "x": {"reference_models": [{"provider": "moa", "model": "y"},
                                   {"provider": "p", "model": "m"}],
              "aggregator": {"provider": "p", "model": "agg"},
              "fanout": {"mode": "every_n", "n": 3}}}})
    assert [s["model"] for s in cfg["presets"]["x"]["reference_models"]] == ["m"], "递归槽必须清洗掉"
    assert cfg["presets"]["x"]["fanout"] == "every_n:3"
    flat = normalize_moa_config({"aggregator": {"provider": "p", "model": "a"}})
    assert flat["default_preset"] == "default" and flat["presets"]["default"]["aggregator"]["model"] == "a"
    assert normalize_moa_config({"presets": {"x": {"fanout": "every_n:1"}}})["presets"]["x"]["fanout"] == "per_iteration"

    # 视图面：工具调用成行、结果折进前 assistant、必以 user 收尾
    now = 0
    msgs = [
        UserMessage(content="查一下 X", timestamp=now),
        AssistantMessage(content=[TextContent(text="我看看"),
                                  __import__("misaka.ai.types", fromlist=["ToolCall"]).ToolCall(
                                      id="1", name="read", arguments={"p": "a.md"})],
                         api="a", provider="p", model="m", usage=_zero_usage(),
                         stopReason="toolUse", timestamp=now),
    ]
    from misaka.ai.types import ToolResultMessage
    msgs.append(ToolResultMessage(toolCallId="1", toolName="read",
                                  content=[TextContent(text="Z" * 9000)], isError=False, timestamp=now))
    v = advisory_view(msgs)
    assert v[0]["role"] == "user" and "[调用工具: read(" in v[1]["content"]
    assert "[工具结果:" in v[1]["content"] and "省略" in v[1]["content"], "9000 字符结果必须 head+tail"
    assert v[-1]["role"] == "user" and v[-1]["content"] == ADVISORY_INSTRUCTION, "必以合成 user 轮收尾"

    # 修剪面：丢最老、剩首必 user、尾轮保住
    long_view = [{"role": "user", "content": "老问题" * 500},
                 {"role": "assistant", "content": "老回答" * 500},
                 {"role": "user", "content": "新问题"},
                 {"role": "assistant", "content": "新动作"},
                 {"role": "user", "content": ADVISORY_INSTRUCTION}]
    t = trim_view_for_window(long_view, 800, reserve=512)
    assert t[-1]["content"] == ADVISORY_INSTRUCTION and t[0]["role"] == "user" and len(t) < 5

    # 附尾面：并进尾 user（str）；无尾 user 时单开一条；原列表不动
    base_msgs = [UserMessage(content="任务", timestamp=0)]
    attached = attach_guidance(base_msgs, "〔指导〕")
    assert attached[-1].content.endswith("〔指导〕") and base_msgs[0].content == "任务"
    seg = [UserMessage(content=[TextContent(text="任务")], timestamp=0)]
    attached2 = attach_guidance(seg, "〔指导〕")
    assert attached2[-1].content[-1].text == "\n\n〔指导〕", "分段形要在 cache 标外追加分段"
    tail_asst = [UserMessage(content="任务", timestamp=0),
                 AssistantMessage(content=[TextContent(text="行")], api="a", provider="p",
                                  model="m", usage=_zero_usage(), stopReason="stop", timestamp=0)]
    assert attach_guidance(tail_asst, "G")[-1].content == "G", "尾轮非 user＝单开一条"

    # 节奏面：user_turn 同轮命中、新 user 轮失效；every_n 记拍
    st: dict[str, Any] = {}
    p = {"fanout": "user_turn", "reference_models": [], "aggregator": {}}
    view1 = [{"role": "user", "content": "Q"}]
    reuse, sig = _cadence_decision(st, p, view1)
    assert not reuse
    st["sig"], st["outputs"] = sig, [("l", "t", None)]
    grown = view1 + [{"role": "assistant", "content": "a"}, {"role": "user", "content": ADVISORY_INSTRUCTION}]
    assert _cadence_decision(st, p, grown)[0], "同轮工具迭代必须命中（合成轮不算 user）"
    assert not _cadence_decision(st, p, view1 + [{"role": "user", "content": "新Q"}])[0], "新 user 轮必须失效"

    # 指导面：全败＋loud＝照实说；全败＋silent＝None（聚合官裸跑）
    pl = {"aggregator": {"provider": "p", "model": "a"}, "degraded_reference_policy": "loud"}
    g = build_guidance("x", pl, [("l1", "[failed: x]", None)])
    assert g and "全部参谋失败" in g
    ps = {**pl, "degraded_reference_policy": "silent"}
    assert build_guidance("x", ps, [("l1", "[failed: x]", None)]) is None
    g2 = build_guidance("x", pl, [("l1", "意见甲", None), ("l2", "[failed: y]", None)])
    assert "参谋 1 — l1" in g2 and "[参谋不可用：l2]" in g2
    print("moa selfcheck ok — 配置(递归拒/平铺折/fanout归一)＋视图不变量＋修剪＋附尾三形＋节奏＋降级指导 全过")
