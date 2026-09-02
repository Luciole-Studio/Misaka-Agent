"""Upstream's temporal rollups (day/week/month) on misaka's shapes.

Two things live here, and nothing else: the one call that keeps rollups being built in a
misaka-shaped session, and the operator surface behind `misaka lcm rollups`.

**The build seam.** Upstream builds rollups in exactly one place: `_bind_lifecycle_state`
schedules a bounded background pass on every `on_session_start`. Nothing else in the
engine ever schedules one -- not compaction, not ingest, not a tool call. That suits
Hermes, whose gateway rebinds a session constantly, but misaka binds once at
`session_start` and then runs for hours: a session that compacts all afternoon publishes
summary node after summary node, each of which stales the rollups covering its days, and
no pass ever consumes them. So `nudge` asks for one pass per compaction -- the moment a
node is published is exactly the moment there is work. Upstream's scheduler dedupes by
(database, scope) and each pass is bounded by `LCM_ROLLUP_BUILDS_PER_PASS` and
`LCM_ROLLUP_MAINTENANCE_BUDGET_MS`, so a nudge with nothing to do costs one dictionary
lookup, and a nudge with work to do never touches the interactive turn.

**Multi-process.** misaka writes one `lcm.db` from many processes; Hermes writes it from
one. Everything that orders two builders lives in the database rather than in the
process -- a claim advances the row's `generation` and stamps a fresh `lease_nonce`, and
every terminal transition is a compare-and-set on (`rollup_id`, `generation`,
`lease_nonce`, `status='building'`) -- so a sister process that starts building the same
period simply wins or loses that CAS. What is *not* shared is upstream's in-process
scheduler and its operator lease: two processes can both decide to build the same period
and duplicate the summariser call. That is cost, not corruption, and it is why `rebuild`
below does not bother taking the operator lease (it would only exclude this process from
itself).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# A rebuild seeds every UTC day the stored summaries cover, so a corrupt or wildly
# skewed timestamp must not turn into a million queued periods. Roughly a year of
# history per scope is far more than the summariser can rebuild in one sitting anyway.
_MAX_SEEDED_DAYS = 400

# Each maintenance pass builds at most `rollup_builds_per_pass` periods, so a rebuild
# loops. The loop stops when a pass stops changing anything rather than when a pass
# attempts nothing, because upstream returns *builds started* -- and an aggregate whose
# daily is not ready yet is started, deferred, and picked again every pass, forever. This
# cap is only the backstop behind that.
_MAX_PASSES = 400

_COVERAGE_SQL = """
    SELECT session_id, COALESCE(earliest_at, created_at), COALESCE(latest_at, created_at)
      FROM summary_nodes
     WHERE session_id IS NOT NULL AND session_id != ''
"""

_COUNTS_SQL = """
    SELECT scope, period_kind, status, COUNT(*)
      FROM lcm_rollups
     GROUP BY scope, period_kind, status
"""

_OLDEST_STALE_SQL = """
    SELECT scope, MIN(period_start) FROM lcm_rollups
     WHERE status = 'stale' GROUP BY scope
"""

_UNFINISHED_SQL = """
    SELECT COUNT(*) FROM lcm_rollups WHERE scope = ? AND status != 'ready'
"""

_LAST_ERROR_SQL = """
    SELECT error FROM lcm_rollups
     WHERE error IS NOT NULL AND error != '' ORDER BY rollup_id DESC LIMIT 1
"""


def nudge(engine) -> None:
    """Ask for one bounded rollup pass over the session this engine is bound to.

    Silent and free when the feature is off, which is its default: the whole family is
    opt-in behind `LCM_TEMPORAL_ROLLUPS_ENABLED`, and an engine built without it has no
    rollup tables, no mutation triggers and nothing to maintain.
    """
    config = getattr(engine, "_config", None)
    session_id = str(getattr(engine, "current_session_id", "") or "")
    if config is None or not config.temporal_rollups_enabled or not session_id:
        return
    # Upstream's own scheduler call. It swallows its own failures (maintenance is
    # opportunistic and must never fail a foreground turn), so there is nothing to catch.
    engine._schedule_rollup_maintenance(session_id)


def _read_only(db_path: str):
    """A read-only connection, or ``None`` when there is no database to read."""
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def status(db_path: str) -> dict:
    """What the rollup tables in one database hold, without writing to it.

    Read-only on purpose: `RollupStore` creates its tables on construction, and an
    operator asking "is anything there" must not be the reason something appears.
    """
    from . import config_bridge

    payload = {
        "database": db_path,
        "enabled": bool(config_bridge.load_config().temporal_rollups_enabled),
        "installed": False,
        "scopes": {},
        "oldest_stale": {},
        "pending_invalidations": 0,
        "last_error": None,
    }
    conn = _read_only(db_path)
    if conn is None:
        return payload
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "lcm_rollups" not in tables:
            return payload
        payload["installed"] = True
        for scope, period_kind, state, count in conn.execute(_COUNTS_SQL):
            payload["scopes"].setdefault(str(scope), {}).setdefault(str(period_kind), {})[str(state)] = int(count)
        payload["oldest_stale"] = {str(scope): str(oldest) for scope, oldest in conn.execute(_OLDEST_STALE_SQL)}
        if "lcm_rollup_invalidations" in tables:
            payload["pending_invalidations"] = int(
                conn.execute("SELECT COUNT(*) FROM lcm_rollup_invalidations").fetchone()[0] or 0
            )
        row = conn.execute(_LAST_ERROR_SQL).fetchone()
        payload["last_error"] = str(row[0]) if row else None
    except sqlite3.Error:
        # A database from a build that predates these tables, or one being written to
        # right now. Reporting what was readable beats a traceback at an operator.
        logger.debug("LCM rollup status query failed.", exc_info=True)
    finally:
        conn.close()
    return payload


def _days(first: float, last: float) -> list[str]:
    """Every UTC day an interval touches, bounded."""
    try:
        start = datetime.fromtimestamp(min(first, last), tz=UTC).date()
        end = datetime.fromtimestamp(max(first, last), tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        return []
    # Clamp from the *old* end. The cap exists for the skewed timestamp the comment on
    # _MAX_SEEDED_DAYS names, and keeping the oldest 400 days handed that case the whole
    # budget: one summary node stamped 0 filled the seed set with empty 1970 dates and left
    # the recent days -- the ones with content, the ones a rebuild exists to repair -- unseeded.
    start = max(start, end - timedelta(days=_MAX_SEEDED_DAYS - 1))
    span = (end - start).days
    return [(start + timedelta(days=offset)).isoformat() for offset in range(span + 1)]


def _unfinished(dag, scope: str) -> int:
    """Periods in this scope a further pass could still change. 0 means done."""
    conn = getattr(dag, "connection", None)
    if conn is None:
        return 0
    try:
        with dag._db_lock:
            row = conn.execute(_UNFINISHED_SQL, (scope,)).fetchone()
    except sqlite3.Error:
        # No rollup tables yet, or a database being written to. Treating that as "no
        # work" is the safe direction: the loop stops rather than spins.
        logger.debug("LCM could not count unfinished rollups.", exc_info=True)
        return 0
    return int(row[0] or 0)


def _covered_days(dag) -> dict[str, set[str]]:
    """The UTC days each scope's summary nodes cover -- the days a rebuild has to seed.

    Scope is the session id: that is what the invalidation triggers write into
    `lcm_rollups.scope`, so seeding by any other key would queue periods no builder ever
    looks for. Days with no canonical-frontier content resolve themselves away on the
    first pass, so seeding a day too many costs a build slot, not a wrong rollup.
    """
    covered: dict[str, set[str]] = {}
    conn = getattr(dag, "connection", None)
    if conn is None:
        return covered
    with dag._db_lock:
        rows = conn.execute(_COVERAGE_SQL).fetchall()
    for scope, first, last in rows:
        if first is not None and last is not None:
            covered.setdefault(str(scope), set()).update(_days(float(first), float(last)))
    return covered


def rebuild(db_path: str) -> dict:
    """Re-seed every period the stored summaries cover and build until they drain.

    This is the repair path, and the migration path: enabling the feature on a database
    that already has history installs the mutation triggers but leaves no invalidation
    events behind for the summaries written before them, so nothing queues itself. A
    rebuild seeds those days explicitly, in one transaction per scope, and then runs
    upstream's own bounded pass in a loop until there is no stale work left.

    It calls the summariser once per period, so it costs auxiliary-model calls in
    proportion to the history it covers.
    """
    from ..vendor.rollup_builder import run_rollup_maintenance
    from ..vendor.rollup_store import RollupStore
    from . import context_engine

    engine = context_engine.engine()
    if engine is None:
        return {"database": db_path, "error": "no usable LCM engine for this database"}
    config = engine._config
    if not config.temporal_rollups_enabled:
        return {"database": db_path, "error": "temporal rollups are disabled "
                                              "(set LCM_TEMPORAL_ROLLUPS_ENABLED=true)"}
    dag = engine._dag
    try:
        seeded: dict[str, int] = {}
        store = RollupStore(dag.db_path)
        try:
            for scope, days in sorted(_covered_days(dag).items()):
                # Newest end again: the union of several nodes' day sets can exceed the cap
                # even when each stayed under it, and the days worth rebuilding are recent.
                targets = [("day", day, scope) for day in sorted(days)[-_MAX_SEEDED_DAYS:]]
                seeded[scope] = store.upsert_stale_many(targets)
        finally:
            store.close()

        built, exhausted = 0, []
        for scope in seeded:
            passes = 0
            while passes < _MAX_PASSES:
                remaining = _unfinished(dag, scope)
                if not remaining:
                    break
                built += run_rollup_maintenance(dag, config, scope)
                passes += 1
                if _unfinished(dag, scope) >= remaining:
                    # A pass that moved nothing will move nothing next time either: what is
                    # left is waiting on something this loop cannot supply -- a daily inside
                    # its retry backoff, or an aggregate whose day never will be ready.
                    exhausted.append(scope)
                    break
            else:
                exhausted.append(scope)
        return {"database": db_path, "seeded": seeded, "built": built, "exhausted": exhausted,
                "status": status(db_path)}
    finally:
        # A CLI run owns the engine it just built: leaving its sqlite connections open
        # would hold a WAL lock past the point the command has printed its answer.
        # (embed.run / assertions.rebuild / operations.* all say the same thing.)
        context_engine.close_all()
