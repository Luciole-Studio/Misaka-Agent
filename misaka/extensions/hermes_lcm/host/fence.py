"""Put misaka's untrusted-data fence back on anything LCM hands to the model.

misaka fences external text before it ever reaches the model: `prompt_guard.untrusted`
wraps a fetched page in sentinels and says, in the prompt's own voice, that the block is
data and cannot change the task. LCM then stores that text verbatim, so a raw row still
carries the fence -- but a *summary* does not. The summariser reads the fenced block and
writes fresh prose about it, and that prose comes back out through `lcm_grep`,
`lcm_expand` or `lcm_recall` wearing the system's own voice. An instruction planted in a
hostile page would arrive laundered, which is a real injection path, so LCM results that
touched fenced material are fenced again on the way out.

Upstream has no notion of this. Its quarantine is storage hygiene -- repeat-flood
isolation, oversized payloads, secret redaction -- not injection defence. So this is
misaka's own design, and it lands entirely in host: no vendored byte changes, no new
column, no write path.

Three facts make that possible:

* **The dye is already in the data.** `untrusted()` writes the sentinel into the text,
  the store keeps message content byte for byte, and `untrusted()` defangs any sentinel
  inside the body to `UNTRUSTED-DATA-ESCAPED` -- which still contains the marker. So
  "did this row come from fenced content" is answered by the row itself. A stored flag
  would be a second copy of a fact the first copy cannot lose, and it would have to be
  written from a seam host does not own: the engine ingests from inside `compress` and
  `handle_tool_call`, assigning store ids where no host code runs.
* **Lineage is upstream's own.** `summary_nodes.source_ids` walks down to the store rows
  a summary covers, through nested nodes, at any depth -- so a node inherits its sources'
  dye by query rather than by bookkeeping that could drift from the DAG.
* **The dye is monotone.** Content can add it -- a message that merely mentions the
  marker counts as untrusted -- and can never remove it. Over-marking degrades one result
  to data; under-marking would hand an attacker the prompt's voice. Forgery therefore
  buys nothing: it taints the forging row and whatever genuinely descends from it, and
  reaches no other row.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import AbstractContextManager

from misaka.core.platform.prompt_guard import MARKER, untrusted

logger = logging.getLogger(__name__)

# Keys upstream's tool payloads cite their material by. Every handler returns
# `json.dumps` of a structure, so reading the ids out of the parsed payload is exact
# where scraping the rendered text would be guesswork.
_STORE_KEYS = frozenset({"store_id", "store_ids", "source_store_id"})
_NODE_KEYS = frozenset({"node_id", "node_ids"})
# `lcm_recent` in rollup mode returns a period summary and cites it by rollup id alone --
# no store row, no node. `lcm_rollup_sources` maps it back to the nodes it was built from.
_ROLLUP_KEYS = frozenset({"rollup_id", "rollup_ids"})
# The side file an externalized payload moved to, cited by its own file name. Three tool
# answers reach that file without naming any row or node -- `lcm_expand` and
# `lcm_describe` in `externalized_ref` mode, and `lcm_grep(content_scope='externalized')`
# in each match -- so these keys are the only trace of the material they return.
_REF_KEYS = frozenset({"externalized_ref", "externalized_refs", "ref"})

# The V4 evidence family's own citation, and the one form of provenance that does not
# come with a `store_id` beside it: `lcm:<store_id>:<start>-<end>` names a character span
# inside one stored row. `evidence_compiler` delivers its `direct_fact`, its `evidence`
# entries and its rendered brief citing rows by this string *only* -- the hydrated dict
# that did carry `store_id` is filtered down to five keys on the way out
# (`requirements_compiler._deliver`) -- so `_collect`'s key list reaches none of it.
#
# Read out of the rendered text rather than out of named fields, because the text is
# where the spelling stops varying: `direct_fact.exact_ref`, `novel_exact_refs[]`,
# `computation.citations[]` and the `- exact evidence: [lcm:2:63-111] ...` line of the
# brief are four shapes of one citation, and a fifth arrives with the next upstream
# release. Over-reading is the safe direction here: a ref that a hostile row merely
# *quotes* resolves to that row, which the check then has to vouch for anyway.
_EXACT_REF = re.compile(r"\blcm:([1-9][0-9]{0,17}):[0-9]+-[0-9]+\b")

# SQLite's default parameter ceiling is 999; a tool result never cites near that many
# rows, but a chunked IN list costs one loop and removes the ceiling as a failure mode.
_MAX_PARAMS = 500


def cited_rows(text: str) -> set[int]:
    """The store rows one piece of LCM output cites by exact ref alone."""
    return {int(store_id) for store_id in _EXACT_REF.findall(text or "")}

# Content this check cannot see through, and therefore will not vouch for. The sentinel
# is the fence itself. The other half is upstream's externalization placeholder: with
# `large_output_externalization_enabled` a big tool result is replaced in `content` by
# `[Externalized tool output: ...; ref=FILE]` (or the `[GC'd externalized ...]` and
# `[Externalized LCM ingest payload: ...]` variants) and the real text moves to a side
# file. The dye leaves the row with it, so the row stops answering the question -- and a
# row that cannot answer is not a row to call clean.
#
# The side file the dye left *for* is opaque in the same way and for a stronger reason:
# it is not in the database at all, so no lineage query reaches it, and the tools that
# read it hand back a caller-chosen slice -- `lcm_expand(content_offset=...)` and
# `lcm_grep`'s match snippet both return the middle of a payload, where a fence's two
# sentinels are not. Reading the file to look would only move the guess: a bounded read
# can miss a marker past its bound, and missing one is the direction that costs the
# prompt its voice. So a cited ref is untrusted on sight, which is also the only answer
# consistent with the stub row that points at it already being one.
_STUB_HEAD = "xternalized "
_STUB_REF = "; ref="
_OPAQUE_ARGS = (MARKER, _STUB_HEAD, _STUB_REF)


def _opaque(column: str) -> str:
    return (f"instr({column}, ?) > 0 "
            f"OR (instr({column}, ?) > 0 AND instr({column}, ?) > 0)")


_TAINTED_ROW = """
    SELECT COUNT(*), MAX(CASE WHEN {opaque} THEN 1 ELSE 0 END) FROM messages
     WHERE store_id IN ({placeholders})
"""

# Upstream's own lineage walk (`SummaryDAG.source_message_ids`), asked a yes/no question
# instead of for the ids: does anything under these nodes -- at any depth, through nested
# nodes -- carry the fence. The walk carries the node it started from, because "no source
# row left" is an answer per node: one pruned lineage beside a live one still has to
# fence, and a `MAX()` over the union would hide it behind its neighbour.
_TAINTED_NODE = """
    WITH RECURSIVE walk(root, source_type, source_id) AS (
        SELECT n.node_id, n.source_type, CAST(j.value AS INTEGER)
          FROM summary_nodes n, json_each(n.source_ids) j
         WHERE n.node_id IN ({placeholders})

        UNION

        SELECT walk.root, child.source_type, CAST(j.value AS INTEGER)
          FROM summary_nodes child
          JOIN walk ON walk.source_type = 'nodes' AND child.node_id = walk.source_id
          JOIN json_each(child.source_ids) j
    )
    SELECT walk.root, MAX(CASE
        WHEN walk.source_type = 'messages' THEN
            CASE WHEN m.store_id IS NULL OR ({opaque}) THEN 1 ELSE 0 END
        WHEN walk.source_type = 'nodes' THEN
            CASE WHEN child.node_id IS NULL OR json_array_length(child.source_ids) = 0 THEN 1 ELSE 0 END
        ELSE 1 END), MAX(CASE WHEN walk.source_type = 'messages' THEN 1 ELSE 0 END)
      FROM walk
      LEFT JOIN messages m ON walk.source_type = 'messages' AND m.store_id = walk.source_id
      LEFT JOIN summary_nodes child ON walk.source_type = 'nodes' AND child.node_id = walk.source_id
     GROUP BY walk.root
"""

_ROLLUP_SOURCES = """
    SELECT rollup_id, node_id FROM lcm_rollup_sources
     WHERE rollup_id IN ({placeholders})
"""


def _integers(value) -> list[int]:
    """The ids under one payload field, whether it holds one or a list of them."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return [number for item in value for number in _integers(item)]
    return []


def _strings(value) -> list[str]:
    """The names under one payload field, whether it holds one or a list of them."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [name for item in value for name in _strings(item)]
    return []


def _collect(payload, stores: set[int], nodes: set[int], rollups: set[int], refs: set[str]) -> None:
    """Every store row, node, rollup and payload ref one tool answer cites, at any nesting."""
    if isinstance(payload, dict):
        for key, item in payload.items():
            if key in _STORE_KEYS:
                stores.update(_integers(item))
            elif key in _NODE_KEYS:
                nodes.update(_integers(item))
            elif key in _ROLLUP_KEYS:
                rollups.update(_integers(item))
            else:
                # A ref key is not a stopping point: `lcm_inspect` spells its inventory
                # as `externalized_refs: [{externalized_ref, store_id, ...}]`, and those
                # rows are worth reaching even though the ref alone already decides.
                if key in _REF_KEYS:
                    refs.update(_strings(item))
                _collect(item, stores, nodes, rollups, refs)
    elif isinstance(payload, list):
        for item in payload:
            _collect(item, stores, nodes, rollups, refs)


def _chunks(ids: set[int]) -> list[list[int]]:
    ordered = sorted(ids)
    return [ordered[start:start + _MAX_PARAMS] for start in range(0, len(ordered), _MAX_PARAMS)]


def _marks(chunk: list[int]) -> str:
    return ",".join("?" * len(chunk))


def _reader(engine) -> tuple[sqlite3.Connection, AbstractContextManager] | None:
    """The engine's database and the lock its statements have to be stepped under.

    `SummaryDAG.connection` is upstream's documented seam for exactly this -- read-only
    ad-hoc queries it does not wrap in a method -- and the DAG and the store share one
    file, so `messages` and `summary_nodes` join on it.

    The lock comes with it. One `sqlite3.Connection` cannot have two threads stepping
    cursors on it -- it raises "bad parameter or other API misuse", and worse, hands one
    thread's row to the other's cursor -- and misaka runs a tool batch in parallel, so
    these checks really do arrive at once. Upstream's own ad-hoc reader over this
    connection (`rollup_builder._scope_frontier`) takes the same `_db_lock` for the same
    reason. It is an `RLock`, so it is free when this thread already holds it.
    """
    dag = getattr(engine, "_dag", None)
    conn = getattr(dag, "connection", None)
    lock = getattr(dag, "_db_lock", None)
    if conn is None or lock is None:
        return None
    return conn, lock


def is_tainted(engine, *, store_ids=(), node_ids=(), rollup_ids=(), externalized_refs=()) -> bool:
    """Whether any of these rows, or any row any of these nodes was summarised from, is fenced.

    Fails closed: a question that cannot be answered is answered "untrusted", because the
    alternative is vouching for material nobody checked. "Cannot be answered" covers a
    missing database, a failing query, an externalized payload that left the database
    entirely, and -- since the answer lives in the source rows -- a node or rollup whose
    sources are no longer there to read.
    """
    if any(externalized_refs):
        return True
    stores = {int(value) for value in store_ids}
    nodes = {int(value) for value in node_ids}
    rollups = {int(value) for value in rollup_ids}
    if not stores and not nodes and not rollups:
        return False
    reader = _reader(engine)
    if reader is None:
        logger.warning("LCM fence check has no database to consult; treating the result as untrusted.")
        return True
    conn, lock = reader
    try:
        with lock:
            for chunk in _chunks(stores):
                sql = _TAINTED_ROW.format(placeholders=_marks(chunk), opaque=_opaque("content"))
                found, tainted = conn.execute(sql, [*_OPAQUE_ARGS, *chunk]).fetchone()
                if found != len(chunk) or tainted:
                    return True
            for chunk in _chunks(rollups):
                sql = _ROLLUP_SOURCES.format(placeholders=_marks(chunk))
                resolved = conn.execute(sql, chunk).fetchall()
                if len({row[0] for row in resolved}) != len(chunk):
                    return True
                nodes.update(row[1] for row in resolved)
            for chunk in _chunks(nodes):
                sql = _TAINTED_NODE.format(placeholders=_marks(chunk), opaque=_opaque("m.content"))
                reached = set()
                for root, tainted, has_messages in conn.execute(sql, [*chunk, *_OPAQUE_ARGS]):
                    if tainted or not has_messages:
                        return True
                    reached.add(root)
                if len(reached) != len(chunk):
                    return True
    except sqlite3.Error:
        logger.warning("LCM fence check failed; treating the result as untrusted.", exc_info=True)
        return True
    return False


def refence(output: str, *, engine, tool_name: str) -> str:
    """One LCM tool result, fenced if it touched untrusted material and untouched if not.

    `output` is the string `LCMEngine.handle_tool_call` returned, unmodified -- the ids
    are read out of that JSON, so anything wrapped around it first would be unreadable
    and get fenced whole.

    Not fencing clean results is the point of checking at all: a model told that every
    memory it retrieves is untrusted data would learn to discount all of them, which
    costs exactly the trust the fence exists to spend on the results that need it.
    """
    if not output:
        return output
    # Raw rows still carry their own fence, and so does a summary the deterministic
    # fallback assembled out of them. Cheaper than the database and true on its face.
    if MARKER in output:
        return untrusted(f"lcm:{tool_name}", output)
    try:
        payload = json.loads(output)
    except ValueError:
        logger.warning("LCM tool %s returned something other than JSON; fencing it unread.", tool_name)
        return untrusted(f"lcm:{tool_name}", output)
    stores: set[int] = cited_rows(output)
    nodes: set[int] = set()
    rollups: set[int] = set()
    refs: set[str] = set()
    _collect(payload, stores, nodes, rollups, refs)
    if is_tainted(engine, store_ids=stores, node_ids=nodes, rollup_ids=rollups, externalized_refs=refs):
        return untrusted(f"lcm:{tool_name}", output)
    return output
