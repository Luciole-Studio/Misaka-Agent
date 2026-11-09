#!/usr/bin/env python3
"""Check that the generated model data matches its manifest and the generated catalog modules.

Translated from pi's ``packages/ai/scripts/check-model-data.ts`` (v1.1.0).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Sibling scripts, as pi's scripts import `./model-data.ts`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_data import validateGeneratedModelData

packageRoot = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        validateGeneratedModelData(packageRoot)
        print("Generated model data is valid.")
        return 0
    except Exception as error:  # noqa: BLE001 - reported, and the exit status says it failed
        print(str(error), file=sys.stderr)
        # MISAKA fork: misaka ships the data pi published, so the remedy is to copy a release, not to generate.
        print(
            "\nModel data is missing or stale. Run `python scripts/refresh_model_catalog.py --version <pi-ai version>` "
            "from the repository root.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
