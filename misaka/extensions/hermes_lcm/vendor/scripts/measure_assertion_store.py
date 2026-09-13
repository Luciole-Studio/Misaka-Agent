#!/usr/bin/env python3
"""Measure the provider-free V4 assertion store on synthetic exact rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarking.replay import _ensure_hermes_lcm_package

_ensure_hermes_lcm_package()

from hermes_lcm.assertion_state import query_assertion_state
from hermes_lcm.assertion_store import AssertionCandidate, AssertionStore
from hermes_lcm.store import MessageStore


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _latency_summary(values: list[float]) -> dict[str, float]:
    return {
        "mean_ms": round(statistics.fmean(values), 6),
        "p50_ms": round(_percentile(values, 0.50), 6),
        "p95_ms": round(_percentile(values, 0.95), 6),
        "p99_ms": round(_percentile(values, 0.99), 6),
        "max_ms": round(max(values), 6),
    }


def _persistent_db_bytes(db_path: Path) -> int:
    return sum(
        path.stat().st_size
        for path in (db_path, Path(f"{db_path}-wal"))
        if path.exists()
    )


def _checkpoint(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def run_measurement(source_count: int, query_count: int) -> dict[str, object]:
    predicate_count = min(100, max(1, source_count))
    rng = random.Random(20260719)
    with tempfile.TemporaryDirectory(prefix="hermes-lcm-assertion-measure-") as temp:
        db_path = Path(temp) / "lcm.db"
        messages = MessageStore(db_path)
        assertions = AssertionStore(db_path)
        snapshots = []
        for index in range(source_count):
            metric = index % predicate_count
            value = index % 7
            content = f"Evidence {index}: value {value} for metric {metric}."
            store_id = messages.append(
                f"session-{index % 25}",
                {"role": "user", "content": content},
            )
            snapshots.append(assertions.snapshot_source(store_id))

        _checkpoint(messages._conn)
        raw_db_bytes = _persistent_db_bytes(db_path)

        publish_latencies: list[float] = []
        publish_failures = 0
        for index, snapshot in enumerate(snapshots):
            metric = index % predicate_count
            value = index % 7
            quote = f"value {value} for metric {metric}"
            start = snapshot.content.index(quote)
            candidate = AssertionCandidate(
                source_span_start=start,
                source_span_end=start + len(quote),
                subject_key="user:self",
                predicate_key=f"metric.{metric}",
                object_value=value,
                value_text=str(value),
                kind="fact",
                confidence=1.0,
            )
            started = time.perf_counter_ns()
            try:
                assertions.publish_source(snapshot, [candidate])
            except Exception:
                publish_failures += 1
            publish_latencies.append((time.perf_counter_ns() - started) / 1_000_000)

        _checkpoint(assertions.connection)
        asserted_db_bytes = _persistent_db_bytes(db_path)
        coverage = assertions.connection.execute(
            """
            SELECT a.source_span_start, a.source_span_end, a.source_quote,
                   m.content
            FROM lcm_assertions AS a
            JOIN lcm_assertion_sources AS s
              ON s.source_store_id = a.source_store_id
             AND s.extraction_version = a.extraction_version
             AND s.source_content_sha256 = a.source_content_sha256
             AND s.invalidated_at IS NULL
            JOIN messages AS m ON m.store_id = a.source_store_id
            ORDER BY a.assertion_id
            """
        ).fetchall()
        exact_matches = sum(
            str(row["content"])[
                int(row["source_span_start"]):int(row["source_span_end"])
            ] == str(row["source_quote"])
            for row in coverage
        )

        query_latencies: list[float] = []
        returned_assertions = 0
        for _ in range(query_count):
            predicate = rng.randrange(predicate_count)
            started = time.perf_counter_ns()
            result = query_assertion_state(
                assertions,
                subject_key="user:self",
                predicate_key=f"metric.{predicate}",
                limit=25,
            )
            query_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
            returned_assertions += len(result.assertions)

        assertion_count = int(assertions.connection.execute(
            "SELECT COUNT(*) FROM lcm_assertions"
        ).fetchone()[0])
        relation_count = int(assertions.connection.execute(
            "SELECT COUNT(*) FROM lcm_assertion_relations"
        ).fetchone()[0])
        assertions.close()
        messages.close()

    incremental_bytes = max(0, asserted_db_bytes - raw_db_bytes)
    return {
        "protocol": {
            "kind": "provider_free_synthetic_store_measurement",
            "source_count": source_count,
            "query_count": query_count,
            "predicate_count": predicate_count,
            "query_limit": 25,
            "random_seed": 20260719,
            "provider_calls": 0,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
        },
        "coverage": {
            "assertions_published": assertion_count,
            "relations_published": relation_count,
            "publish_failures": publish_failures,
            "exact_source_matches": exact_matches,
            "exact_source_coverage": round(
                exact_matches / assertion_count if assertion_count else 1.0,
                6,
            ),
        },
        "storage": {
            "raw_db_bytes": raw_db_bytes,
            "asserted_db_bytes": asserted_db_bytes,
            "incremental_assertion_bytes": incremental_bytes,
            "incremental_bytes_per_assertion": round(
                incremental_bytes / assertion_count if assertion_count else 0.0,
                3,
            ),
        },
        "publish_latency": _latency_summary(publish_latencies),
        "query_latency": {
            **_latency_summary(query_latencies),
            "mean_returned_assertions": round(
                returned_assertions / query_count,
                3,
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=int, default=2_000)
    parser.add_argument("--queries", type=int, default=1_000)
    args = parser.parse_args()
    if not 1 <= args.sources <= 100_000:
        parser.error("--sources must be between 1 and 100000")
    if not 1 <= args.queries <= 100_000:
        parser.error("--queries must be between 1 and 100000")
    print(json.dumps(
        run_measurement(args.sources, args.queries),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
