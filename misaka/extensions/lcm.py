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


_ACTIVE_SESSION = {"id": ""}   # 最近一次压缩的会话号（工具缺省作用域）


def _text(s):
    return {"content": [{"type": "text", "text": s}], "details": {}}


def _render_hit(r):
    role = r.get("role", "?")
    snip = (r.get("snippet") or (r.get("content") or "")[:160]).replace("\n", " ")
    return f"#{r['store_id']} [{role}] {snip}"


def register(harn):
    from typing import Optional

    from pydantic import BaseModel, Field

    from misaka.core.extensions.types import ToolDefinition

    def _register_tool(name, label, description, params_model, execute,
                       snippet=None, guidelines=None):
        async def wrapped(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, params_model) else params_model(**(raw or {}))
            return await execute(args, ctx)
        harn.registerTool(ToolDefinition(
            name=name, label=label, description=description,
            parameters=params_model.model_json_schema(), execute=wrapped,
            promptSnippet=snippet, promptGuidelines=list(guidelines or [])))

    class GrepParams(BaseModel):
        query: str = Field(description="检索词（中文直接写；短语加引号）")
        scope: str = Field("current", description="current＝本会话；all＝全部会话")
        limit: int = Field(10, ge=1, le=50)

    async def lcm_grep(params, ctx):
        c = _compactor()
        sid = None if params.scope == "all" else (_ACTIVE_SESSION["id"] or None)
        rows = c.store.search(params.query, session_id=sid, limit=params.limit)
        nodes = c.dag.search(params.query, session_id=sid, limit=5)
        lines = [_render_hit(r) for r in rows]
        lines += [f"节点 {n.node_id} (d{n.depth}) {n.summary[:120]}" for n in nodes]
        if not lines:
            return _text("没有命中。换更具体的词，或 scope=all 搜全部会话。")
        return _text(f"命中 {len(rows)} 条原文 + {len(nodes)} 个摘要节点：\n"
                     + "\n".join(lines)
                     + "\n\n（#号是 store_id，节点号是 node_id——都可喂给 lcm_expand 看全文）")

    _register_tool(
        "lcm_grep", "搜已压历史",
        "全文检索被压缩进 lcm.db 的历史原文与摘要节点。摘要只是线索，"
        "确切的引用/数字/命令必须 lcm_expand 回原文核对。",
        GrepParams, lcm_grep,
        snippet="检索被压缩的历史原文",
        guidelines=[
            "摘要是回忆线索不是证据：要引用原话、数字、命令时，必须 lcm_expand 钻回原文。",
            "从最窄的检索词开始（FTS 是 AND 语义，别堆同义词），不够再放宽 scope=all。",
        ])

    class ExpandParams(BaseModel):
        store_id: Optional[int] = Field(None, description="消息号（lcm_grep 的 #号）")
        node_id: Optional[int] = Field(None, description="摘要节点号")
        limit: int = Field(10, ge=1, le=50, description="node 模式最多回几条源消息")

    async def lcm_expand(params, ctx):
        c = _compactor()
        if (params.store_id is None) == (params.node_id is None):
            return _text("store_id 和 node_id 二选一。")
        if params.store_id is not None:
            row = c.store.get(params.store_id)
            if row is None:
                return _text(f"没有消息 #{params.store_id}")
            return _text(f"#{row['store_id']} [{row['role']}]\n{row['content']}")
        node = c.dag.get_node(params.node_id)
        if node is None:
            return _text(f"没有节点 {params.node_id}")
        desc = c.dag.describe_subtree(params.node_id)
        if node.source_type == "nodes":
            kids = "\n".join(f"节点 {ch['node_id']} (d{ch['depth']}) "
                             f"[{EXPAND_LABEL(ch)}]" for ch in desc["children"])
            return _text(f"节点 {node.node_id} (d{node.depth}) 的子节点：\n{kids}\n"
                         "\n（继续 lcm_expand(node_id=子节点) 逐层下钻）")
        ids = c.dag.source_message_ids(params.node_id, limit=params.limit)
        rows = c.store.get_batch(ids)
        body = "\n\n".join(f"#{i} [{rows[i]['role']}] {rows[i]['content']}"
                           for i in ids if i in rows)
        more = "" if len(ids) < params.limit else \
            f"\n\n（只回了前 {params.limit} 条；要更多加大 limit）"
        return _text(f"节点 {node.node_id} 的源消息：\n{body}{more}")

    def EXPAND_LABEL(ch):
        return ch.get("expand_hint") or f"{ch.get('token_count', 0)} tok"

    _register_tool(
        "lcm_expand", "钻回原文",
        "从 lcm_grep 命中的消息号/节点号钻回逐字原文（节点逐层下钻到叶子消息）。",
        ExpandParams, lcm_expand, snippet="按号取被压缩内容的逐字原文")

    class LoadParams(BaseModel):
        session_id: Optional[str] = Field(None, description="会话号；缺省＝当前")
        after_store_id: int = Field(0, description="游标（上页最后一条的 #号）")
        limit: int = Field(20, ge=1, le=100)

    async def lcm_load(params, ctx):
        c = _compactor()
        sid = params.session_id or _ACTIVE_SESSION["id"]
        if not sid:
            return _text("还没有已压缩的会话。")
        rows = c.store.load_session_page(sid, after_store_id=params.after_store_id,
                                         limit=params.limit)
        if not rows:
            return _text("没有更多了。")
        body = "\n".join(_render_hit(r) for r in rows)
        return _text(f"{body}\n\n（下一页：after_store_id={rows[-1]['store_id']}）")

    _register_tool(
        "lcm_load_session", "顺序翻历史",
        "按顺序翻某会话被压缩的原文（游标分页，不是搜索——要找内容用 lcm_grep）。",
        LoadParams, lcm_load, snippet="按顺序翻被压缩的会话原文")

    class StatusParams(BaseModel):
        pass

    async def lcm_status(params, ctx):
        c = _compactor()
        sid = _ACTIVE_SESSION["id"]
        if not sid:
            return _text("本会话还没触发过 LCM 压缩。")
        stats = c.dag.get_session_depth_stats(sid)
        depth_line = "｜".join(f"d{d}×{v['count']}({v['tokens']}tok)"
                               for d, v in sorted(stats.items())) or "无节点"
        return _text(f"会话 {sid}：已存原文 {c.store.get_session_count(sid)} 条｜"
                     f"摘要层级 {depth_line}｜前沿 {len(c.dag.frontier_nodes(sid))} 节点")

    _register_tool(
        "lcm_status", "无损存量",
        "看本会话的 LCM 存量：落库原文条数、摘要各层节点、前沿规模。",
        StatusParams, lcm_status, snippet="查看无损上下文存量")

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
            _ACTIVE_SESSION["id"] = session_id   # 回收工具的缺省作用域
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
