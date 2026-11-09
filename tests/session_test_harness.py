"""pi coding-agent's ``test/test-harness.ts`` (v1.1.0): a faux stream function with declarative response sequencing,
a one-call factory for a fully wired AgentSession with real in-memory dependencies, and event capture.

MISAKA forks: stream events are the pydantic event records, so a partial snapshot is ``model_copy()`` (pi spreads the
object); the temp directory comes from ``tempfile``; ``createTestExtensionsResult``/``createTestResourceLoader`` come
from ``tests/suite_harness.py`` (pi: ``test/utilities.ts``).
"""

from __future__ import annotations

import json
import random
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from misaka.agent import Agent
from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Model,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
)
from misaka.ai.utils.event_stream import (
    AssistantMessageEventStream,
    createAssistantMessageEventStream,
)
from misaka.core.agent_session import AgentSession, AgentSessionConfig
from misaka.core.auth_storage import AuthStorage
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.js import queueMicrotask, setTimeout
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createInMemoryModelRegistry, getModelRuntime
from tests.suite_harness import createTestExtensionsResult, createTestResourceLoader

# ============================================================================
# Faux model
# ============================================================================

FAUX_PROVIDER = "faux"
FAUX_MODEL_ID = "faux-1"
FAUX_API = "anthropic-messages"

fauxModel = Model.model_validate({
    "id": FAUX_MODEL_ID,
    "name": "Faux Model",
    "api": FAUX_API,
    "provider": FAUX_PROVIDER,
    "baseUrl": "http://localhost:0",
    "reasoning": False,
    "input": ["text", "image"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": 128000,
    "maxTokens": 16384,
})

# ============================================================================
# Response description
# ============================================================================


@dataclass(slots=True)
class FauxResponse:
    # Text content blocks. String shorthand becomes a single text block.
    text: str | None = None
    # Tool calls to include in the response: {id?, name, args}.
    toolCalls: list[dict[str, Any]] | None = None
    # Thinking content.
    thinking: str | None = None
    # Stop reason. Defaults to "stop", or "toolUse" if toolCalls are present, or "error" if error is set.
    stopReason: str | None = None
    # Error message. Sets stopReason to "error" if not explicitly set.
    error: str | None = None
    # Usage numbers. Merged with defaults (input: 100, output: 50).
    usage: dict[str, Any] | None = None
    # Delay in ms before the response starts.
    delayMs: float | None = None
    # Model overrides (provider, model id) for responses that should look like they came from a different model.
    model: dict[str, str] | None = None


# Shorthand: a string becomes a simple text response; a dict is a FauxResponse's fields.
FauxResponseInput = FauxResponse | dict[str, Any] | str

# ============================================================================
# Faux stream function
# ============================================================================


def normalizeResponse(input: FauxResponseInput) -> FauxResponse:
    if isinstance(input, str):
        return FauxResponse(text=input)
    if isinstance(input, dict):
        return FauxResponse(**input)
    return input


def buildUsage(partial: dict[str, Any] | None = None) -> Usage:
    partial = partial or {}
    input = partial.get("input", 100)
    output = partial.get("output", 50)
    cacheRead = partial.get("cacheRead", 0)
    cacheWrite = partial.get("cacheWrite", 0)
    return Usage.model_validate({
        "input": input,
        "output": output,
        "cacheRead": cacheRead,
        "cacheWrite": cacheWrite,
        "totalTokens": partial.get("totalTokens", input + output + cacheRead + cacheWrite),
        "cost": partial.get("cost", {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}),
    })


_toolCallIdCounter = 0


def buildAssistantMessage(resp: FauxResponse) -> AssistantMessage:
    global _toolCallIdCounter
    content: list[TextContent | ThinkingContent | ToolCall] = []

    if resp.thinking:
        content.append(ThinkingContent(thinking=resp.thinking))
    if resp.text is not None:
        content.append(TextContent(text=resp.text))
    if resp.toolCalls:
        for tc in resp.toolCalls:
            toolCallId = tc.get("id")
            if toolCallId is None:
                _toolCallIdCounter += 1
                toolCallId = f"faux_tc_{_toolCallIdCounter}"
            content.append(
                ToolCall(
                    id=toolCallId,
                    name=tc["name"],
                    arguments=tc["args"],
                )
            )

    # If no content was added at all, add empty text
    if len(content) == 0 and not resp.error:
        content.append(TextContent(text=""))

    if resp.stopReason:
        stopReason = resp.stopReason
    elif resp.error:
        stopReason = "error"
    elif resp.toolCalls and len(resp.toolCalls) > 0:
        stopReason = "toolUse"
    else:
        stopReason = "stop"

    return AssistantMessage(
        content=content,
        api=FAUX_API,
        provider=(resp.model or {}).get("provider") or FAUX_PROVIDER,
        model=(resp.model or {}).get("id") or FAUX_MODEL_ID,
        usage=buildUsage(resp.usage),
        stopReason=stopReason,
        errorMessage=resp.error,
        timestamp=int(time.time() * 1000),
    )


# ============================================================================
# Token-level streaming
# ============================================================================


def chunkString(text: str) -> list[str]:
    """Split a string into chunks of varying size (3-5 chars) for simulating token-by-token streaming."""
    chunks: list[str] = []
    i = 0
    while i < len(text):
        size = 3 + random.randrange(3)  # 3, 4, or 5
        chunks.append(text[i : i + size])
        i += size
    return chunks if len(chunks) > 0 else [""]


def streamWithDeltas(stream: AssistantMessageEventStream, message: AssistantMessage) -> None:
    """Stream a complete AssistantMessage through an EventStream with realistic
    intermediate delta events for each content block."""
    # Build partial progressively as we stream content blocks
    partial = message.model_copy(update={"content": [], "stopReason": "pending"})
    stream.push(StartEvent(partial=partial.model_copy()))

    for i, block in enumerate(message.content):
        if block.type == "thinking":
            partial.content = [*partial.content, ThinkingContent(thinking="")]
            stream.push(ThinkingStartEvent(contentIndex=i, partial=partial.model_copy()))

            for chunk in chunkString(block.thinking):
                partial.content[i].thinking += chunk
                stream.push(ThinkingDeltaEvent(contentIndex=i, delta=chunk, partial=partial.model_copy()))

            stream.push(ThinkingEndEvent(contentIndex=i, content=block.thinking, partial=partial.model_copy()))
        elif block.type == "text":
            partial.content = [*partial.content, TextContent(text="")]
            stream.push(TextStartEvent(contentIndex=i, partial=partial.model_copy()))

            for chunk in chunkString(block.text):
                partial.content[i].text += chunk
                stream.push(TextDeltaEvent(contentIndex=i, delta=chunk, partial=partial.model_copy()))

            stream.push(TextEndEvent(contentIndex=i, content=block.text, partial=partial.model_copy()))
        elif block.type == "toolCall":
            argsJson = json.dumps(block.arguments, separators=(",", ":"), ensure_ascii=False)
            partial.content = [*partial.content, ToolCall(id=block.id, name=block.name, arguments={})]
            stream.push(ToolCallStartEvent(contentIndex=i, partial=partial.model_copy()))

            for chunk in chunkString(argsJson):
                stream.push(ToolCallDeltaEvent(contentIndex=i, delta=chunk, partial=partial.model_copy()))

            # Final toolcall has the real parsed arguments
            partial.content[i].arguments = block.arguments
            stream.push(ToolCallEndEvent(contentIndex=i, toolCall=block, partial=partial.model_copy()))

    if message.stopReason == "pending":
        error = message.model_copy(
            update={"stopReason": "error", "errorMessage": "Faux response ended without a stop reason"}
        )
        stream.push(ErrorEvent(reason="error", error=error))
        return
    if message.stopReason in ("error", "aborted"):
        stream.push(ErrorEvent(reason=message.stopReason, error=message))
        return
    stream.push(DoneEvent(reason=message.stopReason, message=message))


# ============================================================================
# Stream function factory
# ============================================================================


@dataclass(slots=True)
class FauxStreamFnState:
    # Number of times the stream function has been called.
    callCount: int = 0
    # The context passed to each call, in order.
    contexts: list[Any] = field(default_factory=list)


def createFauxStreamFn(
    responses: list[FauxResponseInput],
) -> tuple[Callable[..., AssistantMessageEventStream], FauxStreamFnState]:
    """Create a faux stream function from a sequence of response descriptions.

    The function cycles through responses in order. If more calls are made than
    responses provided, it wraps around.

    Returns the stream function and a state object for inspection.
    """
    if len(responses) == 0:
        raise RuntimeError("createFauxStreamFn requires at least one response")

    state = FauxStreamFnState()

    def streamFn(_model: Any, context: Any, _options: Any = None) -> AssistantMessageEventStream:
        index = state.callCount % len(responses)
        state.callCount += 1
        state.contexts.append(context)

        resp = normalizeResponse(responses[index])
        message = buildAssistantMessage(resp)
        stream = createAssistantMessageEventStream()

        def emit() -> None:
            streamWithDeltas(stream, message)

        if resp.delayMs and resp.delayMs > 0:
            setTimeout(emit, resp.delayMs)
        else:
            queueMicrotask(emit)

        return stream

    return streamFn, state


# ============================================================================
# Session harness
# ============================================================================


@dataclass
class Harness:
    session: AgentSession
    agent: Agent
    sessionManager: SessionManager
    settingsManager: SettingsManager
    # Faux stream function state (call count, captured contexts).
    faux: FauxStreamFnState
    # All events emitted by the session, in order.
    events: list[Any]
    # Temp directory (cleaned up by cleanup()).
    tempDir: str
    # Dispose session and remove temp directory.
    cleanup: Callable[[], None]

    def eventsOfType(self, type: str) -> list[Any]:
        """Filter captured events by type."""
        return [event for event in self.events if read_field(event, "type") == type]


def createTempDir() -> str:
    return tempfile.mkdtemp(prefix="pi-harness-")


async def createHarnessWithResourceLoader(options: dict[str, Any], resourceLoader: Any, tempDir: str) -> Harness:
    baseModel = options.get("model") or fauxModel
    model = (
        baseModel.model_copy(update={"contextWindow": options["contextWindow"]})
        if options.get("contextWindow")
        else baseModel
    )

    streamFn, fauxState = createFauxStreamFn(options.get("responses") or ["ok"])

    agent = Agent(
        getApiKey=lambda *_: "faux-key",
        initialState={
            "model": model,
            "systemPrompt": options.get("systemPrompt") or "You are a test assistant.",
            "tools": options.get("tools") or [],
        },
        streamFn=streamFn,
    )

    sessionManager = SessionManager.inMemory()
    settingsManager = SettingsManager.create(tempDir, tempDir)

    if options.get("settings"):
        settingsManager.applyOverrides(options["settings"])

    authStorage = AuthStorage.inMemory({model.provider: {"type": "api_key", "key": "faux-key"}})
    modelRegistry = await createInMemoryModelRegistry(authStorage)
    modelRegistry.registerProvider(model.provider, {
        "baseUrl": model.baseUrl,
        "api": model.api,
        "models": [
            {
                "id": model.id,
                "name": model.name,
                "api": model.api,
                "reasoning": model.reasoning,
                "input": model.input,
                "cost": model.cost,
                "contextWindow": model.contextWindow,
                "maxTokens": model.maxTokens,
                "baseUrl": model.baseUrl,
            }
        ],
    })

    session = AgentSession(AgentSessionConfig(
        agent=agent,
        sessionManager=sessionManager,
        settingsManager=settingsManager,
        cwd=tempDir,
        modelRuntime=getModelRuntime(modelRegistry),
        resourceLoader=resourceLoader,
        baseToolsOverride=options.get("baseToolsOverride"),
    ))

    events: list[Any] = []
    session.subscribe(events.append)

    def cleanup() -> None:
        session.dispose()
        shutil.rmtree(tempDir, ignore_errors=True)

    return Harness(
        session=session,
        agent=agent,
        sessionManager=sessionManager,
        settingsManager=settingsManager,
        faux=fauxState,
        events=events,
        tempDir=tempDir,
        cleanup=cleanup,
    )


async def createHarness(**options: Any) -> Harness:
    if options.get("extensionFactories"):
        raise RuntimeError("createHarness does not support extensionFactories. Use createHarnessWithExtensions().")

    tempDir = createTempDir()
    return await createHarnessWithResourceLoader(
        options, options.get("resourceLoader") or createTestResourceLoader(), tempDir
    )


async def createHarnessWithExtensions(**options: Any) -> Harness:
    tempDir = createTempDir()
    extensionsResult = await createTestExtensionsResult(options.get("extensionFactories") or [], tempDir)
    resourceLoader = options.get("resourceLoader") or createTestResourceLoader(extensionsResult)
    return await createHarnessWithResourceLoader(options, resourceLoader, tempDir)


__all__ = [
    "FauxResponse",
    "FauxStreamFnState",
    "Harness",
    "createFauxStreamFn",
    "createHarness",
    "createHarnessWithExtensions",
    "fauxModel",
]
