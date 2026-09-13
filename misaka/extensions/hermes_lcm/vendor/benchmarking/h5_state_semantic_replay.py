#!/usr/bin/env python3
"""Replay sweep for the state-level semantic pool-expansion (issue #142, W3a).

Sibling of ``h5_recall_replay`` (the H5(b) adjacency sweep). Same frozen assets,
same injected SOURCE ranks (so the base fused pool is byte-identical), same
golden gate and the same four frozen gate metrics on the same target/preservation
sets -- but the arm under test is ``state_semantic_quota`` against a REAL
per-state Voyage index that has been backfilled into the store copies.

Key differences from the provider-free adjacency sweep:
  * Stores are opened over the BACKFILLED working copies (which carry the
    ``lcm_trajectory_state_embeddings`` table), NOT the bare h31 db-copies.
  * A real Voyage QUERY provider is attached so the state arm can embed the
    query and rank the per-state vectors. Query embeddings are CACHED (embedded
    once, off the timed path) exactly as source ranks are injected -- so the
    latency gate isolates the arm's added algorithmic cost (mat-vec + a small
    SQL fetch), which is what production pays on top of the query embed the
    source-semantic path already performs. Source ranks stay INJECTED so the
    golden gate is byte-identical.

Reported per knob (component gate FROZEN; the orchestrator applies it):
  POOL-ENTRY RECOVERY  -- of the 30 h5-targets, how many gain a NEW target state
                          in the pool via the admitted state-semantic tail.
  DELIVERED-RECALL@16  -- new target state reaches the delivered top-16.
  ANTI-FILLER (loss8)  -- the 8 H32 loss-ids' delivered sets unchanged.
  PRESERVATION         -- stable-correct (154-set) delivered refs dropped.
  LATENCY p95          -- vs the same-composition no-arm pass (cached embeds).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarking.h3_composition_replay import (  # noqa: E402
    _H1, _H31, _REPO_ROOT, _RUN_ROOT, ReplayContext, _bootstrap_package,
    golden_gate, measure_latency,
)
from benchmarking.h5_recall_replay import (  # noqa: E402
    _LOSS_8, H5Context,
)

_H5_TARGETS = _H1 / "h5-targets.json"
_DEFAULT_DB_DIR = Path("/Volumes/LEXAR/hermes-work/W3A-dbwork")


class CachedVoyageQueryProvider:
    """Real Voyage query embedding, cached by text so repeated timed passes make
    zero network calls (the source ranks are injected the same way). Reports the
    voyage/voyage-4 identity the backfilled state profile was written under."""

    provider_id = "voyage"
    model_id = "voyage-4"

    def __init__(self, model: str = "voyage-4", timeout: float = 30.0) -> None:
        from hermes_lcm.embedding_provider import EmbeddingSpendGuard, VoyageProvider

        # Disable the interactive per-minute call-rate guard: this offline sweep
        # embeds the 451 unique queries in a tight loop (each is cheap, ~cents
        # total), which is exactly the bulk pattern for_backfill=True exempts.
        self._real = VoyageProvider(
            model, timeout=timeout, spend_guard=EmbeddingSpendGuard(max_calls=0)
        )
        self.model_id = model
        self._cache: dict[str, list[float]] = {}
        self.last_usage_tokens = 0
        self.calls = 0

    def embed_query(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is None:
            self.calls += 1
            cached = list(self._real.embed_query(text))
            self._cache[text] = cached
        return cached

    def embed_documents(self, texts):  # noqa: ARG002
        raise RuntimeError("CachedVoyageQueryProvider is query-only")


class StateReplayContext(ReplayContext):
    """ReplayContext whose stores are the BACKFILLED copies with a real (cached)
    Voyage query provider attached, so the state-semantic arm is live while the
    injected source ranks keep the base pool byte-identical."""

    def __init__(self, run_root, h1, h31, db_dir: Path, provider) -> None:
        self._db_dir = Path(db_dir)
        self._provider = provider
        super().__init__(run_root, h1, h31)

    def _open_store(self, db_path, domain):  # noqa: ARG002
        real_db = self._db_dir / f"{domain}.lcm.db"
        conn = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
        identity_json = json.loads(
            conn.execute(
                "SELECT identity_json FROM lcm_trajectory_corpora WHERE singleton=1"
            ).fetchone()[0]
        )
        conn.close()
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

        store = _ReplayStore(
            real_db, identity, asset_root=real_db.parent,
            read_only=True, semantic_top_trajectories=12,
        )
        store.embedding_provider = self._provider
        return store


def _state_admitted(ctx: ReplayContext, qid: str) -> set[int]:
    domain, _ = ctx.questions[qid]
    telemetry = ctx.stores[domain].last_query_telemetry() or {}
    expansion = telemetry.get("state_semantic_expansion") or {}
    return {int(e["state_id"]) for e in expansion.get("admitted", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=_RUN_ROOT)
    parser.add_argument("--h1-artifacts", type=Path, default=_H1)
    parser.add_argument("--h31-artifacts", type=Path, default=_H31)
    parser.add_argument("--targets", type=Path, default=_H5_TARGETS)
    parser.add_argument("--db-dir", type=Path, default=_DEFAULT_DB_DIR)
    parser.add_argument("--out", type=Path, default=_H1 / "W3A-state-sweep.json")
    parser.add_argument("--latency-sample", type=int, default=50)
    parser.add_argument("--quotas", type=int, nargs="+", default=[4, 8, 16])
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    if not os.environ.get("VOYAGE_API_KEY", "").strip():
        print("VOYAGE_API_KEY is not set -- the state arm needs a query embedder",
              flush=True)
        return 3

    _bootstrap_package(_REPO_ROOT)
    provider = CachedVoyageQueryProvider()
    ctx = StateReplayContext(
        args.run_root, args.h1_artifacts, args.h31_artifacts, args.db_dir, provider
    )
    h5 = H5Context(ctx, args.targets)

    # (iii) GOLDEN GATE -- knob-off must reproduce recorded delivery byte-for-byte.
    print("== GOLDEN GATE (defaults-off reproduce recorded delivery) ==", flush=True)
    golden = golden_gate(ctx)
    print(f"  {golden['passed']}/{golden['total']} byte-identical; "
          f"failures={golden['failures'][:10]}", flush=True)
    if golden["passed"] != golden["total"]:
        print("GOLDEN GATE FAILED -- aborting before sweep", flush=True)
        return 1

    split = h5.seed_split()
    print(f"\n== SEED SPLIT of the 30 == {split['counts']}", flush=True)

    recon = json.loads(
        (args.h1_artifacts / "old-rescore" / "reconciliation_sets.json").read_text()
    )
    preserved_qids = sorted({item.split("/", 1)[1] for item in recon["preserved"]})

    target_qids = [case["qid"] for case in h5.cases]
    loss_qids = [qid for _dom, qid in _LOSS_8]
    all_qids = sorted(set(target_qids + loss_qids + preserved_qids))
    sample = sorted(ctx.questions)[: args.latency_sample]

    # WARM the query-vector cache + the state matrix so timed passes are network
    # -free (mirrors the injected source ranks). One pass at the largest quota.
    warm_quota = max(args.quotas)
    print(f"\n== warming query-embed cache (quota={warm_quota}) ==", flush=True)
    for qid in sorted(set(all_qids + sample)):
        ctx.deliver(qid, state_semantic_quota=warm_quota)
    print(f"  cached {provider.calls} unique query embeds", flush=True)

    # Base (no-arm) delivery baseline for delivered-recall / preservation / loss8.
    base_delivered = {qid: ctx.deliver(qid) for qid in all_qids}

    results: list[dict[str, Any]] = []
    for quota in args.quotas:
        knob = {"state_semantic_quota": quota}
        label = f"q{quota}"
        started = time.perf_counter()

        pool_recovered = 0
        by_bucket: dict[str, dict[str, int]] = {}
        delivered_recovered = 0
        delivered_qids: list[str] = []
        pool_cases: dict[str, Any] = {}
        for case in h5.cases:
            qid = case["qid"]
            delivered = ctx.deliver(qid, **knob)
            admitted = _state_admitted(ctx, qid)
            outcome = h5.case_pool_recovery(case, admitted)
            pool_cases[qid] = outcome
            slot = by_bucket.setdefault(case["bucket"], {"total": 0, "recovered": 0})
            slot["total"] += 1
            if outcome["recovered"]:
                pool_recovered += 1
                slot["recovered"] += 1
            if h5.case_delivered_recall(case, delivered, base_delivered[qid]):
                delivered_recovered += 1
                delivered_qids.append(qid)

        loss_unchanged = 0
        loss_changed: list[str] = []
        for _dom, qid in _LOSS_8:
            if ctx.deliver(qid, **knob) == base_delivered[qid]:
                loss_unchanged += 1
            else:
                loss_changed.append(qid)

        disturbed: list[str] = []
        for qid in preserved_qids:
            if set(base_delivered[qid]) - set(ctx.deliver(qid, **knob)):
                disturbed.append(qid)

        default_latency = measure_latency(ctx, sample, {})
        knob_latency = measure_latency(ctx, sample, knob)
        base_p95 = default_latency["p95_ms"]
        row = {
            "knob": knob,
            "label": label,
            "pool_recovery": pool_recovered,
            "pool_recovery_by_bucket": by_bucket,
            "pool_cases": pool_cases,
            "delivered_recall": delivered_recovered,
            "delivered_recall_qids": delivered_qids,
            "loss8_unchanged": loss_unchanged,
            "loss8_changed_qids": loss_changed,
            "preservation_disturbed": len(disturbed),
            "preservation_disturbed_qids": disturbed,
            "latency": knob_latency,
            "paired_default_latency": default_latency,
            "latency_pct_vs_baseline": (
                (knob_latency["p95_ms"] / base_p95 - 1.0) * 100.0 if base_p95 else 0.0
            ),
            "elapsed_s": round(time.perf_counter() - started, 1),
        }
        results.append(row)
        print(
            f"  {label}: pool {pool_recovered}/30 | dRec {delivered_recovered}/30 "
            f"loss8 {loss_unchanged}/8 pres {len(disturbed)}/{len(preserved_qids)} "
            f"p95 {row['latency_pct_vs_baseline']:+.1f}% ({row['elapsed_s']}s)",
            flush=True,
        )

    print("\n== SWEEP TABLE (component gate FROZEN; shipping knob NOT picked here) ==")
    header = (f"{'knob':<8}{'poolRec/30':>11}{'dRec/30':>9}"
              f"{'loss8/8':>9}{'pres/154':>10}{'p95 dpct':>10}")
    print(header)
    print("-" * len(header))
    for row in results:
        print(f"{row['label']:<8}{row['pool_recovery']:>11}{row['delivered_recall']:>9}"
              f"{row['loss8_unchanged']:>9}{row['preservation_disturbed']:>10}"
              f"{row['latency_pct_vs_baseline']:>+9.1f}%")

    payload = {
        "golden_gate": golden,
        "seed_split": split,
        "targets_manifest": str(args.targets),
        "db_dir": str(args.db_dir),
        "preserved_universe": len(preserved_qids),
        "unique_query_embeds": provider.calls,
        "quotas": list(args.quotas),
        "sweep": results,
    }
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
