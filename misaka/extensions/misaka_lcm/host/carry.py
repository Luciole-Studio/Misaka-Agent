"""Explicit continuity receipts, not copied history or global memory injection.

Raw rows stay with their original sessions. A branch-local native receipt attests
which archive entries its carried summaries may cite. Database IDs are temporary;
the entry digest detects source replacement when rebuilding the runtime cache.
"""
from __future__ import annotations

import hashlib
import json
from itertools import batched
from pathlib import Path

from . import context_engine as ce
from . import ingest


def entry_identity(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def receipt(transcript):
    return next((entry for entry in reversed(transcript.branch)
                 if entry['type'] == 'compaction' and (entry.get('details') or {}).get('lcm', {}).get('carry')), None)


def sources(built, transcript):
    """Resolve durable SessionEntry references to this disposable store's row IDs."""
    entry = receipt(transcript)
    if entry is None:
        return {}
    expected = entry['details']['lcm']['carry']['sources']
    cache_key = entry_identity(expected)
    cache = vars(built).setdefault('_misaka_carried_sources', {})
    if cache_key in cache:
        # Operator cleanup can remove the raw rows while native archives and
        # carried nodes survive. Do not keep returning retired numeric handles.
        ids = set(cache[cache_key].values())
        if all(built._store._conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE store_id IN ({','.join('?' for _ in batch)})",
                batch).fetchone()[0] == len(batch) for batch in batched(ids, 900)):
            return cache[cache_key]
    from types import SimpleNamespace

    from misaka.core.session_manager import SessionManager

    by_file = {}
    for key, ref in expected.items():
        if not isinstance(ref, dict):
            raise TypeError('LCM carry checkpoint has retired database-row references; its original source archive is required')
        if ref['project'] != built._misaka_project:
            raise ValueError('LCM carry source belongs to another project')
        by_file.setdefault(ref['path'], []).append((key, ref))
    result = {}
    for path, references in by_file.items():
        manager = SessionManager.openInMemory(path)
        source_project = next((item['data']['workspace'] for item in manager.getEntries()
                               if item.get('type') == 'custom' and item.get('customType') == 'lcm-project'), manager.getCwd())
        if str(Path(source_project).resolve()) != built._misaka_project:
            raise ValueError('LCM source session belongs to another project')
        entries = {item['id']: item for item in manager.getEntries()}
        for key, ref in references:
            item = entries.get(ref['entry'])
            if manager.getSessionId() != ref['session'] or item is None or entry_identity(item) != ref['digest']:
                raise ValueError(f'LCM source session entry is missing or changed: {key}')
        source_ctx = SimpleNamespace(sessionManager=manager, cwd=manager.getCwd(),
                                     lcm_project=built._misaka_project, model=None)
        original = ce.snapshot(source_ctx)
        # Private import runtime: never rebind or reset a live source session's
        # engine/cursor. It only ingests original archive messages, no summaries.
        importer = type(built)(config=built._config, hermes_home='')
        try:
            # An archive reader is not a live owner, in either registry or DB.
            # Let the original lifecycle code operate on private in-memory state.
            importer._register_active_engine_binding = lambda: None
            importer._unregister_active_engine_binding = lambda: None
            importer._lifecycle.close()
            importer._lifecycle = type(importer._lifecycle)(':memory:')
            prior = receipt(original)
            conversation = prior['details']['lcm']['carry']['conversation'] if prior else manager.getSessionId()
            importer.on_session_start(manager.getSessionId(), platform='misaka', conversation_id=conversation)
            originals = [message for group in ce._session_originals(original).values() for message in group]
            importer._ingest_cursor = 0
            importer._schedule_ingest_cursor_reconciliation()
            importer.ingest(originals)
            _, rows = ce._archive_map(importer, original)
            for key, ref in references:
                ids = rows.get(ref['entry'], [])
                if type(ref['index']) is not int or not 0 <= ref['index'] < len(ids):
                    raise ValueError(f'LCM carry source was not ingested: {key}')
                result[key] = ids[ref['index']]
        finally:
            ce._shutdown(importer)
    cache[cache_key] = result
    return result


def references(built, transcript, ids):
    """Keep stable source identities in the native checkpoint, never raw payload copies."""
    carried = sources(built, transcript)
    prior = receipt(transcript)
    descriptors = prior['details']['lcm']['carry']['sources'] if prior else {}
    by_id = {row: (key, descriptors[key]) for key, row in carried.items()}
    _, stored = ce._archive_map(built, transcript)
    entries = {entry['id']: entry for entry in transcript.entries}
    for entry_id, rows in stored.items():
        for index, row in enumerate(rows):
            key = f'{transcript.session_id}:{entry_id}:{index}'
            by_id[row] = (key, {'session': transcript.session_id, 'path': transcript.session_file,
                               'project': transcript.project, 'entry': entry_id, 'index': index,
                               'digest': entry_identity(entries[entry_id])})
    result = {}
    row_keys = {}
    for row in ids:
        if row not in by_id or not by_id[row][1]['path']:
            raise ValueError('LCM carry-over requires a persisted source session archive')
        key, ref = by_id[row]
        result[key] = ref
        row_keys[row] = key
    return result, row_keys


def source_ids(built, node):
    # A carried node's raw lineage is NOT bounded by the new session's row count.
    limit = built._store._conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    return built._dag.source_message_ids(node.node_id, limit=limit)


def rollover(target, transcript, ctx):
    """Run the original lifecycle once; keep the native receipt before moving DAG ownership."""
    old = ce.bound_engine(ctx)
    old_id, new_id = ingest.session_id(ctx), target.getSessionId()
    if old_id == new_id or target.getEntries():
        raise ValueError('Carry-over requires a fresh, distinct native session')
    ce.sync(ctx, transcript=transcript)
    with ce._summary_scope(old, transcript=transcript):
        retain = old._config.new_session_retain_depth
        nodes = [node for node in old._summary_frontier_nodes()
                 if retain == -1 or (retain > 0 and node.depth >= retain)]
        records = []
        ids = set()
        for node in nodes:
            original_ids = source_ids(old, node)
            ids.update(original_ids)
            records.append({'id': node.node_id, 'depth': node.depth, 'summary': node.summary,
                            'frame': ce._node_text(node), 'sourceEntries': [],
                            'foreignSources': original_ids})
        old._dag.active_node_ids = {node.node_id for node in nodes}
        assembled = old._assemble_context(None, [], include_lcm_note=False)
        guarded = [{**message, 'content': ce._guarded_summary(old, message['content'])} for message in assembled]
        context = ingest.Replay([]).restore(guarded, getattr(ctx, 'model', None))
    refs, row_keys = references(old, transcript, ids)
    for record in records:
        record['foreignSources'] = [row_keys[row] for row in record['foreignSources']]
    target.appendCustomEntry('lcm-project', {'workspace': transcript.project})
    target.appendCompaction('LCM explicit carry-over', '', 0, details={'lcm': {
        'nodes': records, 'scaffolds': list(range(len(context))), 'carry': {
            'fromSession': old_id, 'conversation': old._conversation_id,
            'sources': refs,
        },
    }}, contextMessages=context)
    target_path = Path(target.getSessionFile()) if target.getSessionFile() else None
    target_bytes = target_path.read_bytes() if target_path is not None and target_path.exists() else None
    originals = [message for group in ce._session_originals(transcript).values() for message in group]
    old._ingest_cursor = 0
    old._schedule_ingest_cursor_reconciliation()
    try:
        old.rollover_session(old_id, new_id, previous_messages=originals,
                             carry_over_context=True, platform='misaka')
    except BaseException as error:
        # No native replacement has happened. Retire the partially rebound
        # engine so the old owner reopens from its last published checkpoint.
        try:
            # The target is still unpublished and has no writer. Restore any
            # already-moved DAG ownership before abandoning that target.
            old._dag.reassign_session_nodes(new_id, old_id)
            if (target_bytes is not None and target_path is not None and not target_path.is_symlink()
                    and target_path.exists() and target_path.read_bytes() == target_bytes):
                target_path.unlink()
        except Exception as rollback_error:  # noqa: BLE001 - preserve the initiating failure and recovery receipt
            # Keep the durable receipt if rollback itself failed; dropping it
            # would strand the only native reference to the moved summaries.
            error.add_note(f'Carry rollback failed; target receipt retained: {rollback_error}')
        finally:
            ce.close(ctx)
        raise
    # The target has no other owner yet. Transfer the same engine rather than
    # replaying reset/start on a second instance or retaining a dead registry key.
    with ce._REGISTRY_LOCK:
        ce._ENGINES.pop(ce._key(ctx))
        ce._ENGINES[(str(old._store.db_path), new_id)] = old
    old._ingest_cursor_needs_reconcile = True
    old._misaka_preflight_turn = None
    old._misaka_preanswer = None
    old._usage_anchor = None
    return len(nodes)
