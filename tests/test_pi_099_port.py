"""The pi 0.87.1 -> 0.99.1 port (2026-09-30). Each test is an upstream test case from
`packages/ai/test/` or `packages/coding-agent/test/`, named after the file it comes from."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from misaka.ai.api import _openai_sdk, openai_completions, openai_responses
from misaka.ai.api import anthropic_messages as anthropic
from misaka.ai.api import mistral_conversations as mistral
from misaka.ai.api.constrained_sampling import createGrammarToolInputProperties
from misaka.ai.api.openai_responses_shared import processResponsesStream
from misaka.ai.compat import completeSimple, getModel, stream
from misaka.ai.models import CreateModelsOptions
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    ProviderStreamOptions,
    SimpleStreamOptions,
    TextContent,
    Tool,
    ToolCall,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import getDeclaredTools, normalizeContext
from tests.anthropic_sse import patchRawResponse

CONTEXT = Context(messages=[UserMessage(content="hi", timestamp=0)])


def _model(api: str, **overrides) -> Model:
    return Model.model_validate(
        {
            "id": "fixture",
            "name": "fixture",
            "api": api,
            "provider": "fixture",
            "baseUrl": "http://127.0.0.1:9",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 100_000,
            "maxTokens": 8192,
            **overrides,
        }
    )


def _output(model: Model) -> AssistantMessage:
    return AssistantMessage.model_validate(
        {
            "content": [],
            "api": model.api,
            "provider": model.provider,
            "model": model.id,
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "stop",
            "timestamp": 0,
        }
    )


class _PayloadCaptured(Exception):
    pass


async def _capture_payload(model: Model, **options) -> dict:
    captured: dict = {}

    def on_payload(payload, _model):
        captured.update(payload)
        raise _PayloadCaptured

    await stream(model, CONTEXT, ProviderStreamOptions(apiKey="fake-key", onPayload=on_payload, **options)).result()
    return captured


# --- sampling-options.test.ts ------------------------------------------------------------


# Model defaults must apply to direct stream()/complete() calls, not only streamSimple() (#9506)
@pytest.mark.parametrize(
    ("api", "base_url"),
    [
        ("openai-completions", "http://127.0.0.1:9"),
        ("openai-responses", "http://127.0.0.1:9"),
        ("azure-openai-responses", "https://fixture.openai.azure.com/openai/v1"),
    ],
)
async def test_model_sampling_params_apply_with_request_keys_taking_precedence(api, base_url):
    model = _model(api, baseUrl=base_url, samplingParams={"top_p": 0.95, "min_p": 0.05})

    payload = await _capture_payload(model, samplingParams={"top_p": 0.5})

    assert payload["top_p"] == 0.5
    assert payload["min_p"] == 0.05


# --- mistral-reasoning-mode.test.ts --------------------------------------------------------

NONE_HIGH_LEVELS = {
    "off": "none",
    "minimal": None,
    "low": None,
    "medium": None,
    "high": "high",
    "xhigh": None,
    "max": None,
}
GLM_5_2_LEVELS = {**NONE_HIGH_LEVELS, "max": "max"}
GLM_5_3_LEVELS = {**NONE_HIGH_LEVELS, "off": None, "low": "low", "max": "max"}


def _mistral_payload(monkeypatch, model_id: str, reasoning: bool, levels=None, level=None) -> dict:
    model = _model(
        "mistral-conversations",
        id=model_id,
        provider="mistral",
        reasoning=reasoning,
        **({"thinkingLevelMap": levels} if levels else {}),
    )
    captured: dict = {}

    def capture(_model, context, options):
        captured["payload"] = mistral._buildChatPayload(_model, context, [], options)
        return AssistantMessageEventStream()

    monkeypatch.setattr(mistral, "stream", capture)
    mistral.streamSimple(model, normalizeContext(CONTEXT), SimpleStreamOptions(apiKey="fake", reasoning=level))
    return captured["payload"]


def test_mistral_uses_prompt_mode_for_reasoning_models_without_a_thinking_level_map(monkeypatch):
    payload = _mistral_payload(monkeypatch, "magistral-medium-latest", True, level="medium")

    assert payload["promptMode"] == "reasoning"
    assert "reasoningEffort" not in payload


def test_mistral_omits_reasoning_controls_for_magistral_when_thinking_is_off(monkeypatch):
    payload = _mistral_payload(monkeypatch, "magistral-medium-latest", True)

    assert "reasoningEffort" not in payload
    assert "promptMode" not in payload


# Regression for #8700 and #9375: Medium and GLM-5.2 ignore Magistral's prompt_mode.
@pytest.mark.parametrize("model_id", ["mistral-small-2603", "mistral-medium-latest", "zai-glm-5-2"])
def test_mistral_effort_map_models(monkeypatch, model_id):
    levels = GLM_5_2_LEVELS if model_id == "zai-glm-5-2" else NONE_HIGH_LEVELS

    enabled = _mistral_payload(monkeypatch, model_id, True, levels, "high")
    assert enabled["reasoningEffort"] == "high"
    assert "promptMode" not in enabled
    # Clamps unsupported levels to a supported effort.
    assert _mistral_payload(monkeypatch, model_id, True, levels, "low")["reasoningEffort"] == "high"
    # Sends reasoning_effort none when thinking is off.
    assert _mistral_payload(monkeypatch, model_id, True, levels)["reasoningEffort"] == "none"


# Regression for #9678: requested levels must reach Mistral-hosted GLM models.
def test_mistral_sends_max_for_glm_5_2(monkeypatch):
    assert _mistral_payload(monkeypatch, "zai-glm-5-2", True, GLM_5_2_LEVELS, "max")["reasoningEffort"] == "max"


@pytest.mark.parametrize(("level", "expected"), [("low", "low"), ("high", "high"), ("max", "max"), ("medium", "high")])
def test_mistral_glm_5_3_levels(monkeypatch, level, expected):
    assert _mistral_payload(monkeypatch, "zai-glm-5-3", True, GLM_5_3_LEVELS, level)["reasoningEffort"] == expected


# Regression for #8700: reasoning controls must respect the model's reasoning capability.
def test_mistral_omits_reasoning_controls_for_non_reasoning_models(monkeypatch):
    payload = _mistral_payload(monkeypatch, "mistral-medium-2604", False, level="medium")

    assert "reasoningEffort" not in payload
    assert "promptMode" not in payload


async def test_mistral_skips_empty_content_deltas():
    # GLM models on Mistral send empty content deltas around thinking and tool calls.
    model = _model("mistral-conversations", provider="mistral")

    async def events():
        for content in [[{"type": "thinking", "thinking": [{"text": "a"}]}], "", [{"type": "text", "text": ""}],
                        [{"type": "thinking", "thinking": [{"text": "b"}]}]]:
            yield {"data": {"choices": [{"delta": {"content": content}}]}}
        yield {"data": {"choices": [{"delta": {}, "finish_reason": "stop"}]}}

    output = _output(model)
    await mistral._consumeChatStream(model, output, AssistantMessageEventStream(), events(), None)

    assert [block.type for block in output.content] == ["thinking"]
    assert output.content[0].thinking == "ab"


# --- openai-responses-terminal-event.test.ts ----------------------------------------------


def _responses_model() -> Model:
    return _model("openai-responses", provider="openai", baseUrl="https://api.openai.com/v1")


async def _events(events):
    for event in events:
        yield event


def _sdk_reply(events):
    """``_openai_sdk._create_with_response`` answering with ``events`` on a 200 response."""
    return AsyncMock(return_value=_openai_sdk._WithResponse(_events(events), httpx.Response(200)))


async def test_responses_rejects_completed_streams_whose_tool_call_never_received_output_item_done():
    model = _responses_model()
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "bash", "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": "fc_1",
            "delta": '{"command":"rm -rf /tmp/build',
        },
        {"type": "response.completed", "response": {"id": "resp_unfinished", "status": "completed"}},
    ]

    with pytest.raises(RuntimeError, match=r"unfinished tool call: bash \(call_1\|fc_1\)"):
        await processResponsesStream(_events(events), _output(model), AssistantMessageEventStream(), model)


# https://github.com/earendil-works/pi/issues/9974
async def test_responses_rejects_parallel_tool_calls_without_output_index():
    # llama.cpp omits output_index from every event and sends both done events after all deltas.
    model = _responses_model()

    def call(n):
        return {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": "bash"}

    events = [
        {"type": "response.output_item.added", "item": {**call("a"), "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_a", "delta": '{"command":"echo a"}'},
        {"type": "response.output_item.added", "item": {**call("b"), "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_b", "delta": '{"command":"echo b"}'},
        {"type": "response.output_item.done", "item": {**call("a"), "arguments": '{"command":"echo a"}'}},
        {"type": "response.output_item.done", "item": {**call("b"), "arguments": '{"command":"echo b"}'}},
        {"type": "response.completed", "response": {"id": "resp_no_output_index", "status": "completed"}},
    ]

    with pytest.raises(RuntimeError, match=r"unfinished tool call: bash \(call_a\|fc_a\)"):
        await processResponsesStream(_events(events), _output(model), AssistantMessageEventStream(), model)


async def test_responses_forwards_parsed_provider_stream_events_in_order(monkeypatch):
    model = _responses_model()
    events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
        {"type": "response.reasoning_text.delta", "output_index": 0, "delta": "thinking"},
    ]
    monkeypatch.setattr(_openai_sdk, "_create_with_response", _sdk_reply(events))
    provider_events, event_models = [], []

    async def on_provider_stream_event(event, event_model):
        provider_events.append(event)
        event_models.append(event_model)

    await openai_responses.stream(
        model, normalizeContext(CONTEXT), {"apiKey": "test", "onProviderStreamEvent": on_provider_stream_event}
    ).result()

    assert [event["type"] for event in provider_events] == [
        "response.created",
        "response.output_item.added",
        "response.reasoning_text.delta",
    ]
    assert event_models == [model, model, model]


@pytest.mark.parametrize(
    ("status", "details", "stop_reason", "raw", "error"),
    [
        ("incomplete", {"reason": "max_output_tokens"}, "length", "incomplete.max_output_tokens", None),
        ("incomplete", {"reason": "content_filter"}, "error", "incomplete.content_filter", "Response incomplete: content_filter"),
        ("incomplete", None, "error", "incomplete", "Response incomplete without a provider reason"),
    ],
)
async def test_responses_incomplete_keeps_the_provider_reason(status, details, stop_reason, raw, error):
    model = _responses_model()
    events = [{"type": "response.incomplete", "response": {"status": status, "incomplete_details": details}}]
    output = _output(model)

    await processResponsesStream(_events(events), output, AssistantMessageEventStream(), model)

    assert (output.stopReason, output.rawStopReason, output.errorMessage) == (stop_reason, raw, error)


async def test_responses_usage_counts_cache_writes_and_reasoning():
    model = _responses_model()
    usage = {
        "input_tokens": 100,
        "output_tokens": 30,
        "total_tokens": 130,
        "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
        "output_tokens_details": {"reasoning_tokens": 12},
    }
    output = _output(model)

    await processResponsesStream(
        _events([{"type": "response.completed", "response": {"status": "completed", "usage": usage}}]),
        output,
        AssistantMessageEventStream(),
        model,
    )

    assert (output.usage.input, output.usage.cacheRead, output.usage.cacheWrite) == (70, 20, 10)
    assert (output.usage.output, output.usage.reasoning, output.usage.totalTokens) == (30, 12, 130)


# --- openai-responses-chatgpt-sign-in.test.ts ---------------------------------------------


def _sign_in_params(api_key: str, model: Model | None = None) -> dict:
    options = {"apiKey": api_key, "maxTokens": 1000, "temperature": 0.5, "cacheRetention": "long"}
    model = model or _responses_model()
    context = normalizeContext(CONTEXT)
    compat = openai_responses._getCompat(model)
    return openai_responses._buildParams(
        model,
        context,
        options,
        compat,
        createGrammarToolInputProperties(getDeclaredTools(context.messages), compat["supportsOpenAIGrammarTools"]),
        openai_responses._getConfigurationUpdateEffort(model, options, compat),
    )


def test_chatgpt_sign_in_omits_request_fields_that_token_sharing_rejects():
    payload = _sign_in_params("chatgpt-access-token")

    assert "max_output_tokens" not in payload
    assert "temperature" not in payload
    assert payload["prompt_cache_retention"] is None


def test_chatgpt_sign_in_omits_prompt_cache_options_on_explicit_cache_models():
    model = Model.model_validate(
        {**_responses_model().model_dump(), "compat": {"supportsExplicitPromptCacheMode": True}}
    )

    assert _sign_in_params("chatgpt-access-token", model)["prompt_cache_options"] is None
    assert _sign_in_params("sk-proj-test", model)["prompt_cache_options"] == {"ttl": "30m"}


@pytest.mark.parametrize(
    ("api_key", "base_url"),
    [("sk-proj-test", "https://api.openai.com/v1"), ("gateway-key", "https://gateway.example.com/v1")],
)
def test_chatgpt_sign_in_keeps_those_fields_for_api_keys_and_other_endpoints(api_key, base_url):
    model = _responses_model().model_copy(update={"baseUrl": base_url})
    payload = _sign_in_params(api_key, model)

    assert payload["max_output_tokens"] == 1000
    assert payload["temperature"] == 0.5
    assert payload["prompt_cache_retention"] == "24h"


# --- openai-responses-usage-limit.test.ts -------------------------------------------------

USAGE_LIMIT_ERROR = {"code": "subscription_sharing_usage_limit_exceeded", "message": "Usage limit reached."}


async def _error_message(monkeypatch, create) -> str | None:
    monkeypatch.setattr(_openai_sdk, "_create_with_response", create)
    result = await openai_responses.stream(
        _responses_model(), normalizeContext(CONTEXT), {"apiKey": "test"}
    ).result()
    assert result.stopReason == "error"
    return result.errorMessage


async def test_usage_limit_links_to_chatgpt_usage_when_the_request_is_rejected(monkeypatch):
    body = {"error": {**USAGE_LIMIT_ERROR, "type": "rate_limit_error"}}
    response = httpx.Response(429, json=body, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    rejected = openai.RateLimitError(f"Error code: 429 - {body}", response=response, body=body)

    error_message = await _error_message(monkeypatch, AsyncMock(side_effect=rejected))

    assert "subscription_sharing_usage_limit_exceeded" in error_message
    assert "Check your ChatGPT usage: https://chatgpt.com/settings/usage" in error_message


async def test_usage_limit_links_to_chatgpt_usage_when_the_stream_fails(monkeypatch):
    event = {"type": "response.failed", "response": {"id": "resp_failed", "status": "failed", "error": USAGE_LIMIT_ERROR}}

    error_message = await _error_message(monkeypatch, _sdk_reply([event]))

    assert "subscription_sharing_usage_limit_exceeded: Usage limit reached." in error_message
    assert "Check your ChatGPT usage: https://chatgpt.com/settings/usage" in error_message


# --- openai-completions-provider-stream-event.test.ts -------------------------------------


# Regression test for #9784.
async def test_completions_exposes_provider_chunks_including_openrouter_metadata(monkeypatch):
    first_chunk = {
        "id": "chatcmpl-1",
        "model": "anthropic/claude-sonnet-4.6",
        "choices": [{"index": 0, "delta": {"content": "hello"}}],
    }
    final_chunk = {
        "id": "chatcmpl-1",
        "model": "anthropic/claude-sonnet-4.6",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": 0.0012, "is_byok": False},
        "openrouter_metadata": {"strategy": "direct", "region": "iad"},
    }
    monkeypatch.setattr(_openai_sdk, "_create_with_response", _sdk_reply([first_chunk, final_chunk]))
    events: list = []
    model = _model(
        "openai-completions", id="openrouter/auto", provider="openrouter", baseUrl="https://openrouter.ai/api/v1"
    )

    message = await completeSimple(model, CONTEXT, SimpleStreamOptions(apiKey="test", onProviderStreamEvent=lambda data, _m: events.append(data)))

    assert [block.model_dump(exclude_none=True) for block in message.content] == [{"type": "text", "text": "hello"}]
    assert events == [first_chunk, final_chunk]


async def test_anthropic_forwards_provider_stream_events(monkeypatch):
    model = getModel("anthropic", "claude-haiku-4-5")
    raw = [
        {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    patchRawResponse(monkeypatch, _events(raw))
    seen: list = []

    await anthropic.stream(
        model,
        normalizeContext(CONTEXT),
        {"client": object(), "onProviderStreamEvent": lambda event, _m: seen.append(json.loads(json.dumps(event)))},
    ).result()

    assert [event["type"] for event in seen] == ["message_start", "message_delta", "message_stop"]


# === B2: coding-agent kernel ===============================================================

# --- session-manager/file-operations.test.ts ----------------------------------------------


def _roles(path) -> list[str]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row["message"]["role"] if row["type"] == "message" else row["type"] for row in rows]


def test_session_file_is_not_created_for_setup_entries_only(tmp_path):
    from misaka.core.session_manager import SessionManager

    session = SessionManager.create(str(tmp_path), str(tmp_path))
    session.appendModelChange("anthropic", "claude-sonnet-4-5")
    session.appendThinkingLevelChange("off")

    assert not os.path.exists(session.getSessionFile())


# #10000: the first prompt must survive a first turn that never produces an assistant message
def test_session_file_is_created_when_the_first_user_message_is_appended(tmp_path):
    from misaka.core.session_manager import SessionManager

    session = SessionManager.create(str(tmp_path), str(tmp_path))
    session.appendModelChange("anthropic", "claude-sonnet-4-5")
    session.appendMessage(UserMessage(content="first question", timestamp=0))

    file = session.getSessionFile()
    assert _roles(file) == ["session", "model_change", "user"]
    assert len(SessionManager.open(file, str(tmp_path)).buildSessionContext().messages) == 1


def test_session_file_appends_later_entries_without_rewriting(tmp_path):
    from misaka.core.session_manager import SessionManager

    session = SessionManager.create(str(tmp_path), str(tmp_path))
    session.appendMessage(UserMessage(content="first question", timestamp=0))
    session.appendCustomEntry("preset-state", {"name": "plan"})
    session.appendMessage(_output(_model("openai-completions")).model_copy(update={"content": []}))

    assert _roles(session.getSessionFile()) == ["session", "user", "custom", "assistant"]
    assert session.getEntryCount() == 3


# --- default-tools-setting.test.ts / settings-manager.test.ts ------------------------------


def _settings(global_settings: dict, project_settings: dict | None = None):
    from misaka.core.settings_manager import InMemorySettingsStorage, SettingsManager

    storage = InMemorySettingsStorage()
    storage.global_value = json.dumps(global_settings)
    if project_settings is not None:
        storage.project_value = json.dumps(project_settings)
    return SettingsManager.fromStorage(storage, {"projectTrusted": True})


@pytest.mark.parametrize(
    ("global_tools", "project_tools", "expected"),
    [
        (None, None, None),
        (["+todo", "-write"], None, ["read", "bash", "edit", "office", "grep", "find", "ls", "todo"]),
        (["read", "bash"], ["+edit"], ["read", "bash", "edit"]),
        (["read", "bash"], ["grep"], ["grep"]),
        (["read"], ["-read", "+ls"], ["ls"]),
        ([], None, []),
    ],
)
def test_default_tools_modifiers_merge_across_layers(global_tools, project_tools, expected):
    settings = _settings(
        {} if global_tools is None else {"defaultTools": global_tools},
        None if project_tools is None else {"defaultTools": project_tools},
    )

    assert settings.getDefaultTools() == expected


# --- llama-extension.test.ts --------------------------------------------------------------


async def test_llama_preserves_cached_context_for_unloaded_autoload_presets(monkeypatch):
    from misaka.ai.auth.types import ApiKeyCredential
    from misaka.ai.models import RefreshModelsContext
    from misaka.extensions.llama import client as llama_client
    from misaka.extensions.llama.provider import createLlamaProvider

    state = {"loaded": True, "args": None}

    async def request(self, path, init=None):
        if path == "/models":
            if state["loaded"]:
                model = {"id": "qwen", "status": {"value": "loaded"}, "source": "preset",
                         "meta": {"n_ctx": 65536, "n_ctx_train": 128000}}
            else:
                status = {"value": "unloaded", **({"args": state["args"]} if state["args"] else {})}
                model = {"id": "qwen", "status": status, "source": "preset", "meta": {"n_ctx_train": 128000}}
            return {"data": [model]}
        if path == "/props?model=qwen&autoload=false":
            return {}
        if path == "/props":
            return {"role": "router", "models_autoload": True}
        raise AssertionError(path)

    monkeypatch.setattr(llama_client.LlamaClient, "request", request)
    stored: dict = {}

    async def publish(publication):
        if publication.persist_is_set:
            stored["entry"] = publication.persist
        if publication.update:
            publication.update()
        return True

    credential = ApiKeyCredential(key="local", env={"LLAMA_BASE_URL": "http://127.0.0.1:9"})

    def context():
        return RefreshModelsContext(
            credential=credential, stored=stored.get("entry"), publish=publish, allowNetwork=True, signal=None
        )

    def windows():
        return [model.contextWindow for model in stored["entry"].models]

    first = createLlamaProvider()
    await first.provider.refreshModels(context())
    assert windows() == [65536, 65536]

    state["loaded"] = False
    second = createLlamaProvider()
    await second.provider.refreshModels(context())
    assert [(m.id, m.contextWindow) for m in second.provider.getModels()] == [("qwen", 65536)]
    assert windows() == [65536, 65536]

    state["args"] = ["llama-server", "--ctx-size", "32768"]
    await second.provider.refreshModels(context())
    assert windows() == [32768, 32768]
    assert [(m.id, m.api) for m in stored["entry"].models] == [("qwen", "openai-completions"), ("qwen", "llama-cpp-classify")]


async def test_llama_detects_reasoning_from_the_chat_template(monkeypatch):
    from misaka.ai.auth.types import ApiKeyCredential
    from misaka.ai.models import RefreshModelsContext
    from misaka.extensions.llama import client as llama_client
    from misaka.extensions.llama.provider import createLlamaProvider

    async def request(self, path, init=None):
        if path == "/models":
            return {"data": [{"id": "qwen", "status": {"value": "loaded"}, "meta": {"n_ctx": 4096}}]}
        if path == "/props?model=qwen&autoload=false":
            return {"chat_template": "{% if enable_thinking %}...{% endif %}"}
        raise AssertionError(path)

    monkeypatch.setattr(llama_client.LlamaClient, "request", request)

    async def publish(publication):
        if publication.update:
            publication.update()
        return True

    controller = createLlamaProvider()
    await controller.provider.refreshModels(RefreshModelsContext(
        credential=ApiKeyCredential(key="local", env={"LLAMA_BASE_URL": "http://127.0.0.1:9"}),
        stored=None, publish=publish, allowNetwork=True, signal=None,
    ))

    [model] = controller.provider.getModels()
    assert model.reasoning and model.thinkingLevelMap["medium"] == "medium"
    assert model.compat.thinkingFormat == "qwen-chat-template"


# --- utils/shell.ts sanitizeBinaryOutput --------------------------------------------------


def test_sanitize_binary_output_keeps_tabs_newlines_and_astral_characters():
    from misaka.utils.shell import sanitizeBinaryOutput

    assert sanitizeBinaryOutput("a\x00b\tc\nd\re\x1b[0m￺\U0001f600\x7f") == "ab\tc\nd\re[0m\U0001f600\x7f"


# --- agent-session.ts prompt dispositions -------------------------------------------------


def test_prompt_disposition_types_match_upstream():
    import typing

    from misaka.core.agent_session import PromptDisposition, QueuedInputDisposition

    assert set(typing.get_args(QueuedInputDisposition)) == {"handled", "queued"}
    assert set(typing.get_args(PromptDisposition)) == {"handled", "queued", "started"}


# === B1b: ai/ layer aligned file by file with 0.99.1 =========================================

_ANTHROPIC_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","model":"MODEL",'
    '"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hi"}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":1}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


@pytest.fixture
def anthropic_wire(monkeypatch):
    """Real SDK client, fake HTTP: every request the adapter sends is recorded."""
    requests: list[httpx.Request] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if responses:
            return responses.pop(0)
        body = _ANTHROPIC_SSE.replace("MODEL", json.loads(request.content)["model"])
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original = anthropic._createClient

    def create_client(*args, **kwargs):
        created = original(*args, **kwargs)
        # A federation clone already received its pool through `DefaultAsyncHttpxClient` below.
        if created.client.credentials is None:
            created.client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=created.client.base_url
            )
        return created

    monkeypatch.setattr(anthropic, "_createClient", create_client)
    monkeypatch.setattr(
        anthropic._anthropic_sdk,
        "DefaultAsyncHttpxClient",
        lambda **_kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return requests, responses


async def test_anthropic_fallback_models_go_through_the_beta_messages_api(anthropic_wire):
    requests, _ = anthropic_wire
    model = getModel("anthropic", "claude-fable-5")

    result = await anthropic.stream(model, normalizeContext(CONTEXT), {"apiKey": "sk-ant-test"}).result()

    assert result.stopReason == "stop", result.errorMessage
    [request] = requests
    body = json.loads(request.content)
    assert [fallback["model"] for fallback in body["fallbacks"]] == [
        fallback.model for fallback in model.compat.allowedFallbackModels
    ]
    assert anthropic._SERVER_SIDE_FALLBACK_BETA in request.headers["anthropic-beta"]
    assert request.headers["x-api-key"] == "sk-ant-test"


async def test_anthropic_sdk_does_not_retry_on_its_own(anthropic_wire):
    requests, responses = anthropic_wire
    responses.append(httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}))

    result = await anthropic.stream(
        getModel("anthropic", "claude-haiku-4-5"), normalizeContext(CONTEXT), {"apiKey": "sk-ant-test"}
    ).result()

    assert result.stopReason == "error"
    assert len(requests) == 1


async def test_anthropic_header_owned_auth_honours_null_headers(anthropic_wire, monkeypatch):
    requests, _ = anthropic_wire
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient")
    headers = {"cf-aig-authorization": "Bearer gateway", "Authorization": None, "x-api-key": None}

    result = await anthropic.stream(
        getModel("anthropic", "claude-haiku-4-5"), normalizeContext(CONTEXT), {"headers": headers}
    ).result()

    assert result.stopReason == "stop", result.errorMessage
    [request] = requests
    assert request.headers["cf-aig-authorization"] == "Bearer gateway"
    assert "x-api-key" not in request.headers and "authorization" not in request.headers


def test_anthropic_refuses_a_request_without_any_auth(monkeypatch):
    _clear_ambient_anthropic_env(monkeypatch)
    with pytest.raises(RuntimeError, match="No API key for provider: anthropic"):
        anthropic.streamSimple(getModel("anthropic", "claude-haiku-4-5"), normalizeContext(CONTEXT))


def test_anthropic_configured_beta_header_replaces_the_computed_betas():
    model = getModel("anthropic", "claude-fable-5")

    configured = anthropic._buildParams(model, normalizeContext(CONTEXT), False, {"headers": {"anthropic-beta": "a, b,a"}}).params
    suppressed = anthropic._buildParams(model, normalizeContext(CONTEXT), False, {"headers": {"Anthropic-Beta": None}}).params

    assert configured["betas"] == ["a", "b"]
    assert "betas" not in suppressed


def test_anthropic_tool_call_ids_keep_only_ascii_word_characters():
    assert anthropic._normalizeToolCallId("call|é1-x") == "call__1-x"


async def test_stream_facade_injects_the_environment_api_key(anthropic_wire, monkeypatch):
    requests, _ = anthropic_wire
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")

    result = await completeSimple(getModel("anthropic", "claude-haiku-4-5"), CONTEXT)

    assert result.stopReason == "stop", result.errorMessage
    assert requests[0].headers["x-api-key"] == "sk-ant-from-env"


def test_env_api_key_reads_scoped_env_first(monkeypatch):
    from misaka.ai.env_api_keys import findEnvKeys, getEnvApiKey

    monkeypatch.setenv("NVIDIA_API_KEY", "ambient")
    assert getEnvApiKey("nvidia") == "ambient"
    assert getEnvApiKey("nvidia", {"NVIDIA_API_KEY": "scoped"}) == "scoped"
    assert findEnvKeys("typesafe", {"TYPESAFE_API_KEY": "k"}) == ["TYPESAFE_API_KEY"]
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_TOKEN", raising=False)
    assert getEnvApiKey("anthropic") is None


# --- B1b-2: OpenAI completions / responses / azure --------------------------------------------


def _openai_wire(monkeypatch, sse_body: str):
    import httpx2  # the OpenAI SDK's own HTTP client

    requests: list = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, text=sse_body, headers={"content-type": "text/event-stream"})

    original = _openai_sdk._sdk_client

    def sdk_client(**kwargs):
        client, request_headers = original(**kwargs)
        client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), base_url=str(client.base_url))
        return client, request_headers

    monkeypatch.setattr(_openai_sdk, "_sdk_client", sdk_client)
    return requests


_COMPLETIONS_SSE = (
    'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"m","choices":[{"index":0,'
    '"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"m","choices":[{"index":0,'
    '"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


async def test_completions_sends_the_request_body_with_upstream_top_level_fields(monkeypatch):
    requests = _openai_wire(monkeypatch, _COMPLETIONS_SSE)
    model = _model(
        "openai-completions",
        reasoning=True,
        compat={"thinkingFormat": "qwen-chat-template"},
        samplingParams={"top_k": 20},
    )

    result = await openai_completions.stream(
        model,
        normalizeContext(CONTEXT),
        {"apiKey": "k", "reasoningEffort": "high", "headers": {"Authorization": None, "x-extra": "1"}},
    ).result()

    assert result.stopReason == "stop", result.errorMessage
    assert result.rawStopReason == "stop"
    [request] = requests
    body = json.loads(request.content)
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "preserve_thinking": True}
    assert body["top_k"] == 20
    assert None not in body.values()
    assert "authorization" not in request.headers and request.headers["x-extra"] == "1"


async def test_completions_header_owned_auth_uses_the_unused_key(monkeypatch):
    requests = _openai_wire(monkeypatch, _COMPLETIONS_SSE)

    result = await openai_completions.stream(
        _model("openai-completions"), normalizeContext(CONTEXT), {"headers": {"cf-aig-authorization": "Bearer gw"}}
    ).result()

    assert result.stopReason == "stop", result.errorMessage
    assert requests[0].headers["cf-aig-authorization"] == "Bearer gw"
    with pytest.raises(RuntimeError, match="No API key for provider: fixture"):
        openai_completions.streamSimple(_model("openai-completions"), normalizeContext(CONTEXT))


def test_completions_usage_reads_kimi_top_level_cached_tokens_and_reasoning():
    usage = openai_completions._parseChunkUsage(
        {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 30,
         "completion_tokens_details": {"reasoning_tokens": 7}},
        _model("openai-completions"),
    )

    assert (usage.input, usage.cacheRead, usage.output, usage.reasoning) == (70, 30, 20, 7)


def test_completions_replays_pipe_ids_with_their_item_ids():
    from misaka.ai.types import ToolResultMessage

    model = _model("openai-completions", provider="openai")
    source = _output(_model("openai-responses", provider="openai")).model_copy(update={
        "content": [
            ToolCall(id="call_1|fc_a", name="t", arguments={}),
            ToolCall(id="call_1|fc_b", name="t", arguments={}),
            ToolCall(id="call_2|" + "x" * 60, name="t", arguments={}),
        ],
        "stopReason": "toolUse",
    })
    results = [
        ToolResultMessage(toolCallId=call.id, toolName="t", content=[], isError=False, timestamp=0)
        for call in source.content
    ]
    messages = openai_completions.convertMessages(
        model, normalizeContext(Context(messages=[UserMessage(content="q", timestamp=0), source, *results])),
        openai_completions._getCompat(model),
    )

    ids = [call["id"] for call in messages[1]["tool_calls"]]
    assert ids[:2] == ["call_1_fc_a", "call_1_fc_b"]
    assert len(ids[2]) <= 40 and ids[2].startswith("call_2_")
    assert [m["content"] for m in messages[2:]] == ["(no tool output)"] * 3


def test_completions_cache_breakpoint_lands_on_a_trailing_tool_result():
    from misaka.ai.types import ToolResultMessage

    model = _model("openai-completions", provider="openrouter", id="anthropic/claude-x", baseUrl="https://openrouter.ai/api/v1")
    call = ToolCall(id="call_1", name="t", arguments={})
    source = _output(model).model_copy(update={"content": [call], "stopReason": "toolUse"})
    context = Context(messages=[
        UserMessage(content="q", timestamp=0),
        source,
        ToolResultMessage(toolCallId="call_1", toolName="t", content=[TextContent(text="r")], isError=False, timestamp=0),
    ])

    params = openai_completions._buildParams(model, normalizeContext(context), {"cacheRetention": "short"})

    assert params["messages"][-1]["role"] == "tool"
    assert params["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_completions_detects_ant_ling():
    compat = openai_completions._detectCompat(_model("openai-completions", provider="ant-ling"))

    assert (compat["thinkingFormat"], compat["maxTokensField"], compat["supportsReasoningEffort"]) == (
        "ant-ling", "max_tokens", False,
    )


async def test_completions_stream_without_finish_reason_is_an_error(monkeypatch):
    async def chunks():
        yield {"id": "c1", "choices": [{"index": 0, "delta": {"content": "half"}}]}

    monkeypatch.setattr(
        _openai_sdk, "_create_with_response", AsyncMock(return_value=_openai_sdk._WithResponse(chunks(), httpx.Response(200)))
    )
    result = await openai_completions.stream(
        _model("openai-completions"), normalizeContext(CONTEXT), {"apiKey": "test"}
    ).result()

    assert result.stopReason == "error"
    assert result.errorMessage == "Stream ended without finish_reason"


def test_responses_fallback_message_ids_and_empty_tool_output():
    from misaka.ai.api.openai_responses_shared import convertResponsesMessages
    from misaka.ai.types import ToolResultMessage

    model = _responses_model()
    assistant = _output(model).model_copy(update={
        "content": [TextContent(text="one"), TextContent(text="two"), ToolCall(id="call_1", name="t", arguments={})],
        "stopReason": "toolUse",
    })
    context = Context(messages=[
        UserMessage(content="q", timestamp=0),
        assistant,
        ToolResultMessage(toolCallId="call_1", toolName="t", content=[], isError=False, timestamp=0),
    ])

    items = convertResponsesMessages(model, normalizeContext(context), {"openai"})

    assert [item.get("id") for item in items if item.get("type") == "message"] == ["msg_pi_1", "msg_pi_1_1"]
    function_call = next(item for item in items if item.get("type") == "function_call")
    assert "id" not in function_call
    assert items[-1]["output"] == "(no tool output)"


def test_responses_requires_a_key_or_an_auth_header():
    with pytest.raises(RuntimeError, match="No API key for provider: openai"):
        openai_responses.streamSimple(_responses_model(), normalizeContext(CONTEXT))
    assert openai_responses._getClientApiKey("openai", None, {"authorization": "Bearer x"}) == "unused"


def test_azure_reads_its_configuration_from_the_request_env():
    from misaka.ai.api import azure_openai_config as azure

    config = azure.resolveAzureConfig(
        _model("azure-openai-responses", baseUrl=""),
        {"env": {"AZURE_OPENAI_RESOURCE_NAME": "res", "AZURE_OPENAI_API_VERSION": "2025-01-01"}},
    )

    assert config == {"baseUrl": "https://res.openai.azure.com/openai/v1", "apiVersion": "2025-01-01"}
    assert azure.resolveDeploymentName(
        _model("azure-openai-responses", id="gpt"), {"env": {"AZURE_OPENAI_DEPLOYMENT_NAME_MAP": "gpt=dep"}}
    ) == "dep"


# --- B1b-3: OpenAI Codex ------------------------------------------------------------------


def _codex_token(account_id: str = "acct_1") -> str:
    import base64

    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.sig"


def _codex_model() -> Model:
    return _model(
        "openai-codex-responses", provider="openai-codex", baseUrl="https://chatgpt.com/backend-api", reasoning=True
    )


def _codex_sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


@pytest.fixture
def codex_wire(monkeypatch):
    from misaka.ai.api import openai_codex_responses as codex

    requests: list[httpx.Request] = []
    responses: list[httpx.Response] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(
        codex.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    return codex, requests, responses


async def test_codex_incomplete_response_is_an_error_with_its_reason(codex_wire):
    codex, requests, responses = codex_wire
    responses.append(httpx.Response(200, text=_codex_sse(
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.incomplete", "response": {
            "id": "resp_1", "status": "incomplete", "incomplete_details": {"reason": "content_filter"},
            "end_turn": True,
        }},
    ), headers={"content-type": "text/event-stream"}))

    result = await codex.stream(
        _codex_model(), normalizeContext(CONTEXT),
        {"apiKey": _codex_token(), "transport": "sse", "sessionId": "session-1", "toolChoice": "none"},
    ).result()

    assert result.stopReason == "error"
    assert result.errorMessage == "Response incomplete: content_filter"
    assert result.endTurn is True
    [request] = requests
    assert request.headers["session-id"] == "session-1"
    assert request.headers["chatgpt-account-id"] == "acct_1"
    assert "session_id" not in request.headers
    body = json.loads(request.content) if request.headers.get("content-encoding") != "zstd" else None
    if body is not None:
        assert body["tool_choice"] == "none" and body["prompt_cache_key"] == "session-1"


async def test_codex_does_not_retry_by_default_and_caps_retry_after(codex_wire):
    codex, requests, responses = codex_wire
    responses.append(httpx.Response(500, json={"error": {"message": "boom"}}))

    result = await codex.stream(
        _codex_model(), normalizeContext(CONTEXT), {"apiKey": _codex_token(), "transport": "sse"}
    ).result()

    assert result.stopReason == "error" and len(requests) == 1
    assert "boom" in result.errorMessage

    responses.append(httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "120"}))
    result = await codex.stream(
        _codex_model(), normalizeContext(CONTEXT),
        {"apiKey": _codex_token(), "transport": "sse", "maxRetries": 2, "maxRetryDelayMs": 1000},
    ).result()

    assert result.stopReason == "error"
    assert result.errorMessage == "Server requested 120s retry delay (max: 1s)"


async def test_codex_error_events_read_the_nested_code():
    from misaka.ai.api import openai_codex_responses as codex

    async def events():
        yield {"type": "error", "error": {"code": "previous_response_not_found", "message": "gone"}}

    with pytest.raises(codex._CodexApiError) as caught:
        async for _event in codex._mapCodexEvents(events(), _output(_codex_model()), _codex_model(), None):
            pass

    assert caught.value.code == "previous_response_not_found"
    assert str(caught.value) == "Codex error: gone"
    assert codex._isPreviousResponseNotFoundError(caught.value)


def test_codex_terminal_rate_limits_are_not_retried():
    from misaka.ai.api import openai_codex_responses as codex

    assert codex._isRetryableError(429, "rate limit") is True
    assert codex._isRetryableError(429, "insufficient_quota: add billing") is False


async def test_codex_sse_parses_a_final_frame_without_a_blank_line():
    from misaka.ai.api import openai_codex_responses as codex

    response = httpx.Response(200, text='data: {"type":"a"}\n\ndata: {"type":"b"}')

    assert [event["type"] async for event in codex._parseSSE(response)] == ["a", "b"]


def test_codex_requires_an_api_key_and_honours_null_headers():
    from misaka.ai.api import openai_codex_responses as codex

    with pytest.raises(RuntimeError, match="No API key for provider: openai-codex"):
        codex.streamSimple(_codex_model(), normalizeContext(CONTEXT))
    headers = codex._buildSSEHeaders({"X-Model": "m"}, {"x-model": None, "X-Extra": "1"}, "acct", "tok", "sid")
    assert {name.lower() for name in headers} >= {"authorization", "session-id", "x-client-request-id", "x-extra"}
    assert "x-model" not in {name.lower() for name in headers}


# --- B1b-4: Google / Vertex / google_shared -----------------------------------------------


def _gemini_model(api: str = "google-generative-ai", **overrides) -> Model:
    return _model(
        api,
        **{
            "id": "gemini-2.5-flash",
            "provider": "google" if api == "google-generative-ai" else "google-vertex",
            "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
            "reasoning": True,
            **overrides,
        },
    )


@pytest.fixture
def gemini_wire(monkeypatch):
    from misaka.ai.api import google_generative_ai as google

    requests: list[httpx.Request] = []
    chunks: list[dict] = []
    real_client = google.GoogleGenAI

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = "".join(f"data: {json.dumps(chunk)}\r\n\r\n" for chunk in chunks)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    def client(**kwargs):
        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return real_client(**{**kwargs, "http_options": {**(kwargs["http_options"] or {}), "httpx_async_client": transport}})

    monkeypatch.setattr(google, "GoogleGenAI", client)
    return google, requests, chunks


async def test_google_error_finish_reason_reports_the_raw_reason_and_upstream_usage(gemini_wire):
    google, requests, chunks = gemini_wire
    chunks += [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "hel"}]}}], "responseId": "r1"},
        {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "lo"}]}, "finishReason": "SAFETY"}],
            "usageMetadata": {
                "promptTokenCount": 100, "cachedContentTokenCount": 30, "candidatesTokenCount": 20,
                "thoughtsTokenCount": 5, "totalTokenCount": 125,
            },
        },
    ]

    result = await google.stream(
        _gemini_model(), normalizeContext(CONTEXT), {"apiKey": "k", "headers": {"X-Extra": None}}
    ).result()

    assert (result.stopReason, result.rawStopReason) == ("error", "SAFETY")
    assert result.errorMessage == "Provider stopped with: SAFETY"
    assert (result.usage.input, result.usage.output, result.usage.cacheRead, result.usage.reasoning) == (70, 25, 30, 5)
    assert result.responseId == "r1"
    [request] = requests
    assert "x-extra" not in request.headers and "toolConfig" not in json.loads(request.content)


async def test_google_stream_without_finish_reason_is_an_error(gemini_wire):
    google, _requests, chunks = gemini_wire
    chunks.append({"candidates": [{"content": {"role": "model", "parts": [{"text": "half"}]}}]})

    result = await google.stream(_gemini_model(), normalizeContext(CONTEXT), {"apiKey": "k"}).result()

    assert result.stopReason == "error"
    assert result.errorMessage == "Google stream ended without a finish reason"


async def test_google_tool_calls_turn_only_a_stop_into_tool_use(gemini_wire):
    google, _requests, chunks = gemini_wire
    call = {"functionCall": {"name": "t", "args": {"a": 1}}}
    chunks.append({"candidates": [{"content": {"role": "model", "parts": [call]}, "finishReason": "MAX_TOKENS"}]})

    result = await google.stream(_gemini_model(), normalizeContext(CONTEXT), {"apiKey": "k"}).result()

    assert result.stopReason == "length" and result.content[0].arguments == {"a": 1}


async def test_google_requires_the_api_key_in_options(monkeypatch):
    from misaka.ai.api import google_generative_ai as google

    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    result = await google.stream(_gemini_model(), normalizeContext(CONTEXT)).result()

    assert result.errorMessage == "No API key for provider: google"
    with pytest.raises(RuntimeError, match="No API key for provider: google"):
        google.streamSimple(_gemini_model(), normalizeContext(CONTEXT))


def test_google_simple_options_carry_tool_choice(monkeypatch):
    from misaka.ai.api import google_generative_ai as google

    seen: dict = {}
    monkeypatch.setattr(google, "stream", lambda _model, _context, options: seen.update(options))
    tool = Tool(name="t", description="d", parameters={"type": "object", "properties": {}})
    context = normalizeContext(Context(messages=[UserMessage(content="q", timestamp=0)], tools=[tool]))

    google.streamSimple(_gemini_model(), context, SimpleStreamOptions(apiKey="k", toolChoice="none"))

    assert seen["toolChoice"] == "none"
    params = google._buildParams(_gemini_model(), context, seen)
    assert params["config"]["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}
    assert "toolConfig" not in google._buildParams(_gemini_model(), context, {})["config"]


def test_gemini_replay_keeps_empty_blocks_that_carry_a_signature():
    from misaka.ai.api.google_shared import convertMessages
    from misaka.ai.types import ThinkingContent, ToolResultMessage

    model = _gemini_model(id="gemini-3-pro-preview")
    same = _output(model).model_copy(update={"content": [
        TextContent(text="", textSignature="c2ln"),
        ThinkingContent(thinking=" ", thinkingSignature="c2ln"),
        TextContent(text="", textSignature=None),
    ]})
    other = _output(_gemini_model(id="gemini-2.5-pro")).model_copy(update={"content": [
        ThinkingContent(thinking="", thinkingSignature="c2ln"),
        TextContent(text="kept"),
        ToolCall(id="call:é1", name="t", arguments={}),
    ], "stopReason": "toolUse"})
    context = Context(messages=[
        UserMessage(content="q", timestamp=0), same, UserMessage(content="r", timestamp=0), other,
        ToolResultMessage(toolCallId="call:é1", toolName="t", content=[], isError=False, timestamp=0),
    ])

    contents = convertMessages(model, normalizeContext(context))

    assert contents[1]["parts"] == [
        {"text": "", "thoughtSignature": "c2ln"},
        {"thought": True, "text": " ", "thoughtSignature": "c2ln"},
    ]
    assert contents[3]["parts"][0] == {"text": "kept"}
    assert contents[3]["parts"][1]["functionCall"]["id"] == "call__1"
    assert contents[4]["parts"][0]["functionResponse"]["id"] == "call__1"


def test_vertex_reads_its_setup_from_the_request_env(tmp_path):
    from misaka.ai.api import google_vertex as vertex

    key_file = tmp_path / "adc.json"
    key_file.write_text(json.dumps(
        {"type": "authorized_user", "client_id": "x", "client_secret": "y", "refresh_token": "z"}
    ))
    env = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "europe-west4",
           "GOOGLE_APPLICATION_CREDENTIALS": str(key_file)}
    options = {"env": env, "apiKey": "<authenticated>"}
    model = _gemini_model("google-vertex", id="gemini-2.5-flash-lite",
                          baseUrl="https://{location}-aiplatform.googleapis.com")

    assert vertex._resolveApiKey(options) is None
    assert vertex._resolveApiKey({"apiKey": "gcp-vertex-credentials"}) is None
    assert (vertex._resolveProject(options), vertex._resolveLocation(options)) == ("proj", "europe-west4")
    client = vertex._createClient(model, "proj", "europe-west4", None, env)
    assert client._api_client._credentials is not None
    assert client._api_client._http_options.api_version == "v1"
    assert vertex._getGoogleBudget(model, "minimal") == 128


def test_provider_headers_merge_case_insensitively_and_null_removes():
    from misaka.ai.utils.headers import providerHeadersToRecord

    assert providerHeadersToRecord(
        {"User-Agent": "a", "X-Gone": "1"}, {"user-agent": "b", "x-gone": None}, None
    ) == {"user-agent": "b"}
    assert providerHeadersToRecord({"X": None}) is None


# --- B1b-5: Mistral over raw HTTP ---------------------------------------------------------


def _mistral_model(**overrides) -> Model:
    return _model("mistral-conversations", **{"provider": "mistral", "baseUrl": "https://api.mistral.ai", **overrides})


@pytest.fixture
def mistral_wire(monkeypatch):
    requests: list[httpx.Request] = []
    responses: list[httpx.Response] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr(
        mistral.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    return requests, responses


def _mistral_sse(*chunks: dict, done: bool = True, separator: str = "\r\n\r\n") -> str:
    frames = [f"data: {json.dumps(chunk)}" for chunk in chunks] + (["data: [DONE]"] if done else [])
    return separator.join(frames)


async def test_mistral_posts_the_wire_payload_and_parses_the_event_stream(mistral_wire):
    from misaka.ai.types import ToolResultMessage

    requests, responses = mistral_wire
    call = {"index": 0, "id": "abcDEF123", "function": {"name": "t", "arguments": '{"a":'}}
    responses.append(httpx.Response(200, text=_mistral_sse(
        {"id": "r1", "choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {"tool_calls": [call]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "t", "arguments": "1}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 3}}},
        done=False, separator="\n\n",
    ), headers={"content-type": "text/event-stream"}))
    seen: list[dict] = []
    history = _output(_mistral_model()).model_copy(update={
        "content": [ToolCall(id="call_1", name="t", arguments={})], "stopReason": "toolUse",
    })
    context = Context(messages=[
        UserMessage(content="q", timestamp=0), history,
        ToolResultMessage(toolCallId="call_1", toolName="t", content=[], isError=False, timestamp=0),
    ])

    result = await mistral.stream(_mistral_model(), normalizeContext(context), {
        "apiKey": "k", "sessionId": "s1", "maxTokens": 5, "headers": {"X-Drop": None},
        "toolChoice": {"type": "function", "function": {"name": "t"}},
        "onResponse": lambda response, _model: seen.append(response),
    }).result()

    assert result.stopReason == "toolUse", result.errorMessage
    assert result.rawStopReason == "tool_calls" and result.responseId == "r1"
    assert [block.type for block in result.content] == ["text", "toolCall"]
    assert result.content[1].arguments == {"a": 1}
    assert (result.usage.input, result.usage.cacheRead, result.usage.output) == (7, 3, 4)
    [request] = requests
    assert str(request.url) == "https://api.mistral.ai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer k" and request.headers["x-affinity"] == "s1"
    assert "x-drop" not in request.headers
    body = json.loads(request.content)
    assert (body["max_tokens"], body["prompt_cache_key"]) == (5, "s1")
    assert body["tool_choice"] == {"type": "function", "function": {"name": "t"}}
    assert body["messages"][1]["tool_calls"][0]["index"] == 0 and body["messages"][1]["prefix"] is False
    assert body["messages"][2]["tool_call_id"] == body["messages"][1]["tool_calls"][0]["id"]
    assert seen[0]["status"] == 200


async def test_mistral_reports_http_errors_and_missing_finish_reasons(mistral_wire):
    _requests, responses = mistral_wire
    responses.append(httpx.Response(400, text='{"message":"bad"}'))
    result = await mistral.stream(_mistral_model(), normalizeContext(CONTEXT), {"apiKey": "k"}).result()
    assert result.errorMessage == 'Mistral API error (400): {"message":"bad"}'

    responses.append(httpx.Response(200, text=_mistral_sse({"choices": [{"delta": {"content": "half"}}]})))
    result = await mistral.stream(_mistral_model(), normalizeContext(CONTEXT), {"apiKey": "k"}).result()
    assert result.errorMessage == "Mistral stream ended without a finish reason"

    responses.append(httpx.Response(200, text='data: {"object":"x"}\n\n'))
    result = await mistral.stream(_mistral_model(), normalizeContext(CONTEXT), {"apiKey": "k"}).result()
    assert result.errorMessage == "Invalid Mistral streaming event"


async def test_mistral_timeout_bounds_only_the_response_headers(mistral_wire):
    # pi 1.2.0 (#10609): `timeoutMs` no longer cuts off a stream that outlives it; body stalls are the HTTP client's
    # idle timeout's to end.
    _requests, responses = mistral_wire

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            await asyncio.sleep(0.1)
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    responses.append(httpx.Response(200, stream=Slow()))
    result = await mistral.stream(
        _mistral_model(), normalizeContext(CONTEXT), {"apiKey": "k", "timeoutMs": 50}
    ).result()

    assert (result.stopReason, [block.text for block in result.content]) == ("stop", ["a"])


def test_mistral_requires_an_api_key_in_options(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "from-env")
    with pytest.raises(RuntimeError, match="No API key for provider: mistral"):
        mistral.streamSimple(_mistral_model(), normalizeContext(CONTEXT))


async def test_abort_reason_of_any_signal_is_the_first_input_that_fired():
    from misaka.utils._abort_signal import (
        AbortController,
        AnyAbortSignal,
        TimeoutAbortSignal,
        abort_reason,
    )

    user = AbortController()
    timeout = TimeoutAbortSignal(0)
    combined = AnyAbortSignal(user, timeout)
    assert isinstance(abort_reason(combined), TimeoutError)
    user.abort()
    assert str(abort_reason(combined)) == "Request was aborted"
    await asyncio.wait_for(TimeoutAbortSignal(10).wait(), 1)


async def test_combine_abort_signals_aborts_with_the_first_reason_and_cleans_up_its_listeners():
    from misaka.ai.utils.abort_signals import combineAbortSignals
    from misaka.utils.js import AbortController as DomAbortController

    assert combineAbortSignals([None, None]).signal is None
    only = DomAbortController()
    assert combineAbortSignals([None, only.signal]).signal is only.signal

    first, second = DomAbortController(), DomAbortController()
    combined = combineAbortSignals([first.signal, second.signal])
    reason = RuntimeError("first")
    first.abort(reason)
    assert combined.signal.aborted is True
    assert combined.signal.reason is reason
    combined.cleanup()

    third, fourth = DomAbortController(), DomAbortController()
    cleaned = combineAbortSignals([third.signal, fourth.signal])
    cleaned.cleanup()
    fourth.abort()
    assert cleaned.signal.aborted is False

    already = DomAbortController()
    already.abort(RuntimeError("before"))
    assert str(combineAbortSignals([already.signal, DomAbortController().signal]).signal.reason) == "before"


# --- B1b-6: Bedrock ------------------------------------------------------------------------


def _bedrock_model(**overrides) -> Model:
    return _model("bedrock-converse-stream", **{
        "provider": "amazon-bedrock", "id": "us.anthropic.claude-opus-4-8", "name": "Claude Opus 4.8",
        "baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com", **overrides,
    })


class _BedrockClient:
    def __init__(self, *events, fail=None):
        self.events, self.fail, self.calls = events, fail, []

    def converse_stream(self, **request):
        self.calls.append(request)

        def stream():
            yield from self.events
            if self.fail is not None:
                raise self.fail

        return {"ResponseMetadata": {"RequestId": "req-1", "HTTPStatusCode": 200}, "stream": stream()}


async def _bedrock_run(client, **options):
    from unittest.mock import patch

    from misaka.ai.api import _bedrock_sdk
    from misaka.ai.api import bedrock_converse_stream as amazon_bedrock

    with patch.object(_bedrock_sdk, "_createBotoClient", lambda _config, _middleware: client):
        return await amazon_bedrock.stream(
            _bedrock_model(), normalizeContext(CONTEXT), {"cacheRetention": "none", **options}
        ).result()


async def test_bedrock_keeps_the_raw_stop_reason_and_one_hour_cache_writes():
    usage = {"inputTokens": 10, "outputTokens": 5, "cacheWriteInputTokens": 8, "totalTokens": 23,
             "cacheDetails": [{"ttl": "1h", "inputTokens": 6}, {"ttl": "5m", "inputTokens": 2}]}
    result = await _bedrock_run(_BedrockClient(
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hi"}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": usage}},
    ))

    assert (result.stopReason, result.rawStopReason) == ("stop", "end_turn")
    assert result.usage.cacheWrite1h == 6

    result = await _bedrock_run(_BedrockClient(
        {"messageStart": {"role": "assistant"}}, {"messageStop": {"stopReason": "guardrail_intervened"}},
    ))
    assert (result.rawStopReason, result.errorMessage) == (
        "guardrail_intervened", "Provider stopped with: guardrail_intervened"
    )

    result = await _bedrock_run(_BedrockClient({"messageStart": {"role": "assistant"}}))
    assert result.errorMessage == "Bedrock stream ended without a stop reason"


async def test_bedrock_mid_stream_exception_reads_as_upstreams_bare_object():
    from botocore.exceptions import EventStreamError

    thrown = EventStreamError(
        {"Error": {"Code": "throttlingException", "Message": "Too many requests, please wait."}}, "ConverseStream"
    )
    result = await _bedrock_run(_BedrockClient({"messageStart": {"role": "assistant"}}, fail=thrown))

    assert result.stopReason == "error"
    assert result.errorMessage == '{"message":"Too many requests, please wait."}'
    [diagnostic] = [d for d in result.diagnostics if d.type == "bedrock_response_failure"]
    assert diagnostic.details == {"requestId": "req-1"}


async def test_bedrock_sends_only_the_request_headers_and_leaves_sdk_retries_and_timeouts_alone(monkeypatch):
    from types import SimpleNamespace

    from misaka.ai.api import _bedrock_sdk
    from misaka.ai.api import bedrock_converse_stream as amazon_bedrock

    seen: dict = {}

    def create(config, middleware):
        seen.update(config=config, middleware=middleware)
        return _BedrockClient({"messageStart": {"role": "assistant"}}, {"messageStop": {"stopReason": "end_turn"}})

    monkeypatch.setattr(_bedrock_sdk, "_createBotoClient", create)
    await amazon_bedrock.stream(
        _bedrock_model(headers={"x-model": "m"}),
        normalizeContext(CONTEXT),
        {"headers": {"X-Tag": "t", "x-gone": None}, "maxRetries": 3, "timeoutMs": 0, "cacheRetention": "none"},
    ).result()

    request = SimpleNamespace(headers={})
    for middleware in seen["middleware"]:
        middleware(request)
    assert request.headers == {"X-Tag": "t"}
    assert not {"retries", "read_timeout", "requestHandler"} & set(seen["config"])


def test_bedrock_simple_options_carry_tool_choice(monkeypatch):
    from misaka.ai.api import bedrock_converse_stream as amazon_bedrock

    seen: dict = {}
    monkeypatch.setattr(amazon_bedrock, "stream", lambda _model, _context, options: seen.update(options))
    amazon_bedrock.streamSimple(_bedrock_model(), normalizeContext(CONTEXT), SimpleStreamOptions(toolChoice="none"))

    assert seen["toolChoice"] == "none" and seen["reasoning"] is None


def test_bedrock_base64_decodes_like_atob():
    from misaka.ai.api import bedrock_converse_stream as amazon_bedrock

    assert amazon_bedrock._base64ToBytes(" QUJD\nRA ") == b"ABCD"
    assert amazon_bedrock._decodeRedactedContent("QUJD!") is None
    with pytest.raises(ValueError):
        amazon_bedrock._createImageBlock("image/png", "Q")


# --- pi-messages ---------------------------------------------------------------------------


def test_pi_messages_terminal_event_carries_the_provider_thinking_level():
    from misaka.ai.api.pi_messages import _createEventConverter

    convertEvent = _createEventConverter(_model("pi-messages"))
    assert convertEvent({"type": "start"}).partial.stopReason == "pending"
    done = convertEvent({"type": "done", "reason": "stop", "usage": {}, "providerThinkingLevel": "high"})

    assert done.message.providerThinkingLevel == "high"


def test_pi_messages_simple_options_are_spread_as_given(monkeypatch):
    from misaka.ai.api import pi_messages

    seen: dict = {}
    monkeypatch.setattr(pi_messages, "stream", lambda _model, _context, options: seen.update(options))
    pi_messages.streamSimple(
        _model("pi-messages"), normalizeContext(CONTEXT), SimpleStreamOptions(apiKey="k", toolChoice="auto")
    )

    assert seen["maxTokens"] is None and seen["toolChoice"] == "auto" and seen["apiKey"] == "k"


def test_pi_messages_server_final_arguments_win_over_the_streamed_text():
    from misaka.ai.api.pi_messages import _createEventConverter

    convertEvent = _createEventConverter(_model("pi-messages"))
    convertEvent({"type": "toolcall_start", "contentIndex": 0, "id": "c", "toolName": "t"})
    convertEvent({"type": "toolcall_delta", "contentIndex": 0, "delta": "{\"a\": 1"})
    end = convertEvent({"type": "toolcall_end", "contentIndex": 0,
                             "toolCall": {"type": "toolCall", "id": "c", "name": "t", "arguments": {"a": 1}}})

    assert end.toolCall.arguments == {"a": 1} and end.toolCall.argumentsError is None


# --- B1b-7: utils ---------------------------------------------------------------------------


def test_chatgpt_subscription_limits_fail_fast_and_unavailability_retries():
    from misaka.ai.utils.retry import isRetryableAssistantError

    def failed(text: str) -> AssistantMessage:
        return _output(_model("openai-responses")).model_copy(update={"stopReason": "error", "errorMessage": text})

    assert not isRetryableAssistantError(failed("429 subscription_sharing_usage_limit_exceeded"))
    assert isRetryableAssistantError(failed("subscription_sharing_usage_unavailable"))
    assert isRetryableAssistantError(failed("subscription_sharing_user_unavailable"))


@pytest.mark.parametrize(("no_proxy", "host", "port", "proxied"), [
    ("example.com", "api.example.com", 443, False),
    ("*example.com", "api.example.com", 443, False),
    ("example.com", "notexample.com", 443, True),
    ("::1", "::1", 80, False),
    ("[::1]:8080", "::1", 80, True),
    ("[::1]:8080", "::1", 8080, False),
    ("example.com:8443", "example.com", 443, True),
])
def test_no_proxy_matching_follows_upstream(no_proxy, host, port, proxied):
    from misaka.ai.utils.node_http_proxy import _shouldProxyHostname

    assert _shouldProxyHostname(host, port, {"no_proxy": no_proxy}) is proxied


def test_retry_after_reads_a_leading_number_like_parse_float():
    from types import SimpleNamespace

    from misaka.ai.utils.provider_retry import _getRetryDelayMs

    assert _getRetryDelayMs(SimpleNamespace(headers={"retry-after": "5s"}), 0, None) == 5000
    assert _getRetryDelayMs(SimpleNamespace(headers={"retry-after-ms": "250ms"}), 0, None) == 250


def test_uuidv7_is_monotonic_within_a_millisecond_and_keeps_a_supplied_timestamp():
    from misaka.ai.utils.uuid import uuidv7

    ids = [uuidv7() for _ in range(500)]
    assert ids == sorted(ids) and len(set(ids)) == 500
    assert all(value[14] == "7" and value[19] in "89ab" for value in ids)
    assert uuidv7(0x0123456789AB).replace("-", "")[:12] == "0123456789ab"
    with pytest.raises(ValueError):
        uuidv7(-1)


# --- B1b follow-up: pi 1.0.0 ------------------------------------------------------------------


def test_retry_after_without_a_finite_delay_falls_back_to_exponential_backoff():
    from types import SimpleNamespace

    from misaka.ai.utils.provider_retry import _getRetryDelayMs

    for headers in (
        {"retry-after-ms": "Infinity"},
        {"retry-after": "Infinity"},
        {"retry-after": "not a date"},
    ):
        delay = _getRetryDelayMs(SimpleNamespace(headers=headers), 0, None)
        assert 375 <= delay <= 500, headers


def test_overflow_detects_the_zai_cn_endpoint_prompt_exceeds_max_length_error():
    from misaka.ai.utils.overflow import isContextOverflow

    # Regression for pi #10208.
    message = _output(_model("openai-completions", provider="zai")).model_copy(update={
        "stopReason": "error",
        "errorMessage": '400 {"code":"1261","message":"Prompt exceeds max length"}',
    })
    assert isContextOverflow(message, 1048576)


def test_responses_drops_foreign_item_ids_when_replaying_grammar_calls_as_custom_items():
    from misaka.ai.api.openai_responses_shared import convertResponsesMessages
    from misaka.ai.types import ToolResultMessage

    model = _responses_model()
    tool_call = ToolCall(id="call_1|ctc_1", name="sample_tool", arguments={"payload": "abc"})
    tool_result = ToolResultMessage(
        toolCallId="call_1|ctc_1", toolName="sample_tool", content=[TextContent(text="done")], isError=False, timestamp=0
    )
    options = {"grammarToolInputProperties": {"sample_tool": "payload"}}

    def replay(**assistant):
        message = _output(model).model_copy(update={"content": [tool_call], "stopReason": "toolUse", **assistant})
        context = Context(messages=[UserMessage(content="q", timestamp=0), message, tool_result])
        items = convertResponsesMessages(model, normalizeContext(context), {"openai"}, options)
        return next(item for item in items if item.get("type") == "custom_tool_call")

    # earendil-works/radius#115: a gateway forwards another model's history as a foreign provider.
    foreign = replay(api="pi-messages", provider="radius", model="gpt-other")
    assert foreign["call_id"] == "call_1" and foreign["input"] == "abc"
    assert "id" not in foreign
    assert replay()["id"] == "ctc_1"


def test_anthropic_strict_keyword_check_tells_booleans_from_minitems_numbers():
    check = anthropic._isAnthropicStrictUnsupportedKeyword
    assert check("minItems", 1) is False and check("minItems", 0) is False
    assert check("minItems", True) is True and check("minItems", 2) is True
    assert check("format", "uuid") is False and check("format", 3) is True
    assert check("maxItems", 1) is True and check("minLength", 1) is False


# pi's ai/test/anthropic-federation.test.ts and anthropic-federation-sdk.test.ts (#10177): the cases below cover both
# files, with the real Anthropic SDK in place of pi's mocked one.
_FEDERATION_ENV = {
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_test",
    "ANTHROPIC_ORGANIZATION_ID": "org-test",
    "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_test",
    "ANTHROPIC_IDENTITY_TOKEN_FILE": "/tmp/identity.jwt",
}

_FEDERATION_CONFIG = {
    "organization_id": "org-test",
    "workspace_id": None,
    "authentication": {
        "type": "oidc_federation",
        "federation_rule_id": "fdrl_test",
        "service_account_id": "svac_test",
        "identity_token": {"source": "file", "path": "/tmp/identity.jwt"},
    },
}

_AMBIENT_ANTHROPIC_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_OAUTH_TOKEN",
    *_FEDERATION_ENV,
    "ANTHROPIC_WORKSPACE_ID",
)


def _clear_ambient_anthropic_env(monkeypatch):
    for name in _AMBIENT_ANTHROPIC_ENV:
        monkeypatch.delenv(name, raising=False)


class _EnvContext:
    """An ``AuthContext`` over one dict."""

    def __init__(self, env: dict[str, str]):
        self._env = env

    async def env(self, name: str) -> str | None:
        return self._env.get(name)

    async def fileExists(self, path: str) -> bool:
        return False


async def _resolve_anthropic_auth(env: dict[str, str]):
    from misaka.ai.providers.anthropic import anthropicProvider

    return await anthropicProvider().auth.apiKey.resolve(ctx=_EnvContext(env), credential=None, signal=None)


# https://github.com/earendil-works/pi/issues/10177
async def test_anthropic_federation_variables_resolve_as_provider_env_with_no_request_auth():
    from misaka.ai.auth.types import AuthResult, ModelAuth

    assert await _resolve_anthropic_auth(_FEDERATION_ENV) == AuthResult(
        auth=ModelAuth(), env=_FEDERATION_ENV, source="workload identity federation"
    )

    with_workspace = await _resolve_anthropic_auth({**_FEDERATION_ENV, "ANTHROPIC_WORKSPACE_ID": "wrkspc_test"})
    assert with_workspace.env["ANTHROPIC_WORKSPACE_ID"] == "wrkspc_test"

    partial = {name: value for name, value in _FEDERATION_ENV.items() if name != "ANTHROPIC_IDENTITY_TOKEN_FILE"}
    assert await _resolve_anthropic_auth(partial) is None

    # ANTHROPIC_SERVICE_ACCOUNT_ID is optional, like the SDK.
    optional = {name: value for name, value in _FEDERATION_ENV.items() if name != "ANTHROPIC_SERVICE_ACCOUNT_ID"}
    assert (await _resolve_anthropic_auth(optional)).env == optional

    # API key and auth token keep precedence over federation.
    assert await _resolve_anthropic_auth({**_FEDERATION_ENV, "ANTHROPIC_API_KEY": "api-key"}) == AuthResult(
        auth=ModelAuth(apiKey="api-key"), source="ANTHROPIC_API_KEY"
    )
    assert await _resolve_anthropic_auth({**_FEDERATION_ENV, "ANTHROPIC_AUTH_TOKEN": "auth-token"}) == AuthResult(
        auth=ModelAuth(headers={"Authorization": "Bearer auth-token"}), source="ANTHROPIC_AUTH_TOKEN"
    )


def test_anthropic_federation_config_needs_the_anthropic_provider_and_no_request_auth(monkeypatch):
    _clear_ambient_anthropic_env(monkeypatch)
    model = getModel("anthropic", "claude-haiku-4-5")

    assert anthropic._getAnthropicFederation(model, None, None, _FEDERATION_ENV) == _FEDERATION_CONFIG
    optional = {name: value for name, value in _FEDERATION_ENV.items() if name != "ANTHROPIC_SERVICE_ACCOUNT_ID"}
    federation = anthropic._getAnthropicFederation(model, None, None, optional)
    assert federation["authentication"]["service_account_id"] is None
    assert anthropic._getAnthropicFederation(model, "explicit-key", None, _FEDERATION_ENV) is None
    assert anthropic._getAnthropicFederation(model, None, {"Authorization": "Bearer t"}, _FEDERATION_ENV) is None
    kimi = model.model_copy(update={"provider": "kimi-coding", "baseUrl": "https://api.kimi.com/coding"})
    assert anthropic._getAnthropicFederation(kimi, None, None, _FEDERATION_ENV) is None


@pytest.fixture
def anthropic_federation_wire(anthropic_wire, monkeypatch, tmp_path):
    """The real SDK's token exchange answered by a fake, with the identity token on disk.

    Yields the recorded message requests, the recorded exchanges, the clients the adapter
    built, and a federation env pointing at the token file.
    """
    from anthropic.lib.credentials import _providers as sdk_credentials

    _clear_ambient_anthropic_env(monkeypatch)
    monkeypatch.setattr(anthropic, "_federationClient", None)
    requests, _ = anthropic_wire
    exchanges: list[httpx.Request] = []
    clients: list = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        exchanges.append(request)
        return httpx.Response(200, json={"access_token": "federated-token", "expires_in": 3600})

    exchange_client = httpx.Client(transport=httpx.MockTransport(token_handler))
    monkeypatch.setattr(sdk_credentials.CredentialsFile, "_get_http_client", lambda self: exchange_client)

    original = anthropic._createClient

    def create_client(*args, **kwargs):
        created = original(*args, **kwargs)
        clients.append(created.client)
        return created

    monkeypatch.setattr(anthropic, "_createClient", create_client)

    identity_token_file = tmp_path / "identity.jwt"
    identity_token_file.write_text("header.payload.signature", encoding="utf-8")
    env = {**_FEDERATION_ENV, "ANTHROPIC_IDENTITY_TOKEN_FILE": str(identity_token_file)}
    yield requests, exchanges, clients, env
    exchange_client.close()


async def test_anthropic_federation_exchanges_the_identity_token_once_across_requests(anthropic_federation_wire):
    requests, exchanges, clients, env = anthropic_federation_wire
    model = getModel("anthropic", "claude-haiku-4-5")
    payloads: list[dict] = []

    for _ in range(3):
        options = {"env": env, "onPayload": lambda params, _model: payloads.append(params)}
        result = await anthropic.stream(model, normalizeContext(CONTEXT), options).result()
        assert result.stopReason == "stop", result.errorMessage

    # The SDK got a federation config instead of a key, and one exchange served all three.
    assert all(client.api_key is None and client.auth_token is None for client in clients)
    assert clients[0].credentials._config == {
        **_FEDERATION_CONFIG,
        "authentication": {
            **_FEDERATION_CONFIG["authentication"],
            "identity_token": {"source": "file", "path": env["ANTHROPIC_IDENTITY_TOKEN_FILE"]},
        },
    }
    assert [exchange.url.path for exchange in exchanges] == ["/v1/oauth/token"]
    assert len(requests) == 3
    for request in requests:
        assert request.headers["authorization"] == "Bearer federated-token"
        assert "x-api-key" not in request.headers
    assert all("oauth-2025-04-20" not in payload.get("betas", []) for payload in payloads)


async def test_anthropic_federation_yields_to_an_explicit_key_and_skips_other_providers(anthropic_federation_wire):
    requests, exchanges, clients, env = anthropic_federation_wire
    model = getModel("anthropic", "claude-haiku-4-5")

    result = await anthropic.stream(
        model, normalizeContext(CONTEXT), {"apiKey": "explicit-key", "env": env}
    ).result()
    assert result.stopReason == "stop", result.errorMessage
    assert requests[-1].headers["x-api-key"] == "explicit-key"
    assert clients[-1].credentials is None  # pi: `constructorOpts.config` is undefined

    kimi = model.model_copy(update={"provider": "kimi-coding", "baseUrl": "https://api.kimi.com/coding"})
    result = await anthropic.stream(kimi, normalizeContext(CONTEXT), {"env": env}).result()
    assert result.stopReason == "error"
    assert result.errorMessage == "No API key for provider: kimi-coding"
    assert len(requests) == 1 and exchanges == [] and len(clients) == 1


async def test_anthropic_header_owned_auth_does_not_run_the_sdk_credential_chain(anthropic_federation_wire, monkeypatch):
    requests, exchanges, clients, env = anthropic_federation_wire
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    result = await anthropic.stream(
        getModel("anthropic", "claude-haiku-4-5"),
        normalizeContext(CONTEXT),
        {"headers": {"Authorization": "Bearer auth-token"}},
    ).result()

    assert result.stopReason == "stop", result.errorMessage
    assert [request.headers["authorization"] for request in requests] == ["Bearer auth-token"]
    assert exchanges == []
    # The SDK's own credential chain stayed off: nothing resolved, nothing to exchange.
    assert clients[0].credentials is None and clients[0]._token_cache is None


async def test_models_thread_auth_context_federation_variables_to_the_adapter(anthropic_federation_wire):
    from misaka.ai.models import createModels
    from misaka.ai.providers.anthropic import anthropicProvider

    requests, exchanges, clients, env = anthropic_federation_wire
    models = createModels(CreateModelsOptions(authContext=_EnvContext(env)))
    models.setProvider(anthropicProvider())

    result = await models.streamSimple(getModel("anthropic", "claude-haiku-4-5"), normalizeContext(CONTEXT)).result()

    assert result.stopReason == "stop", result.errorMessage
    assert clients[0].api_key is None and clients[0].credentials._config["organization_id"] == "org-test"
    assert requests[0].headers["authorization"] == "Bearer federated-token"
    assert len(exchanges) == 1


async def test_anthropic_federation_drops_the_cached_token_after_a_401(anthropic_wire, anthropic_federation_wire):
    _, responses = anthropic_wire
    requests, exchanges, _clients, env = anthropic_federation_wire
    model = getModel("anthropic", "claude-haiku-4-5")
    responses.append(
        httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "revoked"}})
    )

    rejected = await anthropic.stream(model, normalizeContext(CONTEXT), {"env": env}).result()
    accepted = await anthropic.stream(model, normalizeContext(CONTEXT), {"env": env}).result()

    assert rejected.stopReason == "error"
    assert accepted.stopReason == "stop", accepted.errorMessage
    # The rejected token was dropped, so the second request exchanged a fresh one.
    assert [exchange.url.path for exchange in exchanges] == ["/v1/oauth/token", "/v1/oauth/token"]
    assert [request.headers["authorization"] for request in requests] == ["Bearer federated-token"] * 2


# --- B3: TUI --------------------------------------------------------------------------------


# The overlay-after-stop cases moved to tests/test_tui_overlay_options.py (overlay-options.test.ts, #10026).


def test_direct_color_term_values_mean_truecolor(monkeypatch):
    from misaka.ui.tui.terminal_image import detectCapabilities

    for name in ("COLORTERM", "TMUX", "TERM_PROGRAM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "tmux-direct")
    assert detectCapabilities(lambda: False).trueColor is True
    monkeypatch.setenv("TERM", "tmux-256color")
    assert detectCapabilities(lambda: False).trueColor is False


def test_kitty_cell_sizing_picks_the_less_distorted_row_count():
    from misaka.ui.tui import terminal_image
    from misaka.ui.tui.terminal_image import (
        CellDimensions,
        ImageDimensions,
        calculateImageCellSize,
    )

    def size(image, cells, optimize, max_width=60):
        return calculateImageCellSize(ImageDimensions(*image), max_width, None, CellDimensions(*cells), optimize)

    assert size((615, 86), (9, 18), True) == terminal_image.ImageCellSize(columns=60, rows=4)
    assert size((615, 86), (9, 18), False).rows == 5
    assert size((615, 86), (15, 28), True).rows == 5
    assert size((1200, 12), (9, 18), True).rows == 1
    assert calculateImageCellSize(ImageDimensions(400, 900), 30, 15, CellDimensions(14, 28), True).columns == 13

    terminal_image.setCapabilityOverrides({"images": "kitty"})
    try:
        rendered = terminal_image.renderImage("AAAA", ImageDimensions(615, 86), {"maxWidthCells": 60})
        assert ",c=60,r=4" in rendered.sequence
    finally:
        terminal_image.setCapabilityOverrides({})


@pytest.mark.parametrize("before", ["(", "see (", "[", "`", "<", "{", "查看，", "　", "。", "「"])
def test_at_trigger_follows_opening_wrappers_and_cjk_punctuation(before):
    from misaka.ui.tui.components.editor import buildTriggerPattern

    pattern = buildTriggerPattern(["@", "#"])
    assert pattern.test(f"{before}@src")
    assert not pattern.test("foo(@src")


def test_box_keeps_child_lines_unpadded_for_the_cache():
    from misaka.ui.tui.components.box import Box

    class Child:
        def __init__(self):
            self.line = "hello"

        def render(self, _width):
            return [self.line]

    child = Child()
    box = Box(2, 0)
    box.addChild(child)
    first = box.render(20)
    assert first == ["  hello" + " " * 13]
    assert box.cache.childLines[0] is child.line
    assert box.render(20) is first


# --- B1b review fixes -------------------------------------------------------------------------


async def test_anthropic_oauth_sends_one_user_agent_and_unknown_payload_keys_in_the_body(anthropic_wire):
    requests, _ = anthropic_wire
    model = getModel("anthropic", "claude-sonnet-4-5")

    def add_key(payload, _model):
        return {**payload, "custom_field": 1}

    result = await anthropic.stream(
        model, normalizeContext(CONTEXT), {"apiKey": "sk-ant-oat01-token", "onPayload": add_key}
    ).result()

    assert result.stopReason == "stop", result.errorMessage
    [request] = requests
    assert request.headers.get_list("user-agent") == [f"claude-cli/{anthropic._claudeCodeVersion}"]
    assert len(request.headers.get_list("accept")) == 1
    assert json.loads(request.content)["custom_field"] == 1


async def test_google_retries_an_http_failure_before_the_stream_starts(gemini_wire, monkeypatch):
    google, requests, chunks = gemini_wire
    chunks.append({"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}]})
    real_client = google.GoogleGenAI
    failures = [httpx.Response(503, json={"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})]

    def client(**kwargs):
        instance = real_client(**kwargs)
        transport = instance._api_client._async_httpx_client._transport
        original = transport.handle_async_request

        async def handle(request):
            return failures.pop(0) if failures else await original(request)

        transport.handle_async_request = handle
        return instance

    monkeypatch.setattr(google, "GoogleGenAI", client)
    stream = google.stream(_gemini_model(), normalizeContext(CONTEXT), {"apiKey": "k", "maxRetries": 2, "maxRetryDelayMs": 1})
    events = [event.type async for event in stream]

    assert events == ["start", "text_start", "text_delta", "text_end", "done"]
    assert len(requests) == 1 and not failures


def test_vertex_custom_base_url_without_a_version_gets_one():
    from misaka.ai.api import google_vertex as vertex

    options = vertex._buildHttpOptions(_gemini_model("google-vertex", baseUrl="https://proxy.example.com/"))
    assert (options["baseUrl"], options["apiVersion"]) == ("https://proxy.example.com/v1", "")
    options = vertex._buildHttpOptions(_gemini_model("google-vertex", baseUrl="https://proxy.example.com/v1beta1"))
    assert (options["baseUrl"], options["apiVersion"]) == ("https://proxy.example.com/v1beta1", "")


def test_codex_empty_websocket_delta_keeps_the_continuation():
    from misaka.ai.api import openai_codex_responses as codex

    item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q"}]}
    reply = {"type": "message", "role": "assistant", "id": "msg_1", "content": [], "status": "completed"}
    continuation = codex._CachedWebSocketContinuationState(
        lastRequestBody={"model": "m", "input": [item]}, lastResponseId="resp_1", lastResponseItems=[reply]
    )
    entry = codex._CachedWebSocketConnection(socket=None, busy=False, createdAt=0, continuation=continuation)

    body = codex._buildCachedWebSocketRequestBody(entry, {"model": "m", "input": [item, reply]})

    assert body["previous_response_id"] == "resp_1" and body["input"] == []
    assert entry.continuation is continuation


def test_simple_options_keep_signal_objects_and_accept_plain_option_objects(monkeypatch):
    from types import SimpleNamespace

    from misaka.ai.api import google_generative_ai as google
    from misaka.ai.api.simple_options import buildBaseOptions, thinkingBudgetForLevel
    from misaka.ai.types import ThinkingBudgets
    from misaka.utils.js import AbortSignal

    signal = AbortSignal()
    seen: dict = {}
    monkeypatch.setattr(google, "stream", lambda _model, _context, options: seen.update(options))
    google.streamSimple(_gemini_model(), normalizeContext(CONTEXT), SimpleStreamOptions(apiKey="k", signal=signal))
    assert seen["signal"] is signal

    base = buildBaseOptions(_gemini_model(), normalizeContext(CONTEXT), SimpleNamespace(apiKey="k", maxTokens=None))
    assert base.apiKey == "k" and base.maxTokens is not None
    assert thinkingBudgetForLevel("low", {"low": 7, "xhigh": 9}) == 7
    assert ThinkingBudgets.model_validate({"low": 1, "xhigh": 2}).low == 1


def test_stream_injects_the_env_key_into_any_options_object(monkeypatch):
    import importlib
    from types import SimpleNamespace

    stream_module = importlib.import_module("misaka.ai.compat")

    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    options = SimpleNamespace(signal=None)
    injected = stream_module._withEnvApiKey(_gemini_model(), options, SimpleStreamOptions)
    assert injected.apiKey == "from-env" and not hasattr(options, "apiKey")


def test_bedrock_unmodeled_stream_error_is_a_named_error():
    from botocore.exceptions import EventStreamError

    from misaka.ai.api import bedrock_converse_stream as amazon_bedrock

    thrown = amazon_bedrock._bedrock_sdk._thrown_value(
        EventStreamError({"Error": {"Code": "ModelTimeoutException", "Message": "timed out"}}, "ConverseStream")
    )
    assert str(thrown) == "timed out"
    assert amazon_bedrock._extractBedrockErrorCode(thrown) == "ModelTimeoutException"


def test_validation_tolerates_refs_and_drops_optional_nulls_for_pydantic_tools():
    from pydantic import BaseModel

    from misaka.ai.utils.validation import validateToolArguments

    schema = {
        "type": "object",
        "$defs": {"Point": {"type": "object", "properties": {"x": {"type": "number"}}}},
        "properties": {"at": {"anyOf": [{"$ref": "#/$defs/Point"}, {"type": "null"}]}, "n": {"type": "integer"}},
    }
    tool = Tool(name="t", description="d", parameters=schema)
    assert validateToolArguments(tool, ToolCall(id="c", name="t", arguments={"at": None, "n": "2"})) == {"at": None, "n": 2}

    class Params(BaseModel):
        limit: int = 5

    tool = Tool(name="p", description="d", parameters=Params)
    assert validateToolArguments(tool, ToolCall(id="c", name="p", arguments={"limit": None})) == {"limit": 5}


def test_proxy_url_with_a_bad_port_is_rejected_like_new_url():
    from misaka.ai.utils.node_http_proxy import resolveHttpProxyUrlForTarget

    with pytest.raises(RuntimeError, match="Invalid proxy URL"):
        resolveHttpProxyUrlForTarget("https://api.example.com", {"https_proxy": "http://proxy:99999"})


async def test_provider_retry_reports_an_abort_with_upstreams_text():
    from misaka.ai.utils.provider_retry import retryProviderRequest
    from misaka.utils.js import AbortController

    controller = AbortController()

    async def request():
        controller.abort()
        raise httpx.ConnectError("down", request=httpx.Request("GET", "https://x"))

    with pytest.raises(Exception, match="^Request aborted$") as raised:
        await retryProviderRequest(request, {"maxRetries": 2, "signal": controller.signal})
    assert raised.value.name == "AbortError"


# --- agent-loop: AssistantMessage.thinkingLevel (540e174c7) ---------------------------------


@pytest.mark.parametrize(("reasoning", "expected"), [("high", "high"), (None, "off")])
async def test_agent_loop_records_the_requested_thinking_level(reasoning, expected):
    from misaka.agent.agent_loop import _streamAssistantResponse
    from misaka.agent.types import AgentContext, AgentLoopConfig
    from misaka.ai.types import DoneEvent

    model = _model("openai-responses")
    message = _output(model)

    def stream_fn(_model, _context, _options):
        events = AssistantMessageEventStream()
        events.push(DoneEvent(reason="stop", message=message))
        events.end(message)
        return events

    async def emit(_event):
        pass

    config = AgentLoopConfig(model=model, convertToLlm=lambda messages: messages, reasoning=reasoning)
    context = AgentContext(messages=[], tools=[])
    result = await _streamAssistantResponse(context, config, None, emit, stream_fn)

    assert result.thinkingLevel == expected
    assert context.messages[-1].thinkingLevel == expected


# --- sdk-stream-options.test.ts: websocketConnectTimeoutMs -----------------------------------


@pytest.mark.parametrize(("request_options", "expected"), [({}, 1234), ({"websocketConnectTimeoutMs": 0}, 0)])
async def test_sdk_forwards_websocket_connect_timeout_from_settings(tmp_path, request_options, expected):
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime
    from misaka.core.sdk import createAgentSession
    from misaka.core.session_manager import SessionManager
    from misaka.core.settings_manager import SettingsManager

    model = getModel("openai-codex", "gpt-5.5")
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsPath": None})
    captured: list = []
    runtime.streamSimple = lambda _model, _context, options=None: captured.append(options)
    result = await createAgentSession({
        "cwd": str(tmp_path), "agentDir": str(tmp_path / "agent"), "model": model, "modelRuntime": runtime,
        "settingsManager": SettingsManager.inMemory({"websocketConnectTimeoutMs": 1234}),
        "sessionManager": SessionManager.inMemory(str(tmp_path)),
    })
    session = result["session"]
    try:
        await session.agent.streamFunction(model, CONTEXT, request_options)
    finally:
        session.dispose()

    assert captured[0]["websocketConnectTimeoutMs"] == expected


def test_settings_timeouts_reject_invalid_values_and_leave_absent_ones_unset():
    from misaka.core.settings_manager import SettingsManager

    assert SettingsManager.inMemory({}).getWebSocketConnectTimeoutMs() is None
    assert SettingsManager.inMemory({"websocketConnectTimeoutMs": "disabled"}).getWebSocketConnectTimeoutMs() == 0
    with pytest.raises(ValueError, match="Invalid websocketConnectTimeoutMs setting"):
        SettingsManager.inMemory({"websocketConnectTimeoutMs": "soon"}).getWebSocketConnectTimeoutMs()


@pytest.mark.parametrize(("content", "expected"), [
    ([{"type": "image", "data": "x", "mimeType": "image/png"}, {"type": "text", "text": "Image reading is disabled."}], 1),
    ([{"type": "text", "text": "Image reading is disabled."}, {"type": "text", "text": "Image reading is disabled."},
      {"type": "image", "data": "x", "mimeType": "image/png"}], 1),
    ([{"type": "image", "data": "x", "mimeType": "image/png"}, {"type": "text", "text": "hi"},
      {"type": "image", "data": "y", "mimeType": "image/png"}], 3),
])
async def test_sdk_block_images_dedupes_every_consecutive_placeholder(tmp_path, content, expected):
    # sdk.ts: images become the placeholder text first, then any placeholder that follows
    # another one is dropped, whether it was an image or a literal text block.
    from misaka.ai.types import UserMessage
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime
    from misaka.core.sdk import createAgentSession
    from misaka.core.session_manager import SessionManager
    from misaka.core.settings_manager import SettingsManager

    result = await createAgentSession({
        "cwd": str(tmp_path), "agentDir": str(tmp_path / "agent"),
        "modelRuntime": await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsPath": None}),
        "settingsManager": SettingsManager.inMemory({"images": {"blockImages": True}}),
        "sessionManager": SessionManager.inMemory(str(tmp_path)),
    })
    session = result["session"]
    try:
        converted = session.agent.convertToLlm([UserMessage.model_validate({"content": content, "timestamp": 0})])
    finally:
        session.dispose()

    assert len(converted[0].content) == expected
    assert all(item.type == "text" for item in converted[0].content)


# --- llama-extension.test.ts: classifier models (B4) ------------------------------------------


def _llama_publish(stored: dict):
    async def publish(publication):
        if publication.persist_is_set:
            stored["entry"] = publication.persist.model_copy(deep=True) if publication.persist is not None else None
        if publication.update:
            publication.update()
        return True

    return publish


async def test_llama_persists_and_restores_selectable_models_for_cache_only_startup_refreshes(monkeypatch):
    from misaka.ai.auth.types import ApiKeyCredential
    from misaka.ai.models import RefreshModelsContext
    from misaka.extensions.llama import client as llama_client
    from misaka.extensions.llama.provider import createLlamaProvider

    url = "http://127.0.0.1:9"

    async def request(self, path, init=None):
        if path == "/models":
            return {"data": [
                {"id": "loaded", "status": {"value": "loaded"}, "meta": {"n_ctx": 32768}},
                {"id": "sleeping", "status": {"value": "sleeping"}, "meta": {"n_ctx": 32768}},
                {"id": "unloaded", "status": {"value": "unloaded"}},
            ]}
        if path == "/props?model=loaded&autoload=false":
            return {}
        raise AssertionError(path)

    monkeypatch.setattr(llama_client.LlamaClient, "request", request)
    stored: dict = {}
    credential = ApiKeyCredential(key="local", env={"LLAMA_BASE_URL": url})

    first = createLlamaProvider()
    await first.provider.refreshModels(RefreshModelsContext(
        credential=credential, stored=None, publish=_llama_publish(stored), allowNetwork=True, signal=None))
    assert [model.id for model in first.provider.getModels()] == ["loaded", "sleeping"]
    assert [(model.id, model.api) for model in stored["entry"].models] == [
        ("loaded", "openai-completions"), ("sleeping", "openai-completions"),
        ("loaded", "llama-cpp-classify"), ("sleeping", "llama-cpp-classify"),
    ]

    second = createLlamaProvider()
    await second.provider.refreshModels(RefreshModelsContext(
        credential=credential, stored=stored["entry"], publish=_llama_publish(stored), allowNetwork=False, signal=None))
    assert [(m.id, m.baseUrl, m.contextWindow) for m in second.provider.getModels()] == [
        ("loaded", f"{url}/v1", 32768), ("sleeping", f"{url}/v1", 32768),
    ]
    assert [(m.id, m.api, m.baseUrl, m.contextWindow) for m in second.provider.getAllModels() if m.type == "classifier"] == [
        ("loaded", "llama-cpp-classify", url, 32768), ("sleeping", "llama-cpp-classify", url, 32768),
    ]


async def test_llama_exposes_unloaded_presets_only_when_router_autoload_is_enabled(monkeypatch):
    from misaka.ai.auth.types import ApiKeyCredential
    from misaka.ai.models import RefreshModelsContext
    from misaka.extensions.llama import client as llama_client
    from misaka.extensions.llama.provider import createLlamaProvider

    props_requests = 0

    async def request(self, path, init=None):
        nonlocal props_requests
        if path == "/models":
            return {"data": [
                {"id": "preset", "status": {"value": "unloaded"}, "source": "preset", "meta": {"n_ctx": 65536}},
                {"id": "failed-preset", "status": {"value": "unloaded", "failed": True}, "source": "preset"},
                {"id": "cache", "status": {"value": "unloaded"}, "source": "cache"},
                {"id": "models-dir", "status": {"value": "unloaded"}, "source": "models_dir"},
            ]}
        if path == "/props":
            props_requests += 1
            return {"role": "router", "models_autoload": True}
        raise AssertionError(path)

    monkeypatch.setattr(llama_client.LlamaClient, "request", request)
    stored: dict = {}
    controller = createLlamaProvider()
    await controller.provider.refreshModels(RefreshModelsContext(
        credential=ApiKeyCredential(key="local", env={"LLAMA_BASE_URL": "http://127.0.0.1:9"}),
        stored=None, publish=_llama_publish(stored), allowNetwork=True, signal=None))

    assert props_requests == 1
    assert [model.id for model in controller.provider.getModels()] == ["preset"]
    assert [(model.id, model.api) for model in stored["entry"].models] == [
        ("preset", "openai-completions"), ("preset", "llama-cpp-classify"),
    ]


async def test_llama_classifies_with_selectable_models_through_llama_server(monkeypatch):
    from misaka.extensions.llama.client import _modelInfo
    from misaka.extensions.llama.provider import createLlamaProvider

    url = "http://llama-extension.test:8080"
    paths: list = []

    async def post(self, target, *, headers=None, content=None, **_kwargs):
        path = str(target)[len(url):]
        paths.append(path)
        payload = json.loads(content)
        assert payload["model"] == "qwen"
        if path == "/tokenize":
            body = {"tokens": [ord(char) for char in payload.get("content", "")]}
        elif path == "/apply-template":
            body = {"prompt": "<|im_start|>assistant\n"}
        elif path == "/completion":
            body = {"completion_probabilities": [{"top_logprobs": [
                {"id": 66, "token": "B", "logprob": -0.1}, {"id": 65, "token": "A", "logprob": -2.4},
            ]}]}
        else:
            return httpx.Response(404, request=httpx.Request("POST", target))
        return httpx.Response(200, json=body, request=httpx.Request("POST", target))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    controller = createLlamaProvider()
    controller.setCatalog([_modelInfo({"id": "qwen", "status": {"value": "loaded"}})], url)
    classifier = next(model for model in controller.provider.getAllModels() if model.type == "classifier")

    # Provider auth resolves the OpenAI-compatible /v1 URL, which replaces the model's base URL.
    result = await controller.provider.classify(
        classifier.model_copy(update={"baseUrl": f"{url}/v1"}),
        {"state": {"message": "The build is red again."},
         "questions": {"kind": {"type": "choice", "instructions": "What is this about?", "criteria": {"billing": "", "ci": ""}}}},
        {"apiKey": "local"},
    )

    assert result.errorMessage is None
    assert (result.answers["kind"].type, result.answers["kind"].choice) == ("choice", "ci")
    assert "/completion" in paths


async def test_llama_exposes_decision_models_reported_by_the_catalog_as_native_system_one_classifiers(monkeypatch):
    from misaka.ai.auth.types import ApiKeyCredential
    from misaka.ai.models import RefreshModelsContext
    from misaka.extensions.llama import client as llama_client
    from misaka.extensions.llama.provider import createLlamaProvider

    url = "http://llama-extension.test:8080"
    props_models: list = []
    system_one_requests: list = []

    async def request(self, path, init=None):
        target = httpx.URL(path)
        if target.path == "/models":
            return {"data": [
                {"id": "qwen", "status": {"value": "loaded"},
                 "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}, "meta": {"n_ctx": 32768}},
                {"id": "kev", "status": {"value": "loaded"},
                 "architecture": {"input_modalities": ["text"], "output_modalities": ["decisions"]}, "meta": {"n_ctx": 8192}},
                {"id": "laya", "status": {"value": "sleeping"},
                 "architecture": {"input_modalities": ["text"], "output_modalities": ["decisions"]}},
                # Servers before llama.cpp 0.6.0 may omit architecture.
                {"id": "legacy", "status": {"value": "loaded"}},
            ]}
        if target.path == "/props":
            props_models.append(target.params.get("model") or "")
            return {}
        raise AssertionError(path)

    async def post(self, target, *, headers=None, content=None, **_kwargs):
        assert str(target) == f"{url}/v1/systemone"
        payload = json.loads(content)
        system_one_requests.append(payload)
        body = {
            "model": payload["model"],
            "answers": {id: {"type": "noul", "noul": 0.82} for id in payload["questions"]},
            "usage": {"input_tokens": 42, "output_tokens": 0},
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", target))

    monkeypatch.setattr(llama_client.LlamaClient, "request", request)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    stored: dict = {}
    credential = ApiKeyCredential(key="local", env={"LLAMA_BASE_URL": url})
    controller = createLlamaProvider()
    await controller.provider.refreshModels(RefreshModelsContext(
        credential=credential, stored=None, publish=_llama_publish(stored), allowNetwork=True, signal=None))

    # The catalog identifies decision models, so a refresh neither probes them nor reads their chat template.
    assert system_one_requests == []
    assert sorted(props_models) == ["legacy", "qwen"]
    assert [model.id for model in controller.provider.getModels()] == ["qwen", "legacy"]
    assert [(model.id, model.api, model.baseUrl) for model in stored["entry"].models] == [
        ("qwen", "openai-completions", f"{url}/v1"),
        ("legacy", "openai-completions", f"{url}/v1"),
        ("qwen", "llama-cpp-classify", url),
        ("kev", "typesafe-system-one", f"{url}/v1"),
        ("laya", "typesafe-system-one", f"{url}/v1"),
        ("legacy", "llama-cpp-classify", url),
    ]

    kev = next(model for model in controller.provider.getAllModels() if model.id == "kev" and model.type == "classifier")
    assert kev.contextWindow == 8192
    result = await controller.provider.classify(
        kev,
        {"state": {"message": "I was charged twice."},
         "questions": {"angry": {"type": "bool", "instructions": "Is the customer angry?",
                                 "criteria": {"true": "angry", "false": "calm"}}}},
        {"apiKey": "local"},
    )
    assert result.errorMessage is None
    assert result.answers["angry"].model_dump() == {"type": "bool", "probability": 0.82}
    assert (result.usage.input, result.usage.output) == (42, 0)
    assert system_one_requests == [{
        "model": "kev",
        "state": {"message": "I was charged twice."},
        "questions": {"angry": {"type": "noul", "instructions": "Is the customer angry?",
                                "criteria": {"true": "angry", "false": "calm"}}},
    }]

    # A cache-only startup restores decision models with their native API.
    restored = createLlamaProvider()
    await restored.provider.refreshModels(RefreshModelsContext(
        credential=credential, stored=stored["entry"], publish=_llama_publish(stored), allowNetwork=False, signal=None))
    assert [model.id for model in restored.provider.getModels()] == ["qwen", "legacy"]
    assert [(m.id, m.api) for m in restored.provider.getAllModels() if m.type == "classifier"] == [
        ("qwen", "llama-cpp-classify"),
        ("kev", "typesafe-system-one"),
        ("laya", "typesafe-system-one"),
        ("legacy", "llama-cpp-classify"),
    ]


def test_llama_lists_decision_models_that_also_output_text_for_chat():
    from misaka.extensions.llama.client import _modelInfo
    from misaka.extensions.llama.provider import createLlamaProvider

    controller = createLlamaProvider()
    controller.setCatalog(
        [
            _modelInfo({"id": "decide", "status": {"value": "sleeping"}, "architecture": {"output_modalities": ["decisions"]}}),
            _modelInfo({"id": "hybrid", "status": {"value": "loaded"},
                         "architecture": {"output_modalities": ["text", "decisions"]}}),
        ],
        "http://localhost:8080",
    )
    assert [model.id for model in controller.provider.getModels()] == ["hybrid"]
    assert [(m.id, m.api) for m in controller.provider.getAllModels() if m.type == "classifier"] == [
        ("decide", "typesafe-system-one"),
        ("hybrid", "typesafe-system-one"),
    ]
