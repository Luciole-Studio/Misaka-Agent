"""pi coding-agent's ``jev-router-example.test.ts``, with pi's ``examples/extensions/jev-router.ts``
ported next to it (misaka's examples/ holds only the experimental plugin example): a virtual model that plans on a strong model and
implements on a cheap one, choosing the planning model with a classifier."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.auth.types import ApiKeyAuth, AuthResult, ModelAuth, ProviderAuth
from misaka.ai.models import CreateProviderOptions, ModelsRefreshOptions, createProvider
from misaka.ai.types import ClassifierModel, ClassifierResult
from misaka.core.extensions.types import ExtensionVirtualModel, ToolDefinition
from misaka.core.virtual_models import VIRTUAL_MODEL_STATE_ENTRY, ModelRoute
from misaka.utils.values import read_field
from tests.test_virtual_models_session import (
    Faux,
    _in_memory,
    _open,
    fauxAssistantMessage,
    fauxToolCall,
)

# -- examples/extensions/jev-router.ts -------------------------------------------------------

PROVIDER = "openai-codex"
SOL = "gpt-5.6-sol"
TERRA = "gpt-5.6-terra"
LUNA = "gpt-5.6-luna"

# Tools whose successful result means implementation has started.
EDIT_TOOLS = {"edit", "write"}


def routeTo(request, ctx, id: str, state=None) -> ModelRoute:
    model = ctx.modelRegistry.find(PROVIDER, id)
    if model is None:
        raise RuntimeError(f"Model {PROVIDER}/{id} is not in the catalog")
    return ModelRoute(model=model, thinkingLevel=request.thinkingLevel, state=state)


def lastUserText(messages) -> str:
    users = [message for message in messages if read_field(message, "role") == "user"]
    content = read_field(users[-1], "content") if users else ""
    if isinstance(content, str):
        return content
    return "\n".join(read_field(block, "text") for block in content if read_field(block, "type") == "text")


def editedThisTurn(messages) -> bool:
    """Whether a tool call since the last user message edited a file successfully."""
    last_user = max((index for index, message in enumerate(messages) if read_field(message, "role") == "user"), default=-1)
    return any(
        read_field(message, "role") == "toolResult"
        and read_field(message, "toolName") in EDIT_TOOLS
        and not read_field(message, "isError")
        for message in messages[last_user + 1 :]
    )


async def choosePlanningModel(request, ctx) -> str:
    """Planning model for a new session: Sol for complex work, Terra otherwise or when Jev is unavailable."""
    # Keep a planning model the session already uses, so switching to jev/auto costs no cache miss.
    previous = request.previous.model if request.previous is not None else None
    if previous is not None and previous.provider == PROVIDER and previous.id in (SOL, TERRA):
        return previous.id

    jev = ctx.modelRegistry.findOfType("classifier", "typesafe", "jev-latest")
    if jev is None:
        return TERRA
    result = await ctx.modelRegistry.classify(
        jev,
        {
            "state": {"prompt": lastUserText(request.messages)[:16_000]},
            "questions": {
                "complexity": {
                    "type": "choice",
                    "instructions": "How demanding is the software engineering work requested in `prompt`?",
                    "criteria": {
                        "standard": "Ordinary features, fixes, reviews, or questions",
                        "complex": "Subtle design, cross-cutting changes, or hard debugging",
                    },
                },
            },
        },
        {"signal": request.signal},
    )
    answer = result.answers.get("complexity") if result.stopReason == "stop" else None
    return SOL if answer is not None and answer.type == "choice" and answer.probabilities.get("complex", 0) >= 0.5 else TERRA


def jevRouter(pi) -> None:
    async def route(request, ctx):
        if request.reason == "direct":
            return routeTo(request, ctx, LUNA)
        state = request.state
        if not state:
            model = await choosePlanningModel(request, ctx)
            return routeTo(request, ctx, model, {"phase": "planning", "model": model})
        # The planning model made the first edit: hand the rest of the work to Luna.
        if state["phase"] == "planning" and editedThisTurn(request.messages):
            return routeTo(request, ctx, LUNA, {"phase": "implementation", "model": LUNA})
        return routeTo(request, ctx, state["model"])

    pi.registerVirtualModel(ExtensionVirtualModel(
        provider="jev",
        id="auto",
        name="Auto (Jev)",
        thinkingLevels=["low", "medium", "high", "xhigh"],
        # Shared by all three models; shown before the first response.
        contextWindow=272_000,
        maxTokens=128_000,
        route=route,
    ))


# -- jev-router-example.test.ts --------------------------------------------------------------


def createTool(name: str, fails: bool = False):
    async def execute(*_args):
        if fails:
            raise RuntimeError(f"{name} failed")
        return AgentToolResult(content=[{"type": "text", "text": f"{name} ok"}], details={})

    return ToolDefinition(
        name=name, label=name, description=f"Fake {name} tool",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        execute=execute,
    )


jevModel = ClassifierModel.model_validate({
    "type": "classifier", "id": "jev-latest", "name": "Jev", "api": "typesafe-system-one", "provider": "typesafe",
    "baseUrl": "", "input": ["text"], "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": 64_000,
})


async def _keyless(**_input):
    return AuthResult(auth=ModelAuth(), source="test")


@pytest.fixture
def setup(tmp_path):
    sessions = []

    async def create(complex: float, tools):
        """Runs the example against a faux OpenAI Codex provider and a scripted Jev complexity rating."""
        codex = Faux([{"id": id, "reasoning": True} for id in (SOL, TERRA, LUNA)], id=PROVIDER)

        async def classify(model, _context, _options=None):
            return ClassifierResult.model_validate({
                "api": model.api, "provider": model.provider, "model": model.id,
                "answers": {"complexity": {
                    "type": "choice", "choice": "complex" if complex >= 0.5 else "standard",
                    "probabilities": {"standard": 1 - complex, "complex": complex},
                    "confidence": max(complex, 1 - complex),
                }},
                "stopReason": "stop", "timestamp": int(time.time() * 1000),
            })

        typesafe = createProvider(CreateProviderOptions(
            id="typesafe",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="TypeSafe", resolve=_keyless)),
            models=[jevModel],
            classifiers={"typesafe-system-one": SimpleNamespace(classify=classify)},
        ))

        def providers(pi) -> None:
            pi.registerProvider(codex.provider)
            pi.registerProvider(typesafe)
            for tool in tools:
                pi.registerTool(tool)

        from misaka.ai.models_store import InMemoryModelsStore
        from misaka.core.auth_storage import AuthStorage
        from misaka.core.model_runtime import ModelRuntime

        runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": None, "allowModelNetwork": False})
        session_manager = _in_memory(tmp_path)
        session = (await _open(tmp_path, runtime, session_manager, None, [providers, jevRouter]))["session"]
        sessions.append(session)
        session.agent.streamFunction = lambda model, context, options=None: runtime.streamSimple(model, context, options)
        await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
        await session.setModel(runtime.getModel("jev", "auto"))

        def respond(*messages) -> None:
            codex.responses.extend(messages)

        def dispatched() -> list[str]:
            return [
                read_field(message, "model").replace("gpt-5.6-", "")
                for message in session.messages
                if read_field(message, "role") == "assistant"
            ]

        def phases() -> list[str]:
            return [
                entry["data"]["state"]["phase"]
                for entry in session_manager.getBranch()
                if entry["type"] == "custom" and entry["customType"] == VIRTUAL_MODEL_STATE_ENTRY
            ]

        return SimpleNamespace(session=session, dispatched=dispatched, respond=respond, phases=phases)

    yield create
    for session in sessions:
        session.dispose()


def call(tool: str):
    return fauxAssistantMessage(fauxToolCall(tool, {"path": "a.ts"}), stopReason="toolUse")


async def test_lets_sol_make_the_first_edit_and_switches_to_luna_for_the_rest(setup):
    harness = await setup(0.8, [createTool("read"), createTool("edit")])

    harness.respond(call("read"), call("edit"), fauxAssistantMessage("done"))
    await harness.session.prompt("Refactor the cache layer.")
    harness.respond(fauxAssistantMessage("added"))
    await harness.session.prompt("Also add tests.")

    # Sol reads and makes the first edit; the tool-result follow-up of the same turn goes to Luna.
    assert harness.dispatched() == ["sol", "sol", "luna", "luna"]
    assert harness.phases() == ["planning", "implementation"]
    assert (harness.session.model.provider, harness.session.model.id) == ("jev", "auto")


async def test_plans_on_terra_and_ignores_failed_edits(setup):
    harness = await setup(0.2, [createTool("edit", True), createTool("write")])

    harness.respond(call("edit"), call("write"), fauxAssistantMessage("done"))
    await harness.session.prompt("Add a verbose flag.")

    assert harness.dispatched() == ["terra", "terra", "luna"]
    assert harness.phases() == ["planning", "implementation"]
