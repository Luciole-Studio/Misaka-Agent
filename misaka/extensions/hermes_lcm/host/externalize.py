"""Upstream's large-output externalization, on the two misaka seams it needs.

Externalization itself is entirely upstream's: ``MessageStore.append`` runs every message
through ``protect_message_for_ingest`` before it reaches ``messages.content``, so an
oversized tool result lands in the database as ``[Externalized tool output: ...;
ref=FILE]`` while its bytes go to a side file. That happens with no host code at all --
which is why this module is small, and why what is in it is the part upstream's own host
gets for free and misaka does not.

**The live prompt.** Hermes hands its whole message list to ``ContextEngine.compress``
and replaces its context with the return value, so upstream's active-replay stubbing
reaches the provider by simply being in that list. misaka's compaction seam is narrower:
``pi`` owns the context and asks only for the summary that replaces the region it drops,
so nothing upstream does to the fresh tail can arrive that way. The equivalent seam here
is ``transformContext`` -- the extension ``context`` event -- which is the last thing to
touch the messages before they are sent and the only place a kept message can be
rewritten. So ``stub_replay`` runs upstream's own transform there.

**Old rows.** Turning the switch on helps traffic from that moment; a database that has
already swallowed a year of build logs stays swallowed. ``plan``/``run`` externalize
those rows after the fact, dry-run first, in the shape upstream's ingest would have
written them so nothing downstream can tell the two apart.

Both are inert unless the operator sets ``LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED``
(and, for the live prompt, ``LCM_LARGE_OUTPUT_ACTIVE_REPLAY_STUBBING_ENABLED``). That is
upstream's posture and the port keeps it: nothing here changes a byte until asked.
"""

from __future__ import annotations

import logging
import sqlite3

from misaka.ai.types import TextContent
from misaka.utils.values import read_field

from . import context_engine, ingest

logger = logging.getLogger(__name__)


# -- the live prompt ---------------------------------------------------------------

def stub_replay(event, ctx) -> dict | None:
    """Serve one ``context`` event: the messages, with oversized tool payloads as refs.

    Returns ``None`` when nothing would change, which is both the disabled default and
    the common case -- the runner then keeps the messages it already has.

    The decision is upstream's ``_stub_large_tool_results_for_active_replay``, called
    rather than reimplemented: it owns the two switches, the token threshold, the
    protected fresh tail, the "leave the payload the model just asked to expand alone"
    exemption, and the rule that structured content keeps its block shape. What is left
    for host is the join back onto misaka's messages, and that goes by ``toolCallId``
    rather than by position, because ``convert_to_llm`` drops messages excluded from
    context and the two lists are therefore not the same length.

    Deliberately synchronous on the event loop. Upstream reads the protected tail length
    out of the live config, and ``context_engine`` pins that field for the duration of
    one compaction -- a window with no ``await`` in it, so nothing else in this process
    can observe the pinned value unless this work is moved off the loop.
    """
    built = context_engine.engine()
    if built is None or not getattr(built._config, "large_output_active_replay_stubbing_enabled", False):
        return None
    # The payload files this writes are stamped with the engine's bound session, and
    # `lcm_expand` refuses a ref another session owns. One engine serves every session
    # in this process, so the binding has to be checked, not assumed.
    if built.current_session_id != ingest.session_id(ctx):
        context_engine.start(ctx)
    messages = list(read_field(event, "messages") or [])
    upstream = ingest.upstream_messages(messages)
    stubbed = built._stub_large_tool_results_for_active_replay(upstream)
    replacements = {
        str(after.get("tool_call_id") or ""): after.get("content")
        for before, after in zip(upstream, stubbed)
        if before is not after and after.get("role") == "tool" and after.get("tool_call_id")
    }
    if not replacements:
        return None
    rewritten = []
    for message in messages:
        placeholder = None
        if read_field(message, "role") == "toolResult":
            placeholder = replacements.get(str(read_field(message, "toolCallId") or ""))
        if placeholder is None:
            rewritten.append(message)
            continue
        # A validated block, not the dict it would accept: `model_copy` does not run the
        # validators, so a dict left here would travel as a dict through everything that
        # expects a content model -- `convert_to_llm` among them.
        rewritten.append(message.model_copy(update={"content": [TextContent(text=placeholder)]}))
    logger.info("LCM replaced %d oversized tool result(s) in the live context with refs.",
                len(replacements))
    return {"messages": rewritten}


# -- old rows ----------------------------------------------------------------------

# Only these rows are eligible, and the list is upstream's rather than this module's
# judgement: `gc_externalized_tool_result` -- the write upstream already uses to shrink a
# row it has externalized -- accepts a tool row that is not pinned and is not already
# holding this exact text, and refuses everything else. Selecting the same set here just
# means the scan does not walk rows the write would decline.
_CANDIDATES = """
    SELECT store_id, session_id, tool_call_id, content
      FROM messages
     WHERE role = 'tool' AND pinned = 0 AND length(content) > ?
     ORDER BY store_id
"""


def _eligible(engine, limit: int | None):
    """Rows a backfill would move, newest threshold and placeholders already excluded."""
    from ..vendor.externalize import is_externalized_placeholder
    from ..vendor.ingest_protection import is_externalized_ingest_placeholder

    threshold = max(1, int(getattr(engine._config, "large_output_externalization_threshold_chars", 0) or 0))
    found = []
    for store_id, session_id, tool_call_id, content in engine._store._conn.execute(_CANDIDATES, (threshold,)):
        text = content or ""
        if is_externalized_placeholder(text) or is_externalized_ingest_placeholder(text):
            continue
        found.append((store_id, session_id or "", tool_call_id or "", text))
        if limit is not None and len(found) >= limit:
            break
    return found


_OFF = "externalization is off; set LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED=true first"


def _report(engine, rows) -> dict:
    enabled = bool(getattr(engine._config, "large_output_externalization_enabled", False))
    return {
        "database": str(engine._store.db_path),
        "enabled": enabled,
        "directory": str(getattr(engine._config, "large_output_externalization_path", "") or ""),
        "threshold_chars": int(getattr(engine._config, "large_output_externalization_threshold_chars", 0) or 0),
        "rows": len(rows),
        "chars": sum(len(row[3]) for row in rows),
        # The scan does not depend on the switch, and neither does the count -- but
        # `--apply` would move nothing with the switch off, so a plan that stayed silent
        # about it would be promising work that cannot happen.
        "note": "" if enabled else _OFF,
        "bytes": 0,
    }


def plan(limit: int | None = None) -> dict:
    """What a backfill would move, without writing anything."""
    engine = context_engine.engine()
    if engine is None:
        return {"database": "", "enabled": False, "directory": "", "threshold_chars": 0,
                "rows": 0, "chars": 0, "bytes": 0, "note": "no ported LCM database is available"}
    return _report(engine, _eligible(engine, limit))


def run(limit: int | None = None) -> dict:
    """Externalize the eligible rows for real. Returns the plan plus what moved.

    Fail-open in upstream's sense: a row whose payload could not be written keeps its
    original content, so a full disk costs the space saving and nothing else.
    """
    from ..vendor.externalize import maybe_externalize_payload

    engine = context_engine.engine()
    if engine is None:
        return {**plan(limit), "applied": False}
    rows = _eligible(engine, limit)
    report = _report(engine, rows)
    if not report["enabled"]:
        return {**report, "applied": False, "moved": 0, "chars_moved": 0}

    def _archive(conn, store_id):
        # Inside the rewrite's own transaction, as upstream's transcript GC does it: the
        # row's chunk offsets describe content that is one statement away from being
        # gone, and a recall landing between two commits would slice the stub at them.
        engine._archive_chunks_for_messages([store_id], connection=conn)

    moved = 0
    chars_moved = 0
    for store_id, session_id, tool_call_id, content in rows:
        externalized = maybe_externalize_payload(
            content,
            kind="tool_result",
            tool_call_id=tool_call_id,
            session_id=session_id,
            role="tool",
            config=engine._config,
            hermes_home=engine._hermes_home,
        )
        if externalized is None:
            continue
        if engine._store.gc_externalized_tool_result(store_id, externalized["placeholder"], before_commit=_archive):
            moved += 1
            chars_moved += len(content) - len(externalized["placeholder"])
    freed = _reclaim(engine) if moved else 0
    return {**report, "applied": True, "moved": moved, "chars_moved": chars_moved, "bytes": freed}


def _reclaim(engine) -> int:
    """Give the pages the rewrites emptied back to the filesystem. Returns bytes freed.

    Without this the command would be telling a half-truth: the rows really did shrink,
    but SQLite keeps the freed pages for its own reuse, so the file stays its old size
    and every backup still copies the megabytes this was run to be rid of. VACUUM takes
    an exclusive lock for as long as it takes to rewrite the file, which is why it
    belongs here -- in a maintenance command an operator invoked with ``--apply`` -- and
    nowhere on the ingest path.
    """
    path = engine._store.db_path

    def _folded() -> int:
        """The size of the database with its write-ahead log folded back into it.

        Both ends of the subtraction have to be measured this way or the answer is
        nonsense: the rewrites are still in the log when this starts, so an unfolded
        "before" is the size of a nearly empty file, and the "after" -- which the VACUUM
        does fold -- comes out larger than it.
        """
        engine._store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return path.stat().st_size if path.exists() else 0

    try:
        before = _folded()
        engine._store._conn.execute("VACUUM")
        return max(0, before - _folded())
    except sqlite3.Error:
        logger.warning("LCM could not compact %s after the backfill; the rows moved anyway.",
                       path, exc_info=True)
        return 0
