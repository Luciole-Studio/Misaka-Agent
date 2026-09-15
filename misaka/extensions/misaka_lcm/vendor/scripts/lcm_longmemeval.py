#!/usr/bin/env python3
"""LongMemEval retrieval-quality harness CLI for hermes-lcm.

Two subcommands:

    fetch   Download the pinned LongMemEval_S dataset file once (operator step).
    run     Ingest histories into fresh temp LCM stores and score the arms.

Offline by default: `run` never downloads. Deterministic with `--provider stub`;
`--provider fastembed` uses the local FastEmbed model (CI-grade, no network at
query time once cached). See benchmarks/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarking.longmemeval import (  # noqa: E402
    DATASET_FILENAME,
    DATASET_REPO_ID,
    DATASET_REVISION,
    PROVIDERS,
    load_questions,
    render_markdown,
    run_harness,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="Download the pinned LongMemEval_S dataset file.")
    fetch.add_argument("--output", required=True, help="Directory to write the dataset file into.")

    run = sub.add_parser("run", help="Run the retrieval harness over the dataset.")
    run.add_argument("--dataset", required=True, help="Path to the downloaded longmemeval_s file.")
    run.add_argument("--output", required=True, help="Output directory for metrics JSON + markdown.")
    run.add_argument(
        "--provider",
        default="stub",
        choices=PROVIDERS,
        help="Embedding provider. 'stub' is deterministic/offline (scores meaningless).",
    )
    run.add_argument("--model", default="", help="Embedding model id (required for non-stub).")
    run.add_argument("--limit", type=int, default=None, help="Score only the first N questions.")
    run.add_argument(
        "--rerank",
        action="store_true",
        help="Use the real cross-encoder rerank arm (VoyageProvider.rerank, "
        "rerank-2.5-lite) when --provider voyage; otherwise the deterministic "
        "placeholder cosine reranker is used and labeled as such.",
    )
    run.add_argument(
        "--no-db-template",
        dest="reuse_db_template",
        action="store_false",
        help="Disable the reused pre-migrated DB template (bootstrap each question "
        "from scratch). Mainly for measuring the F7 ingest speedup.",
    )
    run.add_argument("--json", action="store_true", help="Print the metrics JSON to stdout.")
    run.add_argument(
        "--allow-external-output",
        action="store_true",
        help="Allow --output outside this repository.",
    )
    return parser.parse_args(argv)


def _validate_output_path(path: Path, *, allow_external: bool) -> Path:
    resolved = path.resolve()
    repo_root = REPO_ROOT.resolve()
    if not allow_external and not resolved.is_relative_to(repo_root):
        raise SystemExit(
            f"Refusing output outside repo: {resolved}. Pass --allow-external-output to override."
        )
    return resolved


def _cmd_fetch(args: argparse.Namespace) -> int:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise SystemExit(
            "huggingface_hub is required for `fetch`; install it, then re-run. "
            "The benchmark `run` step itself needs no network."
        )
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = hf_hub_download(
        repo_id=DATASET_REPO_ID,
        filename=DATASET_FILENAME,
        repo_type="dataset",
        revision=DATASET_REVISION,
        local_dir=str(output_dir),
    )
    print(json.dumps({"dataset_path": path, "revision": DATASET_REVISION}, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.provider != "stub" and not args.model:
        raise SystemExit(f"--model is required for --provider {args.provider}")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be a positive integer")
    dataset_path = Path(args.dataset)
    if not dataset_path.is_file():
        raise SystemExit(f"dataset file not found: {dataset_path}. Run `fetch` first.")
    output_dir = _validate_output_path(Path(args.output), allow_external=args.allow_external_output)
    output_dir.mkdir(parents=True, exist_ok=True)

    questions = load_questions(dataset_path, limit=args.limit)

    with tempfile.TemporaryDirectory(prefix="lcm-longmemeval-") as tmp:
        tmp_dir = Path(tmp)
        os.environ.setdefault("HERMES_HOME", str(tmp_dir / "hermes-home"))
        report = run_harness(
            questions,
            provider_name=args.provider,
            model=args.model,
            tmp_dir=tmp_dir,
            use_rerank=args.rerank,
            reuse_db_template=args.reuse_db_template,
        )

    metrics_path = output_dir / "longmemeval_metrics.json"
    metrics_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown = render_markdown(report)
    (output_dir / "longmemeval_metrics.md").write_text(markdown + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(markdown)
        print(f"\nmetrics: {metrics_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "fetch":
        return _cmd_fetch(args)
    if args.command == "run":
        return _cmd_run(args)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
