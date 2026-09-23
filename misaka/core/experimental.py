"""Pi's process-wide experimental feature gate."""

from __future__ import annotations

import os


def are_experimental_features_enabled() -> bool:
    return os.environ.get("PI_EXPERIMENTAL") == "1"


areExperimentalFeaturesEnabled = are_experimental_features_enabled

__all__ = ["areExperimentalFeaturesEnabled"]
