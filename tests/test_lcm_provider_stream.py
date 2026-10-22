"""LCM streams through the model's provider, except where a task's api_mode put the model on an
API that provider does not serve (``misaka_lcm/host/llm.py`` ``_provider_stream``)."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from misaka.ai.compat import getModel
from misaka.extensions.misaka_lcm.host import llm


def _registry(provider):
    return SimpleNamespace(
        getProvider=lambda _id: provider,
        find=lambda provider_id, model_id: getModel(provider_id, model_id),
    )


def test_a_catalog_model_streams_through_its_provider(monkeypatch):
    calls = []
    provider = SimpleNamespace(streamSimple=lambda model, context, options: calls.append(("provider", model.api)))
    monkeypatch.setattr(importlib.import_module("misaka.ai.compat"), "streamSimple", lambda *a: calls.append(("api", a[0].api)))
    model = getModel("zai", "glm-4.7")

    llm._provider_stream(_registry(provider), model, {"messages": []}, None)

    assert calls == [("provider", "openai-completions")]


def test_an_api_mode_override_goes_by_the_api_it_names(monkeypatch):
    calls = []
    provider = SimpleNamespace(streamSimple=lambda model, context, options: calls.append(("provider", model.api)))
    monkeypatch.setattr(importlib.import_module("misaka.ai.compat"), "streamSimple", lambda *a: calls.append(("api", a[0].api)))
    model = getModel("zai", "glm-4.7").model_copy(update={"api": "anthropic-messages"})

    llm._provider_stream(_registry(provider), model, {"messages": []}, None)

    assert calls == [("api", "anthropic-messages")]


# --- pi 1.3.0: kimi-coding moved to openai-completions -----------------------------------------

_DONE_CHUNKS = [
    {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
]


async def _kimi_aux_payload(monkeypatch, model_id, reasoning):
    """The payload an LCM auxiliary call sends to a Kimi Code model: pi's openai-completions request with the Kimi
    profile's projection merged on top, as ``_complete``'s ``on_payload`` does."""
    import time

    from misaka.ai.compat import streamSimple
    from misaka.ai.types import Context, UserMessage
    from misaka.extensions.misaka_lcm.native import auxiliary_options
    from tests.openai_fake import installFakeOpenAI

    installFakeOpenAI(monkeypatch, _DONE_CHUNKS)
    model = getModel("kimi-coding", model_id)
    level = (reasoning.get("effort") or "medium") if reasoning.get("enabled") is not False else None
    projection = auxiliary_options._project_provider_profile(
        model.provider, model.provider, model.id, model.baseUrl, reasoning
    )
    captured = []

    def on_payload(payload, wire_model):
        sent_by_pi = llm._KIMI_REASONING_CONTROLS & payload.keys()
        payload.update({key: value for key, value in projection.top_level.items() if key != "extra_headers"})
        payload.update(auxiliary_options._merge_aux_extra_body({}, projection, reasoning, wire_model.provider))
        llm._one_kimi_reasoning_control(payload, wire_model, sent_by_pi)
        captured.append((sent_by_pi, dict(payload)))
        return payload

    await streamSimple(
        model,
        Context(messages=[UserMessage(content="Hi", timestamp=time.time_ns() // 1_000_000)]),
        {"apiKey": "test", "reasoning": level, "onPayload": on_payload},
    ).result()
    return captured[0]


@pytest.mark.parametrize("model_id", ["k3", "kimi-for-coding", "kimi-for-coding-highspeed"])
@pytest.mark.parametrize(
    "reasoning",
    [{"enabled": True}, {"enabled": True, "effort": "high"}, {"enabled": True, "effort": "minimal"}, {"enabled": False}],
)
async def test_kimi_aux_calls_send_thinking_or_reasoning_effort_never_both(monkeypatch, model_id, reasoning):
    sent_by_pi, payload = await _kimi_aux_payload(monkeypatch, model_id, reasoning)

    assert not {"thinking", "reasoning_effort"} <= payload.keys()
    # pi's catalog decides which of the two a model takes; the Kimi profile's projection does not override it.
    assert sent_by_pi <= payload.keys()


@pytest.mark.parametrize("model_id", ["k3", "kimi-for-coding", "kimi-for-coding-highspeed"])
async def test_disabled_reasoning_sends_no_thinking_toggle_to_a_model_that_always_thinks(monkeypatch, model_id):
    # pi's catalog: no Kimi Code model can turn thinking off (`thinkingLevelMap.off` is null), and pi sends none of
    # them `thinking: disabled`; the Kimi profile's projection of disabled reasoning must not either.
    _sent_by_pi, payload = await _kimi_aux_payload(monkeypatch, model_id, {"enabled": False})

    assert "thinking" not in payload and "reasoning_effort" not in payload


# --- the whole auxiliary call (``_complete``) on Kimi Code ------------------------------------------


async def _kimi_aux_call(monkeypatch, model_id, *, reasoning, timeout=30.0, delay=0.0, chunks=None):
    """One LCM auxiliary call to Kimi Code through ``_complete``, answered by a fake OpenAI SDK whose stream waits
    ``delay`` seconds before each chunk. Returns the request pi sent."""
    import asyncio
    import time

    import httpx

    from misaka.ai.api import _openai_sdk
    from tests.openai_fake import installFakeOpenAI

    fake = installFakeOpenAI(monkeypatch)
    answer = chunks or [
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    ]

    async def createWithResponse(_client, _resource, params, _requestOptions, _requestHeaders):
        fake.requests.append(params)

        async def data():
            for chunk in answer:
                await asyncio.sleep(delay)
                yield chunk

        return _openai_sdk._WithResponse(data(), httpx.Response(200))

    monkeypatch.setattr(_openai_sdk, "_create_with_response", createWithResponse)
    await llm._complete(
        [{"role": "user", "content": "Summarize."}], model_id, "kimi-coding", 0.3, None, timeout,
        task="", main_runtime={}, base_url=None, api_key="sk-kimi-test", api_mode=None, route_info=None,
        latency_info=None, started=time.monotonic(), reasoning_config=reasoning,
    )
    return fake.requests[0]


@pytest.mark.parametrize("model_id", ["k3", "k3-256k", "kimi-for-coding-highspeed"])
async def test_kimi_code_aux_calls_leave_temperature_to_the_server(monkeypatch, model_id):
    # Hermes' Kimi profile omits temperature (server-managed); its name rule `kimi-*` missed the bare `k3`, and since
    # pi 1.3.0 openai-completions sends whatever LCM asks for (vendor extraction asks for 0.3).
    request = await _kimi_aux_call(monkeypatch, model_id, reasoning={"enabled": False})

    assert "temperature" not in request
    assert "thinking" not in request


async def test_a_kimi_code_aux_call_keeps_streaming_past_its_timeout_while_tokens_arrive(monkeypatch):
    # Hermes streams Kimi Code auxiliary calls (its Anthropic client): the timeout is an idle window re-armed by each
    # delta, under a generous total ceiling, not one flat budget for the whole answer.
    drip = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(6)] + [
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 6}},
    ]

    request = await _kimi_aux_call(monkeypatch, "k3", reasoning={"enabled": True}, timeout=0.5, delay=0.15, chunks=drip)

    assert request["model"] == "k3"
