"""Pi's process-wide experimental feature gate."""

from __future__ import annotations

import os

_PREFER_STRICT_TOOL_SAMPLING = {
    "type": "json_schema",
    "strict": "prefer",
}


def are_experimental_features_enabled() -> bool:
    return os.environ.get("PI_EXPERIMENTAL") == "1"


def get_experimental_tool_sampling() -> dict[str, str] | None:
    return _PREFER_STRICT_TOOL_SAMPLING if are_experimental_features_enabled() else None


areExperimentalFeaturesEnabled = are_experimental_features_enabled

__all__ = ["areExperimentalFeaturesEnabled"]
