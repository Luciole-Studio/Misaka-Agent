"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``7193-event-bus-lifecycle.test.ts``,
``7253-manual-compact-during-response.test.ts``, ``7269-cli-end-of-options.test.ts`` and
``7290-json-stream-linear.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.cli.args import parseArgs
from misaka.core.event_bus import createEventBus
from misaka.core.extensions.loader import (
    createExtensionRuntime,
    loadExtensionFromFactory,
)
from misaka.core.extensions.types import LoadExtensionsResult
from misaka.modes.json_event import toJsonEvent
from misaka.utils.js import jsonStringify
from misaka.utils.values import read_field
from tests.fake_timers import flush
from tests.suite_harness import createHarness, createTestResourceLoader, getUserTexts

# --- issue #7193 extension event-bus lifecycle --------------------------------------------------


async def test_removes_extension_owned_event_bus_listeners_on_reload_and_dispose():
    eventBus = createEventBus()
    calls = {"extension": 0, "host": 0}
    api: dict[str, Any] = {}

    def factory(pi: Any) -> None:
        api.setdefault("first", pi)
        pi.events.on("reload:test", lambda *_: calls.__setitem__("extension", calls["extension"] + 1))

    eventBus.on("reload:test", lambda *_: calls.__setitem__("host", calls["host"] + 1))

    async def loadExtensions() -> LoadExtensionsResult:
        runtime = createExtensionRuntime()
        extension = await loadExtensionFromFactory(factory, os.getcwd(), eventBus, runtime)
        return LoadExtensionsResult(extensions=[extension], errors=[], runtime=runtime)

    state = {"extensionsResult": await loadExtensions()}
    resourceLoader = createTestResourceLoader()
    resourceLoader.getExtensions = lambda: state["extensionsResult"]

    async def reload(*_args: Any) -> None:
        state["extensionsResult"] = await loadExtensions()

    resourceLoader.reload = reload

    async with createHarness(resourceLoader=resourceLoader) as harness:
        await harness.session.bindExtensions({"shutdownHandler": lambda: None})

        async def emit() -> dict[str, int]:
            extensionBefore = calls["extension"]
            hostBefore = calls["host"]
            eventBus.emit("reload:test", None)
            await flush()
            return {"extension": calls["extension"] - extensionBefore, "host": calls["host"] - hostBefore}

        assert await emit() == {"extension": 1, "host": 1}
        await harness.session.reload()
        with pytest.raises(Exception, match="stale after session replacement or reload"):
            api["first"].getCommands()
        assert await emit() == {"extension": 1, "host": 1}
        await harness.session.reload()
        assert await emit() == {"extension": 1, "host": 1}

        harness.session.dispose()
        assert await emit() == {"extension": 0, "host": 1}


# --- issue #7253: manual compaction during an active response -----------------------------------


def createNoopTool() -> AgentTool:
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text="done")], details={})

    return AgentTool(
        name="noop",
        label="No-op",
        description="Return immediately",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )


async def test_persists_the_aborted_response_before_running_the_requested_manual_compaction():
    loop = asyncio.get_running_loop()
    secondResponseStarted: asyncio.Future[None] = loop.create_future()
    secondResponseReleased: asyncio.Future[None] = loop.create_future()

    def extension(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": f"{read_field(event, 'reason')} summary",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 1000, "maxTokens": 1000}],
        settings={"compaction": {"enabled": True, "reserveTokens": 200, "keepRecentTokens": 2}},
        tools=[createNoopTool()],
        extensionFactories=[extension],
    ) as harness:

        async def second(*_: Any) -> Any:
            secondResponseStarted.set_result(None)
            await secondResponseReleased
            return fauxAssistantMessage(f"second response:{'x' * 4000}")

        harness.setResponses([fauxAssistantMessage(fauxToolCall("noop", {}), {"stopReason": "toolUse"}), second])

        promptPromise = asyncio.ensure_future(harness.session.prompt("Run the tool, then continue responding."))
        await secondResponseStarted

        compactPromise = asyncio.ensure_future(harness.session.compact())
        secondResponseReleased.set_result(None)
        _, compactResult = await asyncio.gather(promptPromise, compactPromise)
        assert read_field(compactResult, "summary") == "manual summary"

        assert [read_field(event, "reason") for event in harness.eventsOfType("compaction_start")] == ["manual"]
        assert [read_field(event, "reason") for event in harness.eventsOfType("compaction_end")] == ["manual"]
        entries = harness.sessionManager.getEntries()
        abortedResponseIndex = next(
            (
                index
                for index, entry in enumerate(entries)
                if read_field(entry, "type") == "message"
                and read_field(read_field(entry, "message"), "role") == "assistant"
                and read_field(read_field(entry, "message"), "stopReason") == "aborted"
            ),
            -1,
        )
        compactionIndex = next(
            (index for index, entry in enumerate(entries) if read_field(entry, "type") == "compaction"), -1
        )
        assert abortedResponseIndex > -1
        assert compactionIndex > abortedResponseIndex
        assert len([entry for entry in entries if read_field(entry, "type") == "compaction"]) == 1


# --- issue #7269 CLI end-of-options delimiter ---------------------------------------------------


@pytest.mark.parametrize("prompt", ["- summarize the following points for me", "--answer my question briefly"])
async def test_passes_the_prompt_after_end_of_options(prompt: str):
    parsed = parseArgs(["-ne", "--no-session", "-p", "--", prompt])
    assert parsed.messages == [prompt]
    assert len(parsed.unknownFlags) == 0
    assert parsed.diagnostics == []

    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("ok")])
        await harness.session.prompt(parsed.messages[0])
        assert getUserTexts(harness) == [prompt]


def test_stops_parsing_options_while_retaining_file_handling():
    parsed = parseArgs(["--unknown-flag", "value", "--", "--provider", "openai", "-c", "@prompt.md"])

    assert parsed.unknownFlags.get("unknown-flag") == "value"
    assert parsed.provider is None
    # MISAKA fork: Args keeps `continue` as `continue_`, a bool that defaults to False where pi's is undefined.
    assert parsed.continue_ is False
    # MISAKA fork: past `--` an `@path` argument stays message text (card contracts are passed there verbatim),
    # where pi reads it as a file (`messages` without it, `fileArgs == ["prompt.md"]`).
    assert parsed.messages == ["--provider", "openai", "-c", "@prompt.md"]
    assert parsed.fileArgs == []
    assert parsed.diagnostics == []


# --- regression #7290: JSON event streams stay linear -------------------------------------------


async def measureUpdateBytes(text: str) -> int:
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage(text)])

        await harness.session.prompt("respond")

        sessionUpdates = harness.eventsOfType("message_update")
        for update in sessionUpdates:
            assert read_field(update, "message") is not None
            assert read_field(read_field(update, "assistantMessageEvent"), "partial") is not None

        updates = [toJsonEvent(event) for event in sessionUpdates]
        assert len(updates) > 0
        for update in updates:
            assert "message" not in update
            assert "partial" not in update["assistantMessageEvent"]
        return sum(len(jsonStringify(event).encode("utf-8")) for event in updates)


async def test_emits_delta_only_message_updates_whose_size_scales_linearly():
    smallBytes = await measureUpdateBytes("x" * 2_000)
    largeBytes = await measureUpdateBytes("x" * 4_000)

    assert largeBytes > smallBytes
    assert largeBytes / smallBytes < 2.2
