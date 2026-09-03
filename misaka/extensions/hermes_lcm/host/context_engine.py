"""The vendored ``LCMEngine``, driven from misaka's compaction seam.

Upstream owns the whole message list: its host calls ``compress(messages)`` and replaces
its context with what comes back. misaka's seam is the other way round -- ``pi`` decides
*when* to compact and *where* to cut, then asks an extension for the summary text that
replaces everything before the cut. Reconciling the two is this module's whole job:

* the cut stays misaka's. ``pi`` already reasons about turn boundaries, split turns and
  ``keepRecentTokens``; a second opinion from the engine would only mean two boundaries
  disagreeing, and whichever lost would silently drop messages out of the live context.
* so for one host-driven call the engine's fresh tail is pinned to exactly the region
  ``pi`` is keeping. Everything ``pi`` drops is then, by construction, what the engine
  summarises into DAG leaves -- no message leaves the active context without a summary
  covering it, and none is summarised twice.
* the summary text is read back out of the assembled context the engine returns. That
  block is upstream's own summary prefix, DAG-derived and cumulative, which is exactly
  what a compaction entry should carry.

Everything here is fail-open, the posture the pre-port extension already had: any
failure returns ``None``, ``pi`` runs its native summariser for that round, and the
durable store keeps the originals either way.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager

from misaka.core.platform.prompt_guard import untrusted
from misaka.utils.values import read_field

from . import config_bridge, fence, ingest, llm, rollups

logger = logging.getLogger(__name__)

# Upstream's summary prefix, as `_assemble_context` writes it. Finding it is how the
# host tells "the engine produced a summary" from "the engine decided this was a noop".
_SUMMARY_BLOCK = re.compile(r"\[(?:Recent|Session Arc|Durable|Depth-\d+) Summary \(d(\d+), node (\d+)\)\]")

_ENGINES: dict[str, object] = {}

# Upstream serialises its writes and states that its reads are unlocked (`store.py`
# around `_write_lock`), which holds for one calling thread. Every caller on this side is
# off the event loop -- the tools (`tools._answer`) and the session events
# (`extension._off_loop`) alike -- so without this two worker threads step statements on
# the same sqlite3 connection: that raises `InterfaceError: bad parameter or other API
# misuse` and, worse, hands one thread's row to the other's cursor. One caller at a time,
# off the loop either way -- the point of the worker thread is that the session keeps
# running, not that two engine calls overlap.
#
# It also covers `_host_driven_boundary`: a compaction pins four config fields on the
# shared engine for the length of its run, and every reader of those fields
# (`externalize.stub_replay`, `preanswer.inject`) takes this lock, so none of them can
# observe a pinned value. Nothing here is re-entrant; a holder must not call another
# locked entry point.
ENGINE_LOCK = threading.Lock()


def engine():
    """The process-wide engine for the configured database.

    Never ``None``: a database that cannot be opened raises out of ``LCMEngine`` (through
    `extension.py`'s own `except Exception`), it is not cached as an absence. The
    ``built is None`` guards at the call sites are belt-and-braces for that day, not a
    reachable path; do not read them as "engine() may decline".
    """
    db_path = config_bridge.database_path()
    if db_path in _ENGINES:
        return _ENGINES[db_path]
    from ..vendor.engine import LCMEngine

    llm.install()
    config = config_bridge.load_config()
    try:
        built = LCMEngine(config=config, hermes_home="")
    except sqlite3.OperationalError:
        # Two misaka processes opening the database for the first time race inside
        # upstream's bootstrap -- one is mid-`CREATE VIRTUAL TABLE messages_fts` when the
        # other creates it. Upstream hosts one process and does not guard the window; the
        # loser only has to look again, because the winner has finished by then.
        logger.debug("LCM storage bootstrap raced another process; opening again.", exc_info=True)
        built = LCMEngine(config=config, hermes_home="")
    _ENGINES[db_path] = built
    return built


def close_all() -> None:
    """Release every cached engine. Used when a test or a migration changes the database."""
    for built in _ENGINES.values():
        if built is not None:
            built.shutdown()
    _ENGINES.clear()


@contextmanager
def _host_driven_boundary(config, fresh_tail_count: int):
    """Pin the engine's compaction boundary to the one ``pi`` chose, for one call.

    ``fresh_tail_count`` is a suffix length, so setting it to the number of messages
    ``pi`` keeps makes upstream's "raw backlog outside the fresh tail" equal to the
    region ``pi`` drops. The other two are the gates that would otherwise let the engine
    decline or split that region: the chunk-size floor (``pi`` has already decided it is
    time) and the dynamic chunker (which compacts a slice per pass and would leave the
    rest raw).
    """
    saved = (config.fresh_tail_count, config.fresh_tail_max_tokens,
             config.leaf_chunk_tokens, config.dynamic_leaf_chunk_enabled)
    config.fresh_tail_count = fresh_tail_count
    config.fresh_tail_max_tokens = 0
    config.leaf_chunk_tokens = 1
    config.dynamic_leaf_chunk_enabled = False
    try:
        yield
    finally:
        (config.fresh_tail_count, config.fresh_tail_max_tokens,
         config.leaf_chunk_tokens, config.dynamic_leaf_chunk_enabled) = saved


def _summary_of(messages) -> str:
    """The engine's summary prefix out of the context it assembled."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and _SUMMARY_BLOCK.search(content):
            return content
    return ""


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


def start(ctx) -> str:
    """Bind the engine to this misaka session. Returns the session id, or ``""``."""
    session_id = ingest.session_id(ctx)
    built = engine()
    if session_id and built is not None:
        built.on_session_start(session_id, platform="misaka")
    return session_id


def sync(ctx) -> None:
    """Persist the session's active context. Idempotent: upstream keeps its own cursor."""
    built = engine()
    if built is None or not ingest.session_id(ctx):
        return
    built.ingest(ingest.upstream_messages(ctx.sessionManager.buildSessionContext().messages))


def compact(event, ctx) -> dict | None:
    """Serve one ``session_before_compact``: the summary for the region ``pi`` drops."""
    from misaka.core.session_manager import build_session_context

    built = engine()
    if built is None:
        return None
    prep = read_field(event, "preparation")
    branch = list(read_field(event, "branchEntries") or [])
    dropped = list(read_field(prep, "messagesToSummarize") or []) + list(
        read_field(prep, "turnPrefixMessages") or []
    )
    if not dropped or not branch:
        return None

    messages = ingest.upstream_messages(build_session_context(branch).messages)
    # The built context leads with the previous compaction's summary when there is one;
    # that message belongs to neither the dropped region nor the kept tail.
    lead = 1 if read_field(prep, "previousSummary") is not None else 0
    fresh_tail_count = len(messages) - lead - len(ingest.upstream_messages(dropped))
    if fresh_tail_count <= 0:
        logger.warning("LCM boundary does not fit the built context; leaving this compaction native.")
        return None

    # `_config` is how upstream's own plugin entry point reaches an engine's config
    # (`getattr(active_engine, "_config", None)`); there is no other accessor.
    with _host_driven_boundary(built._config, fresh_tail_count):
        summary = _summary_of(built.compress(messages, current_tokens=int(read_field(prep, "tokensBefore", 0) or 0)))
    if not summary.strip():
        logger.warning(
            "LCM produced no summary (%s: %s); leaving this compaction native.",
            built.last_compression_status, built.last_compression_noop_reason or "-",
        )
        return None
    summary = _guarded_summary(built, summary)
    # This round published a summary node, which stales every rollup covering the days it
    # spans. Upstream only ever schedules that repair at session bind, and a misaka
    # session binds once and then runs for hours -- see `host/rollups.py`.
    rollups.nudge(built)

    details = None
    try:
        from misaka.core.compaction.utils import compute_file_lists

        details = compute_file_lists(read_field(prep, "fileOps"))
    except Exception:  # noqa: BLE001, S110 - the file lists are decoration on the entry
        pass
    logger.info("LCM compacted %d messages, keeping %d (%s)",
                len(messages) - lead - fresh_tail_count, fresh_tail_count, built.current_session_id)
    return {"compaction": {
        "summary": summary,
        "firstKeptEntryId": read_field(prep, "firstKeptEntryId"),
        "tokensBefore": int(read_field(prep, "tokensBefore", 0) or 0),
        "details": details,
    }}
