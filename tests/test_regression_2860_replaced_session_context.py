"""Translated from pi's ``packages/coding-agent/test/suite/regressions/2860-replaced-session-context.test.ts`` (v1.1.0)."""

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
from tests.suite_harness import getMessageText


@pytest.fixture
async def cleanups():
    pending: list[Any] = []
    yield pending
    while pending:
        await pending.pop()()


async def createRuntimeForTest(cleanups: list[Any], extensionFactory: Any, responses: list[str]) -> Any:
    tempDir = tempfile.mkdtemp(prefix="pi-2860-")
    faux = registerFauxProvider({"models": [{"id": "faux-1", "reasoning": False}]})
    faux.setResponses([fauxAssistantMessage(response) for response in responses])

    authStorage = AuthStorage.inMemory()

    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="faux-key")

    await authStorage.modify(faux.getModel().provider, apiKey)
    modelRuntime = await ModelRuntime.create({"authStorage": authStorage, "modelsPath": os.path.join(tempDir, "models.json")})

    def wrapped(pi: Any) -> None:
        pi.registerProvider(faux.getModel().provider, {
            "baseUrl": faux.getModel().baseUrl,
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
        extensionFactory(pi)

    async def createRuntime(options: Any) -> CreateAgentSessionRuntimeResult:
        services = await createAgentSessionServices({
            "cwd": options["cwd"],
            "agentDir": tempDir,
            "modelRuntime": modelRuntime,
            "resourceLoaderOptions": {
                "extensionFactories": [wrapped],
                "noSkills": True,
                "noPromptTemplates": True,
                "noThemes": True,
            },
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

    runtime = await createAgentSessionRuntime(
        createRuntime, {"cwd": tempDir, "agentDir": tempDir, "sessionManager": SessionManager.create(tempDir)}
    )

    async def rebindSession() -> None:
        session = runtime.session

        async def navigateTree(targetId: str, options: Any = None) -> Any:
            result = await session.navigateTree(targetId, {
                "summarize": read_field(options, "summarize"),
                "customInstructions": read_field(options, "customInstructions"),
                "replaceInstructions": read_field(options, "replaceInstructions"),
                "label": read_field(options, "label"),
            })
            return {"cancelled": read_field(result, "cancelled")}

        async def fork(entryId: str, options: Any = None) -> Any:
            result = await runtime.fork(entryId, options)
            return {"cancelled": read_field(result, "cancelled")}

        async def reload() -> None:
            await session.reload()

        await session.bindExtensions({
            "commandContextActions": {
                "waitForIdle": lambda: session.agent.waitForIdle(),
                "newSession": lambda options=None: runtime.newSession(options),
                "fork": fork,
                "navigateTree": navigateTree,
                "switchSession": lambda sessionPath, options=None: runtime.switchSession(sessionPath, options),
                "reload": reload,
            }
        })

    runtime.setRebindSession(lambda _session: rebindSession())
    await rebindSession()

    async def cleanup() -> None:
        await runtime.dispose()
        faux.unregister()
        shutil.rmtree(tempDir, ignore_errors=True)

    cleanups.append(cleanup)
    return runtime


def _transcript(runtime: Any) -> list[str]:
    return [
        f"{read_field(message, 'role')}:{getMessageText(message)}"
        for message in runtime.session.messages
        if read_field(message, "role") != "system"
    ]


# --- regression #2860: replaced session callbacks -----------------------------------------------


async def test_rebinds_before_with_session_targets_the_replacement_session_and_invalidates_stale_pi_ctx(cleanups):
    events: list[str] = []
    state: dict[str, Any] = {"instance": 0, "staleCtxThrows": False, "stalePiThrows": False}

    def extension(pi: Any) -> None:
        state["instance"] += 1
        currentInstance = state["instance"]
        pi.on("session_start", lambda _event, _ctx: events.append(f"start:{currentInstance}"))
        pi.on("session_shutdown", lambda _event, _ctx: events.append(f"shutdown:{currentInstance}"))

        async def handler(_args: str, ctx: Any) -> None:
            oldCtx, oldPi = ctx, pi
            state["oldSessionFile"] = ctx.sessionManager.getSessionFile()

            async def withSession(replacedCtx: Any) -> None:
                events.append(f"with:{currentInstance}")
                state["replacementSessionFile"] = replacedCtx.sessionManager.getSessionFile()
                try:
                    oldCtx.sessionManager.getSessionFile()
                except Exception:  # noqa: BLE001
                    state["staleCtxThrows"] = True
                try:
                    oldPi.sendUserMessage("stale message")
                except Exception:  # noqa: BLE001
                    state["stalePiThrows"] = True
                await replacedCtx.sendUserMessage("Hello from the new session!")

            await ctx.newSession({"parentSession": state["oldSessionFile"], "withSession": withSession})

        pi.registerCommand("repro", {"description": "repro", "handler": handler})

    runtime = await createRuntimeForTest(cleanups, extension, ["hello reply"])

    assert events == ["start:1"]

    await runtime.session.prompt("/repro")

    assert events == ["start:1", "shutdown:1", "start:2", "with:1"]
    assert state.get("replacementSessionFile") is not None
    assert state["replacementSessionFile"] != state["oldSessionFile"]
    assert state["staleCtxThrows"] is True
    assert state["stalePiThrows"] is True
    assert _transcript(runtime) == ["user:Hello from the new session!", "assistant:hello reply"]


async def test_supports_with_session_for_fork(cleanups):
    def extension(pi: Any) -> None:
        async def handler(_args: str, ctx: Any) -> None:
            leafId = ctx.sessionManager.getLeafId()
            if not leafId:
                raise RuntimeError("Missing leaf id")

            async def withSession(replacedCtx: Any) -> None:
                await replacedCtx.sendUserMessage("fork callback message")

            await ctx.fork(leafId, {"position": "at", "withSession": withSession})

        pi.registerCommand("fork-it", {"description": "fork-it", "handler": handler})

    runtime = await createRuntimeForTest(cleanups, extension, ["seed reply", "fork reply"])

    await runtime.session.prompt("seed")
    await runtime.session.prompt("/fork-it")

    assert _transcript(runtime) == ["user:seed", "assistant:seed reply", "user:fork callback message", "assistant:fork reply"]


async def test_supports_with_session_for_switch_session(cleanups):
    target: dict[str, str] = {"path": ""}

    def extension(pi: Any) -> None:
        async def handler(_args: str, ctx: Any) -> None:
            async def withSession(replacedCtx: Any) -> None:
                await replacedCtx.sendUserMessage("switch callback message")

            await ctx.switchSession(target["path"], {"withSession": withSession})

        pi.registerCommand("switch-it", {"description": "switch-it", "handler": handler})

    runtime = await createRuntimeForTest(cleanups, extension, ["root reply", "target reply", "switch reply"])

    await runtime.session.prompt("root")
    originalSessionPath = runtime.session.sessionFile
    newSessionResult = await runtime.newSession()
    assert read_field(newSessionResult, "cancelled") is False
    await runtime.session.prompt("target")
    target["path"] = runtime.session.sessionFile
    await runtime.switchSession(originalSessionPath)

    await runtime.session.prompt("/switch-it")

    assert runtime.session.sessionFile == target["path"]
    assert _transcript(runtime) == [
        "user:target",
        "assistant:target reply",
        "user:switch callback message",
        "assistant:switch reply",
    ]
