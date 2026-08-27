"""``misaka lcm status`` / ``doctor`` / ``rotate`` / ``preset`` -- upstream's operator surface.

The four operations here are the last of ``vendor/command.py`` that misaka had no door
for, and they are forwarded whole for the same reason ``host/embed.py`` and
``host/assertions.py`` forward theirs: the report *is* the contract. Upstream's
``status`` names the config precedence that actually won, its ``doctor`` runs the FTS
integrity checks and the payload scan, its ``rotate`` is backup-first and refuses ignored
sessions by name, and its ``preset`` carries the benchmark provenance behind every
number. A misaka-shaped rewrite of any of those is a second copy that drifts.

Two things here are not forwarding, and both exist because misaka's shape differs from
the host upstream was written for.

**status and doctor are a fork in the road, not an addition.** Two implementations own
the name ``misaka lcm``: the pre-port mini one and the ported engine. Their reports have
nothing in common -- the mini one counts rows, upstream's names sixty runtime fields --
so ``report`` answers with whichever implementation ``context_engine`` actually selected
and leaves the other's output alone. Forcing one shape onto both would mean throwing away
most of upstream's report to fit a header the mini one can fill, which is the opposite of
why the port happened.

**rotate needs a session named on the command line.** Upstream rotates *the active
session*, meaning the one its host has open in the process running the slash command. A
CLI has none, so the session is an argument, and an argument nobody can guess is worse
than no command at all -- hence the listing when it is missing.

What rotate means under misaka's compaction seam is worth stating, because it is not
quite what it means under Hermes'. It does not touch the live prompt: ``pi`` owns the
active context and rebuilds it from its own entries. What it advances is the persisted
lifecycle frontier, which ``_bind_lifecycle_state`` reads back on the *next* session
start -- so rotating a long-lived session is how the raw rows an engine would otherwise
re-examine on every bind stop being examined. The rows stay in the database, recoverable
through ``lcm_load_session`` and ``lcm_expand``, exactly as upstream promises.
"""

from __future__ import annotations

import sqlite3

from . import context_engine, rollups, switch

# How many sessions a bare `misaka lcm rotate` lists before it stops. Long enough to
# recognise the one you meant, short enough not to be the reason you scroll.
_LISTED_SESSIONS = 20

_NO_ENGINE = (
    "No usable LCM engine for {db}. A pre-port database has to be rebuilt first: "
    "`misaka lcm migrate --apply`."
)


def _command(tokens: str, built) -> str:
    """One ``/lcm <tokens>`` run against an engine this process already built."""
    from ..vendor.command import handle_lcm_command

    return handle_lcm_command(tokens, built)


def report(op: str) -> str | None:
    """``status`` or ``doctor`` from the ported engine, or ``None`` if it is not selected.

    ``None`` is the whole routing decision: the caller keeps its pre-port report when the
    pre-port implementation is what serves this install.
    """
    if not switch.selected():
        return None
    built = context_engine.engine()
    if built is None:
        return _NO_ENGINE.format(db=switch.database_path())
    try:
        text = _command(op, built)
        # Upstream's doctor reaches storage, FTS, redaction and externalized payloads --
        # everything its engine does unconditionally. The families behind their own
        # switches came later than it did and it never learned to ask about them.
        return "\n".join([text, "", *_families(built)]) if op == "doctor" else text
    finally:
        # A CLI run owns the engine it just built: leaving its sqlite connections open
        # would hold a WAL lock past the point the command has printed its answer.
        context_engine.close_all()


def rotate(session_id: str, *, apply: bool = False) -> str:
    """One ``/lcm rotate`` run against the named session."""
    built = context_engine.engine()
    if built is None:
        return _NO_ENGINE.format(db=switch.database_path())
    try:
        rows = built._store.scan_session_cleanup_stats()
        if not session_id:
            return _sessions_text(rows, "no session named")
        # Binding is how a CLI names a session to upstream, and binding *creates* the
        # lifecycle row it does not find -- so a mistyped id would leave a session behind
        # that has no messages and no nodes, which is exactly what doctor's
        # `empty_lifecycle_rows` reports as fragmentation. A preview may not do that, so
        # the name is checked against the store before anything is bound.
        if session_id not in {row[0] for row in rows}:
            return _sessions_text(rows, f"no session {session_id!r} in this database")
        # The same call `host/context_engine.start` makes every session, and a no-op
        # against the lifecycle row when the session is already its own current one --
        # which, since misaka never finalizes a session, is every session it wrote.
        built.on_session_start(session_id, platform="misaka")
        return _command("rotate apply" if apply else "rotate", built)
    finally:
        context_engine.close_all()


def preset(subcommand: str, name: str = "", *, apply: bool = False) -> str:
    """One ``/lcm preset show|suggest|apply`` run against the configured database."""
    built = context_engine.engine()
    if built is None:
        return _NO_ENGINE.format(db=switch.database_path())
    try:
        tokens = ["preset", subcommand]
        if name:
            tokens.append(name)
        # Upstream's `preset apply` writes no config in any mode: without `--dry-run` it
        # answers "preview-only for now" and stops. Forwarding that refusal verbatim
        # would send a misaka operator looking for a `--dry-run` flag this CLI does not
        # have -- dry run is its default -- so the preview is what gets forwarded and the
        # host says, in its own words, why `--apply` had nothing to commit.
        if subcommand == "apply":
            tokens.append("--dry-run")
        text = _command(" ".join(tokens), built)
        if subcommand == "apply" and apply:
            text += ("\nnote: upstream's preset apply is preview-only -- it writes no "
                     "config in any mode -- so --apply had nothing to commit and this is "
                     "the preview either way")
        return text
    finally:
        context_engine.close_all()


def _sessions_text(rows: list, reason: str) -> str:
    """What to rotate, when the operator did not name one this database has."""
    rows = sorted(rows, key=lambda row: -int(row[1]))
    lines = [
        "LCM rotate",
        "status: refused",
        f"reason: {reason}",
        ("note: upstream rotates the session its host has open; a CLI has none, so "
         "`misaka lcm rotate SESSION_ID` takes it as an argument"),
    ]
    if not rows:
        lines.append(f"note: no sessions in {switch.database_path()} to rotate")
        return "\n".join(lines)
    lines.append(f"sessions: {len(rows)}")
    for session_id, messages, tokens, nodes in rows[:_LISTED_SESSIONS]:
        lines.append(
            f"- {session_id}: {int(messages)} messages, ~{int(tokens)} tokens, "
            f"{int(nodes)} summary nodes"
        )
    if len(rows) > _LISTED_SESSIONS:
        lines.append(f"- ... {len(rows) - _LISTED_SESSIONS} more, largest first")
    return "\n".join(lines)


def _one(conn, query: str):
    """One row of one query, or ``None`` when the table it names is not in this database.

    A family that was never switched on has no tables, and that is an answer rather than
    an error -- so a missing table reads the same as an empty one to everything below.
    """
    if conn is None:
        return None
    try:
        return conn.execute(query).fetchone()
    except sqlite3.Error:
        return None


def _rows(conn, query: str) -> list:
    if conn is None:
        return []
    try:
        return conn.execute(query).fetchall()
    except sqlite3.Error:
        return []


def _count(conn, query: str) -> int:
    row = _one(conn, query)
    return int(row[0] or 0) if row else 0


def _state(enabled: bool, variable: str) -> str:
    return "enabled" if enabled else f"disabled (set {variable}=true)"


def _families(built) -> list[str]:
    """One line per opt-in family, plus the command that reports on it in full."""
    config = built._config
    store_conn = built._store.connection
    dag_conn = built._dag.connection
    lines = [
        "families:",
        ("note: the four families below are off by default and upstream's own doctor, "
         "written before three of them, does not ask about them"),
    ]

    threshold = config.large_output_externalization_threshold_chars
    lines += [
        "- large_output_externalization: "
        + _state(config.large_output_externalization_enabled, "LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED")
        + f" | over {threshold:,} chars | active-replay stubbing "
        + ("on" if config.large_output_active_replay_stubbing_enabled else "off")
        + " | payload counts above",
        "  -> misaka lcm externalize-backfill [--apply] [--limit N]",
    ]

    rollup = rollups.status(built._store.db_path)
    counted = {}
    for kinds in rollup["scopes"].values():
        for states in kinds.values():
            for state, count in states.items():
                counted[state] = counted.get(state, 0) + count
    lines += [
        "- temporal_rollups: "
        + _state(rollup["enabled"], "LCM_TEMPORAL_ROLLUPS_ENABLED")
        + (" | no rollup tables in this database" if not rollup["installed"] else
           " | " + (", ".join(f"{count} {state}" for state, count in sorted(counted.items())) or "nothing built")
           + f" across {len(rollup['scopes'])} scope(s)"
           + f" | {rollup['pending_invalidations']} pending invalidation(s)"),
        "  -> misaka lcm rollups [--rebuild]",
    ]
    if rollup["last_error"]:
        lines.append(f"  last build error: {rollup['last_error']}")

    profiles = _rows(store_conn, "SELECT task, provider, model_name, dim, dtype, active "
                                 "FROM lcm_embedding_profile ORDER BY task, model_name")
    nodes = _count(dag_conn, "SELECT COUNT(*) FROM summary_nodes")
    vectors = _count(store_conn, "SELECT COUNT(*) FROM lcm_embedding_vectors")
    chunks = _count(store_conn, "SELECT COUNT(*) FROM lcm_chunk_vectors")
    lines += [
        "- semantic_embeddings: "
        + _state(config.embeddings_enabled, "LCM_EMBEDDINGS_ENABLED")
        + f" | configured {config.embedding_provider or '(unset)'}/{config.embedding_model or '(unset)'}"
        + f" | {vectors} summary vector(s) for {nodes} summary node(s), {chunks} chunk vector(s)",
        "  -> misaka lcm embed warmup | misaka lcm embed backfill [--apply] [--limit N]",
    ]
    for task, provider, model_name, dim, dtype, active in profiles:
        lines.append(
            f"  registered {task} profile: {provider}/{model_name} dim {dim} {dtype}"
            + ("" if active else " (archived)")
        )
    if config.embeddings_enabled and not profiles:
        lines.append("  no profile registered yet; `misaka lcm embed warmup` locks the dimension")

    from ..vendor.assertion_store import CURRENT_EXTRACTION_VERSION

    totals = _one(store_conn, "SELECT COUNT(*), COUNT(DISTINCT source_store_id) FROM lcm_assertions")
    versions = _rows(store_conn, "SELECT extraction_version, COUNT(*) FROM lcm_assertions "
                                 "GROUP BY extraction_version ORDER BY extraction_version")
    lines += [
        "- assertions: "
        + _state(config.assertions_enabled, "LCM_ASSERTIONS_ENABLED")
        + " | extraction " + _state(config.assertion_extraction_enabled, "LCM_ASSERTION_EXTRACTION_ENABLED")
        + f" | {int(totals[0]) if totals else 0} assertion(s) from "
        + f"{int(totals[1]) if totals else 0} source row(s)",
        "  -> misaka lcm assertions rebuild [--apply] [--limit N]",
    ]
    # Only the versions that are *not* current earn a line: rows left behind by a moved
    # `CURRENT_EXTRACTION_VERSION` are the health signal, and "all of them are current"
    # is already what the count above says.
    for version, count in versions:
        if version != CURRENT_EXTRACTION_VERSION:
            lines.append(f"  {count} at superseded extraction version {version}; a rebuild re-derives them")
    return lines
