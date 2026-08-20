"""LCM 压缩链主体（hermes-lcm CompactionMixin＋装配段的 misaka 精简版，MIT）。

一轮 compress ＝ ingest（cursor）→ 叶压缩（切最老 chunk → 逃生梯摘要 → D0 节点
带血统）→ 凝聚（fanin 满员逐层上卷）→ 装配（系统提示＋摘要前缀＋保护尾巴）。
状态机：noop/compacted——调用方靠它区分真压缩与空转；一切 no-op 带人话 reason。

上游纪律逐条落位：cursor 是活动列表下标、compress 收尾置 len(compressed)；
首条非 system 消息必须 user（三方 API 抽走 system 后的 400 坑）；被压掉的最新
用户目标以「保留目标」锚进摘要块；chunk 首条永远收（哪怕单条超预算）。

ponytail: 上游的重放 reconcile／占位符账本／债务记账是宿主行为逆向工程，
三期接缝按 misaka 引擎的真实重放行为再定；本件假定调用方在同进程内连续喂
同一活动列表（misaka 引擎正是如此）。
"""
from dataclasses import dataclass, field

from misaka.orchestration.lcm.dag import SummaryDAG, SummaryNode
from misaka.orchestration.lcm.escalation import (
    SummaryCircuitBreaker,
    SummarySpendGuard,
    summarize_with_escalation,
)
from misaka.orchestration.lcm.fresh_tail import resolve_fresh_tail_boundary
from misaka.orchestration.lcm.store import MessageStore
from misaka.orchestration.lcm.tokens import (
    count_message_tokens,
    count_messages_tokens,
    count_tokens,
    normalize_content_value,
)

DEPTH_LABELS = {0: "近期", 1: "全程弧线", 2: "长期"}
SUMMARY_HEADER = "[{label}摘要 (d{depth}, 节点 {node_id})]"
EXPAND_HINT_PREFIX = "展开可见："
PRESERVED_OBJECTIVE_PREFIX = "[保留自被压缩历史的当前用户目标]"


@dataclass
class LCMConfig:
    fresh_tail_count: int = 32
    fresh_tail_max_tokens: int = 0
    leaf_chunk_tokens: int = 20_000
    context_threshold: float = 0.35
    incremental_max_depth: int = 3     # -1=不限；0=只叶
    condensation_fanin: int = 4
    l2_budget_ratio: float = 0.5
    l3_truncate_tokens: int = 512
    max_assembly_tokens: int = 0       # 0=不设硬顶
    summary_timeout: float = 60.0
    spend_max_calls: int = 24
    spend_window_seconds: float = 600.0
    spend_backoff_seconds: float = 1800.0
    breaker_failures: int = 2
    breaker_cooldown_seconds: float = 300.0


@dataclass
class CompressResult:
    messages: list
    status: str                        # "noop" | "compacted"
    reason: str = ""
    leaf_nodes: int = 0
    condensed_nodes: int = 0
    summary_level: int = 0


def _extract_expand_hint(summary):
    for line in reversed((summary or "").splitlines()):
        pos = line.find(EXPAND_HINT_PREFIX)
        if pos >= 0:
            return line[pos + len(EXPAND_HINT_PREFIX):].strip().strip("」。")
    return ""


class LCMCompactor:
    """一个会话一实例可复用（cursor 存 store 元数据，随库持久）。"""

    def __init__(self, db_path, config=None, call_llm=None):
        self.config = config or LCMConfig()
        self.store = MessageStore(db_path)
        self.dag = SummaryDAG(db_path)
        self._call_llm = call_llm      # (prompt, max_tokens, timeout) -> str|None
        self._breaker = SummaryCircuitBreaker(
            self.config.breaker_failures, self.config.breaker_cooldown_seconds)
        self._guard = SummarySpendGuard(
            self.config.spend_max_calls, self.config.spend_window_seconds,
            self.config.spend_backoff_seconds)
        # session → {"cursor": 活动列表下标, "ids": 与活动列表逐位对齐的 store_id}
        self._runtime = {}

    # ── ingest ──────────────────────────────────────────────────────────

    def _state(self, session_id):
        state = self._runtime.get(session_id)
        if state is None:
            state = {"cursor": 0, "ids": []}
            self._runtime[session_id] = state
        return state

    def ingest(self, session_id, messages):
        """cursor 之后的新消息落库；返回与活动列表逐位对齐的 store_id 表。
        cursor 是活动列表下标（上游契约），不是存储偏移。"""
        state = self._state(session_id)
        cursor = min(state["cursor"], len(messages))
        new = messages[cursor:]
        if new:
            state["ids"].extend(self.store.append_batch(session_id, new))
            state["cursor"] = len(messages)
        return state["ids"]

    # ── 判停/触发 ───────────────────────────────────────────────────────

    def should_compress(self, messages, context_window):
        if not context_window:
            return False
        threshold = int(context_window * self.config.context_threshold)
        return count_messages_tokens(messages) >= threshold

    # ── compress ────────────────────────────────────────────────────────

    def compress(self, session_id, messages, *, focus_topic=None):
        if not messages:
            return CompressResult(messages, "noop", "空消息列表")
        ids = self.ingest(session_id, messages)

        leading_anchor = 1 if messages[0].get("role") == "system" else 0
        boundary = resolve_fresh_tail_boundary(
            messages, fresh_tail_count=self.config.fresh_tail_count,
            fresh_tail_max_tokens=self.config.fresh_tail_max_tokens)
        tail_start = boundary.start
        # 上轮 assemble 注入的摘要条 id=None（已压区）：它是压缩产物，血统归 DAG
        # 凝聚管，绝不再进叶压缩——否则「摘要的摘要」会顶着错误 source_ids 进 D0
        #（审查 2026-08-20 实弹复现的血统错位，根因是旧版收尾清空 ids 破坏对齐）。
        start = leading_anchor
        while start < len(ids) and ids[start] is None:
            start += 1
        if tail_start <= start:
            return CompressResult(messages, "noop", "保护尾巴之外没有可压缩的原始积压")

        candidate = messages[start:tail_start]
        candidate_ids = ids[start:tail_start]

        # 叶压缩：切最老 chunk（首条永远收，哪怕单条超预算——上游契约）
        chunk, chunk_ids, used = [], [], 0
        for msg, sid_ in zip(candidate, candidate_ids):
            tokens = count_message_tokens(msg)
            if chunk and used + tokens > self.config.leaf_chunk_tokens:
                break
            chunk.append(msg)
            chunk_ids.append(sid_)
            used += tokens
        if not chunk:
            return CompressResult(messages, "noop", "没有可选的叶压缩块")

        summary, level = self._summarize(chunk, used, depth=0,
                                         focus_topic=focus_topic)
        earliest, latest = self.store.get_time_bounds(chunk_ids)
        node = SummaryNode(
            session_id=session_id, depth=0, summary=summary,
            token_count=count_messages_tokens([{"content": summary}]),
            source_token_count=used, source_ids=chunk_ids,
            source_type="messages", earliest_at=earliest, latest_at=latest,
            expand_hint=_extract_expand_hint(summary))
        self.dag.add_node(node)

        condensed = self._maybe_condense(session_id, focus_topic)

        remaining = messages[start + len(chunk):]   # 旧摘要条不留——frontier 全量重渲染
        compressed = self.assemble(
            messages[0] if leading_anchor else None, session_id, remaining,
            anchor_source=chunk)   # 锚来自被压掉的段（仍在场的不需要锚）
        state = self._state(session_id)
        state["cursor"] = len(compressed)     # 上游纪律：收尾置 len(compressed)
        # 对齐不变量（ingest 的契约）：ids 与活动列表逐位对齐。compressed=
        # [system?][摘要条 0/1 条][remaining]——摘要条无源 id 记 None，其余保真。
        n_summary = len(compressed) - len(remaining) - leading_anchor
        state["ids"] = (ids[:leading_anchor] + [None] * n_summary
                        + ids[start + len(chunk):])
        return CompressResult(compressed, "compacted",
                              leaf_nodes=1, condensed_nodes=condensed,
                              summary_level=level)

    def _summarize(self, chunk, source_tokens, *, depth, focus_topic):
        lines = []
        if focus_topic:
            lines.append(f"（近期用户关注：{focus_topic}）")
        for msg in chunk:
            content = normalize_content_value(msg.get("content"))[:3000]
            lines.append(f"[{msg.get('role', '?')}] {content}")
        budget = min(max(2000, int(source_tokens * (0.20 if depth == 0 else 0.40))),
                     12_000)
        if self._call_llm is None:
            from misaka.orchestration.lcm.escalation import deterministic_truncate
            return deterministic_truncate("\n".join(lines),
                                          self.config.l3_truncate_tokens), 3
        return summarize_with_escalation(
            "\n".join(lines), source_tokens=source_tokens, token_budget=budget,
            call_llm=self._call_llm, l2_budget_ratio=self.config.l2_budget_ratio,
            l3_truncate_tokens=self.config.l3_truncate_tokens,
            timeout=self.config.summary_timeout,
            circuit_breaker=self._breaker, spend_guard=self._guard)

    def _maybe_condense(self, session_id, focus_topic):
        """逐层上卷：某层未凝聚节点 ≥ fanin 就取前 fanin 个压成 depth+1。"""
        max_depth = self.config.incremental_max_depth
        if max_depth == 0:
            return 0
        fanin = max(2, self.config.condensation_fanin)
        condensed = 0
        depth = 0
        while max_depth < 0 or depth < max_depth:
            nodes = self.dag.get_uncondensed_at_depth(session_id, depth)
            if len(nodes) < fanin:
                depth += 1
                if depth > 8:          # ponytail: 深度铁栅，防配置为 -1 时意外深井
                    break
                continue
            group = nodes[:fanin]
            combined = "\n\n---\n\n".join(n.summary for n in group)
            source_tokens = sum(n.token_count for n in group) or 1
            summary, _ = self._summarize(
                [{"role": "summary", "content": combined}], source_tokens,
                depth=depth + 1, focus_topic=focus_topic)
            earliest = min((n.earliest_at or n.created_at for n in group),
                           default=None)
            latest = max((n.latest_at or n.created_at for n in group), default=None)
            self.dag.add_node(SummaryNode(
                session_id=session_id, depth=depth + 1, summary=summary,
                token_count=count_messages_tokens([{"content": summary}]),
                source_token_count=source_tokens,
                source_ids=[n.node_id for n in group], source_type="nodes",
                earliest_at=earliest, latest_at=latest,
                expand_hint=_extract_expand_hint(summary)))
            condensed += 1
        return condensed

    # ── 宿主 compaction 形态（三期接缝：引擎定切点，LCM 消化被压段产摘要）──

    def compact_for_host(self, session_id, messages, *, previous_summary=None,
                         focus_topic=None):
        """被压段落库→多叶循环→凝聚→前沿渲染。返回 (summary_text, CompressResult)。
        previous_summary＝切换 LCM 前引擎原生压缩的存量摘要，首见时收编为 D0 承接。"""
        if previous_summary and not self.dag.get_session_nodes(session_id, limit=1):
            self.dag.add_node(SummaryNode(
                session_id=session_id, depth=0, summary=previous_summary,
                token_count=count_tokens(previous_summary), source_ids=[],
                source_type="messages", expand_hint="切换 LCM 前的原生压缩摘要"))
        ids = self.store.append_batch(session_id, messages)
        pos, leaves, level_max = 0, 0, 0
        while pos < len(messages):
            chunk, chunk_ids, used = [], [], 0
            while pos < len(messages):
                tokens = count_message_tokens(messages[pos])
                if chunk and used + tokens > self.config.leaf_chunk_tokens:
                    break
                chunk.append(messages[pos])
                chunk_ids.append(ids[pos])
                used += tokens
                pos += 1
            summary, level = self._summarize(chunk, used, depth=0,
                                             focus_topic=focus_topic)
            earliest, latest = self.store.get_time_bounds(chunk_ids)
            self.dag.add_node(SummaryNode(
                session_id=session_id, depth=0, summary=summary,
                token_count=count_tokens(summary), source_token_count=used,
                source_ids=chunk_ids, source_type="messages",
                earliest_at=earliest, latest_at=latest,
                expand_hint=_extract_expand_hint(summary)))
            leaves += 1
            level_max = max(level_max, level)
        condensed = self._maybe_condense(session_id, focus_topic)
        text = self.render_frontier(session_id,
                                    anchor=self._latest_user_anchor(messages, []))
        return text, CompressResult([], "compacted", leaf_nodes=leaves,
                                    condensed_nodes=condensed,
                                    summary_level=level_max)

    def render_frontier(self, session_id, *, anchor=None):
        """摘要前沿 → 单块文本（宿主 summary 与 assemble 共用同一渲染）。"""
        parts = [anchor] if anchor else []
        for node in self.dag.frontier_nodes(session_id):
            label = DEPTH_LABELS.get(node.depth, f"深度{node.depth}")
            part = (SUMMARY_HEADER.format(label=label, depth=node.depth,
                                          node_id=node.node_id)
                    + f"\n{node.summary}")
            if node.expand_hint:
                part += f"\n[{EXPAND_HINT_PREFIX}{node.expand_hint}]"
            parts.append(part)
        return "\n\n---\n\n".join(parts)

    # ── 装配 ────────────────────────────────────────────────────────────

    def assemble(self, system_msg, session_id, tail_messages, *, anchor_source=None):
        """[系统提示?] + [单条摘要消息] + [保护尾巴]。
        首条非 system 必须 user（上游 400 坑）；预算超顶先裁摘要段。"""
        result = [dict(system_msg)] if system_msg else []
        anchor = self._latest_user_anchor(anchor_source or [], tail_messages)
        rendered = self.render_frontier(session_id, anchor=anchor)
        parts = rendered.split("\n\n---\n\n") if rendered else []
        if parts:
            summary_role = "user" if (not result or result[-1].get("role") == "system"
                                      ) else "assistant"
            if tail_messages and summary_role == "assistant" \
                    and tail_messages[0].get("role") == "assistant":
                summary_role = "user"
            result.append({"role": summary_role,
                           "content": "\n\n---\n\n".join(parts)})
        result.extend(tail_messages)

        cap = self.config.max_assembly_tokens
        if cap and count_messages_tokens(result) > cap and parts:
            # 超顶先剔保留目标之外裁不动的场面话：逐段丢最老摘要段
            while len(parts) > 1 and count_messages_tokens(result) > cap:
                drop = next((i for i, p in enumerate(parts)
                             if not p.startswith(PRESERVED_OBJECTIVE_PREFIX)), None)
                if drop is None:
                    break
                parts.pop(drop)
                summary_index = 1 if system_msg else 0
                result[summary_index]["content"] = "\n\n---\n\n".join(parts)
        return result

    @staticmethod
    def _latest_user_anchor(compacted_messages, tail_messages):
        """被压掉段里最新的真实用户消息 → 保留目标锚（已在尾巴里就不重复）。"""
        tail_user_texts = {normalize_content_value(m.get("content"))
                           for m in tail_messages if m.get("role") == "user"}
        for msg in reversed(compacted_messages):
            if msg.get("role") != "user":
                continue
            text = normalize_content_value(msg.get("content")).strip()
            if not text or text in tail_user_texts:
                return None
            return f"{PRESERVED_OBJECTIVE_PREFIX}\n{text[:2000]}"
        return None

    def close(self):
        self.dag.close()
        self.store.close()


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    def fake_llm(prompt, max_tokens, timeout):
        return "决定：核对入藏簿原件。展开可见：搬迁记录逐条细节"

    cfg = LCMConfig(fresh_tail_count=3, leaf_chunk_tokens=200,
                    condensation_fanin=2, incremental_max_depth=2)
    c = LCMCompactor(Path(tempfile.mkdtemp()) / "lcm.db", cfg, call_llm=fake_llm)
    sid = "s1"
    msgs = [{"role": "system", "content": "你是研究员"}]
    msgs += [{"role": "user" if i % 2 == 0 else "assistant",
              "content": f"第{i}轮讨论搬迁记录" + "内容" * 30} for i in range(10)]

    assert not c.should_compress(msgs[:2], 100_000)
    assert c.should_compress(msgs, 1_000)

    r = c.compress(sid, msgs)
    assert r.status == "compacted" and r.leaf_nodes == 1 and r.summary_level == 1
    out = r.messages
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user", "system 之后首条必须 user（400 坑）"
    assert "近期摘要 (d0" in out[1]["content"] and "展开可见" in out[1]["content"]
    assert "保留自被压缩历史的当前用户目标" in out[1]["content"], "最新用户目标要锚住"
    assert out[-3:] == msgs[-3:], "保护尾巴原样"
    assert c.store.get_session_count(sid) == len(msgs), "先落库后压缩（无损）"
    node = c.dag.get_session_nodes(sid, depth=0)[0]
    assert node.source_ids and node.expand_hint == "搬迁记录逐条细节"
    walked = c.dag.source_message_ids(node.node_id, limit=100)
    assert len(walked) == len(node.source_ids), "血统可下钻"

    # 连续压缩逼出凝聚（fanin=2）
    active = r.messages
    for i in range(3):
        active = active + [{"role": "user", "content": f"追问{i}" + "字" * 200},
                           {"role": "assistant", "content": f"答{i}" + "字" * 200}]
        active = c.compress(sid, active).messages
    # 血统真值（审查 2026-08-20 实弹复现过错位）：第二轮起每个 D0 的 source_ids
    # 反查 store 必须是真实被压的对话原文——绝不许是上轮的摘要条
    for node in c.dag.get_session_nodes(sid, depth=0):
        rows = c.store._conn.execute(
            "SELECT content FROM messages WHERE store_id IN (%s)"
            % ",".join("?" * len(node.source_ids)), node.source_ids).fetchall()
        assert rows and all("近期摘要 (d" not in row[0] for row in rows), \
            f"血统指向了摘要条而非原文：node {node.node_id}"
    later = [n for n in c.dag.get_session_nodes(sid, depth=0)
             if any("追问" in row[0] or "答" in row[0] for row in
                    c.store._conn.execute(
                        "SELECT content FROM messages WHERE store_id IN (%s)"
                        % ",".join("?" * len(n.source_ids)), n.source_ids))]
    assert later, "第二轮后的 D0 血统必须命中新消息原文"
    stats = c.dag.get_session_depth_stats(sid)
    assert 1 in stats, f"D0 满 fanin 后必须上卷 D1: {stats}"
    frontier_depths = [n.depth for n in c.dag.frontier_nodes(sid)]
    assert frontier_depths == sorted(frontier_depths, reverse=True), "前沿高层在前"

    # 无 system 开头：装配后首条仍必须 user
    c2 = LCMCompactor(Path(tempfile.mkdtemp()) / "lcm.db", cfg, call_llm=fake_llm)
    r2 = c2.compress("s2", msgs[1:])
    assert r2.messages[0]["role"] == "user"

    # 装配硬顶：裁摘要段不裁尾巴
    cfg3 = LCMConfig(fresh_tail_count=2, leaf_chunk_tokens=200,
                     max_assembly_tokens=60)
    c3 = LCMCompactor(Path(tempfile.mkdtemp()) / "lcm.db", cfg3, call_llm=fake_llm)
    r3 = c3.compress("s3", msgs[1:])
    assert r3.messages[-2:] == msgs[-2:], "预算压顶裁摘要，尾巴不动"

    # 空/无积压 noop
    assert c.compress(sid, []).status == "noop"
    small = [{"role": "user", "content": "短"}]
    c4 = LCMCompactor(Path(tempfile.mkdtemp()) / "lcm.db", cfg, call_llm=fake_llm)
    r4 = c4.compress("s4", small)
    assert r4.status == "noop" and "积压" in r4.reason
    c.close(); c2.close(); c3.close(); c4.close()
    print("lcm compactor selfcheck ok — 无损先行/叶压缩/凝聚上卷/装配规则/noop 全对")
