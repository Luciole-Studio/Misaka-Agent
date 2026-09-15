#!/usr/bin/env python3
"""Provider-free replay harness for the H3.1 candidate-composition repair (#127).

This is the *component instrument* for the two composition policies added to
``TrajectoryStore.query()`` (Policy A ``lexical_floor`` and Policy D
``arm_quota``). It never calls an embedding provider or any model/API: the
semantic source ranks are INJECTED from the frozen H1.3 telemetry (the recorded
``source_candidate_ranks``), and every other input is recomputed deterministically
from a read-only COPY of the frozen corpus DB.

Pipeline (mirrors ``TrajectoryStore.query`` exactly):
  1. For each qid, take the verbatim question text (the string the reader harness
     passed to ``query()``), open the frozen DB copy read-only, and inject the
     recorded 12 semantic source ranks in place of ``_semantic_source_ranks``.
  2. GOLDEN GATE: run the shipped policy (defaults) and assert it reproduces the
     recorded ``delivered_evidence_refs`` byte-for-byte.
  3. Swap in a policy knob and re-deliver; measure per knob:
       RECOVERY     -- vanished loss-target refs (genuine-loss ground truth)
                       re-admitted to the delivered top-16, split by bucket;
       PRESERVATION -- stable-correct questions whose recorded delivered refs
                       drop out (must be <= 2);
       CEILING      -- vanished refs absent from ``global_rows`` top-128
                       (un-recoverable at this seam -> a recall problem).
  4. Latency: p95 over a 50-query replay (default vs knob).

Honesty boundary: the reader/judge is NOT replayed, so this proves evidence
DELIVERY recovery + preservation (the necessary condition), not the correctness
flip. The end-to-end +correct delta needs a provider-gated full benchmark re-run.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import statistics
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

# --- defaults (frozen H1.3 assets; override on the CLI) ----------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_H1 = Path(
    "/Volumes/LEXAR/Codex/session-notes/2026-07-23/hermes-benchprog-h1/artifacts"
)
_H31 = Path(
    "/Volumes/LEXAR/Codex/session-notes/2026-07-23/hermes-benchprog-h1-h3.1/artifacts"
)
_RUN_ROOT = Path(
    "/Volumes/LEXAR/Codex/benchmarks/longmemeval-v2/runs/bench-h1-v2clean-2026-07-23"
)
_REF_RE = re.compile(r"^trajectory://[^/]+/(?P<traj>[^/]+)/state/(?P<state>\d+)$")


def _bootstrap_package(repo_root: Path) -> Any:
    """Register the plugin dir as the ``hermes_lcm`` package (mirrors conftest)."""
    pkg = "hermes_lcm"
    if pkg in sys.modules:
        return sys.modules[pkg]
    parent = str(repo_root.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(
        pkg, str(repo_root / "__init__.py"),
        submodule_search_locations=[str(repo_root)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__path__ = [str(repo_root)]
    mod.__package__ = pkg
    sys.modules[pkg] = mod
    for py_file in repo_root.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        sub_name = f"{pkg}.{py_file.stem}"
        if sub_name in sys.modules:
            continue
        sub_spec = importlib.util.spec_from_file_location(
            sub_name, str(py_file), submodule_search_locations=[]
        )
        sub_mod = importlib.util.module_from_spec(sub_spec)
        sub_mod.__package__ = pkg
        sys.modules[sub_name] = sub_mod
        setattr(mod, py_file.stem, sub_mod)
        try:
            sub_spec.loader.exec_module(sub_mod)
        except Exception as exc:
            sys.modules.pop(sub_name, None)
            if getattr(mod, py_file.stem, None) is sub_mod:
                delattr(mod, py_file.stem)
            print(
                f"warning: failed to bootstrap {sub_name}: {exc!r}",
                file=sys.stderr,
            )
    return mod


@lru_cache(maxsize=None)
def _read_trace(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _question_text(question_field: Any) -> str:
    """The exact query string the reader harness passes to ``query()``.

    Mirrors ``get_question_components``: a plain string is used verbatim; a
    multimodal ``{text, image}`` question contributes only its text (the image
    is input-only and never reaches the trajectory store).
    """
    if isinstance(question_field, str):
        return question_field
    return str(question_field["text"])


def _qids(labelled: list[str]) -> set[str]:
    """`domain/qid` -> `qid`."""
    return {item.split("/", 1)[1] for item in labelled}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[rank]


class ReplayContext:
    def __init__(self, run_root: Path, h1: Path, h31: Path) -> None:
        pkg = _bootstrap_package(_REPO_ROOT)
        self._ts = sys.modules["hermes_lcm.trajectory_store"]
        self.h1 = h1
        self.h31 = h31
        self.questions: dict[str, tuple[str, str]] = {}
        for domain in ("web", "enterprise"):
            for item in json.loads(
                (run_root / "runtime_inputs" / domain / "questions.json").read_text()
            ):
                self.questions[item["id"]] = (domain, _question_text(item["question"]))
        self.stores = {
            domain: self._open_store(h31 / "db-copies" / f"{domain}.lcm.db", domain)
            for domain in ("web", "enterprise")
        }
        self._state_id_cache: dict[tuple[str, str, int], int | None] = {}
        del pkg

    def _open_store(self, db_path: Path, domain: str):
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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

        return _ReplayStore(
            db_path, identity, asset_root=db_path.parent,
            read_only=True, semantic_top_trajectories=12,
        )

    def trace(self, qid: str) -> dict[str, Any]:
        domain, _ = self.questions[qid]
        path = (
            self.h31 / "query_traces" / domain / "query_traces" / qid
            / "hermes_lcm_semantic_telemetry.json"
        )
        return _read_trace(str(path))

    def _injected_ranks(self, qid: str) -> list[tuple[int, float]]:
        ranks = sorted(self.trace(qid)["source_candidate_ranks"], key=lambda r: r["rank"])
        return [(int(r["source_id"]), float(r["score"])) for r in ranks]

    def deliver(self, qid: str, **kwargs: Any) -> list[str]:
        """Delivered top-16 exact-refs for ``qid`` under the given policy kwargs."""
        domain, text = self.questions[qid]
        store = self.stores[domain]
        store.injected = self._injected_ranks(qid)
        store.query(
            text, candidate_limit=128, limit=16, image_limit=0,
            include_adjacent=True, text_char_limit=2000, **kwargs,
        )
        return list(store.last_query_telemetry()["delivered_evidence_refs"])

    def global_top_state_ids(self, qid: str, limit: int = 128) -> set[int]:
        domain, text = self.questions[qid]
        store = self.stores[domain]
        expression = store._fts_expression(text)
        if not expression:
            return set()
        rows = store._fts_rows(expression, limit)
        return {int(row["state_id"]) for row in rows}

    def fused_candidate_state_ids(
        self, qid: str, **kwargs: Any
    ) -> set[int]:
        domain, text = self.questions[qid]
        store = self.stores[domain]
        store.injected = self._injected_ranks(qid)
        store.query(
            text, candidate_limit=128, limit=16, image_limit=0,
            include_adjacent=True, text_char_limit=2000, **kwargs,
        )
        return {
            int(row["state_id"])
            for row in store.last_query_telemetry()["state_candidate_pool"]
        }

    def ref_to_state_id(self, qid: str, ref: str) -> int | None:
        match = _REF_RE.match(ref)
        if not match:
            return None
        from urllib.parse import unquote

        traj = unquote(match.group("traj"))
        state_index = int(match.group("state"))
        domain, _ = self.questions[qid]
        key = (domain, traj, state_index)
        if key in self._state_id_cache:
            return self._state_id_cache[key]
        store = self.stores[domain]
        row = store._conn.execute(
            """
            SELECT s.state_id FROM lcm_trajectory_states s
            JOIN lcm_trajectory_sources src ON src.source_id = s.source_id
            WHERE src.trajectory_id = ? AND s.state_index = ?
            """,
            (traj, state_index),
        ).fetchone()
        value = int(row[0]) if row is not None else None
        self._state_id_cache[key] = value
        return value


def golden_gate(ctx: ReplayContext) -> dict[str, Any]:
    passed = 0
    failures: list[str] = []
    qids = sorted(ctx.questions)
    for qid in qids:
        recorded = ctx.trace(qid)["delivered_evidence_refs"]
        got = ctx.deliver(qid)
        if got == recorded:
            passed += 1
        else:
            failures.append(qid)
    return {"total": len(qids), "passed": passed, "failures": failures}


def load_ground_truth(ctx: ReplayContext) -> dict[str, Any]:
    flip = json.loads((ctx.h1 / "old-rescore" / "flip_reconciliation_32_47.json").read_text())
    recon = json.loads((ctx.h1 / "old-rescore" / "reconciliation_sets.json").read_text())
    lost_analysis = {
        e["qid"]: e
        for e in json.loads((ctx.h1 / "H1P3-preservation-scratch" / "lost_analysis.json").read_text())
    }
    h2 = {
        e["qid"]: e.get("cause_norm") or e.get("cause")
        for e in json.loads((ctx.h1 / "h2-attribution-full.json").read_text())
    }
    genuine = _qids(flip["of_32_still_loss_under_rescore"])
    preserved = _qids(recon["preserved"])
    # recoverable universe = genuine losses that actually changed evidence
    recovery_targets: dict[str, dict[str, Any]] = {}
    for qid in genuine:
        vanished = lost_analysis.get(qid, {}).get("vanished", [])
        recovery_targets[qid] = {
            "vanished": list(vanished),
            "bucket": h2.get(qid, "(no-h2)"),
        }
    return {
        "genuine_loss_qids": sorted(genuine),
        "preserved_qids": sorted(preserved),
        "recovery_targets": recovery_targets,
    }


def measure_ceiling(
    ctx: ReplayContext,
    recovery_targets: dict[str, dict[str, Any]],
    *,
    knob_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per vanished ref: is its state present in the policy's candidate pool?"""
    per_ref: dict[str, dict[str, bool]] = {}
    policy = knob_kwargs or {}
    for qid, info in recovery_targets.items():
        if not info["vanished"]:
            continue
        if "arm_quota" in policy:
            top = ctx.fused_candidate_state_ids(qid, **policy)
        else:
            top = ctx.global_top_state_ids(qid, 128)
        for ref in info["vanished"]:
            state_id = ctx.ref_to_state_id(qid, ref)
            per_ref.setdefault(qid, {})[ref] = state_id is not None and state_id in top
    total = sum(len(v) for v in per_ref.values())
    recoverable = sum(1 for refs in per_ref.values() for present in refs.values() if present)
    return {
        "per_ref_in_global_top128": per_ref,
        "total_vanished_refs": total,
        "recoverable": recoverable,
        "absent_ceiling": total - recoverable,
    }


def evaluate_knob(
    ctx: ReplayContext,
    ground: dict[str, Any],
    ceiling: dict[str, Any],
    knob_kwargs: dict[str, Any],
) -> dict[str, Any]:
    targets = ground["recovery_targets"]
    per_ref = ceiling["per_ref_in_global_top128"]
    # RECOVERY (recoverable refs re-admitted), split by bucket
    readmit = 0
    by_bucket: dict[str, dict[str, int]] = {}
    for qid, info in targets.items():
        if not info["vanished"]:
            continue
        delivered = set(ctx.deliver(qid, **knob_kwargs))
        bucket = info["bucket"]
        slot = by_bucket.setdefault(bucket, {"recoverable": 0, "readmitted": 0})
        for ref in info["vanished"]:
            if not per_ref.get(qid, {}).get(ref, False):
                continue  # un-recoverable at this seam; excluded from the pool
            slot["recoverable"] += 1
            if ref in delivered:
                slot["readmitted"] += 1
                readmit += 1
    # PRESERVATION (stable-correct delivered refs dropped). Two lenses:
    #   ref-level    -- ANY recorded delivered ref drops out (the frozen gate);
    #   source-level -- a whole delivered TRAJECTORY vanishes (states merely
    #                   reshuffled within a still-present trajectory are far less
    #                   likely to change correctness than an entirely lost source).
    def _sources(refs: set[str]) -> set[str]:
        out = set()
        for ref in refs:
            match = _REF_RE.match(ref)
            if match:
                out.add(match.group("traj"))
        return out

    disturbed: list[str] = []
    source_disturbed: list[str] = []
    total_dropped = 0
    for qid in ground["preserved_qids"]:
        recorded = set(ctx.trace(qid)["delivered_evidence_refs"])
        delivered = set(ctx.deliver(qid, **knob_kwargs))
        dropped = recorded - delivered
        if dropped:
            disturbed.append(qid)
            total_dropped += len(dropped)
        if _sources(recorded) - _sources(delivered):
            source_disturbed.append(qid)
    recoverable = ceiling["recoverable"]
    return {
        "knob": knob_kwargs,
        "recovery_readmitted": readmit,
        "recovery_recoverable": recoverable,
        "recovery_pct": (readmit / recoverable * 100.0) if recoverable else 0.0,
        "recovery_by_bucket": by_bucket,
        "preservation_disturbed_count": len(disturbed),
        "preservation_disturbed_qids": disturbed,
        "preservation_total_refs_dropped": total_dropped,
        "preservation_source_disturbed_count": len(source_disturbed),
        "preservation_source_disturbed_qids": source_disturbed,
    }


def measure_latency(ctx: ReplayContext, sample_qids: list[str], knob_kwargs: dict[str, Any]) -> dict[str, float]:
    latencies: list[float] = []
    for qid in sample_qids:
        start = time.perf_counter()
        ctx.deliver(qid, **knob_kwargs)
        latencies.append((time.perf_counter() - start) * 1000.0)
    return {
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "mean_ms": statistics.fmean(latencies) if latencies else 0.0,
    }


def _knob_label(kwargs: dict[str, Any]) -> str:
    if "lexical_floor" in kwargs:
        return f"A(K={kwargs['lexical_floor']})"
    if "arm_quota" in kwargs:
        q = kwargs["arm_quota"]
        return f"D(q={q[0]},{q[1]})"
    return "default"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=_RUN_ROOT)
    parser.add_argument("--h1-artifacts", type=Path, default=_H1)
    parser.add_argument("--h31-artifacts", type=Path, default=_H31)
    parser.add_argument("--out", type=Path, default=_H31 / "h3-replay-sweep.json")
    parser.add_argument("--latency-sample", type=int, default=50)
    args = parser.parse_args()

    ctx = ReplayContext(args.run_root, args.h1_artifacts, args.h31_artifacts)

    print("== GOLDEN GATE (defaults reproduce recorded delivery) ==", flush=True)
    golden = golden_gate(ctx)
    print(f"  {golden['passed']}/{golden['total']} byte-identical; "
          f"failures={golden['failures'][:10]}", flush=True)
    if golden["passed"] != golden["total"]:
        print("GOLDEN GATE FAILED -- aborting before sweep", flush=True)
        return 1

    ground = load_ground_truth(ctx)
    ceiling = measure_ceiling(ctx, ground["recovery_targets"])
    print("\n== CEILING (recoverability of vanished loss refs at this seam) ==")
    print(f"  vanished refs (genuine losses): {ceiling['total_vanished_refs']}")
    print(f"  recoverable (in global top-128): {ceiling['recoverable']}")
    print(f"  absent / un-recoverable ceiling: {ceiling['absent_ceiling']}")

    knobs: list[dict[str, Any]] = [{"lexical_floor": k} for k in (1, 2, 3, 4)]
    knobs += [{"arm_quota": q} for q in ((6, 5), (8, 3), (5, 6))]

    sample = sorted(ctx.questions)[: args.latency_sample]
    baseline_latency = measure_latency(ctx, sample, {})

    results = []
    for knob in knobs:
        knob_ceiling = (
            measure_ceiling(
                ctx, ground["recovery_targets"], knob_kwargs=knob
            )
            if "arm_quota" in knob
            else ceiling
        )
        row = evaluate_knob(ctx, ground, knob_ceiling, knob)
        row["latency"] = measure_latency(ctx, sample, knob)
        row["latency_pct_vs_default"] = (
            (row["latency"]["p95_ms"] / baseline_latency["p95_ms"] - 1.0) * 100.0
            if baseline_latency["p95_ms"]
            else 0.0
        )
        row["label"] = _knob_label(knob)
        results.append(row)

    print("\n== SWEEP TABLE (component gate is FROZEN; shipping knob NOT picked here) ==")
    header = (
        f"{'knob':<11}{'recov%':>8}{'readmit':>9}{'recoverbl':>10}"
        f"{'refDist/154':>13}{'srcDist/154':>13}{'refsDropd':>10}{'p95 Δ%':>9}"
    )
    print(header)
    print("-" * len(header))
    for row in results:
        print(
            f"{row['label']:<11}{row['recovery_pct']:>7.1f}%"
            f"{row['recovery_readmitted']:>9}{row['recovery_recoverable']:>10}"
            f"{row['preservation_disturbed_count']:>13}"
            f"{row['preservation_source_disturbed_count']:>13}"
            f"{row['preservation_total_refs_dropped']:>10}"
            f"{row['latency_pct_vs_default']:>+8.1f}%"
        )

    payload = {
        "golden_gate": golden,
        "ceiling": {k: v for k, v in ceiling.items() if k != "per_ref_in_global_top128"},
        "ground_truth_sizes": {
            "genuine_loss_qids": len(ground["genuine_loss_qids"]),
            "preserved_qids": len(ground["preserved_qids"]),
        },
        "baseline_latency": baseline_latency,
        "sweep": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {args.out}")
    return 0 if golden["passed"] == golden["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
