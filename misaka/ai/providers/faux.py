"""Translated from pi's ``packages/ai/src/providers/faux.ts`` (v1.1.0).

A scripted provider for tests: a response is an ``AssistantMessage`` or a function ``(context, options, state, model)``
returning one (or an awaitable of one). ``fauxProvider(options)`` returns a handle whose ``provider`` goes into
``createModels().setProvider``; ``registerFauxProvider`` (``misaka.ai.compat``) registers the same api in the api
registry.

MISAKA fork: pydantic messages are mutable, so every use of ``_DEFAULT_USAGE`` gets its own copy; the two
``queueMicrotask(async ...)`` bodies are the coroutines ``respond``/``fetchBody`` run as tasks held by
``spawn_stream_task`` (the stream owns its producer); options are read whether they arrive as a mapping or as a
pydantic model.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import random
import string
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, NotRequired, TypedDict

from misaka.ai.auth.types import ApiKeyAuth, AuthResult, ModelAuth, ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.types import (
    AssistantMessage,
    DeferredHandle,
    DoneEvent,
    ErrorEvent,
    Model,
    ProviderResponse,
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
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils.event_stream import (
    AssistantMessageEventStream,
    createAssistantMessageEventStream,
    spawn_stream_task,
)
from misaka.ai.utils.text import getSystemMessageText
from misaka.utils.js import (
    errorMessage,
    isAborted,
    jsLength,
    jsonStringify,
    jsSlice,
    toUtf16Units,
)
from misaka.utils.values import read_field

_DEFAULT_API = "faux"
_DEFAULT_PROVIDER = "faux"
_DEFAULT_MODEL_ID = "faux-1"
_DEFAULT_MODEL_NAME = "Faux Model"
_DEFAULT_BASE_URL = "http://localhost:0"
_DEFAULT_MIN_TOKEN_SIZE = 3
_DEFAULT_MAX_TOKEN_SIZE = 5

_DEFAULT_USAGE = Usage(
    input=0,
    output=0,
    cacheRead=0,
    cacheWrite=0,
    totalTokens=0,
    cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
)


def _defaultUsage() -> Usage:
    # MISAKA fork: a copy of `_DEFAULT_USAGE` per use (see the module docstring).
    return _DEFAULT_USAGE.model_copy(deep=True)


def _now() -> int:
    """``Date.now()``."""
    return int(time.time() * 1000)


class FauxModelDefinition(TypedDict):
    id: str
    name: NotRequired[str]
    reasoning: NotRequired[bool]
    input: NotRequired[list[str]]
    inputLimits: NotRequired[Any]
    cost: NotRequired[dict[str, float]]
    contextWindow: NotRequired[int]
    maxTokens: NotRequired[int]


type FauxContentBlock = TextContent | ThinkingContent | ToolCall


def fauxText(text: str) -> TextContent:
    return TextContent(type="text", text=text)


def fauxThinking(thinking: str) -> ThinkingContent:
    return ThinkingContent(type="thinking", thinking=thinking)


def fauxToolCall(name: str, arguments_: dict[str, Any], options: dict[str, Any] | None = None) -> ToolCall:
    options = options if options is not None else {}
    return ToolCall(
        type="toolCall",
        id=options["id"] if options.get("id") is not None else _randomId("tool"),
        name=name,
        arguments=arguments_,
    )


def _normalizeFauxAssistantContent(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [fauxText(content)]
    return content if isinstance(content, list) else [content]


def fauxAssistantMessage(content: Any, options: dict[str, Any] | None = None) -> AssistantMessage:
    options = options if options is not None else {}
    return AssistantMessage(
        role="assistant",
        content=_normalizeFauxAssistantContent(content),
        api=_DEFAULT_API,
        provider=_DEFAULT_PROVIDER,
        model=_DEFAULT_MODEL_ID,
        usage=_defaultUsage(),
        stopReason=options["stopReason"] if options.get("stopReason") is not None else "stop",
        **({} if options.get("deferred") is None else {"deferred": options["deferred"]}),
        **({} if options.get("errorMessage") is None else {"errorMessage": options["errorMessage"]}),
        **({} if options.get("responseId") is None else {"responseId": options["responseId"]}),
        timestamp=options["timestamp"] if options.get("timestamp") is not None else _now(),
    )


@dataclass
class FauxProviderState:
    callCount: int = 0
    deferredFetchCount: int = 0
    cancelledDeferred: list[DeferredHandle] = field(default_factory=list)


type FauxResponseFactory = Callable[
    [TranscriptContext, Any, FauxProviderState, Model], AssistantMessage | Awaitable[AssistantMessage]
]

type FauxResponseStep = AssistantMessage | FauxResponseFactory


class _FauxDeferredOptions(TypedDict, total=False):
    # Number of fetches that return the original handle before the scripted response becomes ready.
    pendingFetches: int
    pollAfterMs: int


class _FauxTokenSize(TypedDict, total=False):
    min: int
    max: int


class RegisterFauxProviderOptions(TypedDict, total=False):
    api: str
    provider: str
    models: list[FauxModelDefinition]
    deferred: _FauxDeferredOptions
    tokensPerSecond: float
    tokenSize: _FauxTokenSize


# The handles are the plain objects upstream returns: `SimpleNamespace`s with these members.
# FauxProviderRegistration: api, models, getModel, state, setResponses, appendResponses, getPendingResponseCount,
#   unregister.
# FauxProviderHandle: provider, api, models, getModel, state, setResponses, appendResponses, getPendingResponseCount.
FauxProviderRegistration = SimpleNamespace
FauxProviderHandle = SimpleNamespace


def _estimateTokens(text: str) -> int:
    return math.ceil(jsLength(text) / 4)


def _randomId(prefix: str) -> str:
    return f"{prefix}:{_now()}:{''.join(random.choices(string.ascii_lowercase + string.digits, k=11))}"


def _contentToText(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        block.text if block.type == "text" else f"[image:{block.mimeType}:{jsLength(block.data)}]" for block in content
    )


def _assistantContentToText(content: list[Any]) -> str:
    def text(block: Any) -> str:
        if block.type == "text":
            return block.text
        if block.type == "thinking":
            return block.thinking
        return f"{block.name}:{jsonStringify(block.arguments)}"

    return "\n".join(text(block) for block in content)


def _toolResultToText(message: Any) -> str:
    return "\n".join([message.toolName, *[_contentToText([block]) for block in message.content]])


def _messageToText(message: Any) -> str:
    if message.role == "system":
        return "\n".join(
            part
            for part in [
                getSystemMessageText(message),
                *[
                    f"tool-:{jsonStringify(tool.model_dump(mode='json', exclude_none=True))}"
                    for tool in message.toolsRemoved or []
                ],
                *[
                    f"tool+:{jsonStringify(tool.model_dump(mode='json', exclude_none=True))}"
                    for tool in message.toolsAdded or []
                ],
            ]
            if len(part) > 0
        )
    if message.role == "user":
        return _contentToText(message.content)
    if message.role == "assistant":
        return _assistantContentToText(message.content)
    return _toolResultToText(message)


def _joinedLength(messages: list[str], count: int | None = None) -> int:
    """Length of the prompt text that joins `messages` with blank lines."""
    count = len(messages) if count is None else count
    length = (count - 1) * 2 if count > 0 else 0
    for index in range(count):
        length += jsLength(messages[index])
    return length


def _commonPromptPrefixLength(previous: list[str], current: list[str]) -> int:
    """Length of the common prefix of the two joined prompts. Equal messages are compared whole; characters are compared
    only from the first message that differs.
    """
    index = 0
    while index < len(previous) and index < len(current) and previous[index] == current[index]:
        index += 1

    def rest(messages: list[str]) -> str:
        return "" if index == len(messages) else ("\n\n" if index > 0 else "") + "\n\n".join(messages[index:])

    return _joinedLength(previous, index) + _commonPrefixLength(rest(previous), rest(current))


def _commonPrefixLength(a: str, b: str) -> int:
    # MISAKA fork: compared in UTF-16 code units, as JavaScript indexes strings.
    a, b = toUtf16Units(a), toUtf16Units(b)
    length = min(len(a), len(b))
    index = 0
    while index < length and a[index] == b[index]:
        index += 1
    return index


def _withUsageEstimate(
    message: AssistantMessage,
    context: TranscriptContext,
    options: Any,
    promptCache: dict[str, list[str]],
) -> AssistantMessage:
    # One text per message; the whole prompt joins them with blank lines.
    prompt = [f"{entry.role}:{_messageToText(entry)}" for entry in context.messages]
    promptLength = _joinedLength(prompt)
    promptTokens = math.ceil(promptLength / 4)
    outputTokens = _estimateTokens(_assistantContentToText(message.content))
    input = promptTokens
    cacheRead = 0
    cacheWrite = 0
    sessionId = read_field(options, "sessionId")

    if sessionId and read_field(options, "cacheRetention") != "none":
        previousPrompt = promptCache.get(sessionId)
        if previousPrompt:
            cachedChars = _commonPromptPrefixLength(previousPrompt, prompt)
            cacheRead = math.ceil(cachedChars / 4)
            cacheWrite = math.ceil((promptLength - cachedChars) / 4)
            input = max(0, promptTokens - cacheRead)
        else:
            cacheWrite = promptTokens
        promptCache[sessionId] = prompt

    return message.model_copy(
        update={
            "usage": Usage(
                input=input,
                output=outputTokens,
                cacheRead=cacheRead,
                cacheWrite=cacheWrite,
                totalTokens=input + outputTokens + cacheRead + cacheWrite,
                cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
            ),
        }
    )


def _splitStringByTokenSize(text: str, minTokenSize: int, maxTokenSize: int) -> list[str]:
    chunks: list[str] = []
    index = 0
    while index < jsLength(text):
        tokenSize = minTokenSize + math.floor(random.random() * (maxTokenSize - minTokenSize + 1))
        charSize = max(1, tokenSize * 4)
        chunks.append(jsSlice(text, index, index + charSize))
        index += charSize
    return chunks if len(chunks) > 0 else [""]


def _cloneMessage(message: AssistantMessage, api: str, provider: str, modelId: str) -> AssistantMessage:
    cloned = message.model_copy(deep=True)
    return cloned.model_copy(
        update={
            "api": api,
            "provider": provider,
            "model": modelId,
            "timestamp": cloned.timestamp if cloned.timestamp is not None else _now(),
            "usage": cloned.usage if cloned.usage is not None else _defaultUsage(),
        }
    )


def _createDeferredMessage(model: Model, handle: DeferredHandle) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_defaultUsage(),
        stopReason="deferred",
        deferred=handle,
        timestamp=_now(),
    )


def _createErrorMessage(error: Any, api: str, provider: str, modelId: str) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api=api,
        provider=provider,
        model=modelId,
        usage=_defaultUsage(),
        stopReason="error",
        errorMessage=errorMessage(error),
        timestamp=_now(),
    )


def _createAbortedMessage(partial: AssistantMessage) -> AssistantMessage:
    return partial.model_copy(update={"stopReason": "aborted", "errorMessage": "Request was aborted", "timestamp": _now()})


async def _scheduleChunk(chunk: str, tokensPerSecond: float | None) -> None:
    if not tokensPerSecond or tokensPerSecond <= 0:
        await asyncio.sleep(0)
        return
    delayMs = (_estimateTokens(chunk) / tokensPerSecond) * 1000
    await asyncio.sleep(delayMs / 1000)


async def _streamWithDeltas(
    stream: AssistantMessageEventStream,
    message: AssistantMessage,
    minTokenSize: int,
    maxTokenSize: int,
    tokensPerSecond: float | None,
    signal: Any,
) -> None:
    partial = message.model_copy(update={"content": [], "stopReason": "pending"})
    if isAborted(signal):
        aborted = _createAbortedMessage(partial)
        stream.push(ErrorEvent(type="error", reason="aborted", error=aborted))
        stream.end(aborted)
        return

    stream.push(StartEvent(type="start", partial=partial.model_copy()))

    for index in range(len(message.content)):
        if isAborted(signal):
            aborted = _createAbortedMessage(partial)
            stream.push(ErrorEvent(type="error", reason="aborted", error=aborted))
            stream.end(aborted)
            return

        block = message.content[index]

        if block.type == "thinking":
            partial.content = [*partial.content, ThinkingContent(type="thinking", thinking="")]
            stream.push(ThinkingStartEvent(type="thinking_start", contentIndex=index, partial=partial.model_copy()))
            for chunk in _splitStringByTokenSize(block.thinking, minTokenSize, maxTokenSize):
                await _scheduleChunk(chunk, tokensPerSecond)
                if isAborted(signal):
                    aborted = _createAbortedMessage(partial)
                    stream.push(ErrorEvent(type="error", reason="aborted", error=aborted))
                    stream.end(aborted)
                    return
                partial.content[index].thinking += chunk
                stream.push(
                    ThinkingDeltaEvent(type="thinking_delta", contentIndex=index, delta=chunk, partial=partial.model_copy())
                )
            stream.push(
                ThinkingEndEvent(
                    type="thinking_end",
                    contentIndex=index,
                    content=block.thinking,
                    partial=partial.model_copy(),
                )
            )
            continue

        if block.type == "text":
            partial.content = [*partial.content, TextContent(type="text", text="")]
            stream.push(TextStartEvent(type="text_start", contentIndex=index, partial=partial.model_copy()))
            for chunk in _splitStringByTokenSize(block.text, minTokenSize, maxTokenSize):
                await _scheduleChunk(chunk, tokensPerSecond)
                if isAborted(signal):
                    aborted = _createAbortedMessage(partial)
                    stream.push(ErrorEvent(type="error", reason="aborted", error=aborted))
                    stream.end(aborted)
                    return
                partial.content[index].text += chunk
                stream.push(TextDeltaEvent(type="text_delta", contentIndex=index, delta=chunk, partial=partial.model_copy()))
            stream.push(
                TextEndEvent(type="text_end", contentIndex=index, content=block.text, partial=partial.model_copy())
            )
            continue

        partial.content = [*partial.content, ToolCall(type="toolCall", id=block.id, name=block.name, arguments={})]
        stream.push(ToolCallStartEvent(type="toolcall_start", contentIndex=index, partial=partial.model_copy()))
        for chunk in _splitStringByTokenSize(jsonStringify(block.arguments), minTokenSize, maxTokenSize):
            await _scheduleChunk(chunk, tokensPerSecond)
            if isAborted(signal):
                aborted = _createAbortedMessage(partial)
                stream.push(ErrorEvent(type="error", reason="aborted", error=aborted))
                stream.end(aborted)
                return
            stream.push(
                ToolCallDeltaEvent(type="toolcall_delta", contentIndex=index, delta=chunk, partial=partial.model_copy())
            )
        partial.content[index].arguments = block.arguments
        stream.push(
            ToolCallEndEvent(type="toolcall_end", contentIndex=index, toolCall=block, partial=partial.model_copy())
        )

    if message.stopReason == "pending":
        raise RuntimeError("Faux response ended without a stop reason")
    if message.stopReason == "error" or message.stopReason == "aborted":
        stream.push(ErrorEvent(type="error", reason=message.stopReason, error=message))
        stream.end(message)
        return

    stream.push(DoneEvent(type="done", reason=message.stopReason, message=message))
    stream.end(message)


@dataclass
class _DeferredEntry:
    handle: DeferredHandle
    step: Any
    context: TranscriptContext
    options: Any
    model: Model
    pendingFetches: int
    cancelled: bool
    final: AssistantMessage | None = None


async def _onResponse(options: Any, model: Model) -> None:
    onResponse = read_field(options, "onResponse")
    if onResponse is not None:
        result = onResponse(ProviderResponse(status=200, headers={}), model)
        if inspect.isawaitable(result):
            await result


def _submissionOptions(options: Any) -> Any:
    """``const { deferred, signal, onResponse, ...submissionOptions } = entry.options ?? {}``."""
    if options is None:
        return {}
    if isinstance(options, dict):
        return {key: value for key, value in options.items() if key not in ("deferred", "signal", "onResponse")}
    return options.model_copy(update={"deferred": None, "signal": None, "onResponse": None})


def createFauxCore(options: RegisterFauxProviderOptions | None = None) -> SimpleNamespace:
    options = options if options is not None else {}
    api = options["api"] if options.get("api") is not None else _randomId(_DEFAULT_API)
    provider = options["provider"] if options.get("provider") is not None else _DEFAULT_PROVIDER
    tokenSize = options.get("tokenSize") or {}
    minTokenSize = max(
        1,
        min(
            tokenSize["min"] if tokenSize.get("min") is not None else _DEFAULT_MIN_TOKEN_SIZE,
            tokenSize["max"] if tokenSize.get("max") is not None else _DEFAULT_MAX_TOKEN_SIZE,
        ),
    )
    maxTokenSize = max(minTokenSize, tokenSize["max"] if tokenSize.get("max") is not None else _DEFAULT_MAX_TOKEN_SIZE)
    pendingResponses: list[Any] = []
    tokensPerSecond = options.get("tokensPerSecond")
    state = FauxProviderState(callCount=0, deferredFetchCount=0, cancelledDeferred=[])
    promptCache: dict[str, list[str]] = {}
    deferredResponses: dict[str, _DeferredEntry] = {}
    deferredOptions = options.get("deferred") or {}

    modelDefinitions: list[Any] = (
        options["models"]
        if options.get("models")
        else [
            {
                "id": _DEFAULT_MODEL_ID,
                "name": _DEFAULT_MODEL_NAME,
                "reasoning": False,
                "input": ["text", "image"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 128000,
                "maxTokens": 16384,
            }
        ]
    )
    models = [
        Model.model_validate(
            {
                "id": definition["id"],
                "name": definition["name"] if definition.get("name") is not None else definition["id"],
                "api": api,
                "provider": provider,
                "baseUrl": _DEFAULT_BASE_URL,
                "reasoning": definition["reasoning"] if definition.get("reasoning") is not None else False,
                "input": definition["input"] if definition.get("input") is not None else ["text", "image"],
                **({} if definition.get("inputLimits") is None else {"inputLimits": definition["inputLimits"]}),
                "cost": (
                    definition["cost"]
                    if definition.get("cost") is not None
                    else {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
                ),
                "contextWindow": definition["contextWindow"] if definition.get("contextWindow") is not None else 128000,
                "maxTokens": definition["maxTokens"] if definition.get("maxTokens") is not None else 16384,
            }
        )
        for definition in modelDefinitions
    ]

    async def resolveResponse(
        step: Any, context: TranscriptContext, streamOptions: Any, requestModel: Model
    ) -> AssistantMessage:
        resolved = step(context, streamOptions, state, requestModel) if callable(step) else step
        if inspect.isawaitable(resolved):
            resolved = await resolved
        return _withUsageEstimate(
            _cloneMessage(resolved, api, provider, requestModel.id),
            context,
            streamOptions,
            promptCache,
        )

    async def respond(
        outer: AssistantMessageEventStream, step: Any, requestModel: Model, context: TranscriptContext, streamOptions: Any
    ) -> None:
        try:
            await _onResponse(streamOptions, requestModel)
            if not step:
                message = _createErrorMessage(
                    RuntimeError("No more faux responses queued"),
                    api,
                    provider,
                    requestModel.id,
                )
                message = _withUsageEstimate(message, context, streamOptions, promptCache)
                outer.push(ErrorEvent(type="error", reason="error", error=message))
                outer.end(message)
                return

            if read_field(streamOptions, "deferred"):
                handle = DeferredHandle(
                    provider=requestModel.provider,
                    modelId=requestModel.id,
                    api=requestModel.api,
                    id=_randomId("deferred"),
                    **(
                        {"pollAfterMs": deferredOptions["pollAfterMs"]}
                        if deferredOptions.get("pollAfterMs") is not None
                        else {}
                    ),
                )
                deferredResponses[handle.id] = _DeferredEntry(
                    handle=handle,
                    step=step,
                    context=context,
                    options=streamOptions,
                    model=requestModel,
                    pendingFetches=max(0, math.floor(deferredOptions.get("pendingFetches") or 0)),
                    cancelled=False,
                )
                await _streamWithDeltas(
                    outer,
                    _createDeferredMessage(requestModel, handle),
                    minTokenSize,
                    maxTokenSize,
                    tokensPerSecond,
                    read_field(streamOptions, "signal"),
                )
                return

            message = await resolveResponse(step, context, streamOptions, requestModel)
            await _streamWithDeltas(
                outer, message, minTokenSize, maxTokenSize, tokensPerSecond, read_field(streamOptions, "signal")
            )
        except Exception as error:  # noqa: BLE001 - `catch (error)`: any failure becomes the stream's error message
            message = _createErrorMessage(error, api, provider, requestModel.id)
            outer.push(ErrorEvent(type="error", reason="error", error=message))
            outer.end(message)

    def stream(requestModel: Model, context: TranscriptContext, streamOptions: Any = None) -> AssistantMessageEventStream:
        outer = createAssistantMessageEventStream()
        step = pendingResponses.pop(0) if pendingResponses else None
        state.callCount += 1
        spawn_stream_task(respond(outer, step, requestModel, context, streamOptions), stream=outer)
        return outer

    def streamSimple(
        streamModel: Model, context: TranscriptContext, streamOptions: Any = None
    ) -> AssistantMessageEventStream:
        return stream(streamModel, context, streamOptions)

    async def fetchBody(
        outer: AssistantMessageEventStream, requestModel: Model, handle: DeferredHandle, fetchOptions: Any
    ) -> None:
        try:
            await _onResponse(fetchOptions, requestModel)
            entry = deferredResponses.get(handle.id)
            if (
                not entry
                or entry.handle.provider != handle.provider
                or entry.handle.modelId != handle.modelId
                or entry.handle.api != handle.api
            ):
                raise RuntimeError(f"Unknown faux deferred response: {handle.id}")
            if entry.cancelled:
                raise RuntimeError(f"Faux deferred response was cancelled: {handle.id}")

            if entry.pendingFetches > 0:
                entry.pendingFetches -= 1
                await _streamWithDeltas(
                    outer,
                    _createDeferredMessage(requestModel, entry.handle),
                    minTokenSize,
                    maxTokenSize,
                    tokensPerSecond,
                    read_field(fetchOptions, "signal"),
                )
                return

            if not entry.final:
                submissionOptions = _submissionOptions(entry.options)
                try:
                    entry.final = await resolveResponse(entry.step, entry.context, submissionOptions, entry.model)
                except Exception as error:  # noqa: BLE001 - the scripted step failed
                    entry.final = _createErrorMessage(error, api, provider, entry.model.id)
            await _streamWithDeltas(
                outer,
                entry.final,
                minTokenSize,
                maxTokenSize,
                tokensPerSecond,
                read_field(fetchOptions, "signal"),
            )
        except Exception as error:  # noqa: BLE001 - `catch (error)`: any failure becomes the stream's error message
            message = _createErrorMessage(error, api, provider, requestModel.id)
            outer.push(ErrorEvent(type="error", reason="error", error=message))
            outer.end(message)

    def fetchDeferred(requestModel: Model, handle: DeferredHandle, fetchOptions: Any = None) -> AssistantMessageEventStream:
        outer = createAssistantMessageEventStream()
        state.deferredFetchCount += 1
        spawn_stream_task(fetchBody(outer, requestModel, handle, fetchOptions), stream=outer)
        return outer

    async def cancelDeferred(requestModel: Model, handle: DeferredHandle, cancelOptions: Any = None) -> None:
        state.cancelledDeferred.append(handle.model_copy(deep=True))
        entry = deferredResponses.get(handle.id)
        if entry:
            entry.cancelled = True
        await _onResponse(cancelOptions, requestModel)

    def getModel(requestedModelId: str | None = None) -> Model | None:
        if not requestedModelId:
            return models[0]
        return next((candidate for candidate in models if candidate.id == requestedModelId), None)

    def setResponses(responses: list[Any]) -> None:
        nonlocal pendingResponses
        pendingResponses = [*responses]

    def appendResponses(responses: list[Any]) -> None:
        pendingResponses.extend(responses)

    return SimpleNamespace(
        api=api,
        provider=provider,
        models=models,
        stream=stream,
        streamSimple=streamSimple,
        fetchDeferred=fetchDeferred,
        cancelDeferred=cancelDeferred,
        getModel=getModel,
        state=state,
        setResponses=setResponses,
        appendResponses=appendResponses,
        getPendingResponseCount=lambda: len(pendingResponses),
    )


async def _resolveFauxAuth(*, ctx: Any, credential: Any = None, signal: Any) -> AuthResult:
    return AuthResult(auth=ModelAuth())


def fauxProvider(options: RegisterFauxProviderOptions | None = None) -> FauxProviderHandle:
    """Faux provider for tests built on explicit `Models` collections::

        faux = fauxProvider()
        models = createModels()
        models.setProvider(faux.provider)
        faux.setResponses([fauxAssistantMessage("hi")])
    """
    core = createFauxCore(options if options is not None else {})
    provider: Provider = createProvider(
        CreateProviderOptions(
            id=core.provider,
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Faux", resolve=_resolveFauxAuth)),
            models=core.models,
            api=SimpleNamespace(
                stream=core.stream,
                streamSimple=core.streamSimple,
                fetchDeferred=core.fetchDeferred,
                cancelDeferred=core.cancelDeferred,
            ),
        )
    )
    return SimpleNamespace(
        provider=provider,
        api=core.api,
        models=core.models,
        getModel=core.getModel,
        state=core.state,
        setResponses=core.setResponses,
        appendResponses=core.appendResponses,
        getPendingResponseCount=core.getPendingResponseCount,
    )


__all__ = [
    "FauxContentBlock",
    "FauxModelDefinition",
    "FauxProviderHandle",
    "FauxProviderRegistration",
    "FauxProviderState",
    "FauxResponseFactory",
    "FauxResponseStep",
    "RegisterFauxProviderOptions",
    "createFauxCore",
    "fauxAssistantMessage",
    "fauxProvider",
    "fauxText",
    "fauxThinking",
    "fauxToolCall",
]
