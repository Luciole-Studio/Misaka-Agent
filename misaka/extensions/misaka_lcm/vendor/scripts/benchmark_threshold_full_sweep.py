#!/usr/bin/env python3
"""Compare one incremental compaction with one bounded threshold full sweep.

The workload and summarizer are deterministic and synthetic. The JSON output
contains aggregate metrics only: it never includes message text, database
paths, session identifiers, or configuration from a live Hermes profile.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarking.replay import _ensure_hermes_lcm_package


FACT_RE = re.compile(r"\bFACT_[0-9]{3}\b")


def _messages(*, historical_messages: int, fresh_tail_messages: int) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": "Synthetic benchmark."}]
    for index in range(historical_messages + fresh_tail_messages):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(
            {
                "role": role,
                "content": f"FACT_{index:03d} " + (f"synthetic_{index:03d} " * 90),
            }
        )
    return messages


def _visible_facts(messages: list[dict[str, Any]]) -> set[str]:
    return set(FACT_RE.findall("\n".join(str(message.get("content") or "") for message in messages)))


def _run_policy(
    root: Path,
    *,
    name: str,
    full_sweep: bool,
    messages: list[dict[str, str]],
    fresh_tail_messages: int,
    leaf_chunk_tokens: int,
) -> dict[str, Any]:
    _ensure_hermes_lcm_package()
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.engine import LCMEngine
    from hermes_lcm.tokens import count_messages_tokens

    config = LCMConfig(
        fresh_tail_count=fresh_tail_messages,
        leaf_chunk_tokens=leaf_chunk_tokens,
        dynamic_leaf_chunk_enabled=True,
        dynamic_leaf_chunk_max=leaf_chunk_tokens,
        incremental_max_depth=0,
        threshold_full_sweep_enabled=full_sweep,
        summary_prefix_target_tokens=1_000_000,
        database_path=str(root / f"{name}.db"),
    )
    engine = LCMEngine(config=config, hermes_home=str(root / f"{name}-home"))
    engine._session_id = f"synthetic-{name}"
    tokens_before = count_messages_tokens(messages)
    engine.threshold_tokens = tokens_before
    summary_calls = 0
    publications = 0

    def deterministic_leaf(
        chunk: list[dict[str, Any]],
        focus_topic: str | None = None,
        deadline: float | None = None,
    ):
        nonlocal summary_calls
        del focus_topic, deadline
        summary_calls += 1
        source_tokens = count_messages_tokens(chunk)
        facts = sorted(_visible_facts(chunk))
        summary = "Synthetic retained facts: " + " ".join(facts)
        return chunk, source_tokens, summary, 1, 0

    original_assemble = engine._assemble_context

    def count_publication(*args: Any, **kwargs: Any):
        nonlocal publications
        publications += 1
        return original_assemble(*args, **kwargs)

    engine._summarize_leaf_chunk_with_rescue = deterministic_leaf
    engine._assemble_context = count_publication
    started = time.perf_counter()
    try:
        compressed = engine.compress(messages, current_tokens=tokens_before)
        duration_ms = (time.perf_counter() - started) * 1000.0
        tokens_after = count_messages_tokens(compressed)
        source_facts = _visible_facts(messages)
        visible_facts = _visible_facts(compressed)
        recoverable_facts: set[str] = set()
        for fact in source_facts:
            hits = engine._store.search(fact, session_id=engine._session_id, limit=1)
            if hits:
                recoverable_facts.add(fact)
        telemetry = engine.get_status().get("threshold_full_sweep", {})
        return {
            "policy": name,
            "prompt_prefix_publications": publications,
            "summary_calls": summary_calls,
            "prompt_tokens_before": tokens_before,
            "prompt_tokens_after": tokens_after,
            "compression_ratio": round(tokens_after / tokens_before, 6),
            "duration_ms": round(duration_ms, 3),
            "synthetic_facts_total": len(source_facts),
            "synthetic_facts_provider_visible": len(source_facts & visible_facts),
            "synthetic_facts_recoverable": len(recoverable_facts),
            "stop_reason": telemetry.get("stop_reason") if full_sweep else "threshold_relieved",
            "budget_exhausted": bool(telemetry.get("budget_exhausted", False)),
        }
    finally:
        engine.shutdown()


def run_benchmark(
    *,
    historical_messages: int = 20,
    fresh_tail_messages: int = 4,
    leaf_chunk_tokens: int = 1_200,
) -> dict[str, Any]:
    messages = _messages(
        historical_messages=historical_messages,
        fresh_tail_messages=fresh_tail_messages,
    )
    with tempfile.TemporaryDirectory(prefix="hermes-lcm-sweep-benchmark-") as temp_dir:
        root = Path(temp_dir)
        runs = [
            _run_policy(
                root,
                name="incremental",
                full_sweep=False,
                messages=messages,
                fresh_tail_messages=fresh_tail_messages,
                leaf_chunk_tokens=leaf_chunk_tokens,
            ),
            _run_policy(
                root,
                name="threshold_full_sweep",
                full_sweep=True,
                messages=messages,
                fresh_tail_messages=fresh_tail_messages,
                leaf_chunk_tokens=leaf_chunk_tokens,
            ),
        ]
    return {
        "schema_version": 1,
        "workload": "deterministic_synthetic_threshold_sweep",
        "transcript_contents_included": False,
        "historical_messages": historical_messages,
        "fresh_tail_messages": fresh_tail_messages,
        "leaf_chunk_tokens": leaf_chunk_tokens,
        "runs": runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-messages", type=int, default=20)
    parser.add_argument("--fresh-tail-messages", type=int, default=4)
    parser.add_argument("--leaf-chunk-tokens", type=int, default=1_200)
    args = parser.parse_args(argv)
    if args.historical_messages < 1:
        parser.error("--historical-messages must be positive")
    if args.fresh_tail_messages < 1:
        parser.error("--fresh-tail-messages must be positive")
    if args.leaf_chunk_tokens < 1:
        parser.error("--leaf-chunk-tokens must be positive")
    print(
        json.dumps(
            run_benchmark(
                historical_messages=args.historical_messages,
                fresh_tail_messages=args.fresh_tail_messages,
                leaf_chunk_tokens=args.leaf_chunk_tokens,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
