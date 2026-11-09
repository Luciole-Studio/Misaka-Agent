"""Translated from pi's ``packages/ai/test/overflow.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time

from misaka.ai.types import AssistantMessage, Usage, UsageCost
from misaka.ai.utils.overflow import isContextOverflow, isRecoverableLength


def _nowMs() -> int:
    return time.time_ns() // 1_000_000


def createErrorMessage(errorMessage: str, provider: str = "ollama") -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api="openai-completions",
        provider=provider,
        model="qwen3.5:35b",
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="error",
        errorMessage=errorMessage,
        timestamp=_nowMs(),
    )


def test_detects_explicit_ollama_prompt_too_long_errors():
    message = createErrorMessage("400 `prompt too long; exceeded max context length by 100918 tokens`")
    assert isContextOverflow(message, 32768) is True


def test_detects_zai_prompt_too_long_errors():
    # Regression for #9805.
    message = createErrorMessage('400 {"code":"1261","message":"Prompt too long"}', "zai")
    assert isContextOverflow(message, 1048576) is True


def test_detects_zai_cn_endpoint_prompt_exceeds_max_length_errors():
    # Regression for #10208.
    message = createErrorMessage('400 {"code":"1261","message":"Prompt exceeds max length"}', "zai")
    assert isContextOverflow(message, 1048576) is True


def test_detects_together_ai_context_length_errors():
    message = createErrorMessage(
        "400 The input (516368 tokens) is longer than the model's context length (262144 tokens)."
    )
    assert isContextOverflow(message, 262144) is True


def test_detects_litellm_wrapped_openai_maximum_context_length_errors():
    message = createErrorMessage(
        "Error: 503 litellm.ServiceUnavailableError: litellm.MidStreamFallbackError: litellm.APIConnectionError: "
        "APIConnectionError: OpenAIException - Requested token count exceeds the model's maximum context length of "
        "131072 tokens."
    )
    assert isContextOverflow(message, 131072) is True


def test_detects_openai_compatible_parenthesized_maximum_context_length_errors():
    message = createErrorMessage("Error: 400 Input length (265330) exceeds model's maximum context length (262144).")
    assert isContextOverflow(message, 262144) is True


def test_detects_openrouter_poolside_maximum_allowed_input_length_errors():
    message = createErrorMessage(
        "Provider returned error: Input length 131393 exceeds the maximum allowed input length of 131040 tokens."
    )
    assert isContextOverflow(message, 131072) is True


def test_detects_ds4_configured_context_size_errors():
    message = createErrorMessage("400 Prompt has 256468 tokens, but the configured context size is 256000 tokens")
    assert isContextOverflow(message, 256000) is True

    commaMessage = createErrorMessage("Prompt has 5,958,968 tokens, but the configured context size is 256,000 tokens")
    assert isContextOverflow(commaMessage, 256000) is True


def test_does_not_treat_generic_non_overflow_ollama_errors_as_overflow():
    message = createErrorMessage("500 `model runner crashed unexpectedly`")
    assert isContextOverflow(message, 32768) is False


def test_only_treats_bodyless_400_and_413_errors_as_overflow_for_cerebras():
    # Regression for #9482.
    for errorMessage in ["400 status code (no body)", "413 status code (no body)"]:
        assert isContextOverflow(createErrorMessage(errorMessage, "cerebras"), 131072) is True
        assert isContextOverflow(createErrorMessage(errorMessage, "opencode-go"), 1000000) is False


def test_does_not_treat_bedrock_throttling_too_many_tokens_as_overflow():
    # Bedrock returns this for HTTP 429 rate limiting, NOT context overflow.
    # formatBedrockError uses a human-readable prefix for ThrottlingException.
    message = createErrorMessage("Throttling error: Too many tokens, please wait before trying again.")
    assert isContextOverflow(message, 200000) is False


def test_does_not_treat_bedrock_service_unavailable_as_overflow():
    message = createErrorMessage("Service unavailable: The service is temporarily unavailable.")
    assert isContextOverflow(message, 200000) is False


def test_does_not_treat_generic_rate_limit_errors_as_overflow():
    message = createErrorMessage("Rate limit exceeded, please retry after 30 seconds.")
    assert isContextOverflow(message, 200000) is False


def test_does_not_treat_http_429_style_errors_as_overflow():
    message = createErrorMessage("Too many requests. Please slow down.")
    assert isContextOverflow(message, 200000) is False


def createLengthStopMessage(
    *,
    input: int,
    cacheRead: int,
    output: int,
    cacheWrite: int | None = None,
    api: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> AssistantMessage:
    cacheWrite = cacheWrite if cacheWrite is not None else 0
    return AssistantMessage(
        role="assistant",
        content=[],
        api=api if api is not None else "openai-completions",
        provider=provider if provider is not None else "test-provider",
        model=model if model is not None else "test-model",
        usage=Usage(
            input=input,
            output=output,
            cacheRead=cacheRead,
            cacheWrite=cacheWrite,
            totalTokens=input + cacheRead + cacheWrite + output,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="length",
        timestamp=_nowMs(),
    )


def test_detects_xiaomi_style_overflow_length_stop_with_zero_output_and_filled_context():
    message = createLengthStopMessage(input=58, cacheRead=1048512, output=0, provider="xiaomi", model="mimo-v2.5-pro")
    assert isContextOverflow(message, 1048576) is True


def test_treats_a_length_stop_below_the_desired_output_limit_as_recoverable():
    message = createLengthStopMessage(
        input=3,
        cacheRead=253584,
        cacheWrite=25554,
        output=16,
        api="openai-responses",
        provider="openai",
        model="gpt-5.6-sol",
    )
    assert isRecoverableLength(message, 128000) is True


def test_does_not_recover_a_length_stop_that_reached_the_desired_output_limit():
    message = createLengthStopMessage(input=4062, cacheRead=0, output=1024)
    assert isRecoverableLength(message, 1024) is False


def test_treats_zero_output_length_stops_as_recoverable_without_context_metadata():
    message = createLengthStopMessage(input=100, cacheRead=0, output=0)
    assert isRecoverableLength(message, 128000) is True


def test_does_not_treat_normal_length_stops_with_output_as_context_overflow():
    message = createLengthStopMessage(input=1000, cacheRead=0, output=4096)
    assert isContextOverflow(message, 200000) is False


def test_does_not_treat_zero_output_length_stops_far_below_context_as_context_overflow():
    message = createLengthStopMessage(input=100, cacheRead=0, output=0)
    assert isContextOverflow(message, 200000) is False
