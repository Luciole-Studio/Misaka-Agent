#!/usr/bin/env python3
"""Provider-free W3b iteration-4 replay for Knob G and Knob H.

Reuses the frozen H5 target/preservation contract and the W3b C1 replay
infrastructure (:mod:`benchmarking.w3b_diversity_replay`). It re-runs only the
two grid cells named by the iteration-4 dispatch -- ``cap2`` (base replay
stores) and ``q32xcap2`` (W3a per-state-embedded working copies) -- three ways
each: the default C1 cell, the same cell with Knob G
(``antiboilerplate=True``), and with Knob H (``title_boost=True``). The delta
vs the frozen W3b build-report table is what the arm dispatch consumes.

No embedding or reader provider is called. The state-semantic query direction
is reconstructed deterministically from frozen source-vector projections, the
same as the parent harness. This is provider-free component evidence, not an
official reader/judge/promotion result.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import sys

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
from benchmarking.w3b_diversity_replay import (  # noqa: E402
    _DEFAULT_DB_DIR,
    ProjectedStateReplayContext,
    _evaluate_cell,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=_RUN_ROOT)
    parser.add_argument("--h1-artifacts", type=Path, default=_H1)
    parser.add_argument("--h31-artifacts", type=Path, default=_H31)
    parser.add_argument("--targets", type=Path, default=_H5_TARGETS)
    parser.add_argument("--db-dir", type=Path, default=_DEFAULT_DB_DIR)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cap", type=int, default=2)
    parser.add_argument("--state-quota", type=int, default=32)
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

    print("== GOLDEN GATES (all W3b knobs off, incl. G/H) ==", flush=True)
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
            args.h1_artifacts / "old-rescore" / "reconciliation_sets.json"
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
    base_delivered = {qid: base_context.deliver(qid) for qid in all_qids}
    state_base_delivered = {
        qid: state_context.deliver(qid) for qid in all_qids
    }
    if base_delivered != state_base_delivered:
        print("STATE-DB BASELINE DIFFERS -- aborting cells", flush=True)
        return 2

    cap = args.cap
    quota = args.state_quota
    cells: list[dict[str, Any]] = []

    def run(context, h5, label, kwargs, mode):
        started = time.perf_counter()
        row = _evaluate_cell(context, h5, kwargs, base_delivered, preserved_qids)
        row["label"] = label
        row["state_query_mode"] = mode
        row["elapsed_s"] = round(time.perf_counter() - started, 3)
        cells.append(row)
        print(
            f"  {label}: pool {row['target_pool_covered']}/30 "
            f"(new {row['new_pool_recovery']}) | "
            f"dRec@16 {row['delivered_recall_at_16']}/30 | "
            f"pres {row['preservation_retained']}/154",
            flush=True,
        )

    # cap2 family (base replay stores)
    run(base_context, base_h5, f"cap{cap}", {"diversity_cap": cap}, "not_used")
    run(
        base_context, base_h5, f"cap{cap}+G",
        {"diversity_cap": cap, "antiboilerplate": True}, "not_used",
    )
    run(
        base_context, base_h5, f"cap{cap}+H",
        {"diversity_cap": cap, "title_boost": True}, "not_used",
    )

    # q32xcap2 family (W3a per-state-embedded working copies)
    run(
        state_context, state_h5, f"q{quota}xcap{cap}",
        {"state_semantic_quota": quota, "diversity_cap": cap},
        "recorded-source-projection",
    )
    run(
        state_context, state_h5, f"q{quota}xcap{cap}+G",
        {
            "state_semantic_quota": quota,
            "diversity_cap": cap,
            "antiboilerplate": True,
        },
        "recorded-source-projection",
    )
    run(
        state_context, state_h5, f"q{quota}xcap{cap}+H",
        {
            "state_semantic_quota": quota,
            "diversity_cap": cap,
            "title_boost": True,
        },
        "recorded-source-projection",
    )

    payload = {
        "proof_boundary": (
            "Provider-free component replay only; reconstructed query directions "
            "do not prove official reader/judge accuracy or promotion."
        ),
        "golden_gate": {"base": base_golden, "state_db": state_golden},
        "targets_manifest": str(args.targets),
        "db_dir": str(args.db_dir),
        "cap": cap,
        "state_quota": quota,
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
