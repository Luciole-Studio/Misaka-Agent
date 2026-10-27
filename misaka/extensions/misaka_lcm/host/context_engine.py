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

from misaka.core.platform.prompt_guard import MARKER, untrusted
from misaka.core.virtual_models import isVirtualModel
from misaka.utils.values import read_field, signal_aborted

from ..vendor import aux_session
from ..vendor.reconcile import _PRESERVED_OBJECTIVE_CONTEXT_PREFIX
from . import config_bridge, entry_rows, execution, fence, ingest, llm, rollups, storage

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
        project = storage.project(ctx)
        if key in _ENGINES and _ENGINES[key]._misaka_cache != storage.current(project):
            _retire(key)
        if key not in _ENGINES:
            from ..vendor.engine import LCMEngine

            llm.install()
            execution.install_worker_ownership()
            class NativeLCMEngine(LCMEngine):
                def _bind_storage(self, *args, **kwargs):
                    super()._bind_storage(*args, **kwargs)
                    # Upstream discards the ids of the rows it writes; keep each one's entry
                    # (entry_rows). On the class, so a private import runtime (carry) records too.
                    store, write = self._store, self._store._append_protected_batch

                    def append_protected_batch(session_id, messages, *args, **kwargs):
                        ids = write(session_id, messages, *args, **kwargs)
                        entry_rows.record(store, ids, messages)
                        return ids

                    store._append_protected_batch = append_protected_batch

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

            storage.acquire(project)
            built = NativeLCMEngine(config=config_bridge.load_config(ctx=ctx), hermes_home="")
            try:
                built._misaka_project = str(project)
                built._misaka_source = storage.source(ctx)
                built._misaka_cache = storage.current(project)
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


def _retire(key) -> None:
    """Drop an engine whose project cache was deleted or replaced under it (``storage.current``):
    it would go on writing into the folder that was moved away while every path-level open
    recreated the directory beside it. The next bind builds one on the project's current cache
    and ``start`` rebinds the session from its transcript, the durable archive."""
    built = _ENGINES.pop(key)
    try:
        _shutdown(built)
    except Exception:
        logger.warning("MISAKA LCM could not settle the workers of a replaced cache; dropping it anyway.",
                       exc_info=True)
    _close_readers(key[0])


def _close_readers(database) -> None:
    """Upstream pools semantic readers beyond a tool/engine's lifetime. Retire only this
    project's; do not close another project's readers."""
    from ..vendor import retrieval_core
    with retrieval_core._pool_lock:
        for key, entry in list(retrieval_core._vector_store_pool.items()):
            if key[0] == database:
                with entry["lock"]:
                    entry["store"].close()
                del retrieval_core._vector_store_pool[key]


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
    entry_rows.forget(built._store)               # reopened on next use; the cache directory may go now


def release_project(ctx=None):
    with _REGISTRY_LOCK:
        if not any(path == config_bridge.database_path(ctx) for path, _ in _ENGINES):
            _close_readers(config_bridge.database_path(ctx))
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
    # A virtual selection has no window of its own: the model each request goes to is bound when the request is
    # prepared (session_context_prepare), and stays bound between hooks.
    if model is None or isVirtualModel(model):
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
        config_bridge.scale_to_window(built._config, built.context_length,
                                      config_bridge.summariser_window(ctx, built.context_length))


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
        cwd=read_field(ctx, 'cwd'), lcm_project=str(storage.project(ctx)), lcm_source=storage.source(ctx),
        sessionManager=SimpleNamespace(getSessionId=lambda: session_id,
                                       getSessionFile=lambda: transcript.session_file,
                                       getEntries=lambda: transcript.entries,
                                       getBranch=lambda: transcript.branch),
        model=copy.deepcopy(read_field(ctx, "model")), modelRegistry=read_field(ctx, "modelRegistry"))


# Entries that open no ingest: a tool result, an edit, a usage receipt or a non-message custom
# entry is appended with no model request, run start or compaction in between.
_NO_INGEST = frozenset({'context_edit', 'usage', 'custom', 'label', 'session_info'})


def _edits_before_ingest(entries):
    """The context edits each entry already carried when live ingest first stored it.

    Ingest stores what ``buildSessionContext`` showed at a model request, a run's start or end, or
    a compaction, and upstream never re-reads a row it has passed. An edit appended before any of
    those (a retry dropping the attempt it failed, ``_omitRecoveryAttempt``) means the entry never
    reached the store; one appended later (a boundary draft) left its original there. Applying
    every edit, or none, gives the archive a different multiset of identical rows than the store,
    and upstream's surplus-skip matcher then re-deals them: a retried 503's empty assistant row
    left a checkpoint's frontier with a source no entry owned (2026-10-26, Last Order's
    cards_failed turn). Only an edit on the target's own branch counts, as in
    ``buildSessionProjection``; the last one wins, as there."""
    by_id = {entry['id']: entry for entry in entries}
    edits = {}
    for entry in entries:
        if entry['type'] != 'context_edit':
            continue
        target, parent = str(entry.get('targetId')), entry.get('parentId')
        while parent is not None and parent != target:
            between = by_id.get(parent)
            if between is None or not (between['type'] in _NO_INGEST or (
                    between['type'] == 'message' and read_field(between.get('message'), 'role') == 'toolResult')):
                break
            parent = between.get('parentId')
        if parent == target:
            edits[target] = entry
    return edits


def _session_originals(transcript):
    from misaka.core.session_manager import projectContextEntry

    edits = _edits_before_ingest(transcript.entries)
    return {item["id"]: entry_rows.tag(item["id"], ingest.upstream_messages(projectContextEntry(item, edits.get(item["id"]))))
            for item in transcript.entries if item["type"] != "compaction"}


def _archive_map(built, transcript, *, through=None):
    if through is not None:
        end = next(i for i, entry in enumerate(transcript.entries) if entry['id'] == through)
        transcript = Transcript(transcript.entries[:end], transcript.branch, transcript.session_id,
                                transcript.session_file, transcript.project)
    projected = _session_originals(transcript)
    # Each entry's rows by the binding recorded when they were written (entry_rows), not by
    # content; only entries no row is bound to are matched by content, among unbound rows.
    rows = [row[0] for row in built._store._conn.execute(
        "SELECT store_id FROM messages WHERE session_id = ?", (built._session_id,))]
    bound, taken = entry_rows.bound(built._store, rows)
    # Only the positions the archive still projects: a reply a retry dropped after agent_end
    # stored it stays as unowned as content matching left it.
    result = {entry: [positions[index] for index in range(len(projected[entry])) if index in positions]
              for entry, positions in bound.items() if projected.get(entry)}
    unbound = [entry for entry in projected if entry not in result]
    if unbound:
        originals = [message for entry in unbound for message in projected[entry]]
        watermark, scope = built._last_compacted_store_id, built.active_store_ids
        try:
            built._last_compacted_store_id = 0
            built.active_store_ids = {row for row in rows if row not in taken} if taken else None
            stored = built._get_store_id_map_for_messages(originals)
        finally:
            built._last_compacted_store_id, built.active_store_ids = watermark, scope
        result.update({entry: [stored[id(message)] for message in projected[entry] if id(message) in stored]
                       for entry in unbound})
    return projected, {entry: result[entry] for entry in projected}


def _archive_sources(built, transcript):
    """Each stored row's owning entry, and each carried row's receipt key."""
    from . import carry
    _, stored = _archive_map(built, transcript)
    return ({source: entry for entry, sources in stored.items() for source in sources},
            {row: key for key, row in carry.sources(built, transcript).items()})


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
    from misaka.core.session_manager import buildSessionProjection

    # buildSessionContext's messages, entry by entry, each row carrying the entry it came from
    # (entry_rows). A compaction's own rows -- its summary scaffold and the copies it retains --
    # are no entry's source and carry none.
    messages = [row for item in buildSessionProjection(transcript.branch).entries
                for row in (ingest.upstream_messages(item.messages) if item.sourceEntry["type"] == "compaction"
                            else entry_rows.tag(item.sourceEntry["id"], ingest.upstream_messages(item.messages)))]
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

    Two rules keep the wrapper from compounding (2026-09-23, card t_9f10b6: one row grew
    from 70 KB to 2.9 MB over 31 rounds and pushed a 272k-window Sister into overflow):

    * the preserved-objective scaffold is recognised by upstream with ``startswith`` on its
      prefix (engine ``_PRESERVED_OBJECTIVE_CONTEXT_PREFIX``), not ``re.search``; a fence
      put in front of the prefix made every round preserve the row again, wrapped, one
      layer deeper. The prefix stays in front and the fence goes around what follows it.
    * a summary this session already fenced is handed back as it is: the same row comes
      through here on every round, and wrapping a wrapper defangs the inner fence and
      grows the row each time.
    """
    node_ids = [int(node) for _, node in _SUMMARY_BLOCK.findall(summary)]
    if not fence.is_tainted(built, node_ids=node_ids):
        return summary
    label = f"lcm:compaction:{built.current_session_id}"
    opener = f'<<<{MARKER} name="{label}">>>'
    body = summary.lstrip()
    if body.startswith(opener):
        return summary
    if body.startswith(_PRESERVED_OBJECTIVE_CONTEXT_PREFIX):
        rest = body[len(_PRESERVED_OBJECTIVE_CONTEXT_PREFIX):].lstrip("\n")
        if rest.startswith(opener):
            return summary
        logger.info("LCM preserved objective covers fenced material; fencing it behind its prefix.")
        return _PRESERVED_OBJECTIVE_CONTEXT_PREFIX + "\n" + untrusted(label, rest)
    logger.info("LCM compaction summary covers fenced material; handing it back as data.")
    return untrusted(label, summary)


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
        # Upstream's "which platform" is, for one project cache shared by Last Order and her
        # Sisters, "which agent": it is what every row of this session stores as its `source`.
        built.on_session_start(session_id, platform=storage.source(ctx), **continuity)
        built._ingest_cursor_needs_reconcile = True  # a fresh DB may still open an existing tree
        built._misaka_preflight_turn = None
        built._misaka_preanswer = None
        branch = read_field(ctx.sessionManager, 'getBranch')
        if branch:
            from misaka.core.session_manager import buildSessionContext

            from ..native.usage_anchor import capture_usage_anchor
            entries = list(branch())
            messages = buildSessionContext(entries).messages
            # Retained tail usage priced the pre-compaction request, not this
            # checkpoint's shorter prefix. Only a subsequent real response can
            # anchor the replacement view; otherwise use the original estimator.
            cut = next((i + 1 for i in range(len(entries) - 1, -1, -1)
                        if entries[i]['type'] == 'compaction'), 0)
            first_priced = len(messages) - len(buildSessionContext(entries[cut:]).messages) if cut else 0
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
        from misaka.core.session_manager import buildSessionContext

        from ..native.image_token_cost import calibrate_from_usage
        from ..native.usage_anchor import capture_usage_anchor
        built = bound_engine(ctx)
        data = llm.usage_dict(reported)
        branch = read_field(ctx.sessionManager, 'getBranch')
        if branch:
            messages = ingest.upstream_messages(buildSessionContext(list(branch())).messages)
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


def _aligned(native, original_native):
    """The live view with its positions in the saved view -- reconciled, never refused.

    Raising here (2026-09-18, B11) failed every turn of a session after one aborted
    turn had left the two views disagreeing on one message: nothing could advance the
    session, so nothing could ever bring the views back together. Upstream's stance is
    that the list handed to it is what the model is sent and the store re-anchors
    itself to it (``_reconcile_ingest_cursor_from_store``). Same here: an unalignable
    live view of the same length keeps its messages and takes the saved view's
    positions one for one; a live view of a different length falls back to the saved
    view for this turn. Either way the difference is logged, once per shape, so the
    next occurrence says what actually differed instead of only that something did.
    """
    positions = ingest.align_sources(native, original_native)
    if positions is not None:
        return native, positions
    detail = ingest.describe_mismatch(native, original_native)
    if len(native) == len(original_native):
        logger.warning('LCM live context and transcript disagree; keeping the live messages '
                       'with one-for-one positions (%s)', detail)
        return native, list(range(len(native)))
    logger.warning('LCM live context and transcript disagree in length; sending the transcript '
                   'view for this turn (%s)', detail)
    return original_native, list(range(len(original_native)))


def prepare(event, ctx, *, transcript=None):
    """Original engine policy, without consulting Pi thresholds or cut points."""
    from misaka.core.session_manager import buildSessionContext

    transcript = transcript or snapshot(ctx)
    original_native = buildSessionContext(transcript.branch).messages
    native = read_field(event, 'messages')
    if native is None:
        native = original_native
    native, positions = _aligned(native, original_native)
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
        from . import carry
        if not prepared.replay_changed:
            # The check below, before the engine commits anything: a frontier the archive cannot
            # account for skips this round and the context goes out uncompacted. Raising instead
            # failed the turn, and its session_compact_failed rebind re-ingests the whole archive
            # as duplicate rows. After compress() the engine has published nodes and moved its
            # cursor, so a refusal there still raises.
            source_entries, foreign = _archive_sources(built, prepared.transcript)
            if any(source not in source_entries and source not in foreign
                   for node in built._summary_frontier_nodes() for source in carry.source_ids(built, node)):
                logger.warning('LCM frontier of %s has sources outside the transcript archive; '
                               'skipping this compaction.', built.current_session_id)
                return None
        try:
            compressed = (built.compress(prepared.messages, current_tokens=prepared.tokens,
                                         focus_topic=prepared.focus, force=prepared.reason == 'manual')
                          if prepared.compress else prepared.messages)
        except AuxiliaryExplicitCancellation as error:
            raise asyncio.CancelledError('Native context compression cancelled') from error
        if prepared.replay.unchanged(compressed) and not prepared.replay_changed:
            return None
        frontier = built._summary_frontier_nodes()
        source_entries, foreign = _archive_sources(built, prepared.transcript)
        records = []
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
        flags_before = [{key: row[key] for key in ingest.NATIVE_METADATA if key in row} for row in prepared.replay.messages]
        flags_after = [{key: row[key] for key in ingest.NATIVE_METADATA if key in row} for row in guarded]
        if not prepared.replay_changed and flags_after == flags_before and prepared.replay.same_view(messages):
            # The engine re-emitted the context it was given (a scaffold without its source
            # ordinal, an empty assistant row spelled differently): the previous checkpoint
            # already says all of this, and writing it again every round is what turned a
            # card's transcript into hundreds of megabytes (2026-09-23). A new native
            # compression flag is a change the checkpoint must carry, so it still writes.
            return None
        details = {'lcm': {'nodes': records, 'scaffolds': scaffolds}}
        if native_metadata:
            details['lcm']['nativeMetadata'] = native_metadata
    rollups.nudge(built)
    return CompactionResult('LCM ' + (built.last_compression_status if prepared.compress else 'replay update'), '', prepared.tokens,
                            details=details, contextMessages=messages, contextSummaries=scaffolds)
