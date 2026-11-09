"""Translated from pi's ``packages/ai/test/reasoning-options.test.ts`` (v1.1.0)."""

from __future__ import annotations

import sys
from pathlib import Path

# pi's tests import `../scripts/<name>.ts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from models_dev_reasoning_options import getEffortThinkingLevelMap


class TestGetEffortThinkingLevelMap:
    def test_exposes_only_verified_effort_values_and_none(self) -> None:
        assert getEffortThinkingLevelMap(
            [{"type": "toggle"}, {"type": "effort", "values": ["none", "low", "high", "max"]}]
        ) == {
            "off": "none",
            "minimal": None,
            "low": "low",
            "medium": None,
            "high": "high",
            "xhigh": None,
            "max": "max",
        }

    def test_does_not_infer_thinking_off_from_an_effort_list(self) -> None:
        assert getEffortThinkingLevelMap([{"type": "effort", "values": ["low", "high", "max"]}]) == {
            "off": None,
            "minimal": None,
            "low": "low",
            "medium": None,
            "high": "high",
            "xhigh": None,
            "max": "max",
        }

    def test_leaves_toggle_and_budget_controls_for_their_adapter_specific_implementations(self) -> None:
        assert getEffortThinkingLevelMap([{"type": "toggle"}]) is None
        assert getEffortThinkingLevelMap([{"type": "budget_tokens", "min": 1024, "max": 32000}]) is None
        assert getEffortThinkingLevelMap([{"type": "effort", "values": [None, "default"]}]) is None
