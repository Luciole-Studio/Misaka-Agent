"""Natural-time parsing for temporal rollup retrieval."""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence

from .sqlite_util import _sqlite_savepoint


@dataclass(frozen=True)
class CoverageNode:
    """A summary node's coverage span, for interval-aware rollup selection.

    ``source_node_ids`` are the *node* ids this node condenses (empty for
    message-sourced leaves); ``depth`` is its DAG depth. ``earliest_at`` /
    ``latest_at`` are its covered-span bounds (newest/oldest covered message
    timestamps), used to reason about which UTC days the node covers.
    """

    node_id: int
    depth: int
    source_node_ids: tuple[int, ...] = ()
    earliest_at: float | None = None
    latest_at: float | None = None


class CanonicalFrontierOverlapError(RuntimeError):
    """Raised when candidate lineages overlap without one containing the other."""


def load_source_lineage(
    connection: Any,
    root_node_ids: Sequence[int],
    *,
    limit: int,
) -> dict[int, tuple[int, ...]]:
    """Load lineage without leaking the TEMP-staging transaction to callers."""
    with _sqlite_savepoint(connection):
        return _load_source_lineage_staged(
            connection,
            root_node_ids,
            limit=limit,
        )


def _load_source_lineage_staged(
    connection: Any,
    root_node_ids: Sequence[int],
    *,
    limit: int,
) -> dict[int, tuple[int, ...]]:
    """Load a bounded transitive node-source graph for candidate roots.

    Intermediate ancestors may be outside a retrieval window or absent from an
    aggregate's selected source IDs. A fixed-size temp frontier walks only named
    node IDs, and every node/edge query has a SQL ``LIMIT``. This avoids the
    recursive-CTE trap where a final ``ORDER BY`` materializes a huge closure
    before an outer limit can apply.
    """
    roots = list(dict.fromkeys(int(node_id) for node_id in root_node_ids))
    if not roots:
        return {}
    work_limit = max(0, int(limit))
    if len(roots) > work_limit:
        raise RuntimeError(f"source lineage exceeds bounded work limit ({limit})")

    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS lcm_lineage_frontier "
        "(node_id INTEGER PRIMARY KEY) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS lcm_lineage_seen "
        "(node_id INTEGER PRIMARY KEY) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS lcm_lineage_current "
        "(node_id INTEGER PRIMARY KEY) WITHOUT ROWID"
    )
    for table in ("lcm_lineage_frontier", "lcm_lineage_seen", "lcm_lineage_current"):
        connection.execute(f"DELETE FROM temp.{table}")
    connection.executemany(
        "INSERT INTO temp.lcm_lineage_frontier(node_id) VALUES(?)",
        ((node_id,) for node_id in roots),
    )

    lineage: dict[int, tuple[int, ...]] = {}
    node_work = 0
    edge_work = 0
    try:
        while True:
            remaining = work_limit - node_work
            if remaining <= 0:
                pending = connection.execute(
                    "SELECT 1 FROM temp.lcm_lineage_frontier LIMIT 1"
                ).fetchone()
                if pending is not None:
                    raise RuntimeError(
                        f"source lineage exceeds bounded work limit ({limit})"
                    )
                break
            batch_size = min(256, remaining)
            rows = connection.execute(
                """
                SELECT frontier.node_id, node.source_type
                FROM temp.lcm_lineage_frontier frontier
                LEFT JOIN summary_nodes node ON node.node_id = frontier.node_id
                LIMIT ?
                """,
                (batch_size,),
            ).fetchall()
            if not rows:
                break

            current_ids = [int(row[0]) for row in rows]
            node_work += len(current_ids)
            connection.execute("DELETE FROM temp.lcm_lineage_current")
            connection.executemany(
                "INSERT INTO temp.lcm_lineage_current(node_id) VALUES(?)",
                ((node_id,) for node_id in current_ids),
            )
            connection.executemany(
                "DELETE FROM temp.lcm_lineage_frontier WHERE node_id=?",
                ((node_id,) for node_id in current_ids),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO temp.lcm_lineage_seen(node_id) VALUES(?)",
                ((node_id,) for node_id in current_ids),
            )

            edge_budget = work_limit - edge_work
            edge_rows = connection.execute(
                """
                SELECT parent.node_id, CAST(edge.value AS INTEGER)
                FROM temp.lcm_lineage_current current
                JOIN summary_nodes parent ON parent.node_id = current.node_id
                JOIN json_each(parent.source_ids) edge
                WHERE parent.source_type = 'nodes'
                LIMIT ?
                """,
                (edge_budget + 1,),
            ).fetchall()
            if len(edge_rows) > edge_budget:
                raise RuntimeError(
                    f"source lineage exceeds bounded work limit ({limit})"
                )
            edge_work += len(edge_rows)

            sources_by_parent: dict[int, list[int]] = {
                node_id: [] for node_id, source_type in rows if source_type == "nodes"
            }
            for parent_id, source_id in edge_rows:
                sources_by_parent.setdefault(int(parent_id), []).append(int(source_id))
            for node_id, source_type in rows:
                if source_type is not None:
                    lineage[int(node_id)] = tuple(sources_by_parent.get(int(node_id), ()))

            if edge_rows:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO temp.lcm_lineage_frontier(node_id)
                    SELECT ?
                    WHERE NOT EXISTS(
                        SELECT 1 FROM temp.lcm_lineage_seen WHERE node_id=?
                    )
                    """,
                    ((int(source_id), int(source_id)) for _parent_id, source_id in edge_rows),
                )
    finally:
        for table in (
            "lcm_lineage_frontier",
            "lcm_lineage_seen",
            "lcm_lineage_current",
        ):
            connection.execute(f"DELETE FROM temp.{table}")
    return lineage


def canonical_frontier(
    nodes: Sequence[CoverageNode],
    *,
    source_lineage: Mapping[int, Sequence[int]] | None = None,
) -> list[CoverageNode]:
    """Return the canonical, order-independent covering set of ``nodes``.

    Every candidate is reduced to its transitive terminal-node lineage. Exact
    lineage duplicates collapse to one deterministic representative (greatest
    depth, then lowest node id), and a proper superset suppresses its subsets.
    Partial overlaps are ambiguous: keeping both double-consumes shared content,
    while dropping either loses unique content, so callers must defer/fail closed.

    Callers whose candidates can omit intermediate nodes supply the bounded
    ``source_lineage`` closure loaded by :func:`load_source_lineage`.
    """
    lineage = {
        int(node_id): tuple(int(source_id) for source_id in source_ids)
        for node_id, source_ids in (source_lineage or {}).items()
    }
    for node in nodes:
        lineage.setdefault(node.node_id, node.source_node_ids)

    leaf_memo: dict[int, frozenset[int]] = {}

    def terminal_lineage(node_id: int, active: set[int]) -> frozenset[int]:
        cached = leaf_memo.get(node_id)
        if cached is not None:
            return cached
        if node_id in active:
            raise CanonicalFrontierOverlapError(
                f"summary lineage cycle reaches node {node_id}"
            )
        source_ids = lineage.get(node_id, ())
        if not source_ids:
            leaves = frozenset((node_id,))
        else:
            active.add(node_id)
            try:
                leaves = frozenset(
                    leaf_id
                    for source_id in source_ids
                    for leaf_id in terminal_lineage(int(source_id), active)
                )
            finally:
                active.remove(node_id)
        leaf_memo[node_id] = leaves
        return leaves

    by_leaf_set: dict[frozenset[int], list[CoverageNode]] = {}
    for node in nodes:
        by_leaf_set.setdefault(terminal_lineage(node.node_id, set()), []).append(node)

    representatives = {
        leaf_set: min(group, key=lambda node: (-node.depth, node.node_id))
        for leaf_set, group in by_leaf_set.items()
    }
    leaf_sets = list(representatives)
    for index, left in enumerate(leaf_sets):
        for right in leaf_sets[index + 1 :]:
            if left & right and not (left <= right or right <= left):
                raise CanonicalFrontierOverlapError(
                    "summary lineages partially overlap without containment"
                )

    maximal = {
        leaf_set
        for leaf_set in leaf_sets
        if not any(leaf_set < other for other in leaf_sets)
    }
    selected_ids = {
        representatives[leaf_set].node_id
        for leaf_set in maximal
    }
    return [node for node in nodes if node.node_id in selected_ids]


@dataclass(frozen=True)
class RecentPeriodWindow:
    """A normalized UTC ``[start, end)`` retrieval window."""

    period: str
    start: datetime
    end: datetime
    rollup_kind: str
    subday: bool = False


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _checked_date_add(value: date, days: int) -> date:
    try:
        return value + timedelta(days=days)
    except OverflowError as exc:
        raise ValueError("period is outside the supported date range") from exc


def _exclusive_day_end(value: date) -> datetime:
    return _day_start(_checked_date_add(value, 1))


def parse_recent_period(period: str, *, now: datetime | None = None) -> RecentPeriodWindow:
    """Parse an ``lcm_recent`` period into a deterministic UTC window."""
    if not isinstance(period, str) or not period.strip():
        raise ValueError("period is required")

    requested = " ".join(period.strip().lower().split())
    current = _utc_now(now)
    today = current.date()

    if requested == "today":
        start = _day_start(today)
        return RecentPeriodWindow(requested, start, _exclusive_day_end(today), "day")

    if requested == "yesterday":
        yesterday = _checked_date_add(today, -1)
        start = _day_start(yesterday)
        return RecentPeriodWindow(requested, start, _exclusive_day_end(yesterday), "day")

    if requested == "week":
        week_start = _checked_date_add(today, -today.weekday())
        start = _day_start(week_start)
        return RecentPeriodWindow(
            requested, start, _day_start(_checked_date_add(week_start, 7)), "week"
        )

    if requested == "month":
        month_start = today.replace(day=1)
        start = _day_start(month_start)
        month_last = month_start.replace(day=monthrange(today.year, today.month)[1])
        end = _exclusive_day_end(month_last)
        return RecentPeriodWindow(requested, start, end, "month")

    date_match = re.fullmatch(r"date:(\d{4}-\d{2}-\d{2})", requested)
    if date_match:
        try:
            parsed_date = date.fromisoformat(date_match.group(1))
        except ValueError as exc:
            raise ValueError("period date must be a valid YYYY-MM-DD") from exc
        start = _day_start(parsed_date)
        return RecentPeriodWindow(
            requested, start, _exclusive_day_end(parsed_date), "day"
        )

    days_match = re.fullmatch(r"(\d+)d", requested)
    if days_match:
        days = int(days_match.group(1))
        if days <= 0:
            raise ValueError("day period must be at least 1d")
        start = _day_start(_checked_date_add(today, -(days - 1)))
        return RecentPeriodWindow(requested, start, _exclusive_day_end(today), "day")

    hours_match = re.fullmatch(r"last (\d+)h", requested)
    if hours_match:
        hours = int(hours_match.group(1))
        if hours <= 0:
            raise ValueError("hour period must be at least last 1h")
        try:
            start = current - timedelta(hours=hours)
        except OverflowError as exc:
            raise ValueError("hour period is outside the supported date range") from exc
        return RecentPeriodWindow(requested, start, current, "day", subday=True)

    raise ValueError(
        "period must be one of: today, yesterday, Nd, week, month, "
        "date:YYYY-MM-DD, last Nh"
    )
