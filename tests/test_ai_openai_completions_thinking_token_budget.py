"""Translated from pi's ``packages/ai/test/openai-completions-thinking-token-budget.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.compat import streamSimple
from misaka.ai.types import Context, Model, UserMessage
from tests.openai_fake import installFakeOpenAI

DEFAULT_CHUNKS = [
    {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
]


@pytest.fixture(autouse=True)
def mockState(monkeypatch):
    return installFakeOpenAI(monkeypatch, DEFAULT_CHUNKS)


def vllmModel(compat: dict[str, Any] | None = None) -> Model:
    return Model.model_validate(
        {
            "id": "zai-org/glm-5.2",
            "name": "GLM 5.2 (local vLLM)",
            "api": "openai-completions",
            "provider": "local-vllm",
            "baseUrl": "http://localhost:8000/v1",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 262144,
            "maxTokens": 16384,
            "compat": compat if compat is not None else {"thinkingFormat": "zai", "supportsThinkingTokenBudget": True},
        }
    )


async def capture(model: Model, options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    captured: list[Any] = []
    await streamSimple(
        model,
        Context(messages=[UserMessage(content="Hi", timestamp=time.time_ns() // 1_000_000)]),
        {
            "apiKey": "test",
            "reasoning": options.get("reasoning"),
            "thinkingBudgets": options.get("thinkingBudgets"),
            "maxTokens": options.get("maxTokens"),
            "onPayload": lambda params, _model: captured.append(params),
        },
    ).result()
    return captured[-1]


async def test_sends_the_configured_budget_for_the_requested_level():
    params = await capture(vllmModel(), {"reasoning": "medium", "thinkingBudgets": {"medium": 4096}})
    assert params["thinking_token_budget"] == 4096


async def test_omits_the_budget_when_neither_the_field_nor_the_alias_is_set():
    params = await capture(vllmModel({"thinkingFormat": "zai"}), {"reasoning": "medium", "thinkingBudgets": {"medium": 4096}})
    assert "thinking_token_budget" not in params
    assert "thinking_budget" not in params
    assert "thinking_budget_tokens" not in params


async def test_omits_the_budget_when_thinking_is_off():
    params = await capture(vllmModel(), {"reasoning": None, "thinkingBudgets": {"high": 8192}})
    assert "thinking_token_budget" not in params


async def test_clamps_xhigh_and_max_to_the_high_budget():
    xhigh = await capture(vllmModel(), {"reasoning": "xhigh", "thinkingBudgets": {"high": 8192}})
    max = await capture(vllmModel(), {"reasoning": "max", "thinkingBudgets": {"high": 8192}})
    assert xhigh["thinking_token_budget"] == 8192
    assert max["thinking_token_budget"] == 8192


async def test_leaves_room_for_the_answer_when_the_budget_meets_the_response_ceiling():
    params = await capture(vllmModel(), {"reasoning": "high"})
    assert params["thinking_token_budget"] == 16384 - 1024


async def test_uses_the_caller_max_tokens_as_the_ceiling_when_it_is_lower_than_the_model_cap():
    params = await capture(vllmModel(), {"reasoning": "high", "thinkingBudgets": {"high": 8192}, "maxTokens": 4096})
    assert params["thinking_token_budget"] == 4096 - 1024


@pytest.mark.parametrize("field", ["thinking_budget", "thinking_budget_tokens"])
async def test_sends_the_field_when_thinking_token_budget_field_is_set(field):
    params = await capture(
        vllmModel({"thinkingFormat": "qwen", "thinkingTokenBudgetField": field}),
        {"reasoning": "medium", "thinkingBudgets": {"medium": 4096}},
    )
    assert params[field] == 4096
    assert "thinking_token_budget" not in params


async def test_lets_thinking_token_budget_field_win_over_the_boolean_alias():
    params = await capture(
        vllmModel({"thinkingFormat": "zai", "supportsThinkingTokenBudget": True, "thinkingTokenBudgetField": "thinking_budget"}),
        {"reasoning": "medium", "thinkingBudgets": {"medium": 4096}},
    )
    assert params["thinking_budget"] == 4096
    assert "thinking_token_budget" not in params


async def test_puts_the_clamped_budget_in_chat_template_kwargs_when_var_is_thinking_budget():
    params = await capture(
        vllmModel(
            {
                "thinkingFormat": "chat-template",
                "chatTemplateKwargs": {
                    "enable_thinking": {"$var": "thinking.enabled"},
                    "thinking_budget": {"$var": "thinking.budget"},
                },
            }
        ),
        {"reasoning": "high"},
    )
    assert params["chat_template_kwargs"] == {"enable_thinking": True, "thinking_budget": 16384 - 1024}
    assert "thinking_token_budget" not in params


async def test_omits_thinking_budget_from_chat_template_kwargs_when_thinking_is_off():
    params = await capture(
        vllmModel(
            {
                "thinkingFormat": "chat-template",
                "chatTemplateKwargs": {
                    "enable_thinking": {"$var": "thinking.enabled"},
                    "thinking_budget": {"$var": "thinking.budget"},
                },
            }
        ),
        {"reasoning": None},
    )
    assert params["chat_template_kwargs"] == {"enable_thinking": False}
