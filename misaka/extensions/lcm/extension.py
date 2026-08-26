"""Pi-native LCM adapter: durable ingest, staged compaction, bounded recall."""

import logging
import os
import time
import uuid
from datetime import datetime

from misaka.config import CFG

logger = logging.getLogger(__name__)

_COMPACTORS = {}
_SEMANTICS = {}
_PENDING = {}
SUMMARIZER_ROLE = "lcm-summarizer"
MAX_TOOL_TEXT = 64_000


def _compactor():
    from misaka.extensions.lcm.compactor import LCMCompactor, LCMConfig

    path = os.path.expanduser(CFG.get("lcm_db") or "~/.misaka/lcm.db")
    compactor = _COMPACTORS.get(path)
    if compactor is None:
        compactor = LCMCompactor(
            path,
            LCMConfig(summary_timeout=float(CFG.get("lcm_summary_timeout") or 60)),
            call_llm=_call_llm,
        )
        _COMPACTORS[path] = compactor
    return compactor


def _summary_models():
    primary = str(CFG.get("lcm_summary_model") or CFG["default_model"]).strip()
    fallbacks = str(CFG.get("lcm_summary_fallback_models") or "")
    return list(dict.fromkeys([primary, *(m.strip() for m in fallbacks.split(",") if m.strip())]))


def _record_summary_usage(prompt, result, failed):
    try:
        from misaka.extensions.lcm.tokens import count_tokens
        _compactor().store.record_summary_usage(
            input_tokens=count_tokens(prompt), output_tokens=count_tokens(result or ""),
            failed=failed,
        )
    except Exception:
        logger.debug("LCM summary usage accounting failed", exc_info=True)


def _call_llm(prompt, max_tokens, timeout):
    """Bare summarizer with same-provider model fallback and usage estimates."""
    from misaka.platform.session import run_text

    profile = os.path.join(os.path.expanduser(CFG["roles_root"]), SUMMARIZER_ROLE)
    os.makedirs(profile, exist_ok=True)
    provider = str(CFG.get("lcm_summary_provider") or CFG["provider"])
    for model in _summary_models():
        try:
            result = run_text(prompt, profile, provider, model,
                              timeout=max(60, int(timeout)), max_tokens=max_tokens)
        except Exception:
            _record_summary_usage(prompt, None, True)
            logger.warning('LCM summary model %s/%s failed; trying the next fallback.', provider, model,
                           exc_info=True)
            continue
        _record_summary_usage(prompt, result, not bool(result and result.strip()))
        if result and result.strip():
            return result
    return None


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_store_dict(msg):
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
            "tool_name": _field(msg, "toolName") or _field(msg, "tool_name"),
            "timestamp": _field(msg, "timestamp")}


def _entry_message(entry):
    kind = _field(entry, "type")
    if kind == "message":
        msg = _to_store_dict(_field(entry, "message") or {})
    elif kind == "custom_message":
        msg = {"role": "custom", "content": _field(entry, "content")}
    elif kind == "branch_summary":
        msg = {"role": "branchSummary", "content": _field(entry, "summary")}
    else:
        return None
    if not msg.get("timestamp"):
        msg["timestamp"] = _field(entry, "timestamp")
    return msg


def _session_id(ctx, explicit=None):
    if explicit:
        return str(explicit)
    try:
        return str(ctx.sessionManager.getSessionId())
    except Exception:
        return ""


def _sync_ctx(ctx):
    """Idempotently sync committed transcript entries; no background worker."""
    sid = _session_id(ctx)
    if not sid:
        return ""
    try:
        entries = list(ctx.sessionManager.getEntries())
        source = ctx.sessionManager.getSessionFile() or ""
    except Exception:
        return sid
    messages, entry_ids = [], []
    for entry in entries:
        msg = _entry_message(entry)
        entry_id = _field(entry, "id")
        if msg is None or not entry_id:
            continue
        messages.append(msg)
        entry_ids.append(str(entry_id))
    if messages:
        _compactor().store.append_batch(sid, messages, source=source,
                                        host_entry_ids=entry_ids)
    return sid


def _summarized_entry_ids(event, session_id):
    """Reconstruct Pi's exact summarized branch slice and map it to store ids."""
    from misaka.core.compaction.compaction import find_turn_start_index

    prep = _field(event, "preparation")
    entries = list(_field(event, "branchEntries") or [])
    first_kept = _field(prep, "firstKeptEntryId")
    first_index = next((i for i, entry in enumerate(entries)
                        if _field(entry, "id") == first_kept), -1)
    if first_index < 0:
        raise ValueError("firstKeptEntryId is absent from branch")
    previous_index = next((i for i in range(len(entries) - 1, -1, -1)
                           if _field(entries[i], "type") == "compaction"), -1)
    boundary = 0
    if previous_index >= 0:
        previous_first = _field(entries[previous_index], "firstKeptEntryId")
        boundary = next((i for i, entry in enumerate(entries)
                         if _field(entry, "id") == previous_first), previous_index + 1)
    history_end = (find_turn_start_index(entries, first_index, boundary)
                   if _field(prep, "isSplitTurn", False) else first_index)
    host_ids = [str(_field(entry, "id")) for entry in entries[boundary:history_end]
                if _entry_message(entry) is not None and _field(entry, "id")]
    mapped = _compactor().store.get_by_host_entry_ids(session_id, host_ids)
    if len(mapped) != len(host_ids):
        missing = [entry_id for entry_id in host_ids if entry_id not in mapped]
        raise RuntimeError(f"LCM transcript sync missing host entries: {missing[:3]}")
    return [mapped[entry_id] for entry_id in host_ids]


def _semantic_index():
    if str(CFG.get("lcm_retrieval_mode") or "fts").strip().lower() != "hybrid":
        return None
    model = str(CFG.get("lcm_embedding_model") or "").strip()
    if not model:
        from misaka.extensions.lcm.semantic import SemanticUnavailable
        raise SemanticUnavailable("MISAKA_LCM_EMBEDDING_MODEL is not configured.")
    path = os.path.expanduser(CFG.get("lcm_db") or "~/.misaka/lcm.db")
    key = (path, model)
    if key not in _SEMANTICS:
        from misaka.extensions.lcm.semantic import SemanticIndex
        _SEMANTICS[key] = SemanticIndex(path, model)
    return _SEMANTICS[key]


def _merge_nodes(lexical, semantic_hits, limit):
    scores, channels = {}, {}
    for channel, items in (("fts", [node.node_id for node in lexical]),
                           ("vector", [node_id for node_id, _ in semantic_hits])):
        for rank, node_id in enumerate(items, 1):
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (60 + rank)
            channels.setdefault(node_id, set()).add(channel)
    nodes = []
    for node_id in sorted(scores, key=scores.get, reverse=True)[:limit]:
        node = _compactor().dag.get_node(node_id)
        if node:
            node.retrieval = "+".join(sorted(channels[node_id]))
            nodes.append(node)
    return nodes


def _text(value):
    value = str(value)
    if len(value) > MAX_TOOL_TEXT:
        value = value[:MAX_TOOL_TEXT] + "\n[LCM tool output truncated; request a smaller page]"
    return {"content": [{"type": "text", "text": value}], "details": {}}


def _render_hit(row, *, show_session=False):
    role = row.get("role", "?")
    snippet = (row.get("snippet") or (row.get("content") or "")[:180]).replace("\n", " ")
    scope = f" {row.get('session_id')}" if show_session else ""
    return f"#{row['store_id']}{scope} [{role}/{row.get('retrieval', 'scan')}] {snippet}"


def _parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    try:
        return float(raw)
    except ValueError:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


def register(harn):

    from pydantic import BaseModel, Field

    from misaka.core.extensions.types import ToolDefinition

    def add_tool(name, label, description, params_model, execute,
                 snippet=None, guidelines=None):
        async def wrapped(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, params_model) else params_model(**(raw or {}))
            return await execute(args, ctx)
        harn.registerTool(ToolDefinition(
            name=name, label=label, description=description,
            parameters=params_model.model_json_schema(), execute=wrapped,
            promptSnippet=snippet, promptGuidelines=list(guidelines or [])))

    class GrepParams(BaseModel):
        query: str = Field(description="Search terms; quote exact phrases")
        scope: str = Field("current", description="current or all")
        session_id: str | None = None
        source: str | None = None
        role: str | None = None
        time_from: str | None = None
        time_to: str | None = None
        limit: int = Field(10, ge=1, le=50)

    async def lcm_grep(params, ctx):
        current = _sync_ctx(ctx)
        sid = params.session_id or (None if params.scope == "all" else current or None)
        if params.scope != "all" and not sid:
            return _text("No current session_id is available.")
        start, end = _parse_time(params.time_from), _parse_time(params.time_to)
        rows = _compactor().store.search(
            params.query, session_id=sid, source=params.source, role=params.role,
            time_from=start, time_to=end, limit=params.limit, sort="relevance")
        lexical = _compactor().dag.search(
            params.query, session_id=sid, time_from=start, time_to=end,
            limit=params.limit)
        nodes, degraded = lexical, ""
        try:
            semantic = _semantic_index()
            if semantic:
                hits, coverage = semantic.search(
                    params.query, session_id=sid, time_from=start, time_to=end,
                    limit=params.limit)
                nodes = _merge_nodes(lexical, hits, params.limit)
                if not coverage["complete"]:
                    degraded = (f"Semantic index coverage is {coverage['indexed']}/{coverage['total']}; "
                                "returning the available mixed results.")
        except Exception as exc:
            degraded = f"Hybrid retrieval fell back to FTS/LIKE: {exc}"
        lines = [_render_hit(row, show_session=sid is None) for row in rows]
        lines += [f"Node {node.node_id} (d{node.depth}/{getattr(node, 'retrieval', 'fts')}) "
                  f"{node.summary[:180].replace(chr(10), ' ')}" for node in nodes]
        if not lines:
            return _text("No hits. Try more specific terms or use scope=all."
                         + (f"\n{degraded}" if degraded else ""))
        suffix = ("\n" + degraded) if degraded else ""
        return _text(f"Found {len(rows)} original messages and {len(nodes)} summary nodes:\n"
                     + "\n".join(lines)
                     + "\n\nPass a message or node number to lcm_expand; summaries are leads, not evidence."
                     + suffix)

    add_tool(
        "lcm_grep", "Search full history",
        "Search original messages and summary nodes across long-term session memory.",
        GrepParams, lcm_grep, snippet="Search full session history",
        guidelines=["Summaries are leads, not evidence; use lcm_expand to verify quotations, numbers, and commands."],
    )

    class ExpandParams(BaseModel):
        store_id: int | None = None
        node_id: int | None = None
        source_offset: int = Field(0, ge=0)
        source_limit: int = Field(10, ge=1, le=50)
        content_offset: int = Field(0, ge=0)
        content_limit: int = Field(8000, ge=1, le=16000)

    async def lcm_expand(params, ctx):
        _sync_ctx(ctx)
        if (params.store_id is None) == (params.node_id is None):
            return _text("Provide exactly one of store_id or node_id.")
        compactor = _compactor()
        if params.store_id is not None:
            row = compactor.store.get(params.store_id)
            if row is None:
                return _text(f"Message #{params.store_id} was not found.")
            content = row.get("content") or ""
            start = min(params.content_offset, len(content))
            end = min(len(content), start + params.content_limit)
            next_cursor = end if end < len(content) else None
            return _text(
                f"#{row['store_id']} [{row['role']}] session={row['session_id']} "
                f"entry={row.get('host_entry_id') or '-'} source={row.get('source') or '-'}\n"
                f"content[{start}:{end}]/{len(content)}\n{content[start:end]}\n"
                f"next_content_offset={next_cursor}")
        node = compactor.dag.get_node(params.node_id)
        if node is None:
            return _text(f"Summary node {params.node_id} was not found.")
        if node.source_type == "nodes":
            desc = compactor.dag.describe_subtree(
                params.node_id, offset=params.source_offset, limit=params.source_limit)
            children = "\n".join(
                f"node {child['node_id']} (d{child['depth']}) "
                f"[{child.get('expand_hint') or str(child.get('token_count', 0)) + ' tok'}]"
                for child in desc["children"]
            )
            return _text(
                f"node {node.node_id} children[{params.source_offset}:]:\n{children}\n"
                f"next_source_offset={desc['next_offset']}"
            )
        total = compactor.dag.source_message_count(params.node_id)
        ids = compactor.dag.source_message_ids(
            params.node_id, offset=params.source_offset, limit=params.source_limit)
        rows = compactor.store.get_batch(ids)
        body = "\n".join(_render_hit(rows[store_id]) for store_id in ids if store_id in rows)
        next_cursor = params.source_offset + len(ids)
        if next_cursor >= total:
            next_cursor = None
        return _text(
            f"node {node.node_id} source messages "
            f"{params.source_offset}:{params.source_offset + len(ids)}/{total}:\n"
            f"{body}\nnext_source_offset={next_cursor}"
        )

    add_tool(
        "lcm_expand", "Expand exact history",
        "Load an original message verbatim or inspect a summary node's source lineage with cursors.",
        ExpandParams, lcm_expand, snippet="Expand exact history by ID",
    )

    class LoadParams(BaseModel):
        session_id: str | None = None
        after_store_id: int = Field(0, ge=0)
        source: str | None = None
        time_from: str | None = None
        time_to: str | None = None
        limit: int = Field(20, ge=1, le=100)

    async def lcm_load(params, ctx):
        current = _sync_ctx(ctx)
        sid = params.session_id or current
        if not sid:
            return _text('Missing session_id.')
        rows = _compactor().store.load_session_page(
            sid, after_store_id=params.after_store_id, source=params.source,
            time_from=_parse_time(params.time_from), time_to=_parse_time(params.time_to),
            limit=params.limit)
        if not rows:
            return _text("No more messages in this session.")
        body = "\n".join(_render_hit(row) for row in rows)
        return _text(f"{body}\nnext_after_store_id={rows[-1]['store_id']}")

    add_tool(
        "lcm_load_session", "Load session history",
        "Read a session's original messages in store-ID order with a stable cursor.",
        LoadParams, lcm_load, snippet="Scroll session history in sequence",
    )

    class RecentParams(BaseModel):
        scope: str = Field("current", description="current or all")
        session_id: str | None = None
        source: str | None = None
        hours: float = Field(24.0, gt=0, le=24 * 3650)
        time_from: str | None = None
        time_to: str | None = None
        limit: int = Field(20, ge=1, le=100)

    async def lcm_recent(params, ctx):
        current = _sync_ctx(ctx)
        sid = params.session_id or (None if params.scope == "all" else current or None)
        if params.scope != "all" and not sid:
            return _text("No current session_id is available.")
        end = _parse_time(params.time_to) or time.time()
        start = _parse_time(params.time_from)
        if start is None:
            start = end - params.hours * 3600
        rows = _compactor().store.recent(
            session_id=sid, source=params.source, time_from=start, time_to=end,
            limit=params.limit)
        if not rows:
            return _text("No messages in the selected time range.")
        return _text("\n".join(_render_hit(row, show_session=sid is None) for row in rows))

    add_tool(
        "lcm_recent", "View recent history",
        "Read recent original session messages by source time without relying on summaries.",
        RecentParams, lcm_recent, snippet="View recent session history",
    )

    class StatusParams(BaseModel):
        session_id: str | None = None

    async def lcm_status(params, ctx):
        current = _sync_ctx(ctx)
        sid = params.session_id or current
        if not sid:
            return _text('Missing session_id.')
        compactor = _compactor()
        stats = compactor.dag.get_session_depth_stats(sid)
        depth_line = ' | '.join(f"d{depth}×{value['count']}({value['tokens']}tok)"
                              for depth, value in sorted(stats.items())) or "no nodes"
        usage = compactor.store.read_metadata_json("summary_usage") or {}
        semantic_line = "fts"
        try:
            semantic = _semantic_index()
            if semantic:
                coverage = semantic.coverage(session_id=sid)
                semantic_line = f"hybrid {coverage['indexed']}/{coverage['total']}"
        except Exception as exc:
            semantic_line = f"hybrid→fts ({exc})"
        return _text(
            f"{sid}: source messages {compactor.store.get_session_count(sid)} | "
            f"summaries {depth_line} | frontier {len(compactor.dag.frontier_nodes(sid))} | "
            f"pending {len(compactor.dag.pending_attempts(sid))} | retrieval {semantic_line} | "
            f"summary model {CFG.get('lcm_summary_provider') or CFG['provider']}/"
            f"{CFG.get('lcm_summary_model') or CFG['default_model']} | "
            f"calls {usage.get('calls', 0)} | failures {usage.get('failures', 0)} | "
            f"input ≈{usage.get('input_tokens_est', 0)} tok | "
            f"output ≈{usage.get('output_tokens_est', 0)} tok"
        )

    add_tool(
        "lcm_status", "LCM status",
        "Show current-session ingestion, summary DAG, pending work, retrieval coverage, and summarization usage.",
        StatusParams, lcm_status, snippet="View LCM status",
    )

    async def sync_event(event, ctx):
        _sync_ctx(ctx)

    async def session_start(event, ctx):
        sid = _sync_ctx(ctx)
        if sid:
            _compactor().reconcile_session(sid, list(ctx.sessionManager.getEntries()))

    async def before_compact(event, ctx):
        if str(CFG.get("context_engine") or "lcm").strip().lower() != "lcm":
            return None
        attempt_id = None
        try:
            prep = _field(event, "preparation")
            raw_messages = list(_field(prep, "messagesToSummarize") or [])
            if not raw_messages:
                return None
            sid = _sync_ctx(ctx)
            if not sid:
                raise RuntimeError("host session id unavailable")
            compactor = _compactor()
            compactor.reconcile_session(sid, list(ctx.sessionManager.getEntries()))
            old = _PENDING.pop(sid, None)
            if old:
                compactor.discard_attempt(old)
            source_ids = _summarized_entry_ids(event, sid)
            messages = [_to_store_dict(message) for message in raw_messages]
            if len(source_ids) != len(messages):
                raise RuntimeError("Pi summarized slice does not align with transcript entries")
            attempt_id = uuid.uuid4().hex
            _PENDING[sid] = attempt_id
            summary, stats = compactor.compact_for_host(
                sid, messages, previous_summary=_field(prep, "previousSummary"),
                source_ids=source_ids, attempt_id=attempt_id,
                first_kept_entry_id=_field(prep, "firstKeptEntryId"))
            if not summary.strip():
                raise RuntimeError("LCM returned an empty summary")
            details = None
            try:
                from misaka.core.compaction.utils import compute_file_lists
                details = compute_file_lists(_field(prep, "fileOps"))
            except Exception:
                pass
            logger.info("LCM staged: %s leaves, %s condensed (%s)",
                        stats.leaf_nodes, stats.condensed_nodes, sid)
            return {"compaction": {
                "summary": summary,
                "firstKeptEntryId": _field(prep, "firstKeptEntryId"),
                "tokensBefore": int(_field(prep, "tokensBefore", 0) or 0),
                "details": details,
            }}
        except Exception:
            if attempt_id:
                try:
                    _compactor().discard_attempt(attempt_id)
                except Exception:
                    logger.debug("LCM pending cleanup failed", exc_info=True)
            sid = _session_id(ctx)
            if sid and _PENDING.get(sid) == attempt_id:
                _PENDING.pop(sid, None)
            logger.warning("LCM compaction failed; keeping the uncompressed messages.", exc_info=True)
            return None

    async def compact_committed(event, ctx):
        sid = _session_id(ctx)
        attempt_id = _PENDING.pop(sid, None)
        entry = _field(event, "compactionEntry") or {}
        from_extension = bool(_field(event, "fromExtension") or _field(event, "fromHook")
                              or _field(entry, "fromHook"))
        if attempt_id:
            if from_extension:
                _compactor().commit_attempt(attempt_id, _field(entry, "id"))
            else:
                _compactor().discard_attempt(attempt_id)
        else:
            _compactor().reconcile_session(sid, list(ctx.sessionManager.getEntries()))

    async def compact_failed(event, ctx):
        sid = _session_id(ctx)
        attempt_id = _PENDING.pop(sid, None)
        if attempt_id:
            _compactor().discard_attempt(attempt_id)

    harn.on("session_start", session_start)
    harn.on("before_agent_start", sync_event)
    harn.on("agent_end", sync_event)
    harn.on("session_shutdown", sync_event)
    harn.on("session_before_compact", before_compact)
    harn.on("session_compact", compact_committed)
    harn.on("session_compact_failed", compact_failed)
