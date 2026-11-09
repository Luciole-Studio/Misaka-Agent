"""Translated from pi's ``packages/coding-agent/test/suite/agent-session-compaction-model-overrides.test.ts`` and
``agent-session-tool-result-images.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import ImageContent, TextContent
from misaka.core import agent_session
from misaka.utils.values import read_field
from tests.suite_harness import createHarness


def seedHistory(harness: Any, totalTokens: int = 650) -> str:
    model = harness.session.model
    recentUserId = ""
    now = int(time.time() * 1000)
    for label in ["old", "recent"]:
        recentUserId = harness.sessionManager.appendMessage({
            "role": "user",
            "content": [{"type": "text", "text": label.ljust(400, "x")}],
            "timestamp": now - 2000,
        })
        assistant = fauxAssistantMessage(label.ljust(400, "y"), {"timestamp": now - 1000})
        harness.sessionManager.appendMessage(assistant.model_copy(update={
            "api": model.api,
            "provider": model.provider,
            "model": model.id,
            "usage": assistant.usage.model_copy(update={"input": totalTokens, "totalTokens": totalTokens}),
        }))
    harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages
    return recentUserId


# Regression coverage for #8133.
@pytest.mark.parametrize("path", ["manual", "pre-prompt", "post-run", "overflow"])
async def test_uses_model_token_settings_for_compaction_and_extension_preparation(path: str):
    preparations: list[Any] = []

    def extension(pi: Any) -> None:
        def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            preparations.append(event)
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": "compacted history",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 4000}],
        tools=[],
        settings={
            "compaction": {
                "enabled": path != "manual",
                "reserveTokens": 10,
                "keepRecentTokens": 20000,
                "modelOverrides": {"faux/faux-1": {"reserveTokens": 2000, "keepRecentTokens": 150}},
            }
        },
        extensionFactories=[extension],
    ) as harness:
        recentUserId = seedHistory(harness, 2500 if path == "pre-prompt" else 650)

        if path == "manual":
            await harness.session.compact()
        else:
            harness.setResponses(
                [
                    fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "prompt is too long"}),
                    fauxAssistantMessage("recovered"),
                ]
                if path == "overflow"
                else [fauxAssistantMessage("z" * 8000 if path == "post-run" else "done")]
            )
            await harness.session.prompt("continue")

        assert len(preparations) == 1
        settings = read_field(read_field(preparations[0], "preparation"), "settings")
        assert {
            "enabled": read_field(settings, "enabled"),
            "reserveTokens": read_field(settings, "reserveTokens"),
            "keepRecentTokens": read_field(settings, "keepRecentTokens"),
        } == {"enabled": path != "manual", "reserveTokens": 2000, "keepRecentTokens": 150}
        assert read_field(preparations[0], "reason") == (
            "manual" if path == "manual" else "overflow" if path == "overflow" else "threshold"
        )
        if path in ("manual", "pre-prompt"):
            assert read_field(read_field(preparations[0], "preparation"), "firstKeptEntryId") == recentUserId
        ends = harness.eventsOfType("compaction_end")
        assert len(ends) == 1
        assert read_field(ends[0], "aborted") is False
        assert read_field(ends[0], "willRetry") is (path == "overflow")
        assert read_field(read_field(ends[0], "result"), "summary") == "compacted history"
        assert harness.getPendingResponseCount() == 0


@pytest.mark.parametrize("path", ["manual", "automatic"])
async def test_passes_resolved_budgets_to_built_in_summarization(path: str):
    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 4000, "maxTokens": 3000}],
        tools=[],
        settings={
            "compaction": {
                "reserveTokens": 10,
                "modelOverrides": {"faux/faux-1": {"reserveTokens": 2000, "keepRecentTokens": 150}},
            }
        },
    ) as harness:
        recentUserId = seedHistory(harness, 2500)
        budgets: list[Any] = []

        def summarize(_context: Any, options: Any = None, *_: Any) -> Any:
            budgets.append(read_field(options, "maxTokens"))
            return fauxAssistantMessage("built-in summary")

        harness.setResponses([summarize, *([fauxAssistantMessage("done")] if path == "automatic" else [])])
        if path == "manual":
            await harness.session.compact()
        else:
            await harness.session.prompt("continue")
        assert budgets == [1600]
        compaction = next(
            entry for entry in harness.sessionManager.getEntries() if read_field(entry, "type") == "compaction"
        )
        assert read_field(compaction, "firstKeptEntryId") == recentUserId
        assert read_field(compaction, "summary") == "built-in summary"
        assert harness.getPendingResponseCount() == 0


async def test_uses_the_newly_selected_model_without_changing_ordinary_settings():
    def extension(pi: Any) -> None:
        def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": "big model summary",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(
        models=[{"id": "small", "contextWindow": 4000}, {"id": "big", "contextWindow": 10000}],
        tools=[],
        settings={
            "compaction": {
                "reserveTokens": 10,
                "modelOverrides": {"faux/big": {"reserveTokens": 8000, "keepRecentTokens": 150}},
            }
        },
        extensionFactories=[extension],
    ) as harness:
        seedHistory(harness, 2500)
        harness.setResponses([fauxAssistantMessage("small response"), fauxAssistantMessage("big response")])
        await harness.session.prompt("continue on small")
        assert harness.eventsOfType("compaction_start") == []
        # Retain usage from the small model: the next check must use the active big model's policy.
        seedHistory(harness, 2500)
        await harness.session.setModel(harness.getModel("big"))
        await harness.session.prompt("continue on big")
        ends = harness.eventsOfType("compaction_end")
        assert len(ends) == 1
        assert read_field(read_field(ends[0], "result"), "summary") == "big model summary"
        assert harness.settingsManager.getCompactionReserveTokens() == 10
        await harness.session.setModel(harness.getModel("small"))
        assert harness.settingsManager.getCompactionReserveTokens(harness.session.model) == 10


async def test_captures_model_identity_before_awaiting_summarization_auth(monkeypatch: pytest.MonkeyPatch):
    async with createHarness(
        models=[{"id": "first"}, {"id": "second"}],
        settings={
            "compaction": {
                "modelOverrides": {
                    "faux/first": {"reserveTokens": 2000, "keepRecentTokens": 150},
                    "faux/second": {"reserveTokens": 4000, "keepRecentTokens": 20000},
                }
            }
        },
    ) as harness:
        seedHistory(harness)
        getAuth = harness.session.modelRuntime.getAuth

        async def switchingGetAuth(model: Any, *args: Any, **kwargs: Any) -> Any:
            harness.session.agent.state.model = harness.getModel("second")
            return await getAuth(model, *args, **kwargs)

        monkeypatch.setattr(harness.session.modelRuntime, "getAuth", switchingGetAuth)
        requests: list[dict[str, Any]] = []

        def summarize(_context: Any, options: Any, _state: Any, model: Any) -> Any:
            requests.append({"id": model.id, "maxTokens": read_field(options, "maxTokens")})
            return fauxAssistantMessage("summary")

        harness.setResponses([summarize])
        # MISAKA fork: the compaction source guard rejects a compaction whose model changed while it ran, where
        # pi publishes it; the summary request has already gone out with the identity captured before auth.
        with pytest.raises(RuntimeError, match="changed during compaction"):
            await harness.session.compact()
        assert requests == [{"id": "first", "maxTokens": 1600}]


# --- AgentSession tool result images ------------------------------------------------------------

TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


async def test_passes_image_settings_and_the_current_model_profile_to_tool_result_normalization(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[Any, Any]] = []

    async def normalizeToolResultImages(content: Any, options: Any) -> Any:
        calls.append((content, options))
        return content

    monkeypatch.setattr(agent_session, "normalizeToolResultImages", normalizeToolResultImages)

    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        return AgentToolResult(
            content=[TextContent(text="captured"), ImageContent(data=TINY_PNG_BASE64, mimeType="image/png")],
            details={},
        )

    screenshotTool = AgentTool(
        name="screenshot",
        label="Screenshot",
        description="Return a screenshot",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
    resizeOptions = {"maxWidth": 1200, "maxHeight": 1000, "maxBytes": 500000, "jpegQuality": 70}
    async with createHarness(tools=[screenshotTool], settings={"images": {"autoResize": False}}) as harness:
        assert harness.session.model is not None, "Expected a model"
        harness.session.model.inputLimits = {"images": {"resize": resizeOptions}}
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("screenshot", {})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("take a screenshot")

        assert len(calls) == 1
        content, options = calls[0]
        assert isinstance(content, list)
        assert read_field(options, "autoResizeImages") is False
        resolved = read_field(options, "resizeOptions")
        assert {key: read_field(resolved, key) for key in resizeOptions} == resizeOptions
