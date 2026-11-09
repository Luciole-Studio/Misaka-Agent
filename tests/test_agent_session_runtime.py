"""Ported from pi's ``packages/coding-agent/test/suite/agent-session-runtime.test.ts`` (v1.1.0): AgentSessionRuntime
characterization -- session replacement, forking, importing and the session events around them."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from misaka.agent import AgentToolResult
from misaka.ai.compat import registerFauxProvider
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.core.agent_session_runtime import (
    CreateAgentSessionRuntimeResult,
    createAgentSessionRuntime,
)
from misaka.core.agent_session_services import (
    createAgentSessionFromServices,
    createAgentSessionServices,
)
from misaka.core.extensions.types import ToolDefinition
from misaka.core.session_manager import SessionManager
from misaka.utils._abort_signal import wait_for_abort
from misaka.utils.values import read_field


def _role(message: Any) -> str | None:
    return read_field(message, "role")


def _messageView(message: Any) -> dict[str, Any]:
    content = read_field(message, "content")
    text = None
    if _role(message) == "user":
        text = content if isinstance(content, str) else "".join(
            read_field(part, "text") for part in content if read_field(part, "type") == "text"
        )
    return {"role": _role(message), "text": text}


def _fauxProviderFactory(faux: Any):
    def register(pi) -> None:
        model = faux.getModel()
        pi.registerProvider(model.provider, {
            "baseUrl": model.baseUrl,
            "apiKey": "faux-key",
            "api": faux.api,
            "models": [
                {
                    "id": registeredModel.id,
                    "name": registeredModel.name,
                    "api": registeredModel.api,
                    "reasoning": registeredModel.reasoning,
                    "input": registeredModel.input,
                    "cost": registeredModel.cost,
                    "contextWindow": registeredModel.contextWindow,
                    "maxTokens": registeredModel.maxTokens,
                }
                for registeredModel in faux.models
            ],
        })

    return register


def _runtimeFactory(runtimeOptions: dict[str, Any]):
    async def createRuntime(options) -> CreateAgentSessionRuntimeResult:
        services = await createAgentSessionServices({
            "agentDir": runtimeOptions["agentDir"],
            "resourceLoaderOptions": runtimeOptions["resourceLoaderOptions"],
            "cwd": options["cwd"],
        })
        fromServices: dict[str, Any] = {
            "services": services,
            "sessionManager": options["sessionManager"],
            "sessionStartEvent": options.get("sessionStartEvent"),
        }
        if runtimeOptions.get("model") is not None:
            fromServices["model"] = runtimeOptions["model"]
        if runtimeOptions.get("thinkingLevel") is not None:
            fromServices["thinkingLevel"] = runtimeOptions["thinkingLevel"]
        result = await createAgentSessionFromServices(fromServices)
        return CreateAgentSessionRuntimeResult(
            session=result["session"],
            services=services,
            diagnostics=services.diagnostics,
            extensionsResult=result.get("extensionsResult"),
            modelFallbackMessage=result.get("modelFallbackMessage"),
        )

    return createRuntime


def _loaderOptions(*factories) -> dict[str, Any]:
    return {"extensionFactories": list(factories), "noSkills": True, "noPromptTemplates": True, "noThemes": True}


@pytest.fixture
async def cleanups():
    pending: list = []
    yield pending
    while pending:
        await pending.pop()()


async def createRuntimeForTest(cleanups: list, extensionFactory, *, cwd: str | None = None, bootstrapModel: bool = True):
    tempDir = cwd or tempfile.mkdtemp(prefix="pi-runtime-suite-")
    os.makedirs(tempDir, exist_ok=True)

    faux = registerFauxProvider({"models": [{"id": "faux-1", "reasoning": True}, {"id": "faux-2", "reasoning": False}]})
    faux.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two"), fauxAssistantMessage("three")])

    def wrapped(pi) -> None:
        _fauxProviderFactory(faux)(pi)
        extensionFactory(pi)

    runtimeOptions = {
        "agentDir": tempDir,
        "model": faux.getModel() if bootstrapModel else None,
        "thinkingLevel": None,
        "resourceLoaderOptions": _loaderOptions(wrapped),
    }
    runtime = await createAgentSessionRuntime(_runtimeFactory(runtimeOptions), {
        "cwd": tempDir,
        "agentDir": tempDir,
        "sessionManager": SessionManager.create(tempDir),
    })
    await runtime.session.bindExtensions({})

    async def cleanup() -> None:
        await runtime.dispose()
        faux.unregister()
        shutil.rmtree(tempDir, ignore_errors=True)

    cleanups.append(cleanup)
    return runtime, faux, tempDir


def _noop(_pi) -> None:
    pass


async def test_persists_message_end_assistant_replacements_to_the_session_manager(cleanups):
    def factory(pi) -> None:
        async def onMessageEnd(event, _ctx):
            message = event["message"]
            if _role(message) != "assistant":
                return None
            cost = message.usage.cost.model_copy(update={"total": 0.123})
            return {"message": message.model_copy(update={"usage": message.usage.model_copy(update={"cost": cost})})}

        pi.on("message_end", onMessageEnd)

    runtime, _faux, _tempDir = await createRuntimeForTest(cleanups, factory)

    await runtime.session.prompt("hello")

    sessionAssistant = next((message for message in runtime.session.messages if _role(message) == "assistant"), None)
    assert sessionAssistant is not None
    assert sessionAssistant.usage.cost.total == 0.123

    persistedAssistant = next(
        (
            entry["message"]
            for entry in runtime.session.sessionManager.getEntries()
            if entry["type"] == "message" and _role(entry["message"]) == "assistant"
        ),
        None,
    )
    assert persistedAssistant is not None
    assert read_field(read_field(read_field(persistedAssistant, "usage"), "cost"), "total") == 0.123


async def test_settles_the_active_response_before_session_replacement(cleanups):
    toolStarted = asyncio.Event()

    def factory(pi) -> None:
        async def execute(_toolCallId, _params, signal=None, *_rest):
            toolStarted.set()
            await wait_for_abort(signal)
            return AgentToolResult(content=[TextContent(text="tool aborted")], details={})

        pi.registerTool(ToolDefinition(
            name="block",
            label="Block",
            description="Blocks until aborted",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        ))

    runtime, faux, _tempDir = await createRuntimeForTest(cleanups, factory)

    await runtime.session.prompt("hello")
    firstSessionFile = runtime.session.sessionFile
    await runtime.newSession()
    await runtime.session.bindExtensions({})

    faux.setResponses([fauxAssistantMessage(fauxToolCall("block", {}), {"stopReason": "toolUse"})])
    outgoingSession = runtime.session
    promptPromise = asyncio.ensure_future(outgoingSession.prompt("start blocking tool"))
    await toolStarted.wait()

    switchResult = await runtime.switchSession(firstSessionFile)
    await promptPromise

    assert switchResult["cancelled"] is False
    assert runtime.session.sessionFile == firstSessionFile
    # The outgoing session settled before replacement: the interrupted tool
    # call has a persisted tool result instead of dangling forever.
    outgoingEntries = [
        entry for entry in SessionManager.open(outgoingSession.sessionFile).getEntries() if entry["type"] == "message"
    ]
    assert [_role(entry["message"]) for entry in outgoingEntries] == [
        "system",
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ]


async def test_preserves_an_existing_session_when_importing_a_file_with_the_same_name(cleanups):
    runtime, _faux, tempDir = await createRuntimeForTest(cleanups, _noop)
    sessionDir = runtime.session.sessionManager.getSessionDir()
    importDir = os.path.join(tempDir, "import")
    filename = "collision.jsonl"
    storedPath = os.path.join(sessionDir, filename)
    importPath = os.path.join(importDir, filename)
    now = datetime.now(UTC).isoformat()
    storedSession = json.dumps({"type": "session", "version": 3, "id": "stored", "timestamp": now, "cwd": tempDir}) + "\n"
    importedSession = (
        json.dumps({"type": "session", "version": 3, "id": "imported", "timestamp": now, "cwd": tempDir}) + "\n"
    )
    os.makedirs(sessionDir, exist_ok=True)
    os.makedirs(importDir, exist_ok=True)
    Path(storedPath).write_text(storedSession, encoding="utf-8")
    Path(importPath).write_text(importedSession, encoding="utf-8")

    await runtime.importFromJsonl(importPath)

    assert Path(storedPath).read_text(encoding="utf-8") == storedSession
    assert runtime.session.sessionFile != storedPath
    assert '"id":"imported"' in Path(runtime.session.sessionFile).read_text(encoding="utf-8").replace(" ", "")


def _recorder(events: list, *names: str):
    def factory(pi) -> None:
        for name in names:
            async def record(event, _ctx, _name=name):
                events.append(dict(event))

            pi.on(name, record)

    return factory


async def test_emits_session_before_switch_and_session_start_for_new_and_resume_flows(cleanups):
    events: list = []
    runtime, _faux, _tempDir = await createRuntimeForTest(
        cleanups, _recorder(events, "session_before_switch", "session_shutdown", "session_start")
    )

    assert events == [{"type": "session_start", "reason": "startup"}]
    events.clear()

    await runtime.session.prompt("hello")
    originalSessionFile = runtime.session.sessionFile
    originalSession = runtime.session

    newSessionResult = await runtime.newSession()
    assert newSessionResult["cancelled"] is False
    await runtime.session.bindExtensions({})
    assert runtime.session is not originalSession
    assert runtime.session.messages == []
    secondSessionFile = runtime.session.sessionFile
    assert events == [
        {"type": "session_before_switch", "reason": "new", "targetSessionFile": None},
        {"type": "session_shutdown", "reason": "new", "targetSessionFile": secondSessionFile},
        {"type": "session_start", "reason": "new", "previousSessionFile": originalSessionFile},
    ]

    events.clear()

    switchResult = await runtime.switchSession(originalSessionFile)
    assert switchResult["cancelled"] is False
    await runtime.session.bindExtensions({})
    assert events == [
        {"type": "session_before_switch", "reason": "resume", "targetSessionFile": originalSessionFile},
        {"type": "session_shutdown", "reason": "resume", "targetSessionFile": originalSessionFile},
        {"type": "session_start", "reason": "resume", "previousSessionFile": secondSessionFile},
    ]


async def test_honors_session_before_switch_cancellation_for_new_and_resume(cleanups, tmp_path):
    events: list = []
    cancelReason: list = [None]

    def factory(pi) -> None:
        async def beforeSwitch(event, _ctx):
            events.append(dict(event))
            if event["reason"] == cancelReason[0]:
                return {"cancel": True}
            return None

        async def onStart(event, _ctx):
            events.append(dict(event))

        pi.on("session_before_switch", beforeSwitch)
        pi.on("session_start", onStart)

    runtime, _faux, _tempDir = await createRuntimeForTest(cleanups, factory)

    await runtime.session.prompt("hello")
    originalSessionFile = runtime.session.sessionFile

    cancelReason[0] = "new"
    newResult = await runtime.newSession()
    assert newResult["cancelled"] is True
    assert runtime.session.sessionFile == originalSessionFile

    events.clear()
    otherDir = str(tmp_path / "other")
    os.makedirs(otherDir, exist_ok=True)
    otherSession = SessionManager.create(otherDir)
    otherSession.appendMessage(
        {"role": "user", "content": [{"type": "text", "text": "other"}], "timestamp": int(time.time() * 1000)}
    )
    otherSessionFile = otherSession.getSessionFile()
    cancelReason[0] = "resume"
    resumeResult = await runtime.switchSession(otherSessionFile)
    assert resumeResult["cancelled"] is True
    assert runtime.session.sessionFile == originalSessionFile


async def test_emits_session_before_fork_and_session_start_and_honors_cancellation(cleanups):
    events: list = []
    cancelNextFork = [False]

    def factory(pi) -> None:
        async def beforeFork(event, _ctx):
            events.append(dict(event))
            if cancelNextFork[0]:
                cancelNextFork[0] = False
                return {"cancel": True}
            return None

        async def record(event, _ctx):
            events.append(dict(event))

        pi.on("session_before_fork", beforeFork)
        pi.on("session_shutdown", record)
        pi.on("session_start", record)

    runtime, _faux, _tempDir = await createRuntimeForTest(cleanups, factory)

    events.clear()
    await runtime.session.prompt("hello")
    userMessage = runtime.session.getUserMessagesForForking()[0]
    previousSessionFile = runtime.session.sessionFile

    successResult = await runtime.fork(userMessage["entryId"])
    assert successResult["cancelled"] is False
    assert successResult["selectedText"] == "hello"
    await runtime.session.bindExtensions({})
    assert events == [
        {"type": "session_before_fork", "entryId": userMessage["entryId"], "position": "before"},
        {"type": "session_shutdown", "reason": "fork", "targetSessionFile": runtime.session.sessionFile},
        {"type": "session_start", "reason": "fork", "previousSessionFile": previousSessionFile},
    ]
    sessionFileName = Path(runtime.session.sessionFile).stem
    assert sessionFileName.endswith(f"_{runtime.session.sessionId}")

    events.clear()
    cancelNextFork[0] = True
    cancelResult = await runtime.fork(userMessage["entryId"])
    assert cancelResult == {"cancelled": True}
    assert events == [{"type": "session_before_fork", "entryId": userMessage["entryId"], "position": "before"}]

    events.clear()
    cancelNextFork[0] = True
    cancelAtResult = await runtime.fork("missing-entry", {"position": "at"})
    assert cancelAtResult == {"cancelled": True}
    assert events == [{"type": "session_before_fork", "entryId": "missing-entry", "position": "at"}]


async def test_reports_why_an_unflushed_session_cannot_be_forked(cleanups):
    runtime, _faux, _tempDir = await createRuntimeForTest(cleanups, _noop)
    sessionFile = runtime.session.sessionFile
    leafId = runtime.session.sessionManager.getLeafId()
    assert sessionFile is not None
    assert not os.path.exists(sessionFile)
    assert leafId

    with pytest.raises(Exception, match="This session has not been saved yet. Send a message before cloning or forking it."):
        await runtime.fork(leafId, {"position": "at"})


async def test_duplicates_the_current_active_branch_when_forking_at_the_current_position(cleanups):
    runtime, _faux, _tempDir = await createRuntimeForTest(cleanups, _noop)
    await runtime.session.prompt("hello")
    await runtime.session.prompt("again")

    beforeMessages = [_messageView(message) for message in runtime.session.messages]
    previousSessionFile = runtime.session.sessionFile
    leafId = runtime.session.sessionManager.getLeafId()
    assert leafId

    result = await runtime.fork(leafId, {"position": "at"})
    assert result == {"cancelled": False, "selectedText": None}
    assert runtime.session.sessionFile != previousSessionFile
    assert [_messageView(message) for message in runtime.session.messages] == beforeMessages


async def test_duplicates_the_current_active_branch_in_memory_when_forking_at_the_current_position(cleanups):
    tempDir = tempfile.mkdtemp(prefix="pi-runtime-suite-in-memory-")

    faux = registerFauxProvider({"models": [{"id": "faux-1", "reasoning": True}, {"id": "faux-2", "reasoning": False}]})
    faux.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two"), fauxAssistantMessage("three")])

    runtimeOptions = {
        "agentDir": tempDir,
        "model": faux.getModel(),
        "resourceLoaderOptions": _loaderOptions(_fauxProviderFactory(faux)),
    }
    runtime = await createAgentSessionRuntime(_runtimeFactory(runtimeOptions), {
        "cwd": tempDir,
        "agentDir": tempDir,
        "sessionManager": SessionManager.inMemory(tempDir),
    })
    await runtime.session.bindExtensions({})

    async def cleanup() -> None:
        await runtime.dispose()
        faux.unregister()
        shutil.rmtree(tempDir, ignore_errors=True)

    cleanups.append(cleanup)

    await runtime.session.prompt("hello")
    await runtime.session.prompt("again")

    beforeMessages = [_messageView(message) for message in runtime.session.messages]
    leafId = runtime.session.sessionManager.getLeafId()
    assert leafId
    assert runtime.session.sessionFile is None

    result = await runtime.fork(leafId, {"position": "at"})
    assert result == {"cancelled": False, "selectedText": None}
    assert runtime.session.sessionFile is None
    assert [_messageView(message) for message in runtime.session.messages] == beforeMessages


async def test_throws_when_forking_with_an_invalid_entry_id(cleanups):
    runtime, _faux, _tempDir = await createRuntimeForTest(cleanups, _noop)
    with pytest.raises(Exception, match="Invalid entry ID for forking"):
        await runtime.fork("missing-entry")


async def _otherRuntime(cleanups: list, faux: Any, tempDir: str, cwd: str):
    otherRuntimeOptions = {"agentDir": tempDir, "resourceLoaderOptions": _loaderOptions(_fauxProviderFactory(faux))}
    otherRuntime = await createAgentSessionRuntime(_runtimeFactory(otherRuntimeOptions), {
        "cwd": cwd,
        "agentDir": tempDir,
        "sessionManager": SessionManager.create(cwd),
    })

    async def cleanup() -> None:
        await otherRuntime.dispose()

    cleanups.append(cleanup)
    return otherRuntime


async def test_updates_the_runtime_session_cwd_on_cross_cwd_session_replacement(cleanups, tmp_path):
    firstDir = str(tmp_path / "cwd-a")
    secondDir = str(tmp_path / "cwd-b")
    os.makedirs(firstDir, exist_ok=True)
    os.makedirs(secondDir, exist_ok=True)
    runtime, faux, tempDir = await createRuntimeForTest(cleanups, _noop, cwd=firstDir)
    otherRuntime = await _otherRuntime(cleanups, faux, tempDir, secondDir)
    await otherRuntime.session.prompt("other")
    otherSessionFile = otherRuntime.session.sessionFile

    await runtime.switchSession(otherSessionFile)

    assert os.path.realpath(runtime.session.sessionManager.getCwd()) == os.path.realpath(secondDir)
    assert os.path.realpath(runtime.cwd) == os.path.realpath(secondDir)


async def test_restores_model_and_thinking_state_from_the_destination_session(cleanups):
    runtime, faux, tempDir = await createRuntimeForTest(cleanups, _noop, bootstrapModel=False)
    otherDir = os.path.join(tempDir, "other")
    os.makedirs(otherDir, exist_ok=True)
    otherRuntime = await _otherRuntime(cleanups, faux, tempDir, otherDir)
    await otherRuntime.session.setModel(faux.getModel("faux-2"))
    otherRuntime.session.setThinkingLevel("off")
    await otherRuntime.session.prompt("hello")
    targetSessionFile = otherRuntime.session.sessionFile

    await runtime.switchSession(targetSessionFile)

    assert runtime.session.model.id == "faux-2"
    assert runtime.session.thinkingLevel == "off"
