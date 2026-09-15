"""LCM owns context policy; MISAKA owns the durable transcript and publication.

The adapter preserves source identity across native messages, engine replay cleanup,
and session-tree checkpoints. It never pins LCM to Pi's cut, chunk or fresh-tail
settings, and never treats an engine failure as permission to call another model.
"""

from __future__ import annotations

import asyncio
import atexit
import copy
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from weakref import WeakValueDictionary

from misaka.core.platform.prompt_guard import untrusted
from misaka.utils.values import read_field, signal_aborted

from ..vendor import aux_session
from . import config_bridge, execution, fence, ingest, llm, rollups, storage

logger = logging.getLogger(__name__)

# Upstream's summary labels, used for lineage fencing, not as proof of compaction.
_SUMMARY_BLOCK = re.compile(r"\[(?:Recent|Session Arc|Durable|Depth-\d+) Summary \(d(\d+), node (\d+)\)\]")

# Per-session locks serialize engine work; the registry serializes admission and
# retirement. Weak locks disappear after their last owner/waiter releases them.
_ENGINES: dict[tuple[str, str], object] = {}
_REGISTRY_LOCK = threading.RLock()
_LOCKS = WeakValueDictionary()


@contextmanager
def operation(ctx=None):
    with _REGISTRY_LOCK:
        key = _key(ctx)
        lock = _LOCKS.setdefault(key, threading.RLock())
    while not lock.acquire(timeout=0.05):
        execution.check_cancelled()
    try:
        execution.check_cancelled()
        yield
    finally:
        lock.release()


def _key(ctx=None):
    return config_bridge.database_path(ctx), ingest.session_id(ctx) if ctx is not None else ""


def engine(ctx=None):
    """One independent upstream runtime per session; the empty key is CLI-only."""
    with operation(ctx), _REGISTRY_LOCK:
        key = _key(ctx)
        if key not in _ENGINES:
            from ..vendor.engine import LCMEngine

            llm.install()
            execution.install_worker_ownership()
            class NativeLCMEngine(LCMEngine):
                def handle_tool_call(self, *args, **kwargs):
                    with execution.worker_owner(self):
                        return super().handle_tool_call(*args, **kwargs)

                def _assemble_context(self, *args, **kwargs):
                    with execution.worker_owner(self):
                        return super()._assemble_context(*args, **kwargs)

                def _build_proactive_recall_message(self, *args, **kwargs):
                    # MISAKA calls the original builder from its ephemeral
                    # request hook, not from a durable compaction checkpoint.
                    return None

                def _state_db_path(self, kwargs=None):
                    return None

                def _session_has_foreground_branch_marker(self, session_id, parent_session_id):
                    from .native import session_is_branch_of
                    return session_is_branch_of(session_id, parent_session_id)

                def _ingest_messages(self, messages):
                    # Cached sanitized rows retain current input provenance, not the
                    # source ordinals from an earlier view or a different branch.
                    return ingest.preserve_sources(messages, super()._ingest_messages(messages))

                def _get_host_fallback_compressor(self):
                    compressor = super()._get_host_fallback_compressor()
                    if compressor is not None:
                        compressor._compression_cancelled_check = lambda: signal_aborted(execution.current_signal())
                    return compressor

                def get_runtime_identity(self):
                    identity = super().get_runtime_identity()
                    identity.update(plugin_name="misaka-lcm", project=str(storage.project(ctx)),
                                    storage_lifetime="project-runtime", history_scope="loaded-project-cache",
                                    native_session_catalog_contains="session-identifiers-not-conversation-content")
                    return identity

            storage.acquire(storage.project(ctx))
            built = NativeLCMEngine(config=config_bridge.load_config(ctx=ctx), hermes_home="")
            try:
                built._misaka_project = str(storage.project(ctx))
                storage.namespace_ids(built._store._conn)
                from .native import session_ids
                built._dag.before_publish = execution.check_cancelled
                built._lifecycle._host_sessions = session_ids
            except BaseException as error:
                try:
                    _shutdown(built)
                except Exception as cleanup_error:  # noqa: BLE001 - preserve the initialization failure
                    error.add_note(f"MISAKA LCM initialization cleanup failed: {cleanup_error}")
                raise
            _ENGINES[key] = built
        return _ENGINES[key]


def close(ctx=None) -> None:
    with operation(ctx):
        built = _ENGINES.get(_key(ctx))
        if built is not None:
            _shutdown(built)
            with _REGISTRY_LOCK:
                _ENGINES.pop(_key(ctx), None)


def _shutdown(built, timeout=25):
    # These workers open their own SQLite connections. Settle them before the
    # last project lease permits deleting the database and externalized payloads.
    deadline = time.monotonic() + timeout
    def remaining():
        return max(0, deadline - time.monotonic())
    execution.drain_tool_workers(built, timeout=remaining())
    if not built.drain_rollup_maintenance(timeout=remaining()):
        raise TimeoutError("MISAKA LCM rollup workers are still active; retaining the cache")
    if not built._assertion_extraction_idle.wait(timeout=remaining()):
        raise TimeoutError("MISAKA LCM assertion workers are still active; retaining the cache")
    from ..vendor import db_bootstrap
    with db_bootstrap._integrity_scan_lock:
        threads = [thread for (path, _), thread in db_bootstrap._integrity_scan_threads.items()
                   if path == str(built._store.db_path)]
    for thread in threads:
        thread.join(remaining())
        if thread.is_alive():
            raise TimeoutError("MISAKA LCM FTS workers are still active; retaining the cache")
    built.shutdown()


def release_project(ctx=None):
    with _REGISTRY_LOCK:
        if not any(path == config_bridge.database_path(ctx) for path, _ in _ENGINES):
            # Upstream pools semantic readers beyond a tool/engine's lifetime.
            # Retire only this project; do not close another project's readers.
            from ..vendor import retrieval_core
            with retrieval_core._pool_lock:
                for key, entry in list(retrieval_core._vector_store_pool.items()):
                    if key[0] == config_bridge.database_path(ctx):
                        with entry["lock"]:
                            entry["store"].close()
                        del retrieval_core._vector_store_pool[key]
            storage.release(storage.project(ctx))


def close_all() -> None:
    """Process teardown after owners settle; not a session switch/reload hook."""
    with _REGISTRY_LOCK:
        for key, built in list(_ENGINES.items()):
            try:
                _shutdown(built)
                del _ENGINES[key]
            except Exception:
                logger.exception("MISAKA LCM shutdown failed; retaining its cache lease")
        for workspace in list(storage._LEASES):
            try:
                release_project(SimpleNamespace(cwd=str(workspace)))
            except Exception:
                logger.exception("MISAKA LCM project cleanup failed; retry on next start")


atexit.register(close_all)


def update_model(ctx) -> None:
    model = read_field(ctx, "model")
    if model is None:
        return
    built = engine(ctx)
    api = str(read_field(model, 'api', '') or '')
    if api in {'openai-responses', 'openai-codex-responses', 'azure-openai-responses'}:
        api = 'codex_responses'  # Hermes' name for Responses reasoning-sidecar semantics.
    values = (str(read_field(model, "id", "")), int(read_field(model, "contextWindow", 0)),
              str(read_field(model, "baseUrl", "") or ""), str(read_field(model, "provider", "")),
              api)
    current = (built.model, built.raw_context_length, built.base_url, built.provider, built.api_mode)
    if values != current:
        built._usage_anchor = None
        name, window, base_url, provider, api = values
        built.update_model(name, window, base_url=base_url, provider=provider, api_mode=api)


def bound_engine(ctx):
    built = engine(ctx)
    from . import settings
    settings.refresh(built, ctx)
    update_model(ctx)
    if ingest.session_id(ctx) and built.bound_session_id != ingest.session_id(ctx):
        start(ctx)
    return built


def _node_text(node) -> str:
    """The exact node frame emitted by upstream's _assemble_context."""
    label = {0: "Recent", 1: "Session Arc", 2: "Durable"}.get(node.depth, f"Depth-{node.depth}")
    return (f"[{label} Summary (d{node.depth}, node {node.node_id})]\n"
            f"{node.summary}\n[Expand for details: {node.expand_hint}]")


@dataclass(frozen=True)
class Transcript:
    """One owner-loop snapshot; a queued worker must not mix transcript generations."""

    entries: list
    branch: list
    session_id: str = ""
    session_file: str = ""
    project: str = ""


def snapshot(ctx) -> Transcript:
    return Transcript(copy.deepcopy(list(ctx.sessionManager.getEntries())),
                      copy.deepcopy(list(ctx.sessionManager.getBranch())),
                      ingest.session_id(ctx),
                      str(ctx.sessionManager.getSessionFile() or ""), str(storage.project(ctx)))


def freeze_context(ctx, transcript):
    """Only the owner loop reads mutable session/model identity before queuing work."""
    session_id = ingest.session_id(ctx)
    return SimpleNamespace(
        cwd=read_field(ctx, 'cwd'), lcm_project=str(storage.project(ctx)),
        sessionManager=SimpleNamespace(getSessionId=lambda: session_id,
                                       getSessionFile=lambda: transcript.session_file,
                                       getEntries=lambda: transcript.entries,
                                       getBranch=lambda: transcript.branch),
        model=copy.deepcopy(read_field(ctx, "model")), modelRegistry=read_field(ctx, "modelRegistry"))


def _session_originals(transcript):
    from misaka.core.session_manager import session_entry_to_context_messages

    return {item["id"]: ingest.upstream_messages(session_entry_to_context_messages(item))
            for item in transcript.entries if item["type"] != "compaction"}


def _archive_map(built, transcript, *, through=None):
    if through is not None:
        end = next(i for i, entry in enumerate(transcript.entries) if entry['id'] == through)
        transcript = Transcript(transcript.entries[:end], transcript.branch, transcript.session_id,
                                transcript.session_file, transcript.project)
    projected = _session_originals(transcript)
    originals = [message for group in projected.values() for message in group]
    watermark, scope = built._last_compacted_store_id, built.active_store_ids
    try:
        built._last_compacted_store_id, built.active_store_ids = 0, None
        stored = built._get_store_id_map_for_messages(originals)
    finally:
        built._last_compacted_store_id, built.active_store_ids = watermark, scope
    return projected, {entry: [stored[id(message)] for message in group if id(message) in stored]
                       for entry, group in projected.items()}


def _checkpoint_nodes(built, entry, transcript):
    """Resolve/import checkpoint lineage by SessionEntry IDs, not matching prose.

    Forks and a rebuilt LCM store have different numeric store/node IDs. The native
    checkpoint carries the frontier plus its original entry references, allowing a
    new DAG to reconstruct that same provenance without importing another branch.
    """
    if entry is None or built._bypasses_lcm_context_management():
        return [], {}
    from . import carry
    full = entry.get("contextMessages") is not None
    capsules = (entry.get("details") or {}).get("lcm", {}) if full else {}
    if full and not capsules:
        raise ValueError("Full context checkpoint has no LCM source provenance")
    from ..vendor.dag import SummaryNode
    from ..vendor.tokens import count_tokens

    # Later, not-yet-ingested duplicates must not make upstream's surplus-skip
    # matcher reassign a published checkpoint's older source to the new occurrence.
    projected, stored = _archive_map(built, transcript, through=entry['id'])
    branch_ids = {item['id'] for item in transcript.branch}
    foreign = carry.sources(built, transcript)
    if full:
        records = capsules['nodes']
    else:
        cut = next(i for i, item in enumerate(transcript.branch) if item['id'] == entry['firstKeptEntryId'])
        records = [{'id': 0, 'summary': entry['summary'], 'depth': 0,
                    'sourceEntries': [item['id'] for item in transcript.branch[:cut] if projected.get(item['id'])]}]
    nodes, replacements = [], {}
    for record in records:
        references = record['sourceEntries']
        if not set(references) <= branch_ids or any(not stored.get(ref) for ref in references):
            raise ValueError('LCM checkpoint source entries are absent from this branch/store')
        carried = record.get('foreignSources', [])
        if not set(carried) <= foreign.keys():
            raise ValueError('LCM checkpoint claims foreign sources without a branch carry receipt')
        sources = sorted({*(foreign[key] for key in carried), *(source for ref in references for source in stored[ref])})
        node = built._dag.get_node(record['id']) if full else None
        if node is not None and (node.session_id != built.current_session_id
                                 or node.depth != record['depth']
                                 or node.summary != record['summary']
                                 or set(carry.source_ids(built, node)) != set(sources)):
            node = None
        if node is None:
            hint = f"MISAKA session compaction {entry['id']} node {record['id']}"
            node = next((node for node in built._dag.get_session_nodes(built.current_session_id, limit=-1)
                         if node.expand_hint == hint and node.summary == record['summary']
                         and node.depth == record['depth']
                         and set(carry.source_ids(built, node)) == set(sources)), None)
            if node is None:
                earliest, latest = built._store.get_time_bounds(sources)
                node = SummaryNode(session_id=built.current_session_id, depth=record['depth'],
                                   summary=record['summary'], source_type='messages', source_ids=sources,
                                   expand_hint=hint, token_count=count_tokens(record['summary']),
                                   created_at=time.time(), earliest_at=earliest, latest_at=latest)
                built._dag.add_node(node)
                built._invalidate_rollups_for_published_node(node)
        nodes.append(node)
        if full and record['frame'] != _node_text(node):
            replacements[record['frame']] = _node_text(node)
    return nodes, replacements


def _checkpoint(transcript):
    return next((entry for entry in reversed(transcript.branch) if entry['type'] == 'compaction'), None)


@contextmanager
def _summary_scope(built, *, transcript):
    """The selected checkpoint frontier and its raw branch, plus new publications."""
    saved = built._dag.active_node_ids, built.active_store_ids
    try:
        nodes, _ = _checkpoint_nodes(built, _checkpoint(transcript), transcript)
        _, stored = _archive_map(built, transcript)
        from . import carry
        built.active_store_ids = {source for entry in transcript.branch for source in stored.get(entry['id'], [])} | set(carry.sources(built, transcript).values())
        built._dag.active_node_ids = {node.node_id for node in nodes}
        built._last_compacted_store_id = max(
            (source for node in nodes for source in carry.source_ids(built, node)), default=0)
        yield
    finally:
        built._dag.active_node_ids, built.active_store_ids = saved



def turn_key(transcript):
    """Ingress identity, not text or assistant/tool sub-turn count.

    Hermes calls sub-threshold maintenance once at turn start. Research work orders
    are native custom messages; tool results and checkpoint publication are not new
    ingress. Entry IDs distinguish repeated identical prompts and selected branches.
    """
    return next((entry['id'] for entry in reversed(transcript.branch)
                 if ingest.is_task_message(entry if entry['type'] == 'custom_message'
                                           else entry.get('message'))), None)

def _messages(transcript, built):
    from misaka.core.session_manager import build_session_context

    native = build_session_context(transcript.branch).messages
    messages = ingest.upstream_messages(native)
    if built._bypasses_lcm_context_management():
        return messages
    entry = _checkpoint(transcript)
    native_import = entry is not None and entry['type'] == 'compaction' and entry.get('contextMessages') is None and not any(
        node.expand_hint == f"MISAKA session compaction {entry['id']} node 0"
        for node in built._dag.get_session_nodes(built.current_session_id, limit=-1))
    # A rebind reconciles the append-only archive in append order, not a branch
    # shaped subsequence that the linear upstream cursor would count twice.
    if built._ingest_cursor_needs_reconcile or native_import:
        originals = [message for group in _session_originals(transcript).values() for message in group]
        built._ingest_cursor = 0
        built._schedule_ingest_cursor_reconciliation()
        built._last_compacted_store_id = 0
        try:
            built.ingest(originals)
        finally:
            built._ingest_cursor_needs_reconcile = True
        if built._consecutive_ingest_failures:
            raise RuntimeError('LCM checkpoint originals were not persisted')
        built._ingest_cursor = len(messages)
    nodes, replacements = _checkpoint_nodes(built, entry, transcript)
    if entry and entry['type'] == 'compaction' and entry.get('contextMessages') is None and nodes:
        messages[0] = {'role': 'user', 'content': _node_text(nodes[0])} if len(nodes) == 1 else messages[0]
    elif replacements:
        # Only rewrite generated scaffold positions recorded in the checkpoint;
        # identical quoted node frames in a raw message are still just raw text.
        for index in (entry.get('details') or {})['lcm']['scaffolds']:
            text = messages[index]['content']
            text = text if isinstance(text, str) else ingest._text_of(text)
            for old, new in replacements.items():
                text = text.replace(old, new)
            messages[index] = {**messages[index], 'content': text}
    if built._ingest_cursor_needs_reconcile:
        from . import carry
        built._last_compacted_store_id = max(
            (source for node in nodes for source in carry.source_ids(built, node)), default=0)
        built._persist_frontier_marker()
        built._ingest_cursor_needs_reconcile = False
    return messages


def _guarded_summary(built, summary: str) -> str:
    """The compaction entry, fenced when the history it summarises was fenced.

    This is LCM's other exit, and the wider one: the tool seam answers when the model
    asks, but a compaction entry arrives every round wearing the prompt's own voice. A
    summary of a page misaka had fenced would hand that voice to the page.

    The check is per node rather than blanket. Fencing every compaction entry would
    teach the model that its own history is data -- the cost the fence exists to avoid --
    while fencing only the rounds that actually swallowed hostile text costs a clean
    session nothing. The prefix upstream writes names the nodes, so there is no guessing.

    Upstream recognises its own scaffold with a `re.search` over the content, so both
    substrings it looks for survive inside the wrapper and the fenced entry is still
    skipped on re-ingest rather than stored as a fresh message.
    """
    node_ids = [int(node) for _, node in _SUMMARY_BLOCK.findall(summary)]
    if not fence.is_tainted(built, node_ids=node_ids):
        return summary
    logger.info("LCM compaction summary covers fenced material; handing it back as data.")
    return untrusted(f"lcm:compaction:{built.current_session_id}", summary)


def _lineage(session_id: str) -> dict | None:
    """The explicit child-to-parent link hermes's ``subagent_start`` hook carries.

    hermes raises that hook in the parent's process; a misaka child is its own process,
    so the child records the same link about itself from the environment the runtime
    gave it, before the engine's ``on_session_start`` consumes it.
    """
    parent = os.environ.get("MISAKA_SUBAGENT_PARENT_SESSION_ID") or ""
    if not parent:
        return None
    return {
        "child_session_id": session_id,
        "parent_session_id": parent,
        "child_subagent_id": os.environ.get("MISAKA_SUBAGENT_ID") or "",
        "parent_subagent_id": "",
        "child_role": os.environ.get("MISAKA_WHO") or "",
    }


def start(ctx) -> str:
    """Bind the engine to this misaka session. Returns the session id, or ``""``."""
    session_id = ingest.session_id(ctx)
    built = engine(ctx)
    if session_id:
        update_model(ctx)
        lineage = _lineage(session_id)
        if lineage is not None:
            aux_session.record_subagent_start(lineage)
        from . import carry
        carried = carry.receipt(snapshot(ctx)) if read_field(ctx.sessionManager, 'getEntries') else None
        continuity = ({'conversation_id': carried['details']['lcm']['carry']['conversation']} if carried else {})
        built.on_session_start(session_id, platform="misaka", **continuity)
        built._ingest_cursor_needs_reconcile = True  # a fresh DB may still open an existing tree
        built._misaka_preflight_turn = None
        built._misaka_preanswer = None
        branch = read_field(ctx.sessionManager, 'getBranch')
        if branch:
            from misaka.core.session_manager import build_session_context

            from ..native.usage_anchor import capture_usage_anchor
            entries = list(branch())
            messages = build_session_context(entries).messages
            # Retained tail usage priced the pre-compaction request, not this
            # checkpoint's shorter prefix. Only a subsequent real response can
            # anchor the replacement view; otherwise use the original estimator.
            cut = next((i + 1 for i in range(len(entries) - 1, -1, -1)
                        if entries[i]['type'] == 'compaction'), 0)
            first_priced = len(messages) - len(build_session_context(entries[cut:]).messages) if cut else 0
            built._usage_anchor = None
            for index in range(len(messages) - 1, first_priced - 1, -1):
                message = messages[index]
                reported = read_field(message, 'usage')
                if reported is None or read_field(message, 'role') != 'assistant':
                    continue
                if ((read_field(message, 'model'), read_field(message, 'provider')) !=
                        (built.model, built.provider)):
                    break
                data = llm.usage_dict(reported)
                built._usage_anchor = capture_usage_anchor(data['prompt_tokens'], data['completion_tokens'],
                                                          ingest.upstream_messages(messages[:index]))
                if built._usage_anchor is not None:
                    break
    return session_id


def end(ctx, *, transcript=None, reset=False) -> None:
    """Flush the append-only archive once, then release lineage and engine ownership."""
    session_id = ingest.session_id(ctx)
    built = bound_engine(ctx)
    if not session_id:
        return
    try:
        # Exactly one upstream final flush, including branches not currently selected.
        originals = [message for group in _session_originals(transcript or snapshot(ctx)).values() for message in group]
        built._ingest_cursor = 0
        built._schedule_ingest_cursor_reconciliation()
        built.on_session_end(session_id, originals)
        if reset:
            built.on_session_reset()
    finally:
        aux_session.record_subagent_stop({"child_session_id": session_id})
        close(ctx)


def usage(message, ctx) -> None:
    reported = read_field(message, "usage")
    if reported is not None:
        from misaka.core.session_manager import build_session_context

        from ..native.image_token_cost import calibrate_from_usage
        from ..native.usage_anchor import capture_usage_anchor
        built = bound_engine(ctx)
        data = llm.usage_dict(reported)
        branch = read_field(ctx.sessionManager, 'getBranch')
        if branch:
            messages = ingest.upstream_messages(build_session_context(list(branch())).messages)
            if messages and messages[-1].get('role') == 'assistant':
                messages = messages[:-1]
            calibrate_from_usage(built, messages, data['prompt_tokens'])
            anchor = capture_usage_anchor(data['prompt_tokens'], data['completion_tokens'], messages)
            if anchor is not None:
                built._usage_anchor = anchor
        built.update_from_response(data)


def sync(ctx, *, transcript=None) -> None:
    """Persist the session's active context. Idempotent: upstream keeps its own cursor."""
    if not ingest.session_id(ctx):
        return
    built = bound_engine(ctx)
    built.ingest(_messages(transcript or snapshot(ctx), built))


@dataclass
class Prepared:
    built: object
    transcript: Transcript
    replay: ingest.Replay
    messages: list
    model: object
    tokens: int
    reason: str
    focus: str | None
    compress: bool
    replay_changed: bool


def prepare(event, ctx, *, transcript=None):
    """Original engine policy, without consulting Pi thresholds or cut points."""
    from misaka.core.session_manager import build_session_context

    transcript = transcript or snapshot(ctx)
    original_native = build_session_context(transcript.branch).messages
    native = read_field(event, 'messages')
    if native is None:
        native = original_native
    positions = ingest.source_indices(native, original_native)
    built = bound_engine(ctx)
    messages = _messages(transcript, built)
    # Establish the new branch rows before selecting its source scope. Ingest
    # inside that scope would exclude new IDs and misbind identical sibling text.
    built.ingest(messages)
    replay_changed = len(positions) != len(messages)
    replay = ingest.Replay(native)
    checkpoint = _checkpoint(transcript)
    if checkpoint is not None:
        metadata = ((checkpoint.get('details') or {}).get('lcm') or {}).get('nativeMetadata', {})
        if not isinstance(metadata, dict):
            raise ValueError('Invalid native compression checkpoint metadata')
        # Metadata belongs to this complete checkpoint's prefix, not a same-text
        # row elsewhere in the archive. New turns have no inherited marker.
        active_positions = {source: index for index, source in enumerate(positions)}
        for index, values in metadata.items():
            index = int(index)
            if (index < 0 or index >= len(checkpoint.get('contextMessages') or [])
                    or not isinstance(values, dict) or not set(values) <= ingest.NATIVE_METADATA
                    or any(type(value) is not bool for value in values.values())):
                raise ValueError('Invalid native compression checkpoint metadata')
            if index in active_positions:
                replay.messages[active_positions[index]].update(values)
    # Checkpoint rebinding changes generated content only. Keep the live source's
    # timestamp/tool metadata (custom persistence assigns a separate receipt time).
    if replay_changed:
        if built._consecutive_ingest_failures:
            raise RuntimeError('LCM retry checkpoint originals were not persisted')
        cleaned = built._cached_active_replay_messages(messages)
        if cleaned is None and not built._bypasses_lcm_context_management():
            raise RuntimeError('LCM retry checkpoint has no matching ingest replay')
    messages = [{**original, 'content': messages[source]['content']}
                for original, source in zip(replay.messages, positions)]
    if replay_changed and cleaned is not None:
        # Hermes' cursor belongs to the current list, not the longer native archive.
        # Every retained row was just ingested. Rebase that proven cursor and its
        # cleanup cache by source position, not upstream's contiguous-tail matcher.
        # The full-context checkpoint makes sync/reopen adopt the same shorter view.
        projected = [{**cleaned[source], **{key: row[key] for key in ingest.NATIVE_METADATA if key in row}}
                     for row, source in zip(messages, positions)]
        built._ingest_cursor = len(messages)
        built._remember_active_replay_messages(messages, ingest.preserve_sources(messages, projected))
    from ..native.request_pressure import _preflight_request_tokens
    pressure = SimpleNamespace(_usage_anchor=getattr(built, '_usage_anchor', None),
                               tools=read_field(event, 'tools'), api_mode=built.api_mode,
                               provider=built.provider, model=built.model, base_url=built.base_url)
    tokens = _preflight_request_tokens(pressure, messages, read_field(event, 'systemPrompt', '') or '')
    reason = read_field(event, 'reason', 'manual')
    with _summary_scope(built, transcript=transcript):
        if not read_field(event, 'allowCompression', True):
            # Disabling automatic summarization does not disable ingest's replay
            # protection. This is the original cleanup, with no summarizer call.
            messages = built._ingest_messages(messages)
            wants = False
        else:
            wants = reason in {'manual', 'overflow'} or built.should_compress(tokens)
        if read_field(event, 'preflight', False):
            key = turn_key(transcript)
            if not wants and read_field(event, 'allowCompression', True):
                if key != getattr(built, '_misaka_preflight_turn', None):
                    wants = built.should_compress_preflight(messages)
                else:
                    # Per-API pressure still runs, but low-pressure maintenance is
                    # not another summarizer pass after every parallel tool batch.
                    messages = built._ingest_messages(messages)
            built._misaka_preflight_turn = key
    if not wants and replay.unchanged(messages) and not replay_changed:
        return None
    return Prepared(built, transcript, replay, messages, read_field(ctx, 'model'), int(tokens),
                    reason, read_field(event, 'customInstructions'), wants, replay_changed)


def compact(prepared):
    """Adopt the engine's entire result, including partial backlog/cleanup-only."""
    from misaka.core.compaction.compaction import CompactionResult

    built = prepared.built
    with _summary_scope(built, transcript=prepared.transcript):
        from ..native.support import AuxiliaryExplicitCancellation
        try:
            compressed = (built.compress(prepared.messages, current_tokens=prepared.tokens,
                                         focus_topic=prepared.focus, force=prepared.reason == 'manual')
                          if prepared.compress else prepared.messages)
        except AuxiliaryExplicitCancellation as error:
            raise asyncio.CancelledError('Native context compression cancelled') from error
        if prepared.replay.unchanged(compressed) and not prepared.replay_changed:
            return None
        frontier = built._summary_frontier_nodes()
        _, stored = _archive_map(built, prepared.transcript)
        source_entries = {source: entry for entry, sources in stored.items() for source in sources}
        records = []
        from . import carry
        foreign = {row: key for key, row in carry.sources(built, prepared.transcript).items()}
        for node in frontier:
            sources = carry.source_ids(built, node)
            if any(source not in source_entries and source not in foreign for source in sources):
                raise ValueError('LCM frontier contains sources outside the transcript archive')
            records.append({'id': node.node_id, 'depth': node.depth, 'summary': node.summary,
                            'frame': _node_text(node),
                            'sourceEntries': list(dict.fromkeys(source_entries[source] for source in sources if source in source_entries)),
                            **({'foreignSources': [foreign[source] for source in sources if source in foreign]} if any(source in foreign for source in sources) else {})})
        scaffolds = []
        native_metadata = {}
        guarded = []
        for index, message in enumerate(compressed):
            metadata = {key: message[key] for key in ingest.NATIVE_METADATA if key in message}
            if metadata:
                native_metadata[str(index)] = metadata
            if ingest.SOURCE not in message:
                scaffolds.append(index)
            if message.get('_compressed_summary'):
                from .native import fence_summary_content
                message = {**message, 'content': fence_summary_content(
                    message.get('content'), f'lcm:native-compaction:{built.current_session_id}')}
            elif ingest.SOURCE not in message:
                content = message.get('content')
                if isinstance(content, str):
                    message = {**message, 'content': _guarded_summary(built, content)}
                    if content.startswith('<relevant-memories>'):
                        message['content'] = untrusted('lcm:proactive-recall', content)
            guarded.append(message)
        messages = prepared.replay.restore(guarded, prepared.model)
        details = {'lcm': {'nodes': records, 'scaffolds': scaffolds}}
        if native_metadata:
            details['lcm']['nativeMetadata'] = native_metadata
    rollups.nudge(built)
    return CompactionResult('LCM ' + (built.last_compression_status if prepared.compress else 'replay update'), '', prepared.tokens,
                            details=details, contextMessages=messages)
