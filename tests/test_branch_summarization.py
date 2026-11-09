"""Translated from pi's ``packages/coding-agent/test/branch-summarization.test.ts`` and
``branch-summary-extensions.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import AssistantMessage, DoneEvent, Model
from misaka.ai.utils.event_stream import createAssistantMessageEventStream
from misaka.core.compaction import generateBranchSummary
from misaka.core.compaction.branch_summarization import GenerateBranchSummaryOptions
from misaka.modes.json_event import to_jsonable
from misaka.utils.js import AbortController
from misaka.utils.values import read_field
from tests.suite_harness import createHarness
from tests.utilities import assistantMsg, userMsg

model = Model.model_validate({
    "id": "test-model",
    "name": "Test Model",
    "api": "anthropic-messages",
    "provider": "anthropic",
    "baseUrl": "https://api.anthropic.com",
    "reasoning": False,
    "input": ["text"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": 200000,
    "maxTokens": 8192,
})

entries = [
    {
        "type": "message",
        "id": "branch-user",
        "parentId": None,
        "timestamp": "1970-01-01T00:00:00.001Z",
        "message": {"role": "user", "content": "Abandoned request", "timestamp": 1},
    }
]


def response(content: list[dict[str, Any]], **extra: Any) -> AssistantMessage:
    return AssistantMessage.model_validate({
        **to_jsonable(fauxAssistantMessage("")),
        "content": content,
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        **extra,
    })


def respondingStreamFn(message: AssistantMessage, reason: str = "stop", captured: dict[str, Any] | None = None) -> Any:
    def streamFn(_model: Any, _context: Any, options: Any = None) -> Any:
        if captured is not None:
            captured["options"] = options
        stream = createAssistantMessageEventStream()
        asyncio.get_running_loop().call_soon(lambda: stream.push(DoneEvent(reason=reason, message=message)))
        return stream

    return streamFn


def branchOptions(streamFn: Any, summaryModel: Model = model) -> GenerateBranchSummaryOptions:
    return GenerateBranchSummaryOptions(
        model=summaryModel, apiKey=None, signal=AbortController().signal, streamFn=streamFn
    )


# --- branch summarization -----------------------------------------------------------------------


async def test_does_not_override_tool_choice_for_branch_summaries():
    captured: dict[str, Any] = {}

    await generateBranchSummary(
        entries, branchOptions(respondingStreamFn(response([{"type": "text", "text": "summary"}]), captured=captured))
    )

    assert read_field(captured["options"], "maxTokens") == 4096
    assert read_field(captured["options"], "toolChoice") is None


async def test_clamps_the_branch_summary_output_cap_to_the_model_limit():
    captured: dict[str, Any] = {}

    await generateBranchSummary(
        entries,
        branchOptions(
            respondingStreamFn(response([{"type": "text", "text": "summary"}]), captured=captured),
            model.model_copy(update={"maxTokens": 1024}),
        ),
    )

    assert read_field(captured["options"], "maxTokens") == 1024


async def test_rejects_tool_calls_from_branch_summaries():
    message = response([{"type": "toolCall", "id": "tool-call-1", "name": "read", "arguments": {"path": "README.md"}}])

    result = await generateBranchSummary(entries, branchOptions(respondingStreamFn(message, "toolUse")))

    assert read_field(result, "error") == "Branch summarization attempted to call a tool"


async def test_rejects_length_limited_branch_summaries():
    message = response([{"type": "text", "text": "partial"}], stopReason="length")

    result = await generateBranchSummary(entries, branchOptions(respondingStreamFn(message, "length")))

    assert read_field(result, "error") == (
        "Branch summarization failed: generation hit the token cap and the summary is incomplete"
    )


# --- Branch summary extensions ------------------------------------------------------------------


async def test_persists_extension_provided_summary_usage_in_session_totals():
    usage = {
        "input": 10,
        "output": 20,
        "cacheRead": 30,
        "cacheWrite": 40,
        "totalTokens": 100,
        "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0.3, "cacheWrite": 0.4, "total": 1},
    }

    def extension(pi: Any) -> None:
        pi.on(
            "session_before_tree",
            lambda _event, _ctx: {"summary": {"summary": "Summary provided by extension", "usage": usage}},
        )

    async with createHarness(extensionFactories=[extension]) as harness:
        targetId = harness.sessionManager.appendMessage(userMsg("first branch"))
        harness.sessionManager.appendMessage(assistantMsg("first reply"))
        harness.sessionManager.appendMessage(userMsg("abandoned branch work"))
        sourceId = harness.sessionManager.appendMessage(assistantMsg("abandoned reply"))

        result = await harness.session.navigateTree(targetId, {"summarize": True})
        summaryEntry = read_field(result, "summaryEntry")

        assert read_field(summaryEntry, "type") == "branch_summary"
        # The target is the root entry, so the summary attaches to the root's parent.
        assert read_field(summaryEntry, "parentId") is None
        assert read_field(summaryEntry, "fromId") == sourceId
        assert read_field(summaryEntry, "fromHook") is True
        assert read_field(summaryEntry, "summary") == "Summary provided by extension"
        assert to_jsonable(read_field(summaryEntry, "usage")) == usage

        stats = harness.session.getSessionStats()
        assert {key: read_field(stats.tokens, key) for key in ("input", "output", "cacheRead", "cacheWrite", "total")} == {
            "input": 12,
            "output": 22,
            "cacheRead": 30,
            "cacheWrite": 40,
            "total": 104,
        }
        assert stats.cost == 1
