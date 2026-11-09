"""Pi's process-wide experimental feature gate."""

from __future__ import annotations

import os


def areExperimentalFeaturesEnabled() -> bool:
    return os.environ.get("PI_EXPERIMENTAL") == "1"



__all__ = ["areExperimentalFeaturesEnabled"]
