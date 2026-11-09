"""Translated from pi's ``packages/ai/test/model-cost-tiers.test.ts`` (v1.1.0)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# pi's tests import `../scripts/<name>.ts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ai_gateway_pricing import getAiGatewayCost
from openrouter_catalog import buildOpenRouterCatalog

from misaka.ai.providers.openrouter_api import openRouterCost


def openRouterChatCost(pricing: dict[str, Any]) -> dict[str, Any] | None:
    catalog = buildOpenRouterCatalog(
        [{"id": "anthropic/claude-haiku-5.5", "name": "Claude Haiku 5.5", "supported_parameters": ["tools"], "pricing": pricing}],
        [],
        [],
    )
    return catalog["chat"][0]["cost"] if catalog["chat"] else None


class TestOpenRouterPricingOverrides:
    def test_turns_prompt_length_overrides_into_tiers(self) -> None:
        assert openRouterChatCost(
            {
                "prompt": "0.0000001",
                "completion": "0.0000005",
                "input_cache_read": "0.00000001",
                "input_cache_write": "0.000000125",
                "overrides": [
                    {
                        "min_prompt_tokens": 100000,
                        "prompt": "0.0000005",
                        "completion": "0.0000025",
                        "input_cache_read": "0.00000005",
                        "input_cache_write": "0.000000625",
                    },
                ],
            }
        ) == {
            "input": 0.1,
            "output": 0.5,
            "cacheRead": 0.01,
            "cacheWrite": 0.125,
            "tiers": [{"inputTokensAbove": 100000, "input": 0.5, "output": 2.5, "cacheRead": 0.05, "cacheWrite": 0.625}],
        }

    def test_keeps_base_rates_missing_from_an_override(self) -> None:
        cost = openRouterChatCost(
            {
                "prompt": "0.000002",
                "completion": "0.000012",
                "input_cache_read": "0.0000002",
                "input_cache_write": "0.000000375",
                "overrides": [{"min_prompt_tokens": 200000, "prompt": "0.000004", "completion": "0.000018"}],
            }
        )
        assert cost is not None
        assert cost["tiers"] == [{"inputTokensAbove": 200000, "input": 4, "output": 18, "cacheRead": 0.2, "cacheWrite": 0.375}]

    def test_skips_time_of_day_overrides(self) -> None:
        assert openRouterChatCost(
            {
                "prompt": "0.000000132",
                "completion": "0.000000528",
                "overrides": [
                    {"utc_start": 0, "utc_end": 1600, "prompt": "0.000000132", "completion": "0.000000528"},
                    {"utc_days": ["saturday"], "min_prompt_tokens": 1000, "prompt": "0.0000001"},
                ],
            }
        ) == {"input": 0.132, "output": 0.528, "cacheRead": 0, "cacheWrite": 0}

    # Regression for #10672: OpenRouter reports -1 for router models such as `openrouter/auto`.
    def test_uses_zero_for_negative_invalid_and_missing_base_prices(self) -> None:
        assert openRouterChatCost({"prompt": "-1", "completion": "abc", "input_cache_read": ""}) == {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
        }

    def test_keeps_base_rates_for_negative_or_invalid_override_rates(self) -> None:
        cost = openRouterChatCost(
            {
                "prompt": "0.000002",
                "completion": "0.000012",
                "overrides": [{"min_prompt_tokens": 200000, "prompt": "-1", "completion": "n/a"}],
            }
        )
        assert cost is not None
        assert cost["tiers"] == [{"inputTokensAbove": 200000, "input": 2, "output": 12, "cacheRead": 0, "cacheWrite": 0}]

    def test_treats_zero_as_free(self) -> None:
        assert openRouterCost({"prompt": "0", "completion": "0.000001"}) == {
            "input": 0,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
        }
        assert openRouterCost(None) == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


class TestVercelAiGatewayPricingTiers:
    def test_turns_bracket_starts_into_tiers_with_the_rates_in_effect_there(self) -> None:
        assert getAiGatewayCost(
            {
                "input": "0.000001",
                "output": "0.000005",
                "input_cache_read": "0.0000002",
                "input_tiers": [
                    {"cost": "0.000001", "min": 0, "max": 32001},
                    {"cost": "0.0000018", "min": 32001, "max": 128001},
                    {"cost": "0.000003", "min": 128001},
                ],
                "output_tiers": [
                    {"cost": "0.000005", "min": 0, "max": 32001},
                    {"cost": "0.000009", "min": 32001, "max": 128001},
                    {"cost": "0.000015", "min": 128001},
                ],
            }
        ) == {
            "input": 1,
            "output": 5,
            "cacheRead": 0.2,
            "cacheWrite": 0,
            "tiers": [
                {"inputTokensAbove": 32000, "input": 1.8, "output": 9, "cacheRead": 0.2, "cacheWrite": 0},
                {"inputTokensAbove": 128000, "input": 3, "output": 15, "cacheRead": 0.2, "cacheWrite": 0},
            ],
        }

    def test_returns_base_rates_without_tiers(self) -> None:
        assert getAiGatewayCost({"input": "0.000003", "output": 0.000015}) == {
            "input": 3,
            "output": 15,
            "cacheRead": 0,
            "cacheWrite": 0,
        }
