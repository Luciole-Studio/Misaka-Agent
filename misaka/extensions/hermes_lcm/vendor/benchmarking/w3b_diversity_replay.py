#!/usr/bin/env python3
"""Provider-free W3b C1 and W3a-state composition replay.

This driver reuses the frozen H5 target/preservation contract. The standalone
C1 cells run on the original read-only replay DBs. The q16/q32 composition cells
run on the read-only W3a backfilled working copies.

No embedding or reader provider is called. For the state-semantic cells, the
query direction is reconstructed deterministically from:

* the recorded source-candidate cosine scores in the frozen H1 trace; and
* the corresponding stored source vectors in the W3a DB.

The minimum-norm vector satisfying those recorded projections is used only to
rank the already-backfilled state vectors. This is provider-free component
evidence, not a replacement for an official reader/judge run.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarking.h3_composition_replay import (  # noqa: E402
    _H1,
    _H31,
    _RUN_ROOT,
    ReplayContext,
    golden_gate,
)
from benchmarking.h5_recall_replay import (  # noqa: E402
    _H5_TARGETS,
    _LOSS_8,
    H5Context,
)

_DEFAULT_DB_DIR = Path("/Volumes/LEXAR/hermes-work/W3A-dbwork")


class ProjectedQueryProvider:
    """Reconstruct query vectors from frozen source-vector projections."""

    provider_id = "voyage"
    model_id = "voyage-4"

    def __init__(
        self,
        context: "ProjectedStateReplayContext",
        domain: str,
        db_path: Path,
    ) -> None:
        self._context = context
        self._domain = domain
        self._cache: dict[str, list[float]] = {}
        self.last_usage_tokens = 0
        self.external_calls = 0
        self.projections_built = 0
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        profile = connection.execute(
            """
            SELECT profile_digest, dim
            FROM lcm_trajectory_embedding_profiles
            WHERE active = 1
            """
        ).fetchone()
        if profile is None:
            connection.close()
            raise RuntimeError(f"{domain}: active source embedding profile missing")
        self._dim = int(profile["dim"])
        rows = connection.execute(
            """
            SELECT source_id, vector
            FROM lcm_trajectory_embeddings
            WHERE profile_digest = ?
            """,
            (str(profile["profile_digest"]),),
        ).fetchall()
        connection.close()
        self._source_vectors = {
            int(row["source_id"]): struct.unpack(
                f"<{self._dim}f", bytes(row["vector"])
            )
            for row in rows
        }
        if not self._source_vectors:
            raise RuntimeError(f"{domain}: source vectors missing")

    @staticmethod
    def _solve(gram: list[list[float]], target: list[float]) -> list[float]:
        """Gaussian elimination with deterministic pivoting and tiny ridge."""
        size = len(target)
        augmented = [
            [
                gram[row][column] + (1e-9 if row == column else 0.0)
                for column in range(size)
            ] + [target[row]]
            for row in range(size)
        ]
        for column in range(size):
            pivot = max(
                range(column, size),
                key=lambda row: abs(augmented[row][column]),
            )
            if abs(augmented[pivot][column]) < 1e-12:
                raise RuntimeError("recorded source projection matrix is singular")
            augmented[column], augmented[pivot] = (
                augmented[pivot],
                augmented[column],
            )
            divisor = augmented[column][column]
            augmented[column] = [
                value / divisor for value in augmented[column]
            ]
            for row in range(size):
                if row == column:
                    continue
                factor = augmented[row][column]
                if factor == 0.0:
                    continue
                augmented[row] = [
                    left - factor * right
                    for left, right in zip(
                        augmented[row], augmented[column]
                    )
                ]
        return [augmented[row][-1] for row in range(size)]

    def embed_query(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        qids = [
            qid
            for qid, (domain, question) in self._context.questions.items()
            if domain == self._domain and question == text
        ]
        if len(qids) != 1:
            raise RuntimeError(
                f"{self._domain}: expected one frozen qid for query, got {qids}"
            )
        ranks = self._context._injected_ranks(qids[0])
        matrix_rows: list[tuple[float, ...]] = []
        scores: list[float] = []
        for source_id, score in ranks:
            vector = self._source_vectors.get(source_id)
            if vector is None:
                continue
            matrix_rows.append(vector)
            scores.append(float(score))
        if not matrix_rows:
            raise RuntimeError(f"{self._domain}/{qids[0]}: no source projections")
        gram = [
            [
                sum(left * right for left, right in zip(row_a, row_b))
                for row_b in matrix_rows
            ]
            for row_a in matrix_rows
        ]
        coefficients = self._solve(gram, scores)
        vector = [
            sum(
                coefficients[row] * matrix_rows[row][column]
                for row in range(len(matrix_rows))
            )
            for column in range(self._dim)
        ]
        norm = sum(value * value for value in vector) ** 0.5
        if norm <= 0.0 or len(vector) != self._dim:
            raise RuntimeError(
                f"{self._domain}/{qids[0]}: invalid reconstructed query vector"
            )
        cached = [float(value) for value in vector]
        self._cache[text] = cached
        self.projections_built += 1
        return cached

    def embed_documents(self, texts):  # noqa: ARG002
        raise RuntimeError("ProjectedQueryProvider is query-only")


class ProjectedStateReplayContext(ReplayContext):
    """W3a DB replay with provider-free reconstructed state-query vectors."""

    def __init__(self, run_root, h1, h31, db_dir: Path) -> None:
        self._db_dir = Path(db_dir)
        self.providers: dict[str, ProjectedQueryProvider] = {}
        super().__init__(run_root, h1, h31)

    def _open_store(self, db_path, domain):  # noqa: ARG002
        real_db = self._db_dir / f"{domain}.lcm.db"
        connection = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
        identity_json = json.loads(
            connection.execute(
                "SELECT identity_json FROM lcm_trajectory_corpora WHERE singleton=1"
            ).fetchone()[0]
        )
        connection.close()
        identity = self._ts.CorpusIdentity(
            dataset_name=identity_json["dataset_name"],
            dataset_revision=identity_json["dataset_revision"],
            harness_commit=identity_json["harness_commit"],
            tier=identity_json["tier"],
            domain=identity_json["domain"],
            ingest_config_digest=identity_json.get("ingest_config_digest", ""),
        )
        base = self._ts.TrajectoryStore

        class _ReplayStore(base):  # type: ignore[misc, valid-type]
            injected: list[tuple[int, float]] = []

            def _semantic_source_ranks(self, query: str):  # noqa: ARG002
                return list(self.injected)

        provider = ProjectedQueryProvider(self, domain, real_db)
        self.providers[domain] = provider
        return _ReplayStore(
            real_db,
            identity,
            asset_root=real_db.parent,
            read_only=True,
            semantic_top_trajectories=12,
            embedding_provider=provider,
        )


def _pool_ids(context: ReplayContext, qid: str) -> set[int]:
    domain, _question = context.questions[qid]
    telemetry = context.stores[domain].last_query_telemetry() or {}
    cap = telemetry.get("diversity_cap") or {}
    return {int(value) for value in cap.get("survivor_state_ids", [])}


def _target_in_pool(h5: H5Context, case: dict[str, Any], pool: set[int]) -> bool:
    domain = str(case["domain"])
    if case["status"] == "pinned":
        target_ids = {
            int(value)
            for target in case["targets"]
            for value in target["state_ids"].values()
        }
        return bool(target_ids & pool)
    if case["status"] == "trajectory":
        return any(
            set(h5.traj_states(domain, target["trajectory_id"]).values()) & pool
            for target in case["targets"]
        )
    return False


def _evaluate_cell(
    context: ReplayContext,
    h5: H5Context,
    kwargs: dict[str, Any],
    baseline_delivered: dict[str, list[str]],
    preserved_qids: list[str],
) -> dict[str, Any]:
    pool_covered = 0
    pool_new = 0
    delivered_recall = 0
    delivered_qids: list[str] = []
    default_pool_covered = 0
    capped_out = 0
    for case in h5.cases:
        qid = str(case["qid"])
        delivered = context.deliver(qid, **kwargs)
        pool = _pool_ids(context, qid)
        default_pool = h5.default_pool_ids(qid)
        covered = _target_in_pool(h5, case, pool)
        covered_default = _target_in_pool(h5, case, default_pool)
        pool_covered += int(covered)
        default_pool_covered += int(covered_default)
        pool_new += int(covered and not covered_default)
        if h5.case_delivered_recall(
            case, delivered, baseline_delivered[qid]
        ):
            delivered_recall += 1
            delivered_qids.append(qid)
        domain, _question = context.questions[qid]
        telemetry = context.stores[domain].last_query_telemetry() or {}
        capped_out += int(
            (telemetry.get("diversity_cap") or {}).get("capped_out", 0)
        )

    disturbed: list[str] = []
    for qid in preserved_qids:
        before = set(baseline_delivered[qid])
        after = set(context.deliver(qid, **kwargs))
        if before - after:
            disturbed.append(qid)
    loss_changed: list[str] = []
    for _domain, qid in _LOSS_8:
        if context.deliver(qid, **kwargs) != baseline_delivered[qid]:
            loss_changed.append(qid)
    return {
        "kwargs": kwargs,
        "target_pool_covered": pool_covered,
        "default_target_pool_covered": default_pool_covered,
        "new_pool_recovery": pool_new,
        "delivered_recall_at_16": delivered_recall,
        "delivered_recall_qids": delivered_qids,
        "preservation_retained": len(preserved_qids) - len(disturbed),
        "preservation_disturbed": len(disturbed),
        "preservation_disturbed_qids": disturbed,
        "loss8_unchanged": len(_LOSS_8) - len(loss_changed),
        "loss8_changed_qids": loss_changed,
        "capped_out_target_queries_total": capped_out,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=_RUN_ROOT)
    parser.add_argument("--h1-artifacts", type=Path, default=_H1)
    parser.add_argument("--h31-artifacts", type=Path, default=_H31)
    parser.add_argument("--targets", type=Path, default=_H5_TARGETS)
    parser.add_argument("--db-dir", type=Path, default=_DEFAULT_DB_DIR)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--caps", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--state-quotas", type=int, nargs="+", default=[16, 32])
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    base_context = ReplayContext(
        args.run_root, args.h1_artifacts, args.h31_artifacts
    )
    state_context = ProjectedStateReplayContext(
        args.run_root, args.h1_artifacts, args.h31_artifacts, args.db_dir
    )
    base_h5 = H5Context(base_context, args.targets)
    state_h5 = H5Context(state_context, args.targets)

    print("== GOLDEN GATES (all W3b knobs off) ==", flush=True)
    base_golden = golden_gate(base_context)
    state_golden = golden_gate(state_context)
    print(
        f"  base {base_golden['passed']}/{base_golden['total']} | "
        f"state-db {state_golden['passed']}/{state_golden['total']}",
        flush=True,
    )
    if (
        base_golden["passed"] != base_golden["total"]
        or state_golden["passed"] != state_golden["total"]
    ):
        print("GOLDEN GATE FAILED -- aborting before cells", flush=True)
        return 1

    reconciliation = json.loads(
        (
            args.h1_artifacts
            / "old-rescore"
            / "reconciliation_sets.json"
        ).read_text()
    )
    preserved_qids = sorted({
        item.split("/", 1)[1] for item in reconciliation["preserved"]
    })
    all_qids = sorted({
        *preserved_qids,
        *(str(case["qid"]) for case in base_h5.cases),
        *(qid for _domain, qid in _LOSS_8),
    })
    base_delivered = {
        qid: base_context.deliver(qid) for qid in all_qids
    }
    state_base_delivered = {
        qid: state_context.deliver(qid) for qid in all_qids
    }
    if base_delivered != state_base_delivered:
        print("STATE-DB BASELINE DIFFERS -- aborting composition cells", flush=True)
        return 2

    cells: list[dict[str, Any]] = []
    for cap in args.caps:
        started = time.perf_counter()
        row = _evaluate_cell(
            base_context,
            base_h5,
            {"diversity_cap": cap},
            base_delivered,
            preserved_qids,
        )
        row["label"] = f"cap{cap}"
        row["state_query_mode"] = "not_used"
        row["elapsed_s"] = round(time.perf_counter() - started, 3)
        cells.append(row)
        print(
            f"  {row['label']}: pool {row['target_pool_covered']}/30 "
            f"(new {row['new_pool_recovery']}) | "
            f"dRec@16 {row['delivered_recall_at_16']}/30 | "
            f"pres {row['preservation_retained']}/154",
            flush=True,
        )
    for quota in args.state_quotas:
        for cap in args.caps:
            started = time.perf_counter()
            row = _evaluate_cell(
                state_context,
                state_h5,
                {
                    "state_semantic_quota": quota,
                    "diversity_cap": cap,
                },
                base_delivered,
                preserved_qids,
            )
            row["label"] = f"q{quota}xcap{cap}"
            row["state_query_mode"] = "recorded-source-projection"
            row["elapsed_s"] = round(time.perf_counter() - started, 3)
            cells.append(row)
            print(
                f"  {row['label']}: pool {row['target_pool_covered']}/30 "
                f"(new {row['new_pool_recovery']}) | "
                f"dRec@16 {row['delivered_recall_at_16']}/30 | "
                f"pres {row['preservation_retained']}/154",
                flush=True,
            )

    payload = {
        "proof_boundary": (
            "Provider-free component replay only; reconstructed query directions "
            "do not prove official reader/judge accuracy or promotion."
        ),
        "golden_gate": {
            "base": base_golden,
            "state_db": state_golden,
        },
        "targets_manifest": str(args.targets),
        "db_dir": str(args.db_dir),
        "caps": list(args.caps),
        "state_quotas": list(args.state_quotas),
        "preserved_universe": len(preserved_qids),
        "external_provider_calls": sum(
            provider.external_calls
            for provider in state_context.providers.values()
        ),
        "query_projections_built": {
            domain: provider.projections_built
            for domain, provider in state_context.providers.items()
        },
        "cells": cells,
    }
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
