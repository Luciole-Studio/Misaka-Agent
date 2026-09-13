"""Explicit continuity receipts, not copied history or global memory injection.

Raw rows stay with their original sessions. A branch-local native receipt attests
which foreign rows its carried summaries may cite. IDs alone are not identities:
the digest detects a replaced database reusing a numeric ID for another message.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import context_engine as ce
from . import ingest


def row_identity(row):
    fields = {key: row.get(key) for key in (
        'session_id', 'source', 'conversation_id', 'role', 'content',
        'tool_call_id', 'tool_calls', 'tool_name', 'observed_at')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def receipt(transcript):
    return next((entry for entry in reversed(transcript.branch)
                 if entry['type'] == 'compaction' and (entry.get('details') or {}).get('lcm', {}).get('carry')), None)


def sources(built, transcript):
    entry = receipt(transcript)
    if entry is None:
        return {}
    expected = entry['details']['lcm']['carry']['sources']
    rows = built._store.get_batch([int(key) for key in expected])
    for key, digest in expected.items():
        row = rows.get(int(key))
        if row is None or row_identity(row) != digest:
            raise ValueError(f'Carried LCM source {key} is missing or has changed; restore its original archive')
    return {int(key): digest for key, digest in expected.items()}


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
    rows = old._store.get_batch(sorted(ids))
    if len(rows) != len(ids):
        raise ValueError('Carry-over source archive is incomplete')
    target.appendCompaction('LCM explicit carry-over', '', 0, details={'lcm': {
        'nodes': records, 'scaffolds': [], 'carry': {
            'fromSession': old_id, 'conversation': old._conversation_id,
            'sources': {str(key): row_identity(row) for key, row in rows.items()},
        },
    }}, contextMessages=[])
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
        ce._ENGINES[(ce.config_bridge.database_path(), new_id)] = old
    old._ingest_cursor_needs_reconcile = True
    old._misaka_preflight_turn = None
    old._misaka_preanswer = None
    old._usage_anchor = None
    return len(nodes)
