"""pi durable's second test pass: its ``npm test`` is ``vitest --run && PI_DURABLE_DOCUMENT_RETENTION_MS=0 vitest --run``.

The durable suite runs again with every unattached document evicted right after use
(``MISAKA_DURABLE_DOCUMENT_RETENTION_MS=0``, read by ``tests/durable_support.py``), so a regression that only shows when
documents leave the cache at once fails the default run. MISAKA fork: pytest has no script of its own, so this test runs
the second pass in a child process; inside that pass it is skipped.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORCED = "MISAKA_DURABLE_DOCUMENT_RETENTION_MS"


@pytest.mark.skipif(os.environ.get(FORCED) is not None, reason="this is the evicting pass")
def test_the_durable_suite_passes_with_documents_evicted_at_once() -> None:
    files = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "tests").glob("test_durable_*.py")
        if path.name != Path(__file__).name
    )
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *files],
        cwd=ROOT,
        env={**os.environ, FORCED: "0"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=1800,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
