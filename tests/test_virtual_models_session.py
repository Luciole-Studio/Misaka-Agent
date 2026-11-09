"""pi coding-agent's ``virtual-models.test.ts`` (``createAgentSession`` cases) and
``suite/virtual-models.test.ts``: sessions route each request of a virtual selection.

The runtime cases of ``virtual-models.test.ts`` live in ``test_model_runtime.py``.
"""

from __future__ import annotations

import json
import math
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from misaka.agent.types import AgentToolResult
from misaka.ai.auth.types import ApiKeyAuth, AuthResult, ModelAuth, ProviderAuth
from misaka.ai.models import CreateProviderOptions, ModelsRefreshOptions, createProvider
from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Model,
    StartEvent,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.core.auth_storage import AuthStorage
from misaka.core.extensions.types import ExtensionVirtualModel, ToolDefinition
from misaka.core.model_runtime import ModelRuntime
from misaka.core.virtual_models import (
    VIRTUAL_MODEL_STATE_ENTRY,
    ModelRoute,
    VirtualModelDefinition,
)
from misaka.utils.values import read_field

ZERO_COST = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def _now_ms() -> int:
    return int(time.time() * 1000)


def fauxAssistantMessage(content, *, stopReason="stop", errorMessage=None, timestamp=None) -> AssistantMessage:
    """pi-ai's ``fauxAssistantMessage``: text or tool calls, answered by whichever model streams it."""
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    return AssistantMessage.model_validate({
        "content": blocks, "api": "faux", "provider": "faux", "model": "faux-1",
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                  "cost": {**ZERO_COST, "total": 0}},
        "stopReason": stopReason, "timestamp": _now_ms() if timestamp is None else timestamp,
        **({} if errorMessage is None else {"errorMessage": errorMessage}),
    })


def fauxToolCall(name: str, arguments: dict) -> list:
    return [{"type": "toolCall", "id": f"call-{name}-{_now_ms()}", "name": name, "arguments": arguments}]


def _estimate(text: str) -> int:
    return math.ceil(len(text) / 4)


def _message_text(message) -> str:
    role = read_field(message, "role")
    content = read_field(message, "content")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        kind = read_field(block, "type")
        if kind == "text":
            parts.append(read_field(block, "text"))
        elif kind == "thinking":
            parts.append(read_field(block, "thinking"))
        elif kind == "toolCall":
            parts.append(f"{read_field(block, 'name')}:{json.dumps(read_field(block, 'arguments'))}")
    if role == "toolResult":
        parts.insert(0, read_field(message, "toolName"))
    return "\n".join(part for part in parts if part)


class Faux:
    """pi-ai's ``fauxProvider``: scripted answers with usage estimated from the prompt."""

    def __init__(self, models: list[dict], id: str = "faux") -> None:
        self.responses: list = []
        self.state = SimpleNamespace(callCount=0)
        self.provider = createProvider(CreateProviderOptions(
            id=id,
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Faux", resolve=self._resolve)),
            api=SimpleNamespace(stream=self._stream, streamSimple=self._stream),
            models=[
                Model.model_validate({
                    "name": model["id"], "api": "faux", "provider": id, "baseUrl": "http://localhost:0",
                    "reasoning": False, "input": ["text", "image"], "cost": ZERO_COST,
                    "contextWindow": 128000, "maxTokens": 16384, **model,
                })
                for model in models
            ],
        ))

    @staticmethod
    async def _resolve(*, ctx, credential, signal):
        return AuthResult(auth=ModelAuth(apiKey="faux-key"), source="faux")

    def setResponses(self, responses) -> None:
        self.responses = list(responses)

    def _stream(self, model, context, options=None):
        self.state.callCount += 1
        step = self.responses.pop(0) if self.responses else None
        if step is None:
            message = fauxAssistantMessage("", stopReason="error", errorMessage="No more faux responses queued")
        else:
            message = step(context, options, model) if callable(step) else step
        prompt = "\n\n".join(
            f"{read_field(item, 'role')}:{_message_text(item)}" for item in read_field(context, "messages") or []
        )
        input_tokens, output_tokens = _estimate(prompt), _estimate(_message_text(message))
        message = message.model_copy(update={
            "api": model.api, "provider": model.provider, "model": model.id,
            "usage": message.usage.model_copy(update={
                "input": input_tokens, "output": output_tokens, "totalTokens": input_tokens + output_tokens,
            }),
        })
        stream = AssistantMessageEventStream()
        stream.push(StartEvent(partial=message))
        if message.stopReason in ("error", "aborted"):
            stream.push(ErrorEvent(reason=message.stopReason, error=message))
        else:
            stream.push(DoneEvent(reason=message.stopReason, message=message))
        stream.end(message)
        return stream


async def createRuntime(requests=None):
    requests = requests if requests is not None else []
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": None, "allowModelNetwork": False})
    faux = Faux([
        {"id": "small", "contextWindow": 1000, "maxTokens": 100, "input": ["text"]},
        {"id": "large", "contextWindow": 50_000, "maxTokens": 5000, "input": ["text", "image"], "reasoning": True},
    ])
    runtime.registerNativeProvider(faux.provider)

    def route(request):
        requests.append(request)
        model = runtime.getModel("faux", "large" if request.thinkingLevel == "high" else "small")
        return ModelRoute(model=model, thinkingLevel="high")

    definition = VirtualModelDefinition(
        provider="router", id="auto", name="Auto", thinkingLevels=["low", "high"], route=route
    )
    runtime.registerVirtualModel(definition)
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    return SimpleNamespace(runtime=runtime, faux=faux, definition=definition, virtual=runtime.getModel("router", "auto"))


def assistantFrom(model: Model, text: str, **overrides) -> AssistantMessage:
    return fauxAssistantMessage(text, **overrides).model_copy(
        update={"api": model.api, "provider": model.provider, "model": model.id}
    )


def _user(text: str, timestamp: int) -> dict:
    return {"role": "user", "content": text, "timestamp": timestamp}


async def _open(tmp_path, runtime, session_manager, model=None, extension_factories=None, settings=None):
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import createAgentSession
    from misaka.core.settings_manager import SettingsManager

    workspace, agent_dir = tmp_path / "workspace", tmp_path / "agent"
    workspace.mkdir(exist_ok=True)
    agent_dir.mkdir(exist_ok=True)
    settings_manager = SettingsManager.inMemory(settings or {})
    options = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    loader = DefaultResourceLoader(
        {**options, "settingsManager": settings_manager, "extensionFactories": extension_factories or []}
    )
    await loader.reload()
    return await createAgentSession({
        **options, "resourceLoader": loader, "modelRuntime": runtime, "settingsManager": settings_manager,
        "noTools": "builtin", "sessionManager": session_manager, **({} if model is None else {"model": model}),
    })


def _in_memory(tmp_path):
    from misaka.core.session_manager import SessionManager

    return SessionManager.inMemory(str(tmp_path / "workspace"))


# -- virtual-models.test.ts: createAgentSession with virtual models ----------------------------


async def _resume(tmp_path, runtime, model=None):
    """Resume a transcript where the virtual model was selected and the large model answered."""
    session_manager = _in_memory(tmp_path)
    session_manager.appendModelChange("router", "auto")
    session_manager.appendMessage(_user("hi", 1))
    session_manager.appendMessage(assistantFrom(runtime.getModel("faux", "large"), "hello"))
    result = await _open(tmp_path, runtime, session_manager, model)
    return result["session"], session_manager


async def test_restores_the_virtual_selection_instead_of_the_physical_model_that_answered(tmp_path):
    runtime = (await createRuntime()).runtime

    session, _ = await _resume(tmp_path, runtime)
    try:
        assert (session.model.provider, session.model.id) == ("router", "auto")
        assert (session.routedModel.model.provider, session.routedModel.model.id) == ("faux", "large")
    finally:
        session.dispose()


async def test_restores_a_virtual_selection_registered_right_before_the_session_opens(tmp_path):
    setup = await createRuntime()
    runtime = setup.runtime
    # Extensions register while the session is created, without waiting for the availability refresh.
    runtime.registerVirtualModel(replace(setup.definition, provider="late"))
    session_manager = _in_memory(tmp_path)
    session_manager.appendModelChange("late", "auto")
    session_manager.appendMessage(_user("hi", 1))
    session_manager.appendMessage(assistantFrom(runtime.getModel("faux", "large"), "hello"))

    result = await _open(tmp_path, runtime, session_manager)
    session = result["session"]
    try:
        assert result["modelFallbackMessage"] is None
        assert (session.model.provider, session.model.id) == ("late", "auto")
    finally:
        session.dispose()


async def test_falls_back_to_the_physical_model_when_the_virtual_model_is_not_registered(tmp_path):
    runtime = (await createRuntime()).runtime
    runtime.unregisterVirtualModel("router", "auto")

    session, _ = await _resume(tmp_path, runtime)
    try:
        assert (session.model.provider, session.model.id) == ("faux", "large")
        assert session.routedModel is None
    finally:
        session.dispose()


async def test_falls_back_to_the_last_physical_response_when_the_transcript_ends_with_a_routing_failure(tmp_path):
    setup = await createRuntime()
    runtime, virtual = setup.runtime, setup.virtual
    session_manager = _in_memory(tmp_path)
    session_manager.appendModelChange("router", "auto")
    session_manager.appendMessage(_user("hi", 1))
    session_manager.appendMessage(assistantFrom(runtime.getModel("faux", "large"), "hello"))
    session_manager.appendMessage(_user("again", 2))
    session_manager.appendMessage(assistantFrom(virtual, "", stopReason="error", errorMessage="router failed"))
    runtime.unregisterVirtualModel("router", "auto")

    result = await _open(tmp_path, runtime, session_manager)
    session = result["session"]
    try:
        assert (session.model.provider, session.model.id) == ("faux", "large")
        assert result["modelFallbackMessage"] is None
    finally:
        session.dispose()


async def test_resumes_the_selection_made_before_tree_navigation_left_its_model_change_on_another_branch(tmp_path):
    setup = await createRuntime()
    runtime, faux, virtual = setup.runtime, setup.faux, setup.virtual
    large = runtime.getModel("faux", "large")

    for before, after in ((virtual, large), (large, virtual)):
        faux.setResponses([fauxAssistantMessage("ok") for _ in range(6)])
        session_manager = _in_memory(tmp_path)
        session = (await _open(tmp_path, runtime, session_manager, before))["session"]
        await session.prompt("one")
        first_answer = session_manager.getLeafId()
        await session.setModel(after)
        await session.prompt("two")
        # Navigating back to before the switch keeps `after` selected, but its model_change is on the old branch.
        await session.navigateTree(first_answer)
        await session.prompt("three")
        session.dispose()

        resumed = (await _open(tmp_path, runtime, session_manager))["session"]
        try:
            assert (resumed.model.provider, resumed.model.id) == (after.provider, after.id)
        finally:
            resumed.dispose()


async def test_does_not_record_a_physical_selection_on_every_prompt_while_requests_are_redirected(tmp_path):
    setup = await createRuntime()
    runtime, faux = setup.runtime, setup.faux
    faux.setResponses([fauxAssistantMessage("ok"), fauxAssistantMessage("ok")])
    small = runtime.getModel("faux", "small")
    session_manager = _in_memory(tmp_path)
    session = (await _open(tmp_path, runtime, session_manager, runtime.getModel("faux", "large")))["session"]
    try:
        prepare_request = session.agent.prepareRequest

        async def redirect(request, signal=None):
            return replace(await prepare_request(request, signal), model=small)

        session.agent.prepareRequest = redirect

        def model_changes() -> int:
            return sum(1 for entry in session_manager.getBranch() if entry["type"] == "model_change")

        initial = model_changes()

        await session.prompt("one")
        await session.prompt("two")

        assert [message.model for message in session.messages if read_field(message, "role") == "assistant"] == [
            "small", "small",
        ]
        assert model_changes() == initial
    finally:
        session.dispose()


async def test_records_an_explicit_model_override_on_resume_with_the_next_prompt(tmp_path):
    setup = await createRuntime()
    runtime, faux = setup.runtime, setup.faux
    faux.setResponses([fauxAssistantMessage("ok")])

    session, session_manager = await _resume(tmp_path, runtime, runtime.getModel("faux", "small"))

    def last_model_change():
        changes = [entry for entry in session_manager.getBranch() if entry["type"] == "model_change"]
        return (changes[-1]["provider"], changes[-1]["modelId"])

    try:
        # Opening the session does not write to it.
        assert last_model_change() == ("router", "auto")

        await session.prompt("again")
        assert last_model_change() == ("faux", "small")
    finally:
        session.dispose()


# -- suite/virtual-models.test.ts ------------------------------------------------------------


def _echo_extension(pi) -> None:
    async def echo(*_args):
        return AgentToolResult(content=[{"type": "text", "text": "echoed"}], details={})

    pi.registerTool(ToolDefinition(
        name="echo", label="Echo", description="Echo text back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        execute=echo,
    ))


def defaultRoute(request, ctx) -> ModelRoute:
    """Router used by these tests: new user turns pick by thinking level, everything else stays put."""

    def find(id: str) -> Model:
        return ctx.modelRegistry.find("faux", id)

    if request.reason == "direct":
        return ModelRoute(model=find("large"), thinkingLevel="low")
    sticky = request.failed or request.previous
    if request.reason != "user" and sticky is not None:
        return ModelRoute(model=sticky.model, thinkingLevel=sticky.thinkingLevel or "high")
    return (
        ModelRoute(model=find("large"), thinkingLevel="high")
        if request.thinkingLevel == "high"
        else ModelRoute(model=find("small"), thinkingLevel="off")
    )


def _compaction_supplier(summary: str):
    def factory(pi) -> None:
        async def supply(event, _ctx):
            preparation = event["preparation"]
            return {"compaction": {
                "summary": summary,
                "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                "tokensBefore": read_field(preparation, "tokensBefore"),
            }}

        pi.on("session_before_compact", supply)

    return factory


@asynccontextmanager
async def routed_harness(tmp_path, route=defaultRoute, *, settings=None, extension_factories=()):
    requests: list = []
    faux = Faux([
        {"id": "small", "contextWindow": 1000},
        {"id": "large", "contextWindow": 50_000, "maxTokens": 4000, "reasoning": True},
    ])
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": None, "allowModelNetwork": False})
    runtime.registerNativeProvider(faux.provider)
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))

    def register(pi) -> None:
        def routed(request, ctx):
            requests.append(request)
            return route(request, ctx)

        pi.registerVirtualModel(ExtensionVirtualModel(
            provider="router", id="auto", name="Auto", thinkingLevels=["low", "high"], contextWindow=1000,
            route=routed,
        ))

    session_manager = _in_memory(tmp_path)
    result = await _open(
        tmp_path, runtime, session_manager, runtime.getModel("faux", "small"),
        [*extension_factories, _echo_extension, register], settings,
    )
    session = result["session"]
    events: list = []
    session.subscribe(events.append)
    # Stream through the runtime, which records the thinking level on responses.
    session.agent.streamFunction = lambda model, context, options=None: runtime.streamSimple(model, context, options)
    await session.setModel(runtime.getModel("router", "auto"))
    session.setThinkingLevel("high")

    def dispatched() -> list[str]:
        """Physical model and thinking level recorded on each response."""
        return [
            f"{read_field(message, 'provider')}/{read_field(message, 'model')}:{read_field(message, 'thinkingLevel')}"
            for message in session.messages
            if read_field(message, "role") == "assistant"
        ]

    harness = SimpleNamespace(
        session=session, sessionManager=session_manager, faux=faux, setResponses=faux.setResponses,
        requests=requests, reasons=lambda: [request.reason for request in requests], dispatched=dispatched,
        eventsOfType=lambda type: [event for event in events if read_field(event, "type") == type],
    )
    try:
        yield harness
    finally:
        session.dispose()


async def test_routes_each_request_including_retries_while_the_selection_stays_virtual(tmp_path):
    settings = {"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}}
    async with routed_harness(tmp_path, settings=settings) as harness:
        harness.setResponses([
            fauxAssistantMessage("", stopReason="error", errorMessage="overloaded_error"),
            fauxAssistantMessage(fauxToolCall("echo", {"text": "hi"}), stopReason="toolUse"),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("hello")

        assert harness.reasons() == ["user", "retry", "continuation"]
        assert harness.requests[1].failed.message.errorMessage == "overloaded_error"
        assert harness.requests[2].previous.model.id == "large"
        assert harness.dispatched() == ["faux/large:high", "faux/large:high"]
        assert (harness.session.model.provider, harness.session.model.id) == ("router", "auto")
        assert harness.session.thinkingLevel == "high"
        # Limits come from the physical model that produced the latest response, not the virtual model.
        assert harness.session.getContextUsage()["contextWindow"] == 50_000


async def test_retries_the_first_request_of_a_turn_on_the_model_routed_for_that_turn(tmp_path):
    settings = {"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}}
    async with routed_harness(tmp_path, settings=settings) as harness:
        harness.session.setThinkingLevel("low")
        harness.setResponses([
            fauxAssistantMessage("easy answer"),
            fauxAssistantMessage("", stopReason="error", errorMessage="overloaded_error"),
            fauxAssistantMessage("hard answer"),
        ])
        await harness.session.prompt("easy")
        harness.session.setThinkingLevel("high")

        await harness.session.prompt("hard")

        assert harness.reasons() == ["user", "user", "retry"]
        # The retry reports the failed request on large next to the small response of the previous turn.
        assert harness.requests[2].failed.model.id == "large"
        assert harness.requests[2].previous.model.id == "small"
        assert harness.dispatched() == ["faux/small:off", "faux/large:high"]


async def test_routes_the_compact_and_retry_after_a_truncated_response_as_a_retry(tmp_path):
    settings = {"compaction": {"keepRecentTokens": 1, "reserveTokens": 0}}
    async with routed_harness(
        tmp_path, settings=settings, extension_factories=[_compaction_supplier("overflow compacted")]
    ) as harness:
        harness.setResponses([
            lambda *_: fauxAssistantMessage("x" * 64, stopReason="length", timestamp=_now_ms() + 10_000),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("x" * 5000)

        assert [event["reason"] for event in harness.eventsOfType("compaction_start")] == ["overflow"]
        # Compaction may fold the prompt into the summary, so the retry is not a new user turn.
        assert harness.reasons() == ["user", "retry"]
        assert harness.requests[1].failed.model.id == "large"
        assert harness.requests[1].failed.message.stopReason == "length"


async def test_routes_requests_after_extension_messages_as_continuations(tmp_path):
    def nudge(pi) -> None:
        continued = False

        def before_settle(_event, _ctx):
            nonlocal continued
            if continued:
                return None
            continued = True
            return {
                "entries": [{"type": "custom_message", "customType": "nudge", "content": "Keep going.",
                             "display": False}],
                "continue": True,
            }

        pi.on("agent_before_settle", before_settle)

    async with routed_harness(tmp_path, extension_factories=[nudge]) as harness:
        harness.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])

        await harness.session.prompt("hello")

        # The hidden custom message becomes a user message for the model, but the user did not write it.
        assert harness.reasons() == ["user", "continuation"]


async def test_routes_the_first_request_of_a_prompt_as_a_user_turn_when_extension_messages_follow(tmp_path):
    def context(pi) -> None:
        pi.on("before_agent_start", lambda _event, _ctx: {
            "message": {"customType": "context", "content": "Extra context.", "display": False},
        })

    async with routed_harness(tmp_path, extension_factories=[context]) as harness:
        harness.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])

        await harness.session.prompt("hello")
        await harness.session.prompt("again")

        # The context ends with the extension message, but the request answers the user's prompt.
        assert read_field(harness.session.messages[-2], "role") == "custom"
        assert harness.reasons() == ["user", "user"]


async def test_ends_the_run_with_an_error_response_when_routing_fails_and_keeps_the_last_physical_limits(tmp_path):
    fail = False

    def route(request, ctx):
        if fail:
            raise RuntimeError("router unavailable")
        return defaultRoute(request, ctx)

    async with routed_harness(tmp_path, route) as harness:
        harness.setResponses([fauxAssistantMessage("answer")])
        await harness.session.prompt("hello")

        fail = True
        await harness.session.prompt("again")

        last = harness.session.messages[-1]
        assert (last.role, last.provider, last.model, last.stopReason) == ("assistant", "router", "auto", "error")
        assert "router unavailable" in last.errorMessage
        assert harness.faux.state.callCount == 1
        # The failed attempt names the virtual model, whose declared window is 1k; the large model's 50k applies.
        assert harness.session.getContextUsage()["contextWindow"] == 50_000


async def test_checks_compaction_against_the_physical_model_that_produced_the_response(tmp_path):
    async with routed_harness(tmp_path) as harness:
        harness.setResponses([fauxAssistantMessage("short answer"), fauxAssistantMessage("long answer")])
        await harness.session.prompt("hello")

        # About 20k tokens exceed the virtual model's declared 1k window but fit the large model's 50k.
        await harness.session.prompt("x" * 80_000)

        assert harness.eventsOfType("compaction_start") == []


async def test_compacts_before_a_request_routed_to_a_model_with_a_smaller_window(tmp_path):
    settings = {"compaction": {"keepRecentTokens": 1, "reserveTokens": 0}}
    async with routed_harness(
        tmp_path, settings=settings, extension_factories=[_compaction_supplier("compacted")]
    ) as harness:
        compacted_before_small = False

        def small_answer(*_args):
            nonlocal compacted_before_small
            compacted_before_small = len(harness.eventsOfType("compaction_end")) == 1
            return fauxAssistantMessage("small answer")

        harness.setResponses([fauxAssistantMessage("y" * 8000), small_answer])
        await harness.session.prompt("hello")
        assert harness.eventsOfType("compaction_start") == []

        # About 2k tokens fit the large model that answered last, but not the small model's 1k window.
        harness.session.setThinkingLevel("low")
        await harness.session.prompt("next")

        assert [event["reason"] for event in harness.eventsOfType("compaction_start")] == ["threshold"]
        assert compacted_before_small is True
        assert harness.dispatched()[-1] == "faux/small:off"


async def test_compacts_between_turns_of_a_run_when_the_next_request_is_routed_to_a_smaller_window(tmp_path):
    def route(request, ctx):
        if request.reason == "continuation":
            return ModelRoute(model=ctx.modelRegistry.find("faux", "small"), thinkingLevel="off")
        return defaultRoute(request, ctx)

    settings = {"compaction": {"keepRecentTokens": 1, "reserveTokens": 0}}
    async with routed_harness(
        tmp_path, route, settings=settings, extension_factories=[_compaction_supplier("compacted")]
    ) as harness:
        compacted_before_small = False

        def small_answer(*_args):
            nonlocal compacted_before_small
            compacted_before_small = len(harness.eventsOfType("compaction_end")) == 1
            return fauxAssistantMessage("small answer")

        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("echo", {"text": "hi"}), stopReason="toolUse"),
            small_answer,
        ])

        # About 2k tokens fit the large model of the first turn, but not the small model of the second.
        await harness.session.prompt("x" * 8000)

        assert [event["reason"] for event in harness.eventsOfType("compaction_start")] == ["threshold"]
        assert compacted_before_small is True
        assert harness.dispatched() == ["faux/large:high", "faux/small:off"]


async def test_projects_the_session_once_per_request_under_a_virtual_selection(tmp_path):
    async with routed_harness(tmp_path) as harness:
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("echo", {"text": "hi"}), stopReason="toolUse"),
            fauxAssistantMessage("done"),
        ])
        manager = harness.sessionManager
        build = manager.buildSessionProjection
        projections: list = []
        manager.buildSessionProjection = lambda *args, **kwargs: projections.append(1) or build(*args, **kwargs)

        await harness.session.prompt("hello")

        # One per request, one between the turns, and one for the compaction check after the run.
        assert len(projections) == 4


async def test_stores_router_state_on_the_branch_and_passes_it_to_later_requests(tmp_path):
    states: list = []

    def route(request, ctx):
        states.append(request.state)
        turns = (request.state or {}).get("turns", 0)
        routed = defaultRoute(request, ctx)
        # Returning request.state keeps it without storing it again. Direct requests neither get nor store state.
        if request.reason == "continuation":
            return replace(routed, state=request.state)
        return replace(routed, state={"turns": turns + 1})

    async with routed_harness(tmp_path, route, settings={"compaction": {"keepRecentTokens": 1}}) as harness:
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("echo", {"text": "hi"}), stopReason="toolUse"),
            fauxAssistantMessage("first"),
            fauxAssistantMessage("second"),
            fauxAssistantMessage("summary"),
            fauxAssistantMessage("summary"),
        ])

        await harness.session.prompt("one")
        await harness.session.prompt("two")

        def stored() -> list:
            return [
                entry["data"]
                for entry in harness.sessionManager.getBranch()
                if entry["type"] == "custom" and entry["customType"] == VIRTUAL_MODEL_STATE_ENTRY
            ]

        assert stored() == [
            {"provider": "router", "modelId": "auto", "state": {"turns": 1}},
            {"provider": "router", "modelId": "auto", "state": {"turns": 2}},
        ]

        await harness.session.compact()

        assert harness.reasons() == ["user", "continuation", "user", "direct"]
        assert states == [None, {"turns": 1}, {"turns": 1}, None]
        assert len(stored()) == 2


async def test_does_not_route_compactions_that_an_extension_supplies(tmp_path):
    def route(request, ctx):
        if request.reason == "direct":
            raise RuntimeError("router unavailable")
        return defaultRoute(request, ctx)

    async with routed_harness(
        tmp_path, route, settings={"compaction": {"keepRecentTokens": 1}},
        extension_factories=[_compaction_supplier("extension summary")],
    ) as harness:
        harness.setResponses([fauxAssistantMessage("first answer"), fauxAssistantMessage("second answer")])
        await harness.session.prompt("first")
        await harness.session.prompt("second")

        result = await harness.session.compact()

        assert result.summary == "extension summary"
        assert harness.reasons() == ["user", "user"]


async def test_routes_compaction_summaries_before_sizing_them(tmp_path):
    summaries: list[str] = []

    def summary(_context, options, model):
        summaries.append(f"{model.id}:{read_field(options, 'reasoning') or 'off'}:{read_field(options, 'maxTokens')}")
        return fauxAssistantMessage("summary")

    async with routed_harness(tmp_path, settings={"compaction": {"keepRecentTokens": 1}}) as harness:
        # Compaction summarizes the history and the split turn prefix with one routed model.
        harness.setResponses([
            fauxAssistantMessage("first answer"),
            fauxAssistantMessage("second answer"),
            summary,
            summary,
        ])
        await harness.session.prompt("first")
        await harness.session.prompt("second")

        result = await harness.session.compact()

        assert "summary" in result.summary
        assert harness.reasons() == ["user", "user", "direct"]
        # The router's thinking level applies, and the output budget respects the large model's 4000 tokens.
        assert summaries == ["large:low:4000", "large:low:4000"]


# -- MISAKA: the extension registration path (loader queue, runner flush, direct calls) --------


async def test_extensions_register_virtual_models_before_and_after_the_runner_binds(tmp_path):
    import pytest

    from misaka.core.extensions.loader import createExtensionRuntime

    with pytest.raises(RuntimeError, match="not initialized"):
        createExtensionRuntime().createContext()

    apis: list = []

    def model(id: str, provider: str = "router") -> ExtensionVirtualModel:
        return ExtensionVirtualModel(provider=provider, id=id, name=id, route=defaultRoute)

    def factory(pi) -> None:
        apis.append(pi)
        pi.registerVirtualModel(model("auto"))
        pi.registerVirtualModel(model("dropped"))
        # Unregistering before the runner binds drops the queued registration.
        pi.unregisterVirtualModel("router", "dropped")

    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": None, "allowModelNetwork": False})
    runtime.registerNativeProvider(Faux([{"id": "small"}, {"id": "large"}]).provider)
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    session = (await _open(tmp_path, runtime, _in_memory(tmp_path), runtime.getModel("faux", "small"), [factory]))[
        "session"
    ]
    try:
        assert runtime.getModel("router", "auto") is not None
        assert runtime.getModel("router", "dropped") is None
        [pi] = apis
        # After binding, registrations take effect right away.
        pi.registerVirtualModel(model("later"))
        assert runtime.getModel("router", "later") is not None
        pi.unregisterVirtualModel("router", "later")
        assert runtime.getModel("router", "later") is None
        # A virtual model cannot take the id of a physical model; the extension sees the error.
        with pytest.raises(RuntimeError, match="conflicts with a physical model"):
            pi.registerVirtualModel(model("large", provider="faux"))
    finally:
        session.dispose()


# -- B6 review regressions -------------------------------------------------------------------


async def test_a_context_engine_is_given_the_model_the_request_is_routed_to(tmp_path):
    seen: list = []

    def engine(pi) -> None:
        def prepare(event, _ctx):
            if event["preflight"]:
                model = event["model"]
                seen.append(f"{model.provider}/{model.id}")
            return {"execute": None}

        pi.on("session_context_prepare", prepare)

    async with routed_harness(tmp_path, extension_factories=[engine]) as harness:
        harness.setResponses([fauxAssistantMessage("hard"), fauxAssistantMessage("easy")])
        await harness.session.prompt("hard question")
        harness.session.setThinkingLevel("low")
        await harness.session.prompt("easy question")

        # The selection stays virtual; the engine sizes each request from the routed model.
        assert seen == ["faux/large", "faux/small"]
        assert harness.dispatched() == ["faux/large:high", "faux/small:off"]


async def test_returning_the_same_router_state_stores_it_once(tmp_path):
    kept = {"phase": "planning"}

    def route(request, ctx):
        return replace(defaultRoute(request, ctx), state=kept)

    async with routed_harness(tmp_path, route) as harness:
        harness.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two"), fauxAssistantMessage("three")])
        for text in ("one", "two", "three"):
            await harness.session.prompt(text)

        stored = [
            entry for entry in harness.sessionManager.getBranch()
            if entry["type"] == "custom" and entry["customType"] == VIRTUAL_MODEL_STATE_ENTRY
        ]
        assert len(stored) == 1


async def test_a_failed_prompt_assembly_keeps_the_next_turn_notifications(tmp_path):
    import pytest

    def bad_section(pi) -> None:
        def before(event, _ctx):
            event["systemPromptOptions"]["sections"]["Bad Name"] = "x"

        pi.on("before_agent_start", before)

    async with routed_harness(tmp_path, extension_factories=[bad_section]) as harness:
        await harness.session.sendCustomMessage({"customType": "note", "content": "keep"}, {"deliverAs": "nextTurn"})

        with pytest.raises(ValueError, match="section name"):
            await harness.session.prompt("hello")

        assert [read_field(message, "content") for message in harness.session._pendingNextTurnMessages] == ["keep"]


async def test_an_aborted_auto_compaction_does_not_summarize_after_the_hooks(tmp_path):
    sessions: list = []

    def abort_in_hook(pi) -> None:
        async def before_compact(_event, _ctx):
            sessions[0].abortCompaction()

        pi.on("session_before_compact", before_compact)

    settings = {"compaction": {"keepRecentTokens": 1, "reserveTokens": 0}}
    async with routed_harness(tmp_path, settings=settings, extension_factories=[abort_in_hook]) as harness:
        sessions.append(harness.session)
        harness.setResponses([fauxAssistantMessage("y" * 8000), fauxAssistantMessage("small answer")])
        await harness.session.prompt("hello")
        harness.session.setThinkingLevel("low")

        await harness.session.prompt("next")

        # Each threshold compaction (before the small model's request and after its answer) was
        # aborted in its hook: nothing was routed for a summary, and the request still went out.
        assert "direct" not in harness.reasons()
        ends = harness.eventsOfType("compaction_end")
        assert ends and all(event["aborted"] for event in ends)
        assert harness.dispatched()[-1] == "faux/small:off"


def test_an_moa_preset_out_of_the_catalog_is_not_recorded_on_every_prompt():
    from misaka.core.agent_session import AgentSession

    preset = Model.model_validate({
        "id": "gone", "name": "gone", "api": "moa", "provider": "moa", "baseUrl": "moa://local",
        "reasoning": False, "input": ["text"], "cost": ZERO_COST, "contextWindow": 1000, "maxTokens": 100,
    })
    appended: list = []
    fake = SimpleNamespace(
        model=preset,
        _modelRuntime=SimpleNamespace(getModel=lambda _provider, _id: None),
        sessionManager=SimpleNamespace(
            getBranch=lambda: [
                {"type": "model_change", "provider": "moa", "modelId": "gone"},
                {"type": "message", "message": {"role": "assistant", "provider": "anthropic", "model": "agg",
                                                "api": "anthropic-messages", "stopReason": "stop"}},
            ],
            appendModelChange=lambda provider, id: appended.append((provider, id)),
        ),
    )

    AgentSession._recordSelection(fake)

    assert appended == []
