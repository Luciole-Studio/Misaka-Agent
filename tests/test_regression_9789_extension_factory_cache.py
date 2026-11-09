"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``9789-context-handler-system-messages.test.ts``
and ``extension-factory-cache.test.ts`` (v1.1.0)."""

from __future__ import annotations

import builtins
import re
from pathlib import Path
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.utils.transcript import getCurrentSystemPrompt, getCurrentTools
from misaka.core.extensions.loader import (
    clearExtensionCache,
    loadExtensions,
    loadExtensionsCached,
)
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.utils.values import read_field
from tests.suite_harness import createHarness

# --- context handlers and system messages -------------------------------------------------------


def compactViaHook(pi: Any) -> None:
    """Compaction supplied by an extension hook, as in the reported sessions."""

    async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
        preparation = read_field(event, "preparation")
        return {
            "compaction": {
                "summary": "extension summary",
                "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                "tokensBefore": read_field(preparation, "tokensBefore"),
                "details": {"source": "test"},
            }
        }

    pi.on("session_before_compact", onBeforeCompact)


def captureRequest(harness: Any, text: str) -> Any:
    captured: dict[str, Any] = {}

    def respond(context: Any, *_: Any) -> Any:
        captured["request"] = context
        return fauxAssistantMessage(text)

    harness.setResponses([respond])

    def getRequest() -> Any:
        if "request" not in captured:
            raise AssertionError("expected a provider request")
        return captured["request"]

    return getRequest


def roles(messages: list[Any]) -> list[str]:
    return [read_field(message, "role") for message in messages]


def toolNames(context: Any) -> list[str]:
    return [tool.name for tool in getCurrentTools(context.messages)]


async def compactSession(harness: Any) -> None:
    harness.settingsManager.applyOverrides({"compaction": {"keepRecentTokens": 1}})
    harness.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])
    await harness.session.prompt("first")
    await harness.session.prompt("second")
    await harness.session.compact()
    assert roles(harness.session.messages)[:2] == ["system", "compactionSummary"]


def _sliceFromSummary(messages: list[Any]) -> list[Any]:
    summary = next(
        (index for index, message in enumerate(messages) if read_field(message, "role") == "compactionSummary"), -1
    )
    return messages[summary:]


# Regression #9789, #9822: pruning from the compaction summary dropped the prompt and tool checkpoint.
async def test_keeps_the_prompt_and_tools_when_a_handler_slices_from_the_compaction_summary():
    seen: list[list[Any]] = []

    def extension(pi: Any) -> None:
        async def onContext(event: Any, _ctx: Any) -> Any:
            messages = read_field(event, "messages")
            seen.append(messages)
            return {"messages": _sliceFromSummary(messages)}

        pi.on("context", onContext)

    async with createHarness(extensionFactories=[compactViaHook, extension]) as harness:
        await compactSession(harness)
        getRequest = captureRequest(harness, "after compaction")

        await harness.session.prompt("third")

        request = getRequest()
        assert not any(read_field(message, "role") == "system" for message in seen[-1])
        assert read_field(request.messages[0], "role") == "system"
        assert toolNames(request) == harness.session.getActiveToolNames()
        assert getCurrentSystemPrompt(request.messages) == harness.session.systemPrompt
        assert len([message for message in request.messages if read_field(message, "role") == "system"]) == 1


async def test_keeps_mid_conversation_system_messages_in_place_when_a_handler_leaves_the_conversation_unchanged():
    state = {"turn": 0}

    def extension(pi: Any) -> None:
        def beforeAgentStart(event: Any, _ctx: Any) -> None:
            state["turn"] += 1
            if state["turn"] == 2:
                read_field(read_field(event, "systemPromptOptions"), "sections")["plan_mode"] = "Plan only."

        async def onContext(event: Any, _ctx: Any) -> Any:
            return {"messages": read_field(event, "messages")}

        pi.on("before_agent_start", beforeAgentStart)
        pi.on("context", onContext)

    async with createHarness(extensionFactories=[extension]) as harness:
        harness.setResponses([fauxAssistantMessage("one")])
        await harness.session.prompt("first")
        getRequest = captureRequest(harness, "two")

        await harness.session.prompt("second")

        systemMessages = [message for message in getRequest().messages if read_field(message, "role") == "system"]
        assert len(systemMessages) == 2
        assert read_field(systemMessages[1], "sections") == {"plan_mode": "<plan_mode>\nPlan only.\n</plan_mode>"}


async def test_applies_in_place_edits_to_event_messages_without_a_return_value():
    def extension(pi: Any) -> None:
        async def onContext(event: Any, _ctx: Any) -> None:
            read_field(event, "messages").insert(
                0, {"role": "user", "content": [{"type": "text", "text": "injected"}], "timestamp": 0}
            )

        pi.on("context", onContext)

    async with createHarness(extensionFactories=[extension]) as harness:
        getRequest = captureRequest(harness, "done")

        await harness.session.prompt("hello")

        request = getRequest()
        assert roles(request.messages) == ["system", "user", "user"]
        assert toolNames(request) == harness.session.getActiveToolNames()


async def test_keeps_system_messages_a_handler_adds_after_the_replayed_head():
    def extension(pi: Any) -> None:
        async def onContext(event: Any, _ctx: Any) -> Any:
            return {
                "messages": [
                    {"role": "system", "content": "ephemeral reminder", "timestamp": 0},
                    *read_field(event, "messages"),
                ]
            }

        pi.on("context", onContext)

    async with createHarness(extensionFactories=[extension]) as harness:
        getRequest = captureRequest(harness, "done")

        await harness.session.prompt("hello")

        request = getRequest()
        assert roles(request.messages) == ["system", "system", "user"]
        assert toolNames(request) == harness.session.getActiveToolNames()
        assert harness.session.systemPrompt in getCurrentSystemPrompt(request.messages)
        assert "ephemeral reminder" in getCurrentSystemPrompt(request.messages)


# --- context_with_system handlers ---------------------------------------------------------------


async def test_runs_after_context_handlers_on_the_restored_transcript_and_sends_its_output_verbatim():
    seen: list[list[Any]] = []

    def extension(pi: Any) -> None:
        async def onContextWithSystem(event: Any, _ctx: Any) -> Any:
            messages = read_field(event, "messages")
            seen.append(messages)

            def withoutBash(message: Any) -> Any:
                toolsAdded = read_field(message, "toolsAdded")
                if read_field(message, "role") != "system" or not toolsAdded:
                    return message
                kept = [tool for tool in toolsAdded if read_field(tool, "name") != "bash"]
                if hasattr(message, "model_copy"):
                    return message.model_copy(update={"toolsAdded": kept})
                return {**message, "toolsAdded": kept}

            return {"messages": [withoutBash(message) for message in messages]}

        async def onContext(event: Any, _ctx: Any) -> Any:
            return {"messages": _sliceFromSummary(read_field(event, "messages"))}

        pi.on("context_with_system", onContextWithSystem)
        # Registered after, but runs first: context handlers precede context_with_system.
        pi.on("context", onContext)

    async with createHarness(extensionFactories=[compactViaHook, extension]) as harness:
        await compactSession(harness)
        getRequest = captureRequest(harness, "after compaction")

        await harness.session.prompt("third")

        input = seen[-1]
        assert read_field(input[0], "role") == "system"
        assert read_field(input[1], "role") == "compactionSummary"
        assert "bash" in harness.session.getActiveToolNames()
        assert toolNames(getRequest()) == [name for name in harness.session.getActiveToolNames() if name != "bash"]


async def test_reports_a_handler_that_drops_the_leading_system_message_but_honors_its_output():
    def extension(pi: Any) -> None:
        async def onContextWithSystem(event: Any, _ctx: Any) -> Any:
            return {
                "messages": [message for message in read_field(event, "messages") if read_field(message, "role") != "system"]
            }

        pi.on("context_with_system", onContextWithSystem)

    async with createHarness(extensionFactories=[extension]) as harness:
        errors: list[str] = []
        harness.session.extensionRunner.onError(
            lambda error: errors.append(f"{read_field(error, 'event')}: {read_field(error, 'error')}")
        )
        getRequest = captureRequest(harness, "done")

        await harness.session.prompt("hello")

        assert roles(getRequest().messages) == ["user"]
        assert len(errors) == 1
        assert re.match(r"^context_with_system: Handler removed the leading system message", errors[0])


# --- extension factory cache --------------------------------------------------------------------
#
# MISAKA fork: pi's counting extension keeps its counters on `globalThis`; a MISAKA extension is a Python module, and
# `builtins` is the namespace every module shares.

STATE_NAME = "__extensionFactoryCacheTest"


def state() -> dict[str, int]:
    return builtins.__dict__.setdefault(STATE_NAME, {})


def resetState() -> None:
    builtins.__dict__.pop(STATE_NAME, None)


def writeCountingExtension(filePath: Path) -> None:
    filePath.write_text(
        f"""
import builtins

state = builtins.__dict__.setdefault("{STATE_NAME}", {{}})
state["moduleLoads"] = state.get("moduleLoads", 0) + 1


def default(_pi):
    state["factoryRuns"] = state.get("factoryRuns", 0) + 1
""",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def extensionCache():
    resetState()
    clearExtensionCache()
    yield
    resetState()
    clearExtensionCache()


def fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    cwd = tmp_path / "project"
    agentDir = tmp_path / "agent"
    cwd.mkdir(parents=True)
    agentDir.mkdir(parents=True)
    return tmp_path, cwd, agentDir


async def test_caches_extension_modules_for_cached_same_cwd_loads_but_reruns_factories(tmp_path: Path):
    root, cwd, _ = fixture(tmp_path)
    extensionPath = root / "counting.py"
    writeCountingExtension(extensionPath)

    first = await loadExtensionsCached([str(extensionPath)], str(cwd))
    second = await loadExtensionsCached([str(extensionPath)], str(cwd))

    assert state().get("moduleLoads") == 1
    assert state().get("factoryRuns") == 2
    assert first.extensions[0] is not second.extensions[0]
    assert first.runtime is not second.runtime


async def test_does_not_cache_direct_load_extensions_calls(tmp_path: Path):
    root, cwd, _ = fixture(tmp_path)
    extensionPath = root / "counting.py"
    writeCountingExtension(extensionPath)

    await loadExtensions([str(extensionPath)], str(cwd))
    await loadExtensions([str(extensionPath)], str(cwd))

    assert state().get("moduleLoads") == 2
    assert state().get("factoryRuns") == 2


async def test_clears_the_cache_on_resource_loader_reload(tmp_path: Path):
    _, cwd, agentDir = fixture(tmp_path)
    extensionDir = agentDir / "extensions"
    extensionDir.mkdir(parents=True)
    writeCountingExtension(extensionDir / "counting.py")
    loader = DefaultResourceLoader({
        "cwd": str(cwd),
        "agentDir": str(agentDir),
        "noSkills": True,
        "noPromptTemplates": True,
        "noThemes": True,
    })

    await loader.reload()
    await loader.reload()

    assert state().get("moduleLoads") == 2
    assert state().get("factoryRuns") == 2


async def test_keeps_the_cache_scoped_to_one_cwd(tmp_path: Path):
    root, _, _ = fixture(tmp_path)
    firstCwd = root / "first"
    secondCwd = root / "second"
    firstCwd.mkdir(parents=True)
    secondCwd.mkdir(parents=True)
    extensionPath = root / "counting.py"
    writeCountingExtension(extensionPath)

    await loadExtensionsCached([str(extensionPath)], str(firstCwd))
    await loadExtensionsCached([str(extensionPath)], str(secondCwd))
    await loadExtensionsCached([str(extensionPath)], str(secondCwd))

    assert state().get("moduleLoads") == 2
    assert state().get("factoryRuns") == 3
