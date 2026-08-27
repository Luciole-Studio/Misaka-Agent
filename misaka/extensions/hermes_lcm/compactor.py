"""LCM context compaction and summary-chain construction."""
from dataclasses import dataclass

from misaka.extensions.hermes_lcm.dag import SummaryDAG, SummaryNode
from misaka.extensions.hermes_lcm.escalation import (
    SummaryCircuitBreaker,
    SummarySpendGuard,
    summarize_with_escalation,
)
from misaka.extensions.hermes_lcm.fresh_tail import resolve_fresh_tail_boundary
from misaka.extensions.hermes_lcm.store import MessageStore
from misaka.extensions.hermes_lcm.tokens import (
    count_message_tokens,
    count_messages_tokens,
    count_tokens,
    normalize_content_value,
)

DEPTH_LABELS = {0: "Recent", 1: "Conversation arc", 2: "Long-term"}
SUMMARY_HEADER = "[{label} summary (d{depth}, node {node_id})]"
EXPAND_HINT_PREFIX = "Expandable:"
PRESERVED_OBJECTIVE_PREFIX = "[Current user objective retained from compacted history]"


@dataclass
class LCMConfig:
    fresh_tail_count: int = 32
    fresh_tail_max_tokens: int = 0
    leaf_chunk_tokens: int = 20_000
    context_threshold: float = 0.35
    incremental_max_depth: int = 3     # -1 means unlimited; 0 means leaves only.
    condensation_fanin: int = 4
    l2_budget_ratio: float = 0.5
    l3_truncate_tokens: int = 512
    max_assembly_tokens: int = 0       # 0 means no hard limit.
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
            return line[pos + len(EXPAND_HINT_PREFIX):].strip().strip('".')
    return ""


class LCMCompactor:
    """Compact sessions incrementally while preserving source messages in storage."""

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
        self._runtime = {}

    # ── ingest ──────────────────────────────────────────────────────────

    def _state(self, session_id):
        state = self._runtime.get(session_id)
        if state is None:
            state = {"cursor": 0, "ids": []}
            self._runtime[session_id] = state
        return state

    def ingest(self, session_id, messages):
        """Store messages added after the active-list cursor and return aligned store IDs."""
        state = self._state(session_id)
        cursor = min(state["cursor"], len(messages))
        new = messages[cursor:]
        if new:
            state["ids"].extend(self.store.append_batch(session_id, new))
            state["cursor"] = len(messages)
        return state["ids"]

    # Compression trigger

    def should_compress(self, messages, context_window):
        if not context_window:
            return False
        threshold = int(context_window * self.config.context_threshold)
        return count_messages_tokens(messages) >= threshold

    # Compression

    def compress(self, session_id, messages, *, focus_topic=None):
        if not messages:
            return CompressResult(messages, "noop", "No messages to compress.")
        ids = self.ingest(session_id, messages)

        leading_anchor = 1 if messages[0].get("role") == "system" else 0
        boundary = resolve_fresh_tail_boundary(
            messages, fresh_tail_count=self.config.fresh_tail_count,
            fresh_tail_max_tokens=self.config.fresh_tail_max_tokens)
        tail_start = boundary.start
        start = leading_anchor
        while start < len(ids) and ids[start] is None:
            start += 1
        if tail_start <= start:
            return CompressResult(messages, "noop", "No raw backlog remains before the protected tail.")

        candidate = messages[start:tail_start]
        candidate_ids = ids[start:tail_start]

        # Compress the oldest eligible chunk; always include at least one message.
        chunk, chunk_ids, used = [], [], 0
        for msg, sid_ in zip(candidate, candidate_ids):
            tokens = count_message_tokens(msg)
            if chunk and used + tokens > self.config.leaf_chunk_tokens:
                break
            chunk.append(msg)
            chunk_ids.append(sid_)
            used += tokens
        if not chunk:
            return CompressResult(messages, "noop", "No eligible leaf block to compress.")

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

        remaining = messages[start + len(chunk):]   # Old summary blocks are dropped; the frontier is re-rendered in full.
        compressed = self.assemble(
            messages[0] if leading_anchor else None, session_id, remaining,
            anchor_source=chunk)
        state = self._state(session_id)
        state["cursor"] = len(compressed)     # Upstream contract: cursor ends at len(compressed).
        n_summary = len(compressed) - len(remaining) - leading_anchor
        state["ids"] = (ids[:leading_anchor] + [None] * n_summary
                        + ids[start + len(chunk):])
        return CompressResult(compressed, "compacted",
                              leaf_nodes=1, condensed_nodes=condensed,
                              summary_level=level)

    def _summarize(self, chunk, source_tokens, *, depth, focus_topic):
        lines = []
        if focus_topic:
            lines.append(f"(Recent user focus: {focus_topic})")
        for msg in chunk:
            content = normalize_content_value(msg.get("content"))[:3000]
            lines.append(f"[{msg.get('role', '?')}] {content}")
        budget = min(max(2000, int(source_tokens * (0.20 if depth == 0 else 0.40))),
                     12_000)
        if self._call_llm is None:
            from misaka.extensions.hermes_lcm.escalation import deterministic_truncate
            return deterministic_truncate("\n".join(lines),
                                          self.config.l3_truncate_tokens), 3
        return summarize_with_escalation(
            "\n".join(lines), source_tokens=source_tokens, token_budget=budget,
            call_llm=self._call_llm, l2_budget_ratio=self.config.l2_budget_ratio,
            l3_truncate_tokens=self.config.l3_truncate_tokens,
            timeout=self.config.summary_timeout,
            circuit_breaker=self._breaker, spend_guard=self._guard)

    def _maybe_condense(self, session_id, focus_topic, *, attempt_id=None):
        """Condense unmerged nodes in fan-in groups until the configured depth."""
        max_depth = self.config.incremental_max_depth
        if max_depth == 0:
            return 0
        fanin = max(2, self.config.condensation_fanin)
        condensed = 0
        depth = 0
        while max_depth < 0 or depth < max_depth:
            nodes = self.dag.get_uncondensed_at_depth(
                session_id, depth, attempt_id=attempt_id)
            if len(nodes) < fanin:
                depth += 1
                if depth > 8:
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
                expand_hint=_extract_expand_hint(summary)), attempt_id=attempt_id)
            condensed += 1
        return condensed

    # Host compaction adapter.

    def compact_for_host(self, session_id, messages, *, previous_summary=None,
                         focus_topic=None, source_ids=None, attempt_id=None,
                         first_kept_entry_id=None):
        """Persist compacted messages, build the summary DAG, and render its frontier."""
        if attempt_id:
            self.dag.stage_attempt(attempt_id, session_id, "", first_kept_entry_id)
        if previous_summary and not self.dag.get_session_nodes(
                session_id, limit=1, attempt_id=attempt_id):
            self.dag.add_node(SummaryNode(
                session_id=session_id, depth=0, summary=previous_summary,
                token_count=count_tokens(previous_summary), source_ids=[],
                source_type="messages", expand_hint="Previous native compaction summary"),
                attempt_id=attempt_id)
        ids = (self.store.append_batch(session_id, messages)
               if source_ids is None else list(source_ids))
        if len(ids) != len(messages):
            raise ValueError("source_ids must align with compacted messages")
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
                expand_hint=_extract_expand_hint(summary)), attempt_id=attempt_id)
            leaves += 1
            level_max = max(level_max, level)
        condensed = self._maybe_condense(session_id, focus_topic,
                                          attempt_id=attempt_id)
        text = self.render_frontier(session_id,
                                    anchor=self._latest_user_anchor(messages, []),
                                    attempt_id=attempt_id)
        if attempt_id:
            self.dag.stage_attempt(attempt_id, session_id, text, first_kept_entry_id)
        return text, CompressResult([], "compacted", leaf_nodes=leaves,
                                    condensed_nodes=condensed,
                                    summary_level=level_max)

    def render_frontier(self, session_id, *, anchor=None, attempt_id=None):
        """Render the visible summary frontier as host-ready text."""
        parts = [anchor] if anchor else []
        for node in self.dag.frontier_nodes(session_id, attempt_id=attempt_id):
            label = DEPTH_LABELS.get(node.depth, f"Depth {node.depth}")
            part = (SUMMARY_HEADER.format(label=label, depth=node.depth,
                                          node_id=node.node_id)
                    + f"\n{node.summary}")
            if node.expand_hint:
                part += f"\n[{EXPAND_HINT_PREFIX}{node.expand_hint}]"
            parts.append(part)
        return "\n\n---\n\n".join(parts)

    def commit_attempt(self, attempt_id, host_compaction_id=None):
        return self.dag.commit_attempt(attempt_id, host_compaction_id)

    def discard_attempt(self, attempt_id):
        return self.dag.discard_attempt(attempt_id)

    def reconcile_session(self, session_id, entries):
        return self.dag.reconcile_attempts(session_id, entries)

    # ── Assembly ────────────────────────────────────────────────────────────

    def assemble(self, system_msg, session_id, tail_messages, *, anchor_source=None):
        """Build the host message list: system prompt, summary frontier, then the protected tail."""
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
            # Drop the oldest non-objective excerpts until the result fits.
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
        """Preserve the latest compacted user objective unless it already appears in the tail."""
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
