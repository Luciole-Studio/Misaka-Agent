"""LCM 上下文引擎挂接（设计 docs/design/lcm.md 三期）。

引擎原生接缝：`session_before_compact` 钩子返回 {"compaction": {...}} 即整体
接管压缩——引擎已算好切点（firstKeptEntryId/被压段/前次摘要），LCM 只消化
被压段（落 lcm.db→叶压缩→凝聚）并产出摘要前缀。handler 异常被 extension
runner 吞掉→自动回落原生压缩（fail-open）；显式逃生舱＝MISAKA_CONTEXT_ENGINE=native。

摘要官＝run_llm_json(raw=True, bare=True) 裸调用（MoA 同口，空人格裸模型）。
与原生压缩同等待遇：花费计入会话自身用量，不进板预算台账（原生也不进）。
"""
import logging
import os

from misaka.config import CFG

logger = logging.getLogger(__name__)

_COMPACTORS = {}   # db_path → LCMCompactor（进程内单例；多会话共享一库按 session 隔离）
SUMMARIZER_ROLE = "lcm-summarizer"


def _compactor():
    from misaka.orchestration.lcm.compactor import LCMCompactor, LCMConfig
    path = os.path.expanduser(CFG.get("lcm_db") or "~/.misaka/lcm.db")
    compactor = _COMPACTORS.get(path)
    if compactor is None:
        compactor = LCMCompactor(path, LCMConfig(), call_llm=_call_llm)
        _COMPACTORS[path] = compactor
    return compactor


def _call_llm(prompt, max_tokens, timeout):
    """摘要官：裸调用（零工具零分身零 JSON 捞取）。失败返回 None→逃生梯降级。"""
    from misaka.extensions.board import worker

    profile = os.path.join(os.path.expanduser(CFG["roles_root"]), SUMMARIZER_ROLE)
    os.makedirs(profile, exist_ok=True)     # 空人格＝裸模型（无 SOUL）
    _obj, text, err = worker.run_llm_json(
        profile, prompt, CFG["provider"], CFG["default_model"],
        timeout=max(60, int(timeout)), raw=True, bare=True)
    return None if err else text


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_store_dict(msg):
    """引擎 AgentMessage → 消息库形状（toolCall 块提成 tool_calls，角色归一）。"""
    role = str(_field(msg, "role") or "unknown")
    content = _field(msg, "content")
    tool_calls = None
    if isinstance(content, list):
        calls = [b for b in content
                 if isinstance(b, dict) and b.get("type") == "toolCall"]
        if calls:
            tool_calls = [{"id": b.get("id"),
                           "function": {"name": b.get("name"),
                                        "arguments": b.get("arguments")}}
                          for b in calls]
    if role == "toolResult":
        role = "tool"
    return {"role": role, "content": content, "tool_calls": tool_calls,
            "tool_call_id": _field(msg, "toolCallId") or _field(msg, "tool_call_id"),
            "timestamp": _field(msg, "timestamp")}


def register(harn):
    async def before_compact(event, ctx):
        if str(CFG.get("context_engine") or "lcm").strip().lower() != "lcm":
            return None                     # 逃生舱：显式走原生
        try:
            prep = _field(event, "preparation")
            raw_messages = list(_field(prep, "messagesToSummarize") or [])
            if not raw_messages:
                return None
            messages = [_to_store_dict(m) for m in raw_messages]
            try:
                session_id = str(ctx.sessionManager.getSessionId())
            except Exception:  # noqa: BLE001 - 无会话号退化到稳定键，不拦压缩
                first = (_field(event, "branchEntries") or [{}])[0]
                session_id = f"anon-{_field(first, 'id') or 'session'}"
            summary, stats = _compactor().compact_for_host(
                session_id, messages,
                previous_summary=_field(prep, "previousSummary"))
            if not summary.strip():
                return None                 # 空摘要不接管，让原生兜底
            details = None
            try:
                from misaka.core.compaction.utils import compute_file_lists
                details = compute_file_lists(_field(prep, "fileOps"))
            except Exception:  # noqa: BLE001 - 文件清单是锦上添花
                details = None
            logger.info("LCM 压缩接管：%s 叶 %s 凝聚（会话 %s）",
                        stats.leaf_nodes, stats.condensed_nodes, session_id)
            return {"compaction": {
                "summary": summary,
                "firstKeptEntryId": _field(prep, "firstKeptEntryId"),
                "tokensBefore": int(_field(prep, "tokensBefore", 0) or 0),
                "details": details,
            }}
        except Exception:  # noqa: BLE001 - fail-open：LCM 任何故障回落原生压缩
            logger.warning("LCM 压缩失败，本轮回落引擎原生压缩", exc_info=True)
            return None

    harn.on("session_before_compact", before_compact)
