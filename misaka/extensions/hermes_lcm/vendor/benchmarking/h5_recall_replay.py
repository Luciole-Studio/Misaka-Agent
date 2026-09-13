#!/usr/bin/env python3
"""Provider-free replay harness for the H5(b) adjacency pool-expansion (#135).

Extends the H3.1 composition replay instrument (``h3_composition_replay``):
same frozen assets, same injected semantic ranks, same golden gate. Sweeps the
``adjacency_radius x adjacency_quota`` knob grid under TWO arm compositions --
``base`` (shipping defaults) and ``hybrid`` (the quarantined A+D composition
``lexical_floor=1, arm_quota=(7,4)``) -- and measures, per knob x composition:

  POOL-ENTRY RECOVERY  -- of the 30 verified H32 NOT_READMITTED cases
                          (h5-targets.json), how many gain a NEW target state
                          in the candidate pool (pinned targets: a pinned
                          state newly present; trajectory targets: a
                          trajectory with ZERO default-pool states gains one).
                          Pool membership is computed EXACTLY (global + scoped
                          FTS union + admitted expansion states), not from the
                          64-truncated telemetry. Knob-level (the pool is
                          composition-independent).
  DELIVERED-RECALL@16  -- same rule against the delivered refs, vs the SAME
                          composition's no-adjacency delivery.
  ANTI-FILLER (gate ii)-- the 8 H32 loss-ids' delivered sets must be UNCHANGED
                          vs the same composition's no-adjacency delivery.
  PRESERVATION         -- stable-correct questions whose same-composition
                          baseline delivered refs drop under the knob.
  LATENCY (gate iv)    -- p95 over a 50-query replay vs the same composition
                          without adjacency (frozen gate reads the base lane).

Also emits the ZERO-SEED / WEAK-SEED split of the 30 (which target
trajectories have no lexical match at all vs a match outside the pool vs a
seed already in the pool -- the adjacency mechanism's reachability boundary).

The component gate is FROZEN (SPEC-H5b): this instrument reports the full
honest table; it does NOT pick the shipping knob.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarking.h3_composition_replay import (  # noqa: E402
    _H1,
    _H31,
    _REF_RE,
    _RUN_ROOT,
    ReplayContext,
    golden_gate,
    measure_latency,
)

_H5_TARGETS = _H1 / "h5-targets.json"
_LOSS_8 = [
    ("web", "41bb40df"), ("web", "0738c1ac"), ("enterprise", "ccf29914"),
    ("enterprise", "2721ca7f"), ("web", "896017e0"), ("enterprise", "0b50ca0d"),
    ("enterprise", "2e90de97"), ("enterprise", "58c34839"),
]
_COMPOSITIONS: dict[str, dict[str, Any]] = {
    "base": {},
    "hybrid": {"lexical_floor": 1, "arm_quota": (7, 4)},
}


def _ref_pairs(refs: list[str]) -> set[tuple[str, int]]:
    """Delivered refs -> {(trajectory_id, state_index)}."""
    out: set[tuple[str, int]] = set()
    for ref in refs:
        match = _REF_RE.match(ref)
        if match:
            out.add((unquote(match.group("traj")), int(match.group("state"))))
    return out


class H5Context:
    """Target-aware wrapper over the H3.1 ReplayContext."""

    def __init__(self, ctx: ReplayContext, targets_path: Path) -> None:
        self.ctx = ctx
        payload = json.loads(targets_path.read_text())
        self.cases: list[dict[str, Any]] = payload["cases"]
        assert len(self.cases) == int(payload["denominator"]) == 30
        self._traj_states: dict[tuple[str, str], dict[int, int]] = {}
        self._pool_cache: dict[str, set[int]] = {}

    # -- corpus lookups --------------------------------------------------
    def traj_states(self, domain: str, trajectory_id: str) -> dict[int, int]:
        """{state_index: state_id} for one trajectory."""
        key = (domain, trajectory_id)
        if key not in self._traj_states:
            store = self.ctx.stores[domain]
            rows = store._conn.execute(
                """SELECT s.state_index, s.state_id
                   FROM lcm_trajectory_states s
                   JOIN lcm_trajectory_sources src ON src.source_id = s.source_id
                   WHERE src.trajectory_id = ?""",
                (trajectory_id,),
            ).fetchall()
            assert rows, f"{domain}/{trajectory_id} missing from frozen DB"
            self._traj_states[key] = {int(r[0]): int(r[1]) for r in rows}
        return self._traj_states[key]

    def traj_source_id(self, domain: str, trajectory_id: str) -> int:
        store = self.ctx.stores[domain]
        row = store._conn.execute(
            "SELECT source_id FROM lcm_trajectory_sources WHERE trajectory_id = ?",
            (trajectory_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"{domain}/{trajectory_id} missing from frozen DB")
        return int(row[0])

    # -- exact pool membership --------------------------------------------
    def default_pool_ids(self, qid: str) -> set[int]:
        """The EXACT default candidate pool: global FTS top-128 union scoped
        FTS top-128 within the injected semantic-top sources (mirrors
        ``TrajectoryStore.query`` pool construction; membership only)."""
        if qid in self._pool_cache:
            return self._pool_cache[qid]
        domain, text = self.ctx.questions[qid]
        store = self.ctx.stores[domain]
        expression = store._fts_expression(text)
        ids: set[int] = set()
        if expression:
            ids = {int(r["state_id"]) for r in store._fts_rows(expression, 128)}
            ranks = self.ctx._injected_ranks(qid)
            if ranks:
                scoped = store._fts_rows(
                    expression, 128,
                    source_ids=[source_id for source_id, _score in ranks],
                )
                ids |= {int(r["state_id"]) for r in scoped}
        self._pool_cache[qid] = ids
        return ids

    def deliver_and_admitted(
        self, qid: str, **kwargs: Any
    ) -> tuple[list[str], set[int]]:
        """Delivered refs + the adjacency-admitted state ids (telemetry)."""
        domain, _text = self.ctx.questions[qid]
        delivered = self.ctx.deliver(qid, **kwargs)
        telemetry = self.ctx.stores[domain].last_query_telemetry()
        expansion = telemetry.get("adjacency_expansion") or {}
        admitted = {int(e["state_id"]) for e in expansion.get("admitted", [])}
        return delivered, admitted

    # -- per-case classification -------------------------------------------
    def seed_split(self) -> dict[str, Any]:
        """Zero-seed / weak-seed / seeded split of the 30 target cases."""
        per_case: dict[str, str] = {}
        for case in self.cases:
            if not case["targets"]:
                per_case[case["qid"]] = "no_target"
                continue
            domain = case["domain"]
            qid = case["qid"]
            store = self.ctx.stores[domain]
            _dom, text = self.ctx.questions[qid]
            expression = store._fts_expression(text)
            pool = self.default_pool_ids(qid)
            any_match = False
            any_pooled = False
            for target in case["targets"]:
                states = self.traj_states(domain, target["trajectory_id"])
                if set(states.values()) & pool:
                    any_pooled = True
                if expression:
                    source_id = self.traj_source_id(domain, target["trajectory_id"])
                    rows = store._fts_rows(expression, 1, source_ids=[source_id])
                    if rows:
                        any_match = True
            per_case[qid] = (
                "seeded_in_pool" if any_pooled
                else "weak_seed_unpooled" if any_match
                else "zero_seed"
            )
        counts: dict[str, int] = {}
        for value in per_case.values():
            counts[value] = counts.get(value, 0) + 1
        return {"per_case": per_case, "counts": counts}

    def case_pool_recovery(self, case: dict[str, Any], admitted: set[int]) -> dict[str, Any]:
        """Did a NEW target state enter the (exact) pool via the expansion?"""
        qid = case["qid"]
        domain = case["domain"]
        default_pool = self.default_pool_ids(qid)
        result = {"recovered": False, "present_at_default": False, "new_target_states": 0}
        if case["status"] == "pinned":
            target_ids: set[int] = set()
            for target in case["targets"]:
                target_ids |= {int(v) for v in target["state_ids"].values()}
            result["present_at_default"] = bool(target_ids & default_pool)
            new = (target_ids & admitted) - default_pool
            result["new_target_states"] = len(new)
            result["recovered"] = bool(new)
        elif case["status"] == "trajectory":
            for target in case["targets"]:
                states = set(self.traj_states(domain, target["trajectory_id"]).values())
                if states & default_pool:
                    result["present_at_default"] = True
                    continue  # trajectory already reachable; not a recovery target
                new = states & admitted
                if new:
                    result["new_target_states"] += len(new)
                    result["recovered"] = True
        return result

    def case_delivered_recall(
        self,
        case: dict[str, Any],
        delivered: list[str],
        baseline_delivered: list[str],
    ) -> bool:
        """Did a NEW target state reach the delivered 16-slot set?"""
        got = _ref_pairs(delivered)
        base = _ref_pairs(baseline_delivered)
        if case["status"] == "pinned":
            for target in case["targets"]:
                for index in target["state_indices"]:
                    pair = (target["trajectory_id"], int(index))
                    if pair in got and pair not in base:
                        return True
            return False
        if case["status"] == "trajectory":
            base_trajs = {traj for traj, _idx in base}
            for target in case["targets"]:
                traj = target["trajectory_id"]
                if traj in base_trajs:
                    continue
                if any(t == traj for t, _idx in got):
                    return True
            return False
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=_RUN_ROOT)
    parser.add_argument("--h1-artifacts", type=Path, default=_H1)
    parser.add_argument("--h31-artifacts", type=Path, default=_H31)
    parser.add_argument("--targets", type=Path, default=_H5_TARGETS)
    parser.add_argument("--out", type=Path, default=_H1 / "h5-replay-sweep.json")
    parser.add_argument("--latency-sample", type=int, default=50)
    parser.add_argument(
        "--radii", type=int, nargs="+", default=[1, 2])
    parser.add_argument(
        "--quotas", type=int, nargs="+", default=[8, 16, 32, 64, 128])
    args = parser.parse_args()

    ctx = ReplayContext(args.run_root, args.h1_artifacts, args.h31_artifacts)
    h5 = H5Context(ctx, args.targets)

    # (iii) GOLDEN GATE -- mandatory before any sweep.
    print("== GOLDEN GATE (defaults reproduce recorded delivery) ==", flush=True)
    golden = golden_gate(ctx)
    print(f"  {golden['passed']}/{golden['total']} byte-identical; "
          f"failures={golden['failures'][:10]}", flush=True)
    if golden["passed"] != golden["total"]:
        print("GOLDEN GATE FAILED -- aborting before sweep", flush=True)
        return 1

    # Seed split (knob-independent reachability boundary).
    split = h5.seed_split()
    print(f"\n== SEED SPLIT of the 30 == {split['counts']}", flush=True)

    # Preservation universe (same ground truth as the H3.1 instrument).
    recon = json.loads(
        (args.h1_artifacts / "old-rescore" / "reconciliation_sets.json").read_text()
    )
    preserved_qids = sorted({item.split("/", 1)[1] for item in recon["preserved"]})

    # Same-composition baselines (no adjacency).
    print("\n== composition baselines (no adjacency) ==", flush=True)
    target_qids = [case["qid"] for case in h5.cases]
    loss_qids = [qid for _dom, qid in _LOSS_8]
    baseline_delivered: dict[str, dict[str, list[str]]] = {}
    baseline_latency: dict[str, dict[str, float]] = {}
    sample = sorted(ctx.questions)[: args.latency_sample]
    for comp_name, comp_kwargs in _COMPOSITIONS.items():
        per_qid: dict[str, list[str]] = {}
        for qid in set(target_qids + loss_qids + preserved_qids):
            per_qid[qid] = ctx.deliver(qid, **comp_kwargs)
        baseline_delivered[comp_name] = per_qid
        baseline_latency[comp_name] = measure_latency(ctx, sample, comp_kwargs)
        print(f"  {comp_name}: baseline p95 "
              f"{baseline_latency[comp_name]['p95_ms']:.1f}ms", flush=True)

    knobs = [
        {"adjacency_radius": radius, "adjacency_quota": quota}
        for radius in args.radii
        for quota in args.quotas
    ]

    results: list[dict[str, Any]] = []
    for knob in knobs:
        label = f"r{knob['adjacency_radius']}q{knob['adjacency_quota']}"
        started = time.perf_counter()
        row: dict[str, Any] = {"knob": knob, "label": label}

        # Pool-entry recovery (composition-independent; expansion is
        # pre-selection) + base-composition delivered recall from the SAME
        # query call (base merged kwargs == the bare knob kwargs).
        pool_cases: dict[str, dict[str, Any]] = {}
        pool_recovered = 0
        by_bucket: dict[str, dict[str, int]] = {}
        base_delivered_by_case: dict[str, list[str]] = {}
        for case in h5.cases:
            delivered, admitted = h5.deliver_and_admitted(case["qid"], **knob)
            base_delivered_by_case[case["qid"]] = delivered
            outcome = h5.case_pool_recovery(case, admitted)
            pool_cases[case["qid"]] = outcome
            slot = by_bucket.setdefault(case["bucket"], {"total": 0, "recovered": 0})
            slot["total"] += 1
            if outcome["recovered"]:
                pool_recovered += 1
                slot["recovered"] += 1
        row["pool_recovery"] = pool_recovered
        row["pool_recovery_by_bucket"] = by_bucket
        row["pool_cases"] = pool_cases

        for comp_name, comp_kwargs in _COMPOSITIONS.items():
            merged = dict(comp_kwargs)
            merged.update(knob)
            base = baseline_delivered[comp_name]
            delivered_recovered = 0
            delivered_case_ids: list[str] = []
            for case in h5.cases:
                if comp_name == "base":
                    delivered = base_delivered_by_case[case["qid"]]
                else:
                    delivered = ctx.deliver(case["qid"], **merged)
                if h5.case_delivered_recall(case, delivered, base[case["qid"]]):
                    delivered_recovered += 1
                    delivered_case_ids.append(case["qid"])
            loss_unchanged = 0
            loss_changed: list[str] = []
            for _dom, qid in _LOSS_8:
                if ctx.deliver(qid, **merged) == base[qid]:
                    loss_unchanged += 1
                else:
                    loss_changed.append(qid)
            disturbed: list[str] = []
            for qid in preserved_qids:
                recorded = set(base[qid])
                now = set(ctx.deliver(qid, **merged))
                if recorded - now:
                    disturbed.append(qid)
            # Paired latency read: the frozen-DB replay lives on an external
            # drive with high I/O variance, so a knob pass is only comparable
            # to a default pass measured back-to-back in the same phase.
            default_latency = measure_latency(ctx, sample, comp_kwargs)
            latency = measure_latency(ctx, sample, merged)
            base_p95 = default_latency["p95_ms"]
            row[comp_name] = {
                "delivered_recall": delivered_recovered,
                "delivered_recall_qids": delivered_case_ids,
                "loss8_unchanged": loss_unchanged,
                "loss8_changed_qids": loss_changed,
                "preservation_disturbed": len(disturbed),
                "preservation_disturbed_qids": disturbed,
                "latency": latency,
                "paired_default_latency": default_latency,
                "latency_pct_vs_baseline": (
                    (latency["p95_ms"] / base_p95 - 1.0) * 100.0 if base_p95 else 0.0
                ),
            }
        row["elapsed_s"] = round(time.perf_counter() - started, 1)
        results.append(row)
        print(
            f"  {label}: pool {pool_recovered}/30 | "
            f"base dRec {row['base']['delivered_recall']}/30 "
            f"loss8 {row['base']['loss8_unchanged']}/8 "
            f"pres {row['base']['preservation_disturbed']}/{len(preserved_qids)} "
            f"p95 {row['base']['latency_pct_vs_baseline']:+.1f}% | "
            f"hyb dRec {row['hybrid']['delivered_recall']}/30 "
            f"loss8 {row['hybrid']['loss8_unchanged']}/8 "
            f"pres {row['hybrid']['preservation_disturbed']} "
            f"p95 {row['hybrid']['latency_pct_vs_baseline']:+.1f}% "
            f"({row['elapsed_s']}s)",
            flush=True,
        )

    print("\n== SWEEP TABLE (component gate FROZEN; shipping knob NOT picked here) ==")
    header = (
        f"{'knob':<8}{'poolRec/30':>11}"
        f"{'b:dRec':>8}{'b:loss8':>9}{'b:pres':>8}{'b:p95Δ%':>9}"
        f"{'h:dRec':>8}{'h:loss8':>9}{'h:pres':>8}{'h:p95Δ%':>9}"
    )
    print(header)
    print("-" * len(header))
    for row in results:
        print(
            f"{row['label']:<8}{row['pool_recovery']:>11}"
            f"{row['base']['delivered_recall']:>8}"
            f"{row['base']['loss8_unchanged']:>9}"
            f"{row['base']['preservation_disturbed']:>8}"
            f"{row['base']['latency_pct_vs_baseline']:>+8.1f}%"
            f"{row['hybrid']['delivered_recall']:>8}"
            f"{row['hybrid']['loss8_unchanged']:>9}"
            f"{row['hybrid']['preservation_disturbed']:>8}"
            f"{row['hybrid']['latency_pct_vs_baseline']:>+8.1f}%"
        )

    payload = {
        "golden_gate": golden,
        "seed_split": split,
        "targets_manifest": str(args.targets),
        "preserved_universe": len(preserved_qids),
        "baseline_latency": baseline_latency,
        "compositions": {k: {kk: list(vv) if isinstance(vv, tuple) else vv
                             for kk, vv in v.items()}
                         for k, v in _COMPOSITIONS.items()},
        "sweep": results,
    }
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
