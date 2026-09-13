"""Tests for the scrubbed threshold full-sweep benchmark."""

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_threshold_full_sweep.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("threshold_full_sweep_benchmark", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_threshold_full_sweep_benchmark_is_scrubbed_and_compares_atomic_publication():
    report = _load_script().run_benchmark(
        historical_messages=8,
        fresh_tail_messages=2,
        leaf_chunk_tokens=300,
    )

    assert report["transcript_contents_included"] is False
    incremental, sweep = report["runs"]
    assert incremental["policy"] == "incremental"
    assert sweep["policy"] == "threshold_full_sweep"
    assert incremental["prompt_prefix_publications"] == 1
    assert sweep["prompt_prefix_publications"] == 1
    assert sweep["summary_calls"] > incremental["summary_calls"]
    assert sweep["prompt_tokens_after"] < incremental["prompt_tokens_after"]
    assert sweep["synthetic_facts_provider_visible"] == sweep["synthetic_facts_total"]
    assert sweep["synthetic_facts_recoverable"] == sweep["synthetic_facts_total"]
    assert sweep["budget_exhausted"] is False
