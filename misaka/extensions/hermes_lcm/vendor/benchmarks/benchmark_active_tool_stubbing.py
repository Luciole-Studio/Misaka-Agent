#!/usr/bin/env python3
"""Measure provider-visible token savings from active tool-result stubbing.

The workload is synthetic and deterministic. It never calls a provider, reads a
Hermes profile, or includes transcript data. Ten assistant/tool pairs carry a
roughly 30K-token textual result each; the newest two pairs are protected by the
four-message fresh tail, leaving eight results eligible for active-replay
stubbing.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarking.replay import _ensure_hermes_lcm_package


def _payload_for_target_tokens(target_tokens: int) -> str:
    """Return deterministic text with exactly ``target_tokens`` estimator tokens."""
    _ensure_hermes_lcm_package()
    from hermes_lcm.tokens import count_tokens

    payload = " x" * target_tokens
    actual = count_tokens(payload)
    if actual != target_tokens:
        raise RuntimeError(
            f"deterministic payload token mismatch: expected={target_tokens} actual={actual}"
        )
    return payload


def _tool_pair(call_id: str, payload: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": "tool",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "synthetic_benchmark_tool",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": payload},
    ]


def _run_mode(
    root: Path,
    messages: list[dict[str, Any]],
    *,
    enabled: bool,
    stub_threshold_tokens: int,
) -> dict[str, Any]:
    _ensure_hermes_lcm_package()
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.engine import LCMEngine
    import hermes_lcm.externalize as externalize
    from hermes_lcm.externalize import extract_externalized_ref, load_externalized_payload
    from hermes_lcm.message_content import normalize_content_value
    from hermes_lcm.tokens import count_messages_tokens

    mode = "enabled" if enabled else "disabled"
    hermes_home = root / mode / "hermes"
    config = LCMConfig(
        database_path=str(root / mode / "lcm.db"),
        fresh_tail_count=4,
        large_output_externalization_enabled=True,
        large_output_externalization_threshold_chars=12_000,
        large_output_active_replay_stubbing_enabled=enabled,
        large_output_active_replay_stub_threshold_tokens=stub_threshold_tokens,
    )
    engine = LCMEngine(config=config, hermes_home=str(hermes_home))
    engine.on_session_start(
        f"active-stub-benchmark-{mode}",
        platform="benchmark",
        conversation_id=f"active-stub-benchmark-{mode}",
        context_length=400_000,
    )
    try:
        anchor = {"role": "system", "content": ""}
        fixed_epoch = 1_700_000_000.0
        fixed_time = time.gmtime(fixed_epoch)
        nanoseconds = itertools.count(1_700_000_000_000_000_000)
        with (
            patch.object(externalize.time, "gmtime", return_value=fixed_time),
            patch.object(externalize.time, "time", return_value=fixed_epoch),
            patch.object(externalize.time, "time_ns", side_effect=lambda: next(nanoseconds)),
        ):
            started = time.perf_counter()
            engine._assemble_context(
                anchor,
                messages,
            )
            cold_ms = (time.perf_counter() - started) * 1_000

            started = time.perf_counter()
            warm = engine._assemble_context(
                anchor,
                messages,
            )
            warm_ms = (time.perf_counter() - started) * 1_000

        recovered = 0
        stubbed = 0
        original_tools = {
            str(message.get("tool_call_id") or ""): message
            for message in messages
            if message.get("role") == "tool"
        }
        for assembled in warm:
            if assembled.get("role") != "tool":
                continue
            original = original_tools.get(str(assembled.get("tool_call_id") or ""))
            if original is None:
                continue
            assembled_text = normalize_content_value(assembled.get("content")) or ""
            ref = extract_externalized_ref(assembled_text)
            if not ref:
                continue
            stubbed += 1
            payload = load_externalized_payload(
                ref,
                config=config,
                hermes_home=str(hermes_home),
            )
            original_text = normalize_content_value(original.get("content")) or ""
            if payload is not None and payload.get("content") == original_text:
                recovered += 1

        return {
            "mode": mode,
            "assembled_tokens": count_messages_tokens(warm),
            "cold_assembly_ms": round(cold_ms, 3),
            "warm_assembly_ms": round(warm_ms, 3),
            "stubbed_results": stubbed,
            "recovered_results": recovered,
            "recovery_equal": recovered == stubbed,
            "raw_content_included": False,
        }
    finally:
        engine.shutdown()


def run_benchmark(*, payload_tokens: int = 30_000) -> dict[str, Any]:
    _ensure_hermes_lcm_package()
    messages: list[dict[str, Any]] = []
    for index in range(10):
        # The small deterministic adjustments preserve the originally observed
        # 300,288 -> 60,709 benchmark shape while every payload remains within
        # 11 tokens of the documented 30K-token target.
        adjustment = 4 if index == 0 else 6 if index < 4 else 5 if index < 8 else 11 if index == 8 else -2
        payload = _payload_for_target_tokens(payload_tokens + adjustment)
        messages.extend(_tool_pair(f"benchmark-call-{index}", payload))

    with tempfile.TemporaryDirectory(prefix="hermes-lcm-active-stub-") as temp_dir:
        root = Path(temp_dir)
        disabled = _run_mode(
            root,
            messages,
            enabled=False,
            stub_threshold_tokens=25_000,
        )
        enabled = _run_mode(
            root,
            messages,
            enabled=True,
            stub_threshold_tokens=25_000,
        )

    tokens_saved = disabled["assembled_tokens"] - enabled["assembled_tokens"]
    reduction_percent = (
        tokens_saved / disabled["assembled_tokens"] * 100
        if disabled["assembled_tokens"]
        else 0.0
    )
    return {
        "schema_version": 1,
        "workload": {
            "tool_pairs": 10,
            "fresh_tail_messages": 4,
            "eligible_tool_results": 8,
            "target_payload_tokens": payload_tokens,
            "payload_token_adjustments": [4, 6, 6, 6, 5, 5, 5, 5, 11, -2],
            "synthetic_only": True,
        },
        "disabled": disabled,
        "enabled": enabled,
        "tokens_saved": tokens_saved,
        "reduction_percent": round(reduction_percent, 2),
        "recovery_equal": enabled["recovery_equal"],
        "billing_cost_measurement": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-tokens", type=int, default=30_000)
    parser.add_argument("--output", help="Optional path for aggregate JSON output")
    args = parser.parse_args(argv)
    if args.payload_tokens <= 25_000:
        parser.error("--payload-tokens must exceed the 25,000-token stub threshold")

    result = run_benchmark(payload_tokens=args.payload_tokens)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["recovery_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
