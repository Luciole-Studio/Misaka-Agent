"""Translated from pi's ``packages/coding-agent/test/agent-session-runtime-events.test.ts`` (v1.1.0)."""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any

import pytest

from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.compat import registerFauxProvider
from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.core.agent_session_runtime import (
    CreateAgentSessionRuntimeResult,
    createAgentSessionRuntime,
)
from misaka.core.agent_session_services import (
    createAgentSessionFromServices,
    createAgentSessionServices,
)
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_runtime import ModelRuntime
from misaka.core.session_manager import SessionManager
from misaka.utils.values import read_field

STALE_CTX = (
    "This extension ctx is stale after session replacement or reload. Do not use a captured pi or command ctx after "
    "ctx.newSession(), ctx.fork(), ctx.switchSession(), or ctx.reload(). For newSession, fork, and switchSession, move "
    "post-replacement work into withSession and use the ctx passed to withSession. For reload, do not use the old ctx "
    "after await ctx.reload()."
)


def event(record: Any) -> dict[str, Any]:
    """The event as pi's `toEqual` sees it: undefined fields are absent."""
    keys = ("type", "reason", "targetSessionFile", "previousSessionFile", "entryId", "position")
    return {key: read_field(record, key) for key in keys if read_field(record, key) is not None}


@pytest.fixture
async def cleanups():
    pending: list[Any] = []
    yield pending
    while pending:
        await pending.pop()()


async def createRuntimeHost(cleanups: list[Any], extensionFactory: Any) -> Any:
    tempDir = tempfile.mkdtemp(prefix="pi-runtime-events-")

    faux = registerFauxProvider()
    faux.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two"), fauxAssistantMessage("three")])

    authStorage = AuthStorage.inMemory()

    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="faux-key")

    await authStorage.modify(faux.getModel().provider, apiKey)
    modelRuntime = await ModelRuntime.create({"authStorage": authStorage, "modelsPath": os.path.join(tempDir, "models.json")})
    model = faux.getModel()
    modelRuntime.registerProvider(model.provider, {
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

    async def createRuntime(options: Any) -> CreateAgentSessionRuntimeResult:
        services = await createAgentSessionServices({
            "agentDir": tempDir,
            "modelRuntime": modelRuntime,
            "model": faux.getModel(),
            "resourceLoaderOptions": {
                "extensionFactories": [extensionFactory],
                "noSkills": True,
                "noPromptTemplates": True,
                "noThemes": True,
            },
            "cwd": options["cwd"],
        })
        result = await createAgentSessionFromServices({
            "services": services,
            "sessionManager": options["sessionManager"],
            "sessionStartEvent": options.get("sessionStartEvent"),
            "model": faux.getModel(),
        })
        return CreateAgentSessionRuntimeResult(
            session=result["session"],
            services=services,
            diagnostics=services.diagnostics,
            extensionsResult=result.get("extensionsResult"),
            modelFallbackMessage=result.get("modelFallbackMessage"),
        )

    runtimeHost = await createAgentSessionRuntime(
        createRuntime, {"cwd": tempDir, "agentDir": tempDir, "sessionManager": SessionManager.create(tempDir)}
    )
    await runtimeHost.session.bindExtensions({})

    async def cleanup() -> None:
        await runtimeHost.dispose()
        faux.unregister()
        shutil.rmtree(tempDir, ignore_errors=True)

    cleanups.append(cleanup)
    return runtimeHost


def recorder(events: list[Any], *names: str, result: Any = None) -> Any:
    def extension(pi: Any) -> None:
        for name in names:
            pi.on(name, lambda record, _ctx: (events.append(record), result)[1])

    return extension


async def test_emits_session_before_switch_and_session_start_for_new_and_resume_flows(cleanups):
    events: list[Any] = []
    runtimeHost = await createRuntimeHost(
        cleanups, recorder(events, "session_before_switch", "session_shutdown", "session_start")
    )

    assert [event(record) for record in events] == [{"type": "session_start", "reason": "startup"}]
    events.clear()

    await runtimeHost.session.prompt("hello")
    originalSessionFile = runtimeHost.session.sessionFile
    assert originalSessionFile

    newSessionResult = await runtimeHost.newSession()
    assert read_field(newSessionResult, "cancelled") is False
    await runtimeHost.session.bindExtensions({})
    secondSessionFile = runtimeHost.session.sessionFile
    assert [event(record) for record in events] == [
        {"type": "session_before_switch", "reason": "new"},
        {"type": "session_shutdown", "reason": "new", "targetSessionFile": secondSessionFile},
        {"type": "session_start", "reason": "new", "previousSessionFile": originalSessionFile},
    ]

    events.clear()
    assert secondSessionFile

    switchResult = await runtimeHost.switchSession(originalSessionFile)
    assert read_field(switchResult, "cancelled") is False
    await runtimeHost.session.bindExtensions({})
    assert [event(record) for record in events] == [
        {"type": "session_before_switch", "reason": "resume", "targetSessionFile": originalSessionFile},
        {"type": "session_shutdown", "reason": "resume", "targetSessionFile": originalSessionFile},
        {"type": "session_start", "reason": "resume", "previousSessionFile": secondSessionFile},
    ]


async def test_honors_session_before_switch_cancellation(cleanups):
    events: list[Any] = []

    def extension(pi: Any) -> None:
        def onBeforeSwitch(record: Any, _ctx: Any) -> Any:
            events.append(record)
            return {"cancel": True}

        pi.on("session_before_switch", onBeforeSwitch)
        pi.on("session_start", lambda record, _ctx: events.append(record))

    runtimeHost = await createRuntimeHost(cleanups, extension)

    assert [event(record) for record in events] == [{"type": "session_start", "reason": "startup"}]
    events.clear()

    await runtimeHost.session.prompt("hello")
    originalSessionFile = runtimeHost.session.sessionFile

    result = await runtimeHost.newSession()
    assert read_field(result, "cancelled") is True
    assert runtimeHost.session.sessionFile == originalSessionFile
    assert [event(record) for record in events] == [{"type": "session_before_switch", "reason": "new"}]


async def test_runs_before_session_invalidate_after_session_shutdown_and_before_rebind_session(cleanups):
    phases: list[str] = []
    runtimeHost = await createRuntimeHost(
        cleanups, lambda pi: pi.on("session_shutdown", lambda _event, _ctx: phases.append("session_shutdown"))
    )
    oldSession = runtimeHost.session

    def beforeSessionInvalidate() -> None:
        phases.append("beforeSessionInvalidate")
        assert oldSession.extensionRunner.createContext().cwd == oldSession.sessionManager.getCwd()

    async def rebindSession(*_: Any) -> None:
        phases.append("rebindSession")

    runtimeHost.setBeforeSessionInvalidate(beforeSessionInvalidate)
    runtimeHost.setRebindSession(rebindSession)

    await runtimeHost.newSession()

    assert phases == ["session_shutdown", "beforeSessionInvalidate", "rebindSession"]
    with pytest.raises(Exception) as error:
        _ = oldSession.extensionRunner.createContext().cwd
    assert str(error.value) == STALE_CTX
    runtimeHost.setBeforeSessionInvalidate(None)
    runtimeHost.setRebindSession(None)


async def test_emits_session_before_fork_and_session_start_and_honors_cancellation(cleanups):
    events: list[Any] = []
    state = {"cancelNextFork": False}

    def extension(pi: Any) -> None:
        def onBeforeFork(record: Any, _ctx: Any) -> Any:
            events.append(record)
            if state["cancelNextFork"]:
                state["cancelNextFork"] = False
                return {"cancel": True}
            return None

        pi.on("session_before_fork", onBeforeFork)
        pi.on("session_shutdown", lambda record, _ctx: events.append(record))
        pi.on("session_start", lambda record, _ctx: events.append(record))

    runtimeHost = await createRuntimeHost(cleanups, extension)

    assert [event(record) for record in events] == [{"type": "session_start", "reason": "startup"}]
    events.clear()

    await runtimeHost.session.prompt("hello")
    userMessage = runtimeHost.session.getUserMessagesForForking()[0]
    entryId = read_field(userMessage, "entryId")
    previousSessionFile = runtimeHost.session.sessionFile

    successResult = await runtimeHost.fork(entryId)
    assert read_field(successResult, "cancelled") is False
    assert read_field(successResult, "selectedText") == "hello"
    await runtimeHost.session.bindExtensions({})
    assert [event(record) for record in events] == [
        {"type": "session_before_fork", "entryId": entryId, "position": "before"},
        {"type": "session_shutdown", "reason": "fork", "targetSessionFile": runtimeHost.session.sessionFile},
        {"type": "session_start", "reason": "fork", "previousSessionFile": previousSessionFile},
    ]

    events.clear()
    state["cancelNextFork"] = True
    cancelResult = await runtimeHost.fork(entryId)
    assert cancelResult == {"cancelled": True}
    assert [event(record) for record in events] == [{"type": "session_before_fork", "entryId": entryId, "position": "before"}]

    events.clear()
    state["cancelNextFork"] = True
    cancelAtResult = await runtimeHost.fork("missing-entry", {"position": "at"})
    assert cancelAtResult == {"cancelled": True}
    assert [event(record) for record in events] == [
        {"type": "session_before_fork", "entryId": "missing-entry", "position": "at"}
    ]
