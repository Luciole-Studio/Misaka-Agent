"""pi-ai's ``test/faux-provider.test.ts``: the scripted faux provider that the durable and chat tests run on."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from typing import Any

import pytest

from misaka.ai.compat import complete, registerFauxProvider, stream
from misaka.ai.providers.faux import (
    fauxAssistantMessage,
    fauxText,
    fauxThinking,
    fauxToolCall,
)
from misaka.ai.types import (
    Context,
    ImageContent,
    ProviderStreamOptions,
    TextContent,
    Tool,
    ToolResultMessage,
    UserMessage,
)
from misaka.utils.js import AbortController, jsonStringify


def now() -> int:
    """`Date.now()`."""
    return int(time.time() * 1000)

registrations: list[Any] = []


@pytest.fixture(autouse=True)
def unregisterAfterwards() -> Iterator[None]:
    yield
    while registrations:
        registrations.pop().unregister()


def register(options: dict[str, Any] | None = None) -> Any:
    registration = registerFauxProvider(options)
    registrations.append(registration)
    return registration


def hi() -> UserMessage:
    return UserMessage(content="hi", timestamp=now())


def blocks(message: Any) -> list[dict[str, Any]]:
    return [block.model_dump(exclude_none=True) for block in message.content]


async def collectEvents(streamResult: Any) -> list[Any]:
    return [event async for event in streamResult]


async def test_registers_a_custom_provider_and_estimates_usage():
    registration = register()
    registration.setResponses([fauxAssistantMessage("hello world")])

    context = Context(systemPrompt="Be concise.", messages=[UserMessage(content="hi there", timestamp=now())])

    response = await complete(registration.getModel(), context)
    assert blocks(response) == [{"type": "text", "text": "hello world"}]
    assert response.usage.input > 0
    assert response.usage.output > 0
    assert response.usage.totalTokens == response.usage.input + response.usage.output
    assert registration.state.callCount == 1


async def test_supports_helper_blocks_for_text_thinking_and_tool_calls():
    registration = register()
    registration.setResponses(
        [
            fauxAssistantMessage(
                [fauxThinking("think"), fauxToolCall("echo", {"text": "hi"}), fauxText("done")], {"stopReason": "toolUse"})
        ]
    )

    response = await complete(registration.getModel(), Context(messages=[hi()]))

    content = blocks(response)
    assert isinstance(content[1].pop("id"), str)
    assert content == [
        {"type": "thinking", "thinking": "think"},
        {"type": "toolCall", "name": "echo", "arguments": {"text": "hi"}},
        {"type": "text", "text": "done"},
    ]
    assert response.stopReason == "toolUse"


async def test_supports_multiple_models_with_per_model_reasoning_and_model_aware_factories():
    registration = register(
        {
            "models": [
                {"id": "faux-fast", "name": "Faux Fast", "reasoning": False},
                {"id": "faux-thinker", "name": "Faux Thinker", "reasoning": True},
            ]
        }
    )

    def describe(_context: Any, _options: Any, _state: Any, model: Any) -> Any:
        return fauxAssistantMessage(f"{model.id}:{str(model.reasoning).lower()}")

    registration.setResponses([describe, describe])

    assert [model.id for model in registration.models] == ["faux-fast", "faux-thinker"]
    assert registration.getModel() is registration.models[0]
    assert registration.getModel("faux-fast").reasoning is False
    assert registration.getModel("faux-thinker").reasoning is True

    fast = await complete(registration.getModel("faux-fast"), Context(messages=[hi()]))
    thinker = await complete(registration.getModel("faux-thinker"), Context(messages=[hi()]))

    assert blocks(fast) == [{"type": "text", "text": "faux-fast:false"}]
    assert blocks(thinker) == [{"type": "text", "text": "faux-thinker:true"}]


async def test_rewrites_api_provider_and_model_on_returned_messages():
    registration = register({"api": "faux:test", "provider": "faux-provider", "models": [{"id": "faux-model"}]})
    registration.setResponses([fauxAssistantMessage("hello")])

    response = await complete(registration.getModel(), Context(messages=[hi()]))

    assert response.api == "faux:test"
    assert response.provider == "faux-provider"
    assert response.model == "faux-model"


async def test_consumes_queued_responses_in_order_and_errors_when_exhausted():
    registration = register()
    registration.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])

    context = Context(messages=[hi()])

    first = await complete(registration.getModel(), context)
    second = await complete(registration.getModel(), context)
    exhausted = await complete(registration.getModel(), context)

    assert blocks(first) == [{"type": "text", "text": "first"}]
    assert blocks(second) == [{"type": "text", "text": "second"}]
    assert exhausted.stopReason == "error"
    assert exhausted.errorMessage == "No more faux responses queued"
    assert registration.getPendingResponseCount() == 0
    assert registration.state.callCount == 3


async def test_can_replace_and_append_queued_responses():
    registration = register()
    registration.setResponses([fauxAssistantMessage("first")])

    context = Context(messages=[hi()])

    assert blocks(await complete(registration.getModel(), context)) == [{"type": "text", "text": "first"}]
    assert registration.getPendingResponseCount() == 0

    registration.setResponses([fauxAssistantMessage("second")])
    assert registration.getPendingResponseCount() == 1
    assert blocks(await complete(registration.getModel(), context)) == [{"type": "text", "text": "second"}]

    registration.appendResponses([fauxAssistantMessage("third"), fauxAssistantMessage("fourth")])
    assert registration.getPendingResponseCount() == 2
    assert blocks(await complete(registration.getModel(), context)) == [{"type": "text", "text": "third"}]
    assert blocks(await complete(registration.getModel(), context)) == [{"type": "text", "text": "fourth"}]
    assert registration.getPendingResponseCount() == 0


async def test_supports_async_response_factories():
    registration = register()

    async def factory(context: Any, _options: Any, state: Any, *_: Any) -> Any:
        return fauxAssistantMessage(f"{len(context.messages)}:{state.callCount}")

    registration.setResponses([factory])

    response = await complete(registration.getModel(), Context(messages=[hi()]))

    assert blocks(response) == [{"type": "text", "text": "1:1"}]


async def test_emits_an_error_when_a_response_factory_throws():
    registration = register()

    def boom(*_: Any) -> Any:
        raise RuntimeError("boom")

    registration.setResponses([boom])

    events = await collectEvents(stream(registration.getModel(), Context(messages=[hi()])))

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error.stopReason == "error"
    assert events[0].error.errorMessage == "boom"


async def test_rejects_a_queued_response_without_a_terminal_stop_reason():
    registration = register()
    registration.setResponses([fauxAssistantMessage("partial", {"stopReason": "pending"})])

    events = await collectEvents(stream(registration.getModel(), Context(messages=[hi()])))

    assert not any(event.type == "done" for event in events)
    terminal = events[-1]
    assert terminal.type == "error"
    assert terminal.error.stopReason == "error"
    assert terminal.error.errorMessage == "Faux response ended without a stop reason"


async def test_estimates_prompt_and_output_tokens_from_serialized_context():
    registration = register()
    registration.setResponses([fauxAssistantMessage("done")])

    tool = Tool(
        name="echo",
        description="Echo back text",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )
    context = Context(
        systemPrompt="sys",
        messages=[
            UserMessage(
                content=[TextContent(text="hello"), ImageContent(mimeType="image/png", data="abcd")], timestamp=1
            ),
            fauxAssistantMessage("prior"),
            ToolResultMessage(
                toolCallId="tool-1",
                toolName="echo",
                content=[TextContent(text="tool out")],
                isError=False,
                timestamp=2,
            ),
        ],
        tools=[tool],
    )

    response = await complete(registration.getModel(), context)
    promptText = "\n\n".join(
        [
            "system:sys",
            "user:hello\n[image:image/png:4]",
            "assistant:prior",
            "toolResult:echo\ntool out",
            f"tools:{jsonStringify([tool.model_dump(exclude_none=True)])}",
        ]
    )
    expectedPromptTokens = math.ceil(len(promptText) / 4)
    expectedOutputTokens = math.ceil(len("done") / 4)

    assert response.usage.input == expectedPromptTokens
    assert response.usage.output == expectedOutputTokens
    assert response.usage.cacheRead == 0
    assert response.usage.cacheWrite == 0
    assert response.usage.totalTokens == expectedPromptTokens + expectedOutputTokens


async def test_does_not_share_cache_across_sessions_or_requests_without_session_id():
    registration = register()
    registration.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second"), fauxAssistantMessage("third")])

    context = Context(messages=[UserMessage(content="hello", timestamp=now())])

    first = await complete(registration.getModel(), context, ProviderStreamOptions(sessionId="session-1", cacheRetention="short"))
    assert first.usage.cacheWrite > 0
    context.messages.append(first)
    context.messages.append(UserMessage(content="follow up", timestamp=now() + 1))

    second = await complete(registration.getModel(), context, ProviderStreamOptions(sessionId="session-2", cacheRetention="short"))
    assert second.usage.cacheRead == 0
    assert second.usage.cacheWrite > 0

    third = await complete(registration.getModel(), context)
    assert third.usage.cacheRead == 0
    assert third.usage.cacheWrite == 0


async def test_simulates_prompt_caching_per_session_id():
    registration = register()
    registration.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])

    context = Context(systemPrompt="Be concise.", messages=[UserMessage(content="hello", timestamp=now())])

    first = await complete(registration.getModel(), context, ProviderStreamOptions(sessionId="session-1", cacheRetention="short"))
    assert first.usage.cacheRead == 0
    assert first.usage.cacheWrite > 0

    context.messages.append(first)
    context.messages.append(UserMessage(content="follow up", timestamp=now() + 1))

    second = await complete(registration.getModel(), context, ProviderStreamOptions(sessionId="session-1", cacheRetention="short"))
    assert second.usage.cacheRead > 0
    assert second.usage.input + second.usage.cacheRead > second.usage.input


async def test_counts_cached_characters_up_to_the_first_difference_in_the_joined_prompt():
    registration = register()
    registration.setResponses([fauxAssistantMessage("a"), fauxAssistantMessage("b"), fauxAssistantMessage("c")])
    options = ProviderStreamOptions(sessionId="session-1", cacheRetention="short")

    def user(content: str) -> UserMessage:
        return UserMessage(content=content, timestamp=1)

    # Prompt texts: "user:hello world" (16 characters), then 16 + 2 + "user:next" (9) = 27.
    await complete(registration.getModel(), Context(messages=[user("hello world")]), options)
    extended = await complete(registration.getModel(), Context(messages=[user("hello world"), user("next")]), options)
    assert (extended.usage.input, extended.usage.cacheRead, extended.usage.cacheWrite) == (3, 4, 3)

    # The first message now differs after "user:hello w" (12 characters): "user:hello wide" + 2 + 9 = 26.
    edited = await complete(registration.getModel(), Context(messages=[user("hello wide"), user("next")]), options)
    assert (edited.usage.input, edited.usage.cacheRead, edited.usage.cacheWrite) == (4, 3, 4)


async def test_does_not_simulate_caching_when_cache_retention_is_none():
    registration = register()
    registration.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])

    context = Context(messages=[UserMessage(content="hello", timestamp=now())])

    await complete(registration.getModel(), context, ProviderStreamOptions(sessionId="session-1", cacheRetention="none"))
    context.messages.append(fauxAssistantMessage("first"))
    context.messages.append(UserMessage(content="follow up", timestamp=now() + 1))
    second = await complete(registration.getModel(), context, ProviderStreamOptions(sessionId="session-1", cacheRetention="none"))
    assert second.usage.cacheRead == 0
    assert second.usage.cacheWrite == 0


async def test_streams_thinking_text_and_partial_tool_call_deltas():
    registration = register()
    registration.setResponses(
        [
            fauxAssistantMessage(
                [
                    fauxThinking("thinking text"),
                    fauxText("answer text"),
                    fauxToolCall("echo", {"text": "hi", "count": 12}, {"id": "tool-1"}),
                ], {"stopReason": "toolUse"})
        ]
    )

    events: list[str] = []
    toolCallDeltas: list[str] = []
    async for event in stream(registration.getModel(), Context(messages=[hi()])):
        events.append(event.type)
        if event.type == "toolcall_delta":
            toolCallDeltas.append(event.delta)

    assert "thinking_start" in events
    assert "thinking_delta" in events
    assert "text_start" in events
    assert "text_delta" in events
    assert "toolcall_start" in events
    assert "toolcall_delta" in events
    assert "toolcall_end" in events
    assert len(toolCallDeltas) > 1
    assert json.loads("".join(toolCallDeltas)) == {"text": "hi", "count": 12}


async def test_streams_an_exact_event_order_for_fixed_size_chunks():
    registration = register({"tokenSize": {"min": 1, "max": 1}})
    registration.setResponses(
        [
            fauxAssistantMessage(
                [fauxThinking("go"), fauxText("ok"), fauxToolCall("echo", {}, {"id": "tool-1"})], {"stopReason": "toolUse"})
        ]
    )

    events = await collectEvents(stream(registration.getModel(), Context(messages=[hi()])))

    assert events[0].type == "start"
    assert events[0].partial.stopReason == "pending"
    assert [event.type for event in events] == [
        "start",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "text_start",
        "text_delta",
        "text_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
        "done",
    ]


async def test_streams_multiple_tool_calls_in_one_message():
    registration = register()
    registration.setResponses(
        [
            fauxAssistantMessage(
                [fauxToolCall("echo", {"text": "one"}, {"id": "tool-1"}), fauxToolCall("echo", {"text": "two"}, {"id": "tool-2"})], {"stopReason": "toolUse"})
        ]
    )

    events = await collectEvents(stream(registration.getModel(), Context(messages=[hi()])))

    assert len([event for event in events if event.type == "toolcall_start"]) == 2
    assert len([event for event in events if event.type == "toolcall_end"]) == 2


async def test_streams_an_explicit_assistant_error_message_as_a_terminal_error():
    registration = register({"tokenSize": {"min": 2, "max": 2}})
    registration.setResponses(
        [fauxAssistantMessage("partial").model_copy(update={"stopReason": "error", "errorMessage": "upstream failed"})]
    )

    events = await collectEvents(stream(registration.getModel(), Context(messages=[hi()])))

    assert [event.type for event in events] == ["start", "text_start", "text_delta", "text_end", "error"]
    terminal = events[-1]
    assert terminal.type == "error"
    assert terminal.reason == "error"
    assert terminal.error.stopReason == "error"
    assert terminal.error.errorMessage == "upstream failed"


async def test_streams_an_explicit_assistant_aborted_message_as_a_terminal_error():
    registration = register({"tokenSize": {"min": 2, "max": 2}})
    registration.setResponses(
        [
            fauxAssistantMessage("partial").model_copy(
                update={"stopReason": "aborted", "errorMessage": "Request was aborted"}
            )
        ]
    )

    events = await collectEvents(stream(registration.getModel(), Context(messages=[hi()])))

    assert [event.type for event in events] == ["start", "text_start", "text_delta", "text_end", "error"]
    terminal = events[-1]
    assert terminal.type == "error"
    assert terminal.reason == "aborted"
    assert terminal.error.stopReason == "aborted"
    assert terminal.error.errorMessage == "Request was aborted"


async def test_supports_aborting_before_the_first_chunk():
    registration = register({"tokensPerSecond": 50, "tokenSize": {"min": 3, "max": 3}})
    registration.setResponses([fauxAssistantMessage("abcdefghijklmnopqrstuvwxyz")])

    controller = AbortController()
    controller.abort()
    events = await collectEvents(
        stream(registration.getModel(), Context(messages=[hi()]), ProviderStreamOptions(signal=controller.signal))
    )

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].reason == "aborted"
    assert events[0].error.stopReason == "aborted"


async def test_supports_aborting_mid_text_stream_when_paced():
    registration = register({"tokensPerSecond": 100, "tokenSize": {"min": 3, "max": 3}})
    registration.setResponses([fauxAssistantMessage("abcdefghijklmnopqrstuvwxyz")])

    controller = AbortController()
    events: list[str] = []
    textDeltaCount = 0
    async for event in stream(registration.getModel(), Context(messages=[hi()]), ProviderStreamOptions(signal=controller.signal)):
        events.append(event.type)
        if event.type == "text_delta":
            textDeltaCount += 1
            controller.abort()

    assert textDeltaCount == 1
    assert "text_start" in events
    assert "text_delta" in events
    assert "error" in events
    assert "text_end" not in events


async def test_supports_aborting_mid_thinking_stream_when_paced():
    registration = register({"tokensPerSecond": 100, "tokenSize": {"min": 3, "max": 3}})
    registration.setResponses(
        [
            fauxAssistantMessage("ignored").model_copy(
                update={"content": [fauxThinking("abcdefghijklmnopqrstuvwxyz")]}
            )
        ]
    )

    controller = AbortController()
    events: list[str] = []
    thinkingDeltaCount = 0
    async for event in stream(registration.getModel(), Context(messages=[hi()]), ProviderStreamOptions(signal=controller.signal)):
        events.append(event.type)
        if event.type == "thinking_delta":
            thinkingDeltaCount += 1
            controller.abort()

    assert thinkingDeltaCount == 1
    assert "thinking_start" in events
    assert "thinking_delta" in events
    assert "error" in events
    assert "thinking_end" not in events


async def test_supports_aborting_mid_toolcall_stream_when_paced():
    registration = register({"tokensPerSecond": 100, "tokenSize": {"min": 3, "max": 3}})
    registration.setResponses(
        [
            fauxAssistantMessage("done").model_copy(
                update={
                    "content": [
                        fauxToolCall("echo", {"text": "abcdefghijklmnopqrstuvwxyz", "count": 123456789}, {"id": "tool-1"})
                    ],
                    "stopReason": "toolUse",
                }
            )
        ]
    )

    controller = AbortController()
    events: list[str] = []
    toolCallDeltaCount = 0
    async for event in stream(registration.getModel(), Context(messages=[hi()]), ProviderStreamOptions(signal=controller.signal)):
        events.append(event.type)
        if event.type == "toolcall_delta":
            toolCallDeltaCount += 1
            controller.abort()

    assert toolCallDeltaCount == 1
    assert "toolcall_start" in events
    assert "toolcall_delta" in events
    assert "error" in events
    assert "toolcall_end" not in events


async def test_unregisters_the_provider():
    registration = register()
    registration.setResponses([fauxAssistantMessage("hello")])
    registration.unregister()

    with pytest.raises(Exception, match=f"No API provider registered for api: {registration.api}"):
        await complete(registration.getModel(), Context(messages=[hi()]))
