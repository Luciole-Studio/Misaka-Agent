"""Compatibility entry point; maintained regressions live in the default test suite."""

import runpy
from pathlib import Path

globals().update(
    {
        key: value
        for key, value in runpy.run_path(
            str(
                Path(__file__).resolve().parents[2]
                / "tests/test_research_sweep_repairs.py"
            )
        ).items()
        if not key.startswith("__")
    }
)
