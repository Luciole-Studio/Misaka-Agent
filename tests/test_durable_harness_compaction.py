"""pi durable's ``test/harness-compaction.test.ts``."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import (
    fauxAssistantMessage,
    fauxText,
    fauxThinking,
    fauxToolCall,
)
from misaka.durable import (
    CompactionTask,
    LiveDoc,
    MemoryStorage,
    ProviderDoc,
    UsageDoc,
    defineTask,
    defineTool,
    watchEvents,
)
from misaka.durable.harness.ai_bridge import toMessageDict
from misaka.durable.harness.compaction import selectCut, serializeConversation
from misaka.durable.harness.context import orderToolResults
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.chat_support import (
    allEntries,
    chatSetup,
    openChat,
    requestText,
    textOf,
    toolsNamed,
    waitFor,
)
from tests.durable_support import AnyOf, ControlledStorage, context, toMatchObject
from tests.harness_support import addHooks, addSection, addTask, addTool
from tests.task_support import aborted, deferred, race

EMPTY_OBJECT = {"type": "object", "properties": {}}


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-compaction-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


def text(label: str, tokens: int) -> str:
    """Text of about ``tokens`` estimated tokens, starting with ``label``."""
    return f"{label} {'x' * max(0, tokens * 4 - len(label) - 1)}"


def isSummaryRequest(messages: list[Any]) -> bool:
    first = messages[0] if messages else None
    return (
        first is not None
        and first.role == "system"
        and isinstance(first.content, str)
        and first.content.startswith("You are a context summarization assistant")
    )


def script(setup: Any) -> SimpleNamespace:
    """Scripted faux model that answers agent requests and summarization requests from separate queues."""
    result = SimpleNamespace(agent=[], summaries=[], agentRequests=[], summaryRequests=[])

    async def step(transcript: Any, options: Any = None, state: Any = None, model: Any = None) -> Any:
        request = SimpleNamespace(messages=list(transcript.messages), options=options, model=model.id)
        summarizing = isSummaryRequest(request.messages)
        (result.summaryRequests if summarizing else result.agentRequests).append(request)
        queue = result.summaries if summarizing else result.agent
        scripted = queue.pop(0) if queue else None
        if scripted is None:
            raise RuntimeError(f"No scripted {'summary' if summarizing else 'agent'} response")
        if callable(scripted):
            value = scripted(request)
            return await value if inspect.isawaitable(value) else value
        return scripted

    setup.faux.setResponses([step] * 500)
    return result


def gated(gate: Any, message: Any, reached: Any = None) -> Callable[[Any], Any]:
    """A step that waits for ``gate``, or rejects when the request is cancelled."""

    async def step(request: Any) -> Any:
        if reached is not None:
            reached.resolve()
        await race(gate.promise, aborted(request.options.signal))
        return message

    return step


def answer(content: str) -> Any:
    return fauxAssistantMessage(content)


def failure(errorMessage: str) -> Any:
    return fauxAssistantMessage("", {"stopReason": "error", "errorMessage": errorMessage})


OVERFLOW = "prompt is too long: 250000 tokens > 200000 maximum"

# Small thresholds: no automatic compaction unless a test enables it.
MANUAL = {"enabled": False, "reserveTokens": 1000, "keepRecentTokens": 150, "backgroundTokens": 0}


async def openCompaction(
    options: dict[str, Any] | None = None, setup: Any = None, faux: Any = None
) -> SimpleNamespace:
    options = options or {}
    if setup is None:
        setup = chatSetup(
            {"models": [{"id": "faux-1", "contextWindow": options.get("contextWindow", 100_000), "maxTokens": 900}]}
        )
    if faux is None:
        faux = script(setup)
    addSection(setup.registry, "preamble", lambda *_: "You are helpful.", {"tag": False})
    opened = await openChat(options.get("storage") or MemoryStorage(), setup)
    # These tests exercise compaction accounting and thresholds, not the faux provider's cache simulation.
    setup.settings["stream"] = {"cacheRetention": "none"}
    setup.settings["compaction"] = options.get("policy") or MANUAL
    setup.settings["retry"] = {"enabled": True, "maxRetries": 2, "baseDelayMs": 1}
    opened.harness.resume()
    return SimpleNamespace(harness=opened.harness, root=opened.root, setup=setup, faux=faux)


async def turn(chat: Any, user: str, reply: str) -> None:
    """Run one turn: ``user`` answered by ``reply``."""
    chat.faux.agent.append(answer(reply))
    submission = await chat.root.submit({"type": "input", "content": user}, context)
    assert (await submission.wait(context))["status"] == "done"


async def history(chat: Any) -> None:
    """Three turns of about 100-token messages."""
    await turn(chat, text("u1", 100), text("a1", 100))
    await turn(chat, text("u2", 100), text("a2", 100))
    await turn(chat, text("u3", 100), text("a3", 100))


def summary(content: str = "SUMMARY") -> Any:
    return answer(content)


async def result(chat: Any, id: int) -> dict[str, Any]:
    record = await chat.harness.waitForTask(id, context)
    return record["state"]["outcome"]  # type: ignore[no-any-return]


def submissionIdOf(outcome: dict[str, Any]) -> Any:
    return outcome["result"]["submissionId"] if outcome["status"] == "completed" else None


async def kinds(conversation: Any) -> list[str]:
    return [entry["kind"] for entry in await allEntries(conversation)]


async def live(chat: Any) -> dict[str, Any]:
    return await chat.harness.snapshot(LiveDoc, chat.root.id, context) or {}  # type: ignore[no-any-return]


def userText(message: Any) -> str:
    if message is None:
        return ""
    if isinstance(message, dict):
        return textOf(message) or ""
    return requestText(message) or ""


def firstUserText(messages: Any) -> str:
    """Text of the first user message; a leading baseline system message comes before it."""

    def role(message: Any) -> Any:
        return message.get("role") if isinstance(message, dict) else getattr(message, "role", None)

    return userText(next((message for message in messages if role(message) == "user"), None))


# ─── Range selection ──────────────────────────────────────────────────────

nextId = 1


def entry(kind: str, model: list[dict[str, Any]], head: int | None = None) -> dict[str, Any]:
    global nextId
    record = {"id": nextId, "conversationId": 1, "kind": kind, "model": model, **({} if head is None else {"head": head})}
    nextId += 1
    return record


def user(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content, "timestamp": 0}


def assistant(content: str, calls: list[str] | None = None, stopReason: str = "stop") -> dict[str, Any]:
    message = fauxAssistantMessage([fauxText(content), *[fauxToolCall("read", {}, {"id": id}) for id in calls or []]])
    return {**toMessageDict(message), "stopReason": stopReason}


def toolResult(callId: str, content: str) -> dict[str, Any]:
    return {
        "role": "toolResult",
        "toolCallId": callId,
        "toolName": "read",
        "content": [{"type": "text", "text": content}],
        "isError": False,
        "timestamp": 0,
    }


def view(entries: list[dict[str, Any]], head: dict[str, Any] | None = None) -> dict[str, Any]:
    """A view over ``entries`` whose contributions are their models, with excluded assistants removed."""
    everything = entries if head is None else [head, *entries]
    contributions = [
        [
            message
            for message in record.get("model") or []
            if message["role"] != "assistant" or message["stopReason"] not in ("error", "aborted", "deferred")
        ]
        for record in everything
    ]
    return {
        "head": head,
        "entries": everything,
        "contributions": contributions,
        "messages": orderToolResults([message for messages in contributions for message in messages]),
    }


def test_range_selection_keeps_about_keep_recent_tokens_and_cuts_at_the_first_candidate_at_or_after_the_budget():
    entries = [
        entry("pi.user", [user(text("1", 10))]),
        entry("pi.assistant", [assistant("2", ["c1"])]),
        entry("pi.tool-result", [toolResult("c1", text("3", 3000))]),
        entry("pi.assistant", [assistant(text("4", 10))]),
        entry("pi.user", [user(text("5", 10))]),
        entry("pi.assistant", [assistant(text("6", 10))]),
    ]
    assert selectCut(view(entries), 2000) == 3


def test_range_selection_cuts_at_a_user_entry():
    entries = [
        entry("pi.user", [user(text("u1", 100))]),
        entry("pi.assistant", [assistant(text("a1", 100))]),
        entry("pi.user", [user(text("u2", 100))]),
        entry("pi.assistant", [assistant(text("a2", 100))]),
    ]
    assert selectCut(view(entries), 150) == 2


def test_range_selection_cuts_at_an_assistant_in_the_middle_of_one_long_run_and_never_at_a_tool_result():
    entries = [entry("pi.user", [user("do it")])]
    for index in range(5):
        entries.append(entry("pi.assistant", [assistant(f"step {index}", [f"c{index}"])]))
        entries.append(entry("pi.tool-result", [toolResult(f"c{index}", text(f"r{index}", 100))]))
    cut = selectCut(view(entries), 150)
    assert cut is not None
    # The budget is reached at the fourth result; the cut is the last call, whose result it keeps.
    assert entries[cut]["kind"] == "pi.assistant"
    assert cut == 9


def test_range_selection_never_cuts_at_a_display_only_entry_where_the_budget_is_reached_10407():
    entries = [
        entry("pi.user", [user(text("u1", 100))]),
        entry("pi.assistant", [assistant(text("a1", 100))]),
        entry("chat.message", []),
        entry("pi.assistant", [assistant(text("a2", 100))]),
    ]
    # The budget is reached at a1; the display-only entry after it is skipped for the assistant a2.
    assert selectCut(view(entries), 150) == 1
    assert selectCut(view(entries), 50) == 3


def test_range_selection_keeps_a_huge_last_tool_result_together_with_its_assistant():
    entries = [
        entry("pi.user", [user("u")]),
        entry("pi.assistant", [assistant("a", ["c"])]),
        entry("pi.tool-result", [toolResult("c", text("big", 5000))]),
    ]
    assert selectCut(view(entries), 100) == 1


def test_range_selection_never_cuts_at_a_system_entry_or_an_excluded_error_or_aborted_answer():
    entries = [
        entry("pi.user", [user(text("u1", 100))]),
        entry("pi.assistant", [assistant(text("a1", 100))]),
        entry("pi.system", [{"role": "system", "content": "", "sections": {"s": text("s", 100)}, "timestamp": 0}]),
        entry("pi.assistant", [assistant(text("err", 100), [], "error")]),
        entry("pi.assistant", [assistant(text("stopped", 100), [], "aborted")]),
        entry("pi.assistant", [assistant(text("a2", 100))]),
    ]
    # The walk reaches 150 at the system entry; the excluded answers after it contribute nothing.
    assert selectCut(view(entries), 150) == 5


def test_range_selection_follows_edited_contributions_an_omitted_entry_adds_nothing_and_is_no_candidate():
    entries = [
        entry("pi.user", [user(text("u1", 100))]),
        entry("pi.assistant", [assistant(text("a1", 100))]),
        entry("pi.user", [user(text("u2", 100))]),
        entry("pi.assistant", [assistant(text("a2", 100))]),
    ]
    plain = view(entries)
    assert selectCut(plain, 150) == 2
    contributions = [[] if index == 2 else messages for index, messages in enumerate(plain["contributions"])]
    omitted = {
        **plain,
        "contributions": contributions,
        "messages": orderToolResults([message for messages in contributions for message in messages]),
    }
    assert selectCut(omitted, 150) == 1


def test_range_selection_does_not_cut_at_a_user_entry_that_a_result_of_the_preceding_call_still_follows():
    entries = [
        entry("pi.user", [user(text("u1", 100))]),
        entry("pi.assistant", [assistant("a", ["c"])]),
        entry("pi.user", [user(text("steer", 100))]),
        entry("pi.tool-result", [toolResult("c", text("r", 100))]),
        entry("pi.assistant", [assistant(text("a2", 100))]),
    ]
    # The budget is reached at the steer; its result follows it, so the cut moves to the next assistant.
    assert selectCut(view(entries), 250) == 4


def test_range_selection_finds_nothing_when_the_budget_is_never_reached_or_only_the_marker_precedes_the_cut():
    small = [entry("pi.user", [user("hi")]), entry("pi.assistant", [assistant("hello")])]
    assert selectCut(view(small), 150) is None
    marker = entry("pi.compaction", [user("summary")], 0)
    # The budget is reached at the only entry after the marker, so the marker alone would be summarized.
    assert selectCut(view([entry("pi.user", [user(text("u", 200))])], marker), 150) is None


def test_range_selection_summarizes_an_earlier_summary_marker_first():
    marker = entry("pi.compaction", [user("EARLIER")], 0)
    kept = [
        entry("pi.user", [user(text("u1", 100))]),
        entry("pi.assistant", [assistant(text("a1", 100))]),
        entry("pi.user", [user(text("u2", 100))]),
        entry("pi.assistant", [assistant(text("a2", 100))]),
    ]
    selected = view(kept, marker)
    assert selectCut(selected, 150) == 3
    prefix = [message for messages in selected["contributions"][:3] for message in messages]
    assert re.match(r"^\[User\]: EARLIER", serializeConversation(prefix))


def test_serialization_writes_a_transcript_truncates_tool_results_and_omits_system_messages_and_thinking():
    call = fauxToolCall("read", {"path": "a.ts"}, {"id": "c"})
    messages = [
        {"role": "system", "content": "", "sections": {"s": "hidden"}, "timestamp": 0},
        user("hello"),
        toMessageDict(fauxAssistantMessage([fauxThinking("hmm"), fauxText("sure"), call])),
        toolResult("c", "y" * 2500),
    ]
    serialized = serializeConversation(messages)
    assert "hidden" not in serialized
    assert "[User]: hello" in serialized
    assert "hmm" not in serialized
    assert "[Assistant thinking]" not in serialized
    assert "[Assistant]: sure" in serialized
    assert '[Assistant tool calls]: read(path="a.ts")' in serialized
    assert f"[Tool result]: {'y' * 2000}\n\n[... 500 more characters truncated]" in serialized


# ─── Manual compaction ────────────────────────────────────────────────────


async def test_manual_places_the_summary_at_once_when_idle_and_keeps_raw_history():
    chat = await openCompaction()
    await history(chat)
    before = await allEntries(chat.root)
    usageBefore = (await chat.harness.snapshot(UsageDoc, chat.root.id, context))["models"]["faux/faux-1"]
    chat.faux.summaries.append(summary())

    id = await chat.root.compact("focus on files", context)
    outcome = await result(chat, id)
    assert outcome["status"] == "completed"
    placed = await (await chat.harness.submission(submissionIdOf(outcome), context)).wait(context)
    assert placed["status"] == "done"

    # Raw history is unchanged; one summary entry heads the first kept entry, u3.
    after = await allEntries(chat.root)
    assert after[: len(before)] == before
    marker = after[-1]
    u3 = next(record for record in before if userText((record.get("model") or [None])[0]).startswith("u3"))
    toMatchObject(marker, {"kind": "pi.compaction", "head": u3["id"], "data": {"reason": "manual"}})
    assert placed["status"] == "done"
    assert placed["entry"] == marker["id"]
    assert userText(marker["model"][0]) == (
        "The conversation history before this point was compacted into the following summary:\n\n"
        "<summary>\nSUMMARY\n</summary>"
    )

    # The model context is the summary followed by the kept entries.
    contextView = await chat.root.context(context)
    assert [userText(message) for message in contextView["messages"]] == [
        userText(marker["model"][0]),
        text("u3", 100),
        text("a3", 100),
    ]

    # The summarizer saw the serialized prefix, the prompt, and the instructions, without tools or caching.
    request = chat.faux.summaryRequests[0]
    assert len(request.messages) == 2
    prompt = userText(request.messages[1])
    assert re.match(r"^<conversation>\n\[User\]: u1 ", prompt)
    assert "[Assistant]: a2 " in prompt
    assert "u3 " not in prompt
    assert "## Goal" in prompt
    assert prompt.endswith("\n\nAdditional focus: focus on files")
    assert request.options.cacheRetention == "none"
    assert request.options.maxTokens == 800
    assert request.options.sessionId == (await chat.harness.snapshot(ProviderDoc, chat.root.id, context))["sessionId"]
    assert getattr(request.options, "deferred", None) is None

    # The summarizer's spend is in the ledger, and nothing counts it again later.
    usage = (await chat.harness.snapshot(UsageDoc, chat.root.id, context))["models"]["faux/faux-1"]
    assert usage["input"] > usageBefore["input"]
    await turn(chat, "next", "done")
    agent = chat.faux.agentRequests[-1].messages
    # The next request: one complete system baseline leading the summary, the kept turn, and the new input (#10904).
    assert [message.role for message in agent] == ["system", "user", "user", "assistant", "user"]
    assert agent[0].sections == {"preamble": "You are helpful."}
    await chat.harness.close(context)


async def test_creates_provider_state_before_a_legacy_conversations_summarization_request():
    # Regression coverage for #10424.
    chat = await openCompaction()
    await history(chat)
    await chat.root.commit(lambda tx: tx.retireDoc(ProviderDoc, chat.root.id), context)
    assert await chat.harness.snapshot(ProviderDoc, chat.root.id, context) is None
    chat.faux.summaries.append(summary())
    assert (await result(chat, await chat.root.compact(None, context)))["status"] == "completed"
    stored = await chat.harness.snapshot(ProviderDoc, chat.root.id, context)
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", stored["sessionId"])
    assert chat.faux.summaryRequests[0].options.sessionId == stored["sessionId"]
    await chat.harness.close(context)


async def test_manual_keeps_working_while_busy_and_places_the_summary_at_the_next_final_boundary():
    chat = await openCompaction()
    await history(chat)
    gate = deferred()
    reached = deferred()
    chat.faux.agent.append(gated(gate, answer("late answer"), reached))
    input = await chat.root.submit({"type": "input", "content": "busy"}, context)
    await reached.promise
    chat.faux.summaries.append(summary())
    outcome = await result(chat, await chat.root.compact(None, context))
    submissionId = submissionIdOf(outcome)
    assert (await (await chat.harness.submission(submissionId, context)).status(context))["status"] == "queued"
    gate.resolve()
    assert (await input.wait(context))["status"] == "done"
    placed = await (await chat.harness.submission(submissionId, context)).wait(context)
    assert placed["status"] == "done"
    # The summary follows the answer; the kept range still includes the busy turn.
    assert (await kinds(chat.root))[-3:] == ["pi.user", "pi.assistant", "pi.compaction"]
    await chat.harness.close(context)


async def test_manual_places_a_queued_summary_at_post_tools_and_the_run_continues_in_the_compacted_context():
    chat = await openCompaction()

    async def wait(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "waited"}]}

    addTool(
        chat.setup.registry,
        defineTool({"name": "wait", "description": "wait", "parameters": EMPTY_OBJECT, "execute": wait}),
    )
    await chat.root.configure({"tools": toolsNamed(chat.setup, "wait")}, context)
    await history(chat)
    gate = deferred()
    reached = deferred()
    chat.faux.agent.append(
        gated(gate, fauxAssistantMessage([fauxToolCall("wait", {})], {"stopReason": "toolUse"}), reached)
    )
    chat.faux.agent.append(answer("after tools"))
    input = await chat.root.submit({"type": "input", "content": "use a tool"}, context)
    await reached.promise
    chat.faux.summaries.append(summary())
    await result(chat, await chat.root.compact(None, context))
    gate.resolve()
    assert (await input.wait(context))["status"] == "done"
    # The continuation request starts with the new baseline, then the summary, ahead of the kept tool call (#10904).
    continuation = chat.faux.agentRequests[-1].messages
    assert continuation[0].role == "system"
    assert "<summary>\nSUMMARY\n</summary>" in userText(continuation[1])
    assert (await kinds(chat.root))[-4:] == ["pi.tool-result", "pi.compaction", "pi.system", "pi.assistant"]
    await chat.harness.close(context)


async def test_manual_runs_follow_ups_left_by_a_failed_run_after_placing_the_summary():
    chat = await openCompaction()
    await history(chat)
    gate = deferred()
    reached = deferred()
    chat.faux.agent.append(gated(gate, failure("bad request"), reached))
    failed = await chat.root.submit({"type": "input", "content": "fails"}, context)
    await reached.promise
    followUp = await chat.root.submit({"type": "input", "content": "follow-up"}, context)
    gate.resolve()
    assert (await failed.wait(context))["status"] == "unanswered"
    assert (await followUp.status(context))["status"] == "queued"

    chat.faux.summaries.append(summary())
    chat.faux.agent.append(answer("followed"))
    await result(chat, await chat.root.compact(None, context))
    assert (await followUp.wait(context))["status"] == "done"
    request = chat.faux.agentRequests[-1].messages
    assert "SUMMARY" in firstUserText(request)
    assert any(userText(message) == "follow-up" for message in request)
    await chat.harness.close(context)


async def test_manual_settles_stale_when_a_reset_lands_while_it_summarizes():
    chat = await openCompaction()
    await history(chat)
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary(), reached))
    id = await chat.root.compact(None, context)
    await reached.promise
    await chat.root.reset(None, context)
    gate.resolve()
    outcome = await result(chat, id)
    toMatchObject(
        await (await chat.harness.submission(submissionIdOf(outcome), context)).status(context),
        {"status": "unanswered", "reason": "stale"},
    )
    assert (await kinds(chat.root))[-1] == "pi.reset"
    await chat.harness.close(context)


async def test_manual_does_not_make_the_conversation_busy_a_submission_during_summarization_starts_its_run_at_once():
    chat = await openCompaction()
    await history(chat)
    summaryGate = deferred()
    summaryReached = deferred()
    chat.faux.summaries.append(gated(summaryGate, summary(), summaryReached))
    id = await chat.root.compact(None, context)
    await summaryReached.promise
    answerGate = deferred()
    answerReached = deferred()
    chat.faux.agent.append(gated(answerGate, answer("a4"), answerReached))
    input = await chat.root.submit({"type": "input", "content": "u4"}, context)
    # Placed and answered immediately with the uncompacted context, not queued behind the compaction.
    assert (await input.status(context))["status"] == "placed"
    await answerReached.promise
    assert firstUserText(chat.faux.agentRequests[-1].messages) == text("u1", 100)
    # The summary is ready while the run is busy, so it queues and lands after the answer.
    summaryGate.resolve()
    outcome = await result(chat, id)
    submission = await chat.harness.submission(submissionIdOf(outcome), context)
    assert (await submission.status(context))["status"] == "queued"
    answerGate.resolve()
    assert (await input.wait(context))["status"] == "done"
    assert (await submission.wait(context))["status"] == "done"
    assert (await kinds(chat.root))[-3:] == ["pi.user", "pi.assistant", "pi.compaction"]
    await chat.harness.close(context)


async def test_manual_counts_the_spend_of_a_summary_that_ends_stale_and_writes_no_entry_for_it():
    chat = await openCompaction()
    await history(chat)

    async def inputTokens() -> int:
        return (await chat.harness.snapshot(UsageDoc, chat.root.id, context))["models"]["faux/faux-1"]["input"]  # type: ignore[no-any-return]

    before = await inputTokens()
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary(), reached))
    id = await chat.root.compact(None, context)
    await reached.promise
    await chat.root.reset(None, context)
    gate.resolve()
    outcome = await result(chat, id)
    submission = await chat.harness.submission(submissionIdOf(outcome), context)
    toMatchObject(await submission.status(context), {"status": "unanswered", "reason": "stale"})
    assert await inputTokens() > before
    assert "pi.compaction" not in await kinds(chat.root)
    await chat.harness.close(context)


async def test_manual_lets_the_compaction_that_cuts_furthest_win_whatever_finishes_first():
    chat = await openCompaction()
    await history(chat)
    first = deferred()
    firstReached = deferred()
    chat.faux.summaries.append(gated(first, summary("FIRST"), firstReached))
    early = await chat.root.compact(None, context)
    await firstReached.promise
    await turn(chat, text("u4", 100), text("a4", 100))
    chat.faux.summaries.append(summary("SECOND"))
    later = await result(chat, await chat.root.compact(None, context))
    assert later["status"] == "completed"
    first.resolve()
    outcome = await result(chat, early)
    # The early compaction cut at u3, before the later cut at u4.
    toMatchObject(
        await (await chat.harness.submission(submissionIdOf(outcome), context)).status(context),
        {"status": "unanswered", "reason": "stale"},
    )
    assert "SECOND" in firstUserText((await chat.root.context(context))["messages"])
    await chat.harness.close(context)


async def test_manual_places_an_older_selected_summary_that_cuts_later_than_the_newer_one():
    chat = await openCompaction()
    await history(chat)
    # A: small budget, late cut; selected first, finishes last.
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary("A"), reached))
    a = await chat.root.compact(None, context)
    await reached.promise
    # B: larger budget, earlier cut; placed first.
    chat.setup.settings["compaction"] = {**MANUAL, "keepRecentTokens": 350}
    chat.faux.summaries.append(summary("B"))
    await result(chat, await chat.root.compact(None, context))
    assert "B" in firstUserText((await chat.root.context(context))["messages"])
    gate.resolve()
    outcome = await result(chat, a)
    assert (await (await chat.harness.submission(submissionIdOf(outcome), context)).status(context))["status"] == "done"
    messages = (await chat.root.context(context))["messages"]
    assert "<summary>\nA\n</summary>" in firstUserText(messages)
    assert [userText(message) for message in messages[1:]] == [text("u3", 100), text("a3", 100)]
    await chat.harness.close(context)


@pytest.mark.parametrize(
    ("order", "keeps", "stale"),
    [("before", [150, 350], True), ("at", [150, 150], False), ("after", [350, 150], False)],
)
async def test_manual_places_two_queued_summaries_in_one_boundary_when_the_second_cuts_relative_to_the_first(
    order, keeps, stale
):
    chat = await openCompaction()
    await history(chat)
    gate = deferred()
    reached = deferred()
    chat.faux.agent.append(gated(gate, answer("done"), reached))
    input = await chat.root.submit({"type": "input", "content": "busy"}, context)
    await reached.promise
    submissions = []
    for index, keep in enumerate(keeps):
        chat.setup.settings["compaction"] = {**MANUAL, "keepRecentTokens": keep}
        chat.faux.summaries.append(summary(f"S{index}"))
        outcome = await result(chat, await chat.root.compact(None, context))
        submissions.append(submissionIdOf(outcome))
    gate.resolve()
    await input.wait(context)
    statuses = []
    for id in submissions:
        statuses.append((await (await chat.harness.submission(id, context)).wait(context))["status"])
    assert statuses == ["done", "unanswered" if stale else "done"]
    await chat.harness.close(context)


async def test_manual_is_aborted_by_conversation_abort_an_already_queued_summary_survives_it():
    chat = await openCompaction()
    await history(chat)
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    id = await chat.root.compact(None, context)
    await reached.promise
    assert len((await live(chat))["compactions"]) == 1
    await chat.root.abort(context)
    assert (await result(chat, id))["status"] == "aborted"
    assert (await live(chat)).get("compactions") is None
    assert "pi.compaction" not in await kinds(chat.root)

    # Queued while busy, then Esc: the queued write stays and lands with the next run's boundary.
    gate = deferred()
    busy = deferred()
    chat.faux.agent.append(gated(gate, answer("never"), busy))
    await chat.root.submit({"type": "input", "content": "busy"}, context)
    await busy.promise
    chat.faux.summaries.append(summary())
    queued = await result(chat, await chat.root.compact(None, context))
    await chat.root.abort(context)
    submission = await chat.harness.submission(submissionIdOf(queued), context)
    assert (await submission.status(context))["status"] == "queued"
    assert await submission.abort(context) == "aborted"
    await turn(chat, "next", "ok")
    toMatchObject(await submission.status(context), {"status": "unanswered", "reason": "aborted"})
    assert "pi.compaction" not in await kinds(chat.root)
    await chat.harness.close(context)


async def test_manual_is_ordinary_work_idle_waits_include_it():
    chat = await openCompaction()
    await history(chat)
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary(), reached))
    id = await chat.root.compact(None, context)
    await reached.promise
    holder = {"idle": False}

    async def waiting() -> None:
        await chat.root.waitForIdle(context)
        holder["idle"] = True

    wait = asyncio.ensure_future(waiting())
    await asyncio.sleep(0.02)
    assert holder["idle"] is False
    gate.resolve()
    await wait
    assert (await chat.harness.getTask(id, context))["state"]["status"] == "terminal"
    await chat.harness.close(context)


async def test_manual_enables_scheduling_right_after_open():
    setup = chatSetup()
    faux = script(setup)
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    setup.settings["compaction"] = {**MANUAL, "keepRecentTokens": 10}
    id = await root.compact(None, context)

    # getTask() only reads, so progress here comes from compact() itself.
    async def terminal() -> bool:
        return (await harness.getTask(id, context))["state"]["status"] == "terminal"

    await waitFor(terminal)
    toMatchObject(
        (await harness.getTask(id, context))["state"], {"outcome": {"status": "completed", "result": {}}}
    )
    assert len(faux.summaryRequests) == 0
    await harness.close(context)


# ─── Compaction outcomes ──────────────────────────────────────────────────


async def test_outcomes_completes_without_a_summary_when_there_is_nothing_to_compact():
    chat = await openCompaction()
    await turn(chat, "hi", "hello")
    outcome = await result(chat, await chat.root.compact(None, context))
    assert outcome == {"status": "completed", "result": {}}
    assert len(chat.faux.summaryRequests) == 0
    assert (await live(chat)).get("compactions") is None
    await chat.harness.close(context)


async def test_outcomes_asks_before_compact_the_first_decision_wins_a_throw_is_reported_and_skipped():
    chat = await openCompaction()
    seen: list[Any] = []

    def broken(*_: Any) -> Any:
        raise RuntimeError("hook broke")

    def supplying(compaction: Any, *_: Any) -> Any:
        seen.append(compaction)
        return {"summary": "FROM HOOK"}

    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": broken})
    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": supplying})
    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": lambda *_: {"decline": True}})
    await history(chat)
    outcome = await result(chat, await chat.root.compact("why", context))
    assert outcome["status"] == "completed"
    assert len(chat.faux.summaryRequests) == 0
    assert "<summary>\nFROM HOOK\n</summary>" in firstUserText((await chat.root.context(context))["messages"])
    assert [str(error) for error in chat.setup.reports] == ["hook broke"]
    compaction = seen[0]
    toMatchObject(compaction, {"reason": "manual", "instructions": "why"})
    assert [record["kind"] for record in compaction["entries"]] == [
        "pi.user",
        "pi.system",
        "pi.assistant",
        "pi.user",
        "pi.assistant",
    ]
    assert [userText(message) for message in compaction["messages"] if message["role"] != "system"] == [
        text("u1", 100),
        text("a1", 100),
        text("u2", 100),
        text("a2", 100),
    ]
    await chat.harness.close(context)


async def test_outcomes_completes_without_a_summary_when_a_hook_declines():
    chat = await openCompaction()
    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": lambda *_: {"decline": True}})
    await history(chat)
    assert await result(chat, await chat.root.compact(None, context)) == {"status": "completed", "result": {}}
    assert len(chat.faux.summaryRequests) == 0
    await chat.harness.close(context)


async def test_outcomes_fails_with_no_model_without_a_configured_model():
    chat = await openCompaction()
    await history(chat)
    await chat.root.configure({"model": None}, context)
    outcome = await result(chat, await chat.root.compact(None, context))
    toMatchObject(outcome, {"status": "failed", "error": {"detail": {"reason": "no_model"}}})
    assert (await live(chat)).get("compactions") is None
    await chat.harness.close(context)


async def compactionInput(chat: Any, run: Callable[[], Any]) -> int:
    """Input tokens a compaction added to the ledger."""

    async def inputTokens() -> int:
        return (await chat.harness.snapshot(UsageDoc, chat.root.id, context))["models"]["faux/faux-1"]["input"]  # type: ignore[no-any-return]

    before = await inputTokens()
    await run()
    return await inputTokens() - before


async def test_outcomes_retries_a_retryable_error_with_the_pinned_request_and_counts_every_attempt_once():
    single = await openCompaction()
    await history(single)
    await single.root.configure({"thinkingLevel": "high"}, context)
    single.faux.summaries.append(summary())

    async def runSingle() -> Any:
        return await result(single, await single.root.compact(None, context))

    once = await compactionInput(single, runSingle)
    assert once > 0
    await single.harness.close(context)

    chat = await openCompaction()
    await history(chat)
    await chat.root.configure({"thinkingLevel": "high"}, context)
    chat.setup.settings["stream"] = {"timeoutMs": 1234, "deferred": True}

    async def overloaded(request: Any) -> Any:
        # Changed during the attempt: the retry still uses the pinned request.
        await chat.root.configure({"thinkingLevel": "low"}, context)
        chat.setup.settings["stream"] = {"timeoutMs": 1}
        return failure("overloaded")

    chat.faux.summaries.append(overloaded)
    chat.faux.summaries.append(summary())

    async def runTwice() -> None:
        assert (await result(chat, await chat.root.compact(None, context)))["status"] == "completed"

    twice = await compactionInput(chat, runTwice)
    assert len(chat.faux.summaryRequests) == 2
    assert twice == 2 * once
    sessionId = (await chat.harness.snapshot(ProviderDoc, chat.root.id, context))["sessionId"]
    for request in chat.faux.summaryRequests:
        assert request.options.reasoning == "high"
        assert request.options.timeoutMs == 1234
        assert request.options.cacheRetention == "none"
        assert request.options.sessionId == sessionId
        assert getattr(request.options, "deferred", None) is None
    await chat.harness.close(context)


@pytest.mark.parametrize("decision", [{"decline": True}, {"summary": "HOOK"}])
async def test_outcomes_adds_no_usage_when_a_hook_declines_or_supplies_the_summary(decision):
    chat = await openCompaction()
    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": lambda *_: decision})
    await history(chat)

    async def run() -> Any:
        return await result(chat, await chat.root.compact(None, context))

    assert await compactionInput(chat, run) == 0
    await chat.harness.close(context)


async def test_outcomes_caps_max_tokens_at_the_models_output_limit_and_sends_no_tools():
    chat = await openCompaction()

    async def read(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    addTool(
        chat.setup.registry,
        defineTool({"name": "read", "description": "read", "parameters": EMPTY_OBJECT, "execute": read}),
    )
    await chat.root.configure({"tools": toolsNamed(chat.setup, "read")}, context)
    await history(chat)
    chat.setup.settings["compaction"] = {**MANUAL, "reserveTokens": 2000}
    chat.faux.summaries.append(summary())
    await result(chat, await chat.root.compact(None, context))
    request = chat.faux.summaryRequests[0]
    # 0.8 * 2000 = 1600, above the model's 900.
    assert request.options.maxTokens == 900
    assert len(request.messages) == 2
    assert not any(
        message.role == "system" and getattr(message, "toolsAdded", None) is not None for message in request.messages
    )
    await chat.harness.close(context)


@pytest.mark.parametrize(
    ("name", "response", "message"),
    [
        ("retries run out", failure("overloaded"), "Summarization failed: overloaded"),
        ("a non-retryable error", failure("bad request"), "Summarization failed: bad request"),
        (
            "a length stop",
            fauxAssistantMessage("partial", {"stopReason": "length"}),
            "Summarization hit the token limit; the summary is incomplete",
        ),
        (
            "a tool call",
            fauxAssistantMessage([fauxToolCall("read", {})], {"stopReason": "stop"}),
            "Summarization attempted to call a tool",
        ),
        ("empty text", answer("  "), "Summarization produced no text"),
    ],
)
async def test_outcomes_fails_with_model_error_on(name, response, message):
    chat = await openCompaction()
    await history(chat)
    for _ in range(3):
        chat.faux.summaries.append(response)
    outcome = await result(chat, await chat.root.compact(None, context))
    toMatchObject(outcome, {"status": "failed", "error": {"message": message, "detail": {"reason": "model_error"}}})
    # The retry policy allows two retries after the first attempt.
    assert len(chat.faux.summaryRequests) == (3 if name == "retries run out" else 1)
    assert (await live(chat)).get("compactions") is None
    assert "pi.compaction" not in await kinds(chat.root)
    await chat.harness.close(context)


# ─── Automatic compaction ─────────────────────────────────────────────────

# Background threshold at 500 and blocking threshold at 1500 tokens of a 2000-token window.
BACKGROUND = {"enabled": True, "reserveTokens": 500, "keepRecentTokens": 150, "backgroundTokens": 1000}
# Blocking threshold at 700 tokens of a 1000-token window, no background compaction.
BLOCKING = {"enabled": True, "reserveTokens": 300, "keepRecentTokens": 150, "backgroundTokens": 0}


async def compactionTasks(chat: Any) -> list[dict[str, Any]]:
    inspection = await chat.harness.inspect(context)
    return [task["record"] for task in inspection["tasks"] if task["record"]["kind"] == "pi.compaction"]


async def test_background_starts_above_the_background_threshold_without_blocking_the_run_idle_waits_and_esc_ignore_it():
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    chat.setup.settings["compaction"] = BACKGROUND
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary(), reached))
    await turn(chat, text("u4", 100), text("a4", 100))
    await reached.promise
    [task] = await compactionTasks(chat)
    toMatchObject(task, {"background": True, "input": {"reason": "threshold"}})
    assert task.get("owner") is None
    assert (await live(chat))["compactions"] == [
        {"taskId": task["id"], "reason": "threshold", "blocking": False, "attempt": 1}
    ]
    # Background work: neither conversation idle nor Esc waits for or stops it.
    await chat.root.waitForIdle(context)
    await chat.root.abort(context)
    assert (await chat.harness.getTask(task["id"], context))["state"]["status"] == "running"
    gate.resolve()
    outcome = await result(chat, task["id"])
    assert outcome["status"] == "completed"
    assert (await kinds(chat.root))[-1] == "pi.compaction"
    assert "SUMMARY" in firstUserText((await chat.root.context(context))["messages"])
    await chat.harness.close(context)


@pytest.mark.parametrize(
    ("name", "policy"),
    [
        ("disabled", {**BACKGROUND, "enabled": False}),
        ("backgroundTokens is 0", {**BACKGROUND, "backgroundTokens": 0}),
        ("there is no cut", {**BACKGROUND, "keepRecentTokens": 100_000}),
    ],
)
async def test_background_does_not_start_when(name, policy):
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    chat.setup.settings["compaction"] = policy
    await turn(chat, text("u4", 100), text("a4", 100))
    assert await compactionTasks(chat) == []
    assert len(chat.faux.summaryRequests) == 0
    await chat.harness.close(context)


async def test_background_does_not_start_while_another_compaction_is_listed():
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    manual = await chat.root.compact(None, context)
    await reached.promise
    chat.setup.settings["compaction"] = BACKGROUND
    await turn(chat, text("u4", 100), text("a4", 100))
    assert [task["id"] for task in await compactionTasks(chat)] == [manual]
    await chat.harness.abortTask(manual, context)
    await chat.harness.close(context)


async def test_background_records_a_failure_in_pi_live_the_snapshot_and_compaction_end_until_a_summary_is_produced():
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    chat.setup.settings["compaction"] = BACKGROUND
    events: list[dict[str, Any]] = []
    stream = await watchEvents(chat.harness, chat.root.id, context)

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    chat.faux.summaries.append(failure("bad request"))
    await turn(chat, text("u4", 100), text("a4", 100))
    await waitFor(lambda: any(event["type"] == "compaction_end" for event in events))
    recorded = (await live(chat)).get("compactionFailure")
    toMatchObject(recorded, {"reason": "threshold", "error": "Summarization failed: bad request"})
    assert next(event for event in events if event["type"] == "compaction_end") == {
        "type": "compaction_end",
        "taskId": recorded["taskId"],
        "reason": "threshold",
        "error": "Summarization failed: bad request",
    }
    reopened = await watchEvents(chat.harness, chat.root.id, context)
    assert reopened.snapshot.get("compactionFailure") == recorded
    await reopened.stop()
    # A compaction that produces a summary removes the failure, and ends without an error.
    chat.setup.settings["compaction"] = MANUAL
    chat.faux.summaries.append(summary())
    manual = await chat.root.compact(None, context)
    assert (await result(chat, manual))["status"] == "completed"
    assert (await live(chat)).get("compactionFailure") is None

    def manualEnd() -> Any:
        return next(
            (event for event in events if event["type"] == "compaction_end" and event["taskId"] == manual), None
        )

    await waitFor(lambda: manualEnd() is not None)
    assert "error" not in manualEnd()
    await stream.stop()
    await chat.harness.close(context)


@pytest.mark.parametrize("stop", ["task", "conversation"])
async def test_background_stops_through_abort_task_and_conversation_abort_with_background(stop):
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    chat.setup.settings["compaction"] = BACKGROUND
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    await turn(chat, text("u4", 100), text("a4", 100))
    await reached.promise
    [task] = await compactionTasks(chat)
    if stop == "task":
        await chat.harness.abortTask(task["id"], context)
    else:
        await chat.root.abort(context, {"background": True})
    assert (await result(chat, task["id"]))["status"] == "aborted"
    assert (await live(chat)).get("compactions") is None
    await chat.harness.close(context)


async def test_blocking_waits_for_its_compaction_which_appends_the_summary_before_the_request():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    # A new section: preparation has a system entry to append, but must not append it before the wait.
    addSection(chat.setup.registry, "extra", lambda *_: "EXTRA")
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary(), reached))
    chat.faux.agent.append(answer("a4"))
    input = await chat.root.submit({"type": "input", "content": text("u4", 200)}, context)
    await reached.promise
    [child] = await compactionTasks(chat)
    generation = (await live(chat))["run"]["taskId"]
    toMatchObject(child, {"owner": generation, "background": False, "input": {"reason": "threshold"}})
    toMatchObject(
        (await chat.harness.getTask(generation, context))["state"],
        {
            "status": "waiting",
            "on": [child["id"]],
            "checkpoint": {"phase": "prepare", "attempt": 1, "compacted": child["id"]},
        },
    )
    assert (await live(chat))["compactions"] == [
        {"taskId": child["id"], "reason": "threshold", "blocking": True, "attempt": 1}
    ]
    # Nothing was appended before the wait.
    assert (await kinds(chat.root))[-1] == "pi.user"
    gate.resolve()
    assert (await input.wait(context))["status"] == "done"
    assert (await result(chat, child["id"]))["status"] == "completed"
    assert (await kinds(chat.root))[-3:] == ["pi.compaction", "pi.system", "pi.assistant"]
    request = chat.faux.agentRequests[-1].messages
    assert "SUMMARY" in firstUserText(request)
    systems = [message for message in request if message.role == "system"]
    assert len(systems) == 1
    assert systems[0].sections == {"preamble": "You are helpful.", "extra": "<extra>\nEXTRA\n</extra>"}
    await chat.harness.close(context)


async def test_blocking_sends_the_request_once_without_a_second_compaction_when_the_kept_part_is_still_above_the_threshold():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = {**BLOCKING, "keepRecentTokens": 700}
    chat.faux.summaries.append(summary())
    await turn(chat, text("u4", 400), "a4")
    assert len(chat.faux.summaryRequests) == 1
    assert len([kind for kind in await kinds(chat.root) if kind == "pi.compaction"]) == 1
    await chat.harness.close(context)


async def test_blocking_sends_the_request_anyway_when_its_compaction_declines():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": lambda *_: {"decline": True}})
    await turn(chat, text("u4", 200), "a4")
    assert "pi.compaction" not in await kinds(chat.root)
    assert firstUserText(chat.faux.agentRequests[-1].messages) == text("u1", 100)
    assert (await live(chat)).get("compactionFailure") is None
    await chat.harness.close(context)


# A failed compaction leaves a context it judged too large; sending it once filled a 1M window and got an empty
# `length` answer on every turn while the failure stayed invisible.
async def test_blocking_fails_the_run_with_the_compactions_error_without_a_request_when_its_compaction_fails():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    chat.faux.summaries.append(failure("bad request"))
    requests = len(chat.faux.agentRequests)
    input = await chat.root.submit({"type": "input", "content": text("u4", 200)}, context)
    toMatchObject(
        await input.wait(context),
        {
            "status": "unanswered",
            "reason": "model_error",
            "detail": "Compaction failed: Summarization failed: bad request",
        },
    )
    assert len(chat.faux.agentRequests) == requests
    assert (await kinds(chat.root))[-1] == "pi.user"
    state = await live(chat)
    assert state.get("compactions") is None
    toMatchObject(state.get("compactionFailure"), {"reason": "threshold", "error": "Summarization failed: bad request"})
    await chat.harness.close(context)


async def test_blocking_fails_the_run_when_its_compaction_is_aborted_directly():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    requests = len(chat.faux.agentRequests)
    input = await chat.root.submit({"type": "input", "content": text("u4", 200)}, context)
    await reached.promise
    [child] = await compactionTasks(chat)
    await chat.harness.abortTask(child["id"], context)
    toMatchObject(
        await input.wait(context),
        {"status": "unanswered", "reason": "model_error", "detail": "Compaction was aborted"},
    )
    assert len(chat.faux.agentRequests) == requests
    assert "pi.compaction" not in await kinds(chat.root)
    # Aborting is not a failure.
    assert (await live(chat)).get("compactionFailure") is None
    await chat.harness.close(context)


async def test_blocking_is_aborted_with_its_generation_by_esc_before_the_generations_abort_handler():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    events: list[dict[str, Any]] = []
    stream = await watchEvents(chat.harness, chat.root.id, context)

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    input = await chat.root.submit({"type": "input", "content": text("u4", 200)}, context)
    await reached.promise
    [child] = await compactionTasks(chat)
    await chat.root.abort(context)
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "aborted"})
    toMatchObject((await chat.harness.getTask(child["id"], context))["state"], {"outcome": {"status": "aborted"}})
    await waitFor(lambda: any(event["type"] == "run_end" for event in events))
    types = [event["type"] for event in events]
    assert types.index("compaction_end") < types.index("run_end")
    await stream.stop()
    await chat.harness.close(context)


async def test_blocking_wins_over_a_background_compaction_still_in_flight_which_then_settles_stale():
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    chat.setup.settings["compaction"] = BACKGROUND
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary("BACKGROUND"), reached))
    await turn(chat, text("u4", 100), text("a4", 100))
    await reached.promise
    [background] = await compactionTasks(chat)
    chat.faux.summaries.append(summary("BLOCKING"))
    await turn(chat, text("u5", 1000), "a5")
    assert "BLOCKING" in firstUserText((await chat.root.context(context))["messages"])
    gate.resolve()
    outcome = await result(chat, background["id"])
    toMatchObject(
        await (await chat.harness.submission(submissionIdOf(outcome), context)).status(context),
        {"status": "unanswered", "reason": "stale"},
    )
    await chat.harness.close(context)


# ─── Overflow compaction ──────────────────────────────────────────────────

ENABLED = {**MANUAL, "enabled": True}


async def test_overflow_compacts_and_retries_with_the_same_attempt_leaving_the_error_out_of_the_retry():
    chat = await openCompaction()
    await history(chat)
    chat.setup.settings["compaction"] = ENABLED
    chat.faux.summaries.append(summary())
    holder: dict[str, Any] = {}
    chat.faux.agent.append(failure(OVERFLOW))

    async def fits(request: Any) -> Any:
        holder["attempt"] = ((await live(chat)).get("generation") or {}).get("attempt")
        return answer("fits")

    chat.faux.agent.append(fits)
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    assert (await input.wait(context))["status"] == "done"
    assert holder["attempt"] == 1
    assert (await kinds(chat.root))[-5:] == ["pi.user", "pi.assistant", "pi.compaction", "pi.system", "pi.assistant"]
    [compaction] = [record for record in await allEntries(chat.root) if record["kind"] == "pi.compaction"]
    toMatchObject(compaction, {"data": {"reason": "overflow"}})
    retry = chat.faux.agentRequests[-1].messages
    assert retry[0].role == "system"
    assert "SUMMARY" in userText(retry[1])
    assert not any(message.role == "assistant" and message.stopReason == "error" for message in retry)
    await chat.harness.close(context)


async def test_overflow_fails_a_second_overflow_with_its_error_entry():
    chat = await openCompaction()
    await history(chat)
    chat.setup.settings["compaction"] = ENABLED
    chat.faux.summaries.append(summary())
    chat.faux.agent.extend([failure(OVERFLOW), failure(OVERFLOW)])
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "model_error", "detail": OVERFLOW})
    assert len([kind for kind in await kinds(chat.root) if kind == "pi.compaction"]) == 1
    assert (await kinds(chat.root))[-1] == "pi.assistant"
    await chat.harness.close(context)


async def test_overflow_fails_without_compacting_when_compaction_is_disabled_even_for_a_retryable_looking_overflow():
    chat = await openCompaction()
    await history(chat)
    chat.faux.agent.append(failure(f"overloaded: {OVERFLOW}"))
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "model_error"})
    assert len(chat.faux.agentRequests) == 4
    assert len(chat.faux.summaryRequests) == 0
    await chat.harness.close(context)


async def test_overflow_fails_after_a_blocking_threshold_compaction_in_the_same_generation():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    chat.faux.summaries.append(summary())
    chat.faux.agent.append(failure(OVERFLOW))
    input = await chat.root.submit({"type": "input", "content": text("u4", 200)}, context)
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "model_error"})
    assert len(chat.faux.summaryRequests) == 1
    await chat.harness.close(context)


@pytest.mark.parametrize(("name", "requests"), [("declines", 0), ("fails", 1), ("cannot cut", 0)])
async def test_overflow_fails_with_the_overflow_text_when_compaction(name, requests):
    chat = await openCompaction()
    await turn(chat, text("u1", 100), text("a1", 100))
    chat.setup.settings["compaction"] = ENABLED
    if name == "declines":
        addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": lambda *_: {"decline": True}})
    elif name == "fails":
        chat.faux.summaries.append(failure("bad request"))
    else:
        # Classification finds no cut, so no compaction starts and the ordinary failure carries the text.
        chat.setup.settings["compaction"] = {**ENABLED, "keepRecentTokens": 100_000}
    chat.faux.agent.append(failure(OVERFLOW))
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "model_error", "detail": OVERFLOW})
    assert len(chat.faux.summaryRequests) == requests
    await chat.harness.close(context)


# ─── Estimates and interactions ───────────────────────────────────────────


@pytest.mark.parametrize("clock", ["real", "fixed"])
async def test_estimates_ignore_usage_measured_before_a_summary_placed_mid_run(clock):
    setup = chatSetup({"models": [{"id": "faux-1", "contextWindow": 2000, "maxTokens": 900}]})
    if clock == "fixed":
        setup.now = lambda: 1_000
    chat = await openCompaction({"contextWindow": 2000}, setup)
    toolGate = deferred()
    toolReached = deferred()

    async def slow(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        toolReached.resolve()
        await toolGate.promise
        return {"output": [{"type": "text", "text": text("result", 200)}]}

    addTool(
        chat.setup.registry,
        defineTool({"name": "slow", "description": "slow", "parameters": EMPTY_OBJECT, "execute": slow}),
    )
    await chat.root.configure({"tools": toolsNamed(chat.setup, "slow")}, context)
    await turn(chat, text("u1", 220), text("a1", 220))
    await turn(chat, text("u2", 220), text("a2", 220))
    await turn(chat, text("u3", 220), text("a3", 220))
    # Background at 900, blocking at 1500: the tool call's usage plus its result would cross 1500.
    chat.setup.settings["compaction"] = {**BACKGROUND, "backgroundTokens": 600}
    # The request starts a background compaction; its tool call's usage measures the whole context.
    chat.faux.agent.append(fauxAssistantMessage([fauxToolCall("slow", {})], {"stopReason": "toolUse"}))
    chat.faux.agent.append(answer("done"))
    summaryGate = deferred()
    summaryReached = deferred()
    chat.faux.summaries.append(gated(summaryGate, summary(), summaryReached))
    input = await chat.root.submit({"type": "input", "content": text("u4", 50)}, context)
    await asyncio.gather(toolReached.promise, summaryReached.promise)
    [background] = await compactionTasks(chat)
    summaryGate.resolve()
    # Queued: the run is busy in its tool round.
    queued = await result(chat, background["id"])
    assert (await (await chat.harness.submission(submissionIdOf(queued), context)).status(context))["status"] == "queued"
    toolGate.resolve()
    assert (await input.wait(context))["status"] == "done"
    # The summary landed at postTools; the successor saw a small context and did not compact again.
    assert len(chat.faux.summaryRequests) == 1
    assert await compactionTasks(chat) == []
    assert "SUMMARY" in firstUserText(chat.faux.agentRequests[-1].messages)
    await chat.harness.close(context)


async def test_estimates_rebaselines_the_system_prompt_over_kept_system_deltas():
    chat = await openCompaction()
    holder = {"mood": "cheerful"}
    addSection(chat.setup.registry, "mood", lambda *_: holder["mood"], {"tag": False})
    await turn(chat, text("u1", 100), text("a1", 100))
    await turn(chat, text("u2", 100), text("a2", 100))
    holder["mood"] = "terse"
    await turn(chat, text("u3", 100), text("a3", 100))
    chat.setup.settings["compaction"] = {**MANUAL, "keepRecentTokens": 250}
    chat.faux.summaries.append(summary())
    await result(chat, await chat.root.compact(None, context))
    # The kept range holds the delta for the terse mood; the next request has one complete baseline.
    assert any(record["kind"] == "pi.system" for record in (await chat.root.context(context))["entries"])
    await turn(chat, "next", "ok")
    systems = [message for message in chat.faux.agentRequests[-1].messages if message.role == "system"]
    assert len(systems) == 1
    assert systems[0].sections == {"preamble": "You are helpful.", "mood": "terse"}
    await chat.harness.close(context)


async def test_estimates_summarizes_a_replaced_entrys_replacement_and_shows_it_to_the_hook():
    chat = await openCompaction()
    await history(chat)
    u1 = (await allEntries(chat.root))[0]
    await chat.root.submit(
        {
            "type": "write",
            "entry": {
                "kind": "app.redact",
                "edits": [
                    {
                        "target": u1["id"],
                        "action": "replace",
                        "messages": [{"role": "user", "content": "REDACTED", "timestamp": 0}],
                    }
                ],
            },
        },
        context,
    )
    holder: dict[str, Any] = {"messages": []}

    def beforeCompact(compaction: Any, *_: Any) -> None:
        holder["messages"] = compaction["messages"]

    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": beforeCompact})
    chat.faux.summaries.append(summary())
    await result(chat, await chat.root.compact(None, context))
    assert firstUserText(holder["messages"]) == "REDACTED"
    assert "[User]: REDACTED" in userText(chat.faux.summaryRequests[0].messages[1])
    await chat.harness.close(context)


async def test_estimates_compacts_a_fork_whose_cut_falls_on_a_parent_entry():
    chat = await openCompaction()
    await history(chat)
    entries = await allEntries(chat.root)
    fork = await chat.root.fork(entries[-1]["id"], {"ownership": {"kind": "ownerless"}}, context)
    chat.faux.summaries.append(summary())
    outcome = await result(chat, await fork.compact(None, context))
    assert (await (await chat.harness.submission(submissionIdOf(outcome), context)).wait(context))["status"] == "done"
    u3 = next(record for record in entries if userText((record.get("model") or [None])[0]).startswith("u3"))
    contextView = await fork.context(context)
    toMatchObject(contextView["head"], {"kind": "pi.compaction", "head": u3["id"], "conversationId": fork.id})
    assert [userText(message) for message in contextView["messages"][1:]] == [text("u3", 100), text("a3", 100)]
    # The parent is untouched.
    assert "pi.compaction" not in await kinds(chat.root)
    # The fork's view keeps the parent entries the summary kept.
    state = await fork.viewState(context)
    assert state.value["entries"] == contextView["entries"]
    state.dispose()
    await chat.harness.close(context)


async def test_estimates_settles_stale_in_a_fork_reset_while_it_summarizes():
    chat = await openCompaction()
    await history(chat)
    entries = await allEntries(chat.root)
    fork = await chat.root.fork(entries[-1]["id"], {"ownership": {"kind": "ownerless"}}, context)
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary(), reached))
    id = await fork.compact(None, context)
    await reached.promise
    await fork.reset(None, context)
    gate.resolve()
    outcome = await result(chat, id)
    submission = await chat.harness.submission(submissionIdOf(outcome), context)
    toMatchObject(await submission.status(context), {"status": "unanswered", "reason": "stale"})
    assert (await fork.context(context))["head"]["kind"] == "pi.reset"
    await chat.harness.close(context)


async def test_estimates_places_an_older_queued_summary_and_the_current_one_together_when_idle_admission_drains_the_inbox():
    chat = await openCompaction()
    await history(chat)
    gate = deferred()
    reached = deferred()
    chat.faux.agent.append(gated(gate, failure("bad request"), reached))
    failed = await chat.root.submit({"type": "input", "content": "fails"}, context)
    await reached.promise
    chat.faux.summaries.append(summary("OLDER"))
    older = await result(chat, await chat.root.compact(None, context))
    gate.resolve()
    await failed.wait(context)
    # Idle now, with the older summary still queued; the current one queues behind it and a final boundary runs.
    chat.faux.summaries.append(summary("CURRENT"))
    current = await result(chat, await chat.root.compact(None, context))
    statuses = []
    for outcome in (older, current):
        statuses.append((await (await chat.harness.submission(submissionIdOf(outcome), context)).status(context))["status"])
    assert statuses == ["done", "done"]
    assert "CURRENT" in firstUserText((await chat.root.context(context))["messages"])
    await chat.harness.close(context)


def defineChildTask(gate: Any) -> dict[str, Any]:
    async def run(_task: Any, runtime: Any, taskContext: Any) -> None:
        await gate.promise
        await runtime.commit(
            lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": None}}, taskContext
        )

    async def abort(_task: Any, runtime: Any, taskContext: Any) -> None:
        await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, taskContext)

    return defineTask(
        {
            "name": "test.child",
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": run},
            "abort": abort,
        }
    )


async def test_estimates_places_a_hooks_summary_and_holds_while_work_the_hook_created_runs():
    chat = await openCompaction()
    childGate = deferred()
    Child = defineChildTask(childGate)
    addTask(chat.setup.registry, Child)

    async def beforeCompact(_compaction: Any, api: Any, hookContext: Any) -> dict[str, Any]:
        async def create(tx: Any) -> None:
            await tx.createTask(
                Child, {}, {"ownership": {"kind": "task", "taskId": api.taskId}, "conversationId": chat.root.id}
            )

        await chat.harness.commit(create, hookContext)
        return {"summary": "HOOK"}

    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": beforeCompact})
    await history(chat)
    id = await chat.root.compact(None, context)

    async def holding() -> bool:
        return (await chat.harness.getTask(id, context))["state"]["status"] == "completing"

    await waitFor(holding)
    # The summary and the status removal landed at the hold.
    assert (await kinds(chat.root))[-1] == "pi.compaction"
    assert (await live(chat)).get("compactions") is None
    childGate.resolve()
    assert (await result(chat, id))["status"] == "completed"
    await chat.harness.close(context)


async def test_estimates_removes_the_status_of_a_faulted_compaction():
    chat = await openCompaction()
    await history(chat)
    original = chat.setup.models.getModel

    def broken(*_: Any) -> Any:
        raise RuntimeError("models broke")

    chat.setup.models.getModel = broken
    outcome = await result(chat, await chat.root.compact(None, context))
    chat.setup.models.getModel = original
    toMatchObject(outcome, {"status": "faulted", "error": {"message": "models broke"}})
    assert (await live(chat)).get("compactions") is None
    await chat.harness.close(context)


# ─── Compaction events and live status ────────────────────────────────────


async def test_events_report_start_and_end_and_the_retry_backoff_in_a_late_joiners_snapshot():
    chat = await openCompaction()
    await history(chat)
    chat.setup.settings["retry"] = {"enabled": True, "maxRetries": 2, "baseDelayMs": 60_000}
    events: list[dict[str, Any]] = []
    stream = await watchEvents(chat.harness, chat.root.id, context)

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    chat.faux.summaries.append(failure("overloaded"))
    id = await chat.root.compact(None, context)

    async def backingOff() -> bool:
        compactions = (await live(chat)).get("compactions")
        return compactions is not None and len(compactions) > 0 and compactions[0].get("retry") is not None

    await waitFor(backingOff)
    late = await watchEvents(chat.harness, chat.root.id, context)
    assert late.snapshot["compactions"] == [
        {
            "taskId": id,
            "reason": "manual",
            "blocking": False,
            "attempt": 1,
            "retry": {"at": AnyOf(int), "error": "overloaded"},
        }
    ]
    await late.stop()
    await chat.harness.abortTask(id, context)
    await waitFor(lambda: any(event["type"] == "compaction_end" for event in events))
    assert [event for event in events if event["type"].startswith("compaction_")] == [
        {"type": "compaction_start", "taskId": id, "reason": "manual", "blocking": False},
        {"type": "compaction_end", "taskId": id, "reason": "manual"},
    ]
    await stream.stop()
    await chat.harness.close(context)


async def test_events_list_concurrent_compactions_in_task_id_order():
    chat = await openCompaction()
    await history(chat)
    first = deferred()
    second = deferred()
    chat.faux.summaries.extend([gated(deferred(), summary(), first), gated(deferred(), summary(), second)])
    a = await chat.root.compact(None, context)
    b = await chat.root.compact(None, context)
    await asyncio.gather(first.promise, second.promise)
    assert [status["taskId"] for status in (await live(chat))["compactions"]] == [a, b]
    await chat.root.abort(context)
    assert (await live(chat)).get("compactions") is None
    await chat.harness.close(context)


# ─── Recovery ─────────────────────────────────────────────────────────────


async def reopen(path: str, setup: Any, faux: Any) -> SimpleNamespace:
    opened = await openChat(await openNodeSqliteStorage(path), setup)
    opened.harness.resume()
    return SimpleNamespace(harness=opened.harness, root=opened.root, setup=setup, faux=faux)


async def firstOpen(path: str) -> SimpleNamespace:
    setup = chatSetup()
    addSection(setup.registry, "preamble", lambda *_: "You are helpful.", {"tag": False})
    faux = script(setup)
    opened = await openChat(await openNodeSqliteStorage(path), setup)
    setup.settings["compaction"] = MANUAL
    setup.settings["retry"] = {"enabled": True, "maxRetries": 2, "baseDelayMs": 1}
    opened.harness.resume()
    chat = SimpleNamespace(harness=opened.harness, root=opened.root, setup=setup, faux=faux)
    await history(chat)
    return chat


async def test_recovery_repeats_nothing_after_reopen_once_the_summary_is_placed(sqlitePath):
    chat = await firstOpen(sqlitePath)
    chat.faux.summaries.append(summary())
    await result(chat, await chat.root.compact(None, context))
    entries = await allEntries(chat.root)
    usage = await chat.harness.snapshot(UsageDoc, chat.root.id, context)
    await chat.harness.close(context)
    chat = await reopen(sqlitePath, chat.setup, chat.faux)
    await chat.harness.waitForIdle(context)
    assert len(chat.faux.summaryRequests) == 1
    assert await allEntries(chat.root) == entries
    assert await chat.harness.snapshot(UsageDoc, chat.root.id, context) == usage
    inspection = await chat.harness.inspect(context)
    assert [inspection["tasks"], inspection["submissions"]] == [[], []]
    await chat.harness.close(context)


async def test_recovery_fails_an_overflow_run_with_its_text_when_its_compaction_fails_after_reopen(sqlitePath):
    chat = await firstOpen(sqlitePath)
    chat.setup.settings["compaction"] = {**MANUAL, "enabled": True}
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    chat.faux.agent.append(failure(OVERFLOW))
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    await reached.promise
    generation = (await live(chat))["run"]["taskId"]
    toMatchObject(
        (await chat.harness.getTask(generation, context))["state"],
        {"status": "waiting", "checkpoint": {"phase": "prepare", "overflow": OVERFLOW}},
    )
    await chat.harness.close(context)
    chat = await reopen(sqlitePath, chat.setup, chat.faux)
    chat.faux.summaries.append(failure("bad request"))
    settled = await (await chat.harness.submission(input.id, context)).wait(context)
    toMatchObject(settled, {"status": "unanswered", "reason": "model_error", "detail": OVERFLOW})
    await chat.harness.close(context)


async def test_recovery_reruns_select_and_its_hook_after_a_crash_in_select(sqlitePath):
    chat = await firstOpen(sqlitePath)
    holder = {"calls": 0}
    reached = deferred()

    async def beforeCompact(_compaction: Any, _api: Any, hookContext: Any) -> None:
        holder["calls"] += 1
        if holder["calls"] == 1:
            reached.resolve()
            await aborted(hookContext.abortSignal)

    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": beforeCompact})
    id = await chat.root.compact(None, context)
    await reached.promise
    await chat.harness.close(context)
    chat = await reopen(sqlitePath, chat.setup, chat.faux)
    chat.faux.summaries.append(summary())
    assert (await result(chat, id))["status"] == "completed"
    assert holder["calls"] == 2
    await chat.harness.close(context)


async def test_recovery_resends_an_interrupted_summarization_once_and_counts_only_the_answered_attempt(sqlitePath):
    chat = await firstOpen(sqlitePath)
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    id = await chat.root.compact(None, context)
    await reached.promise
    usageBefore = (await chat.harness.snapshot(UsageDoc, chat.root.id, context))["models"]["faux/faux-1"]
    toMatchObject((await chat.harness.getTask(id, context))["state"], {"checkpoint": {"phase": "summarize"}})
    await chat.harness.close(context)
    chat = await reopen(sqlitePath, chat.setup, chat.faux)
    chat.faux.summaries.append(summary())
    assert (await result(chat, id))["status"] == "completed"
    assert len(chat.faux.summaryRequests) == 2
    first, resent = chat.faux.summaryRequests[0], chat.faux.summaryRequests[1]
    assert [message.model_dump(exclude={"timestamp"}) for message in resent.messages] == [
        message.model_dump(exclude={"timestamp"}) for message in first.messages
    ]
    usage = (await chat.harness.snapshot(UsageDoc, chat.root.id, context))["models"]["faux/faux-1"]
    assert usage["input"] > usageBefore["input"]
    assert usage["output"] - usageBefore["output"] == 2
    await chat.harness.close(context)


async def test_recovery_resumes_a_retry_backoff_after_reopen(sqlitePath):
    chat = await firstOpen(sqlitePath)
    clock = {"now": int(time.time() * 1000)}
    chat.setup.now = lambda: clock["now"]
    chat.setup.settings["retry"] = {"enabled": True, "maxRetries": 2, "baseDelayMs": 60_000}
    chat.faux.summaries.append(failure("overloaded"))
    id = await chat.root.compact(None, context)

    async def backingOff() -> bool:
        compactions = (await live(chat)).get("compactions")
        return compactions is not None and len(compactions) > 0 and compactions[0].get("retry") is not None

    await waitFor(backingOff)
    toMatchObject((await chat.harness.getTask(id, context))["state"], {"checkpoint": {"phase": "retry", "attempt": 1}})
    await chat.harness.close(context)
    clock["now"] += 120_000
    chat = await reopen(sqlitePath, chat.setup, chat.faux)
    chat.faux.summaries.append(summary())
    assert (await result(chat, id))["status"] == "completed"
    assert len(chat.faux.summaryRequests) == 2
    await chat.harness.close(context)


async def test_recovery_keeps_a_generation_waiting_on_its_blocking_compaction_across_reopen(sqlitePath):
    chat = await firstOpen(sqlitePath)
    setup = chat.setup
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    # The faux model window is 128k; this blocking threshold needs a small context window.
    chat.setup.settings["compaction"] = {**BLOCKING, "reserveTokens": 128_000 - 700}
    input = await chat.root.submit({"type": "input", "content": text("u4", 200)}, context)
    await reached.promise
    await chat.harness.close(context)
    chat = await reopen(sqlitePath, setup, chat.faux)
    chat.faux.summaries.append(summary())
    chat.faux.agent.append(answer("a4"))
    assert (await (await chat.harness.submission(input.id, context)).wait(context))["status"] == "done"
    assert (await kinds(chat.root))[-3:] == ["pi.compaction", "pi.system", "pi.assistant"]
    await chat.harness.close(context)


async def test_recovery_keeps_a_queued_summary_across_reopen_and_places_it_at_the_next_boundary(sqlitePath):
    chat = await firstOpen(sqlitePath)
    reached = deferred()
    chat.faux.agent.append(gated(deferred(), answer("never"), reached))
    input = await chat.root.submit({"type": "input", "content": "busy"}, context)
    await reached.promise
    chat.faux.summaries.append(summary())
    outcome = await result(chat, await chat.root.compact(None, context))
    await chat.harness.close(context)
    chat = await reopen(sqlitePath, chat.setup, chat.faux)
    chat.faux.agent.append(answer("answered"))
    assert (await (await chat.harness.submission(input.id, context)).wait(context))["status"] == "done"
    assert (await (await chat.harness.submission(submissionIdOf(outcome), context)).wait(context))["status"] == "done"
    await chat.harness.close(context)


# ─── Compaction and the inbox ─────────────────────────────────────────────


async def queuedSummary(chat: Any, content: str = "SUMMARY") -> Any:
    chat.faux.summaries.append(summary(content))
    outcome = await result(chat, await chat.root.compact(None, context))
    return await chat.harness.submission(submissionIdOf(outcome), context)


async def busyRun(chat: Any, reply: Any = None) -> SimpleNamespace:
    gate = deferred()
    reached = deferred()
    chat.faux.agent.append(gated(gate, reply if reply is not None else answer("done"), reached))
    input = await chat.root.submit({"type": "input", "content": "busy"}, context)
    await reached.promise
    return SimpleNamespace(input=input, release=lambda: gate.resolve())


@pytest.mark.parametrize("order", ["summary first", "reset first"])
async def test_inbox_places_a_reset_queued_after_the_summary_last_and_makes_a_summary_queued_after_a_reset_stale(order):
    chat = await openCompaction()
    await history(chat)
    run = await busyRun(chat)
    if order == "summary first":
        submission = await queuedSummary(chat)
        await chat.root.reset(None, context)
    else:
        await chat.root.reset(None, context)
        submission = await queuedSummary(chat)
    run.release()
    await run.input.wait(context)
    settled = await submission.wait(context)
    assert settled["status"] == ("done" if order == "summary first" else "unanswered")
    assert (await kinds(chat.root))[-1] == "pi.reset"
    assert (await chat.root.context(context))["head"]["kind"] == "pi.reset"
    await chat.harness.close(context)


async def test_inbox_places_a_summary_left_queued_by_a_failed_run_at_the_next_submission_before_its_input():
    chat = await openCompaction()
    await history(chat)
    run = await busyRun(chat, failure("bad request"))
    submission = await queuedSummary(chat)
    run.release()
    assert (await run.input.wait(context))["status"] == "unanswered"
    assert (await submission.status(context))["status"] == "queued"
    await turn(chat, "again", "ok")
    assert (await submission.status(context))["status"] == "done"
    request = chat.faux.agentRequests[-1].messages
    assert "SUMMARY" in firstUserText(request)
    assert "again" in [userText(message) for message in request]
    await chat.harness.close(context)


async def test_inbox_keeps_the_full_retry_budget_after_an_overflow_compaction():
    chat = await openCompaction()
    await history(chat)
    chat.setup.settings["compaction"] = {**MANUAL, "enabled": True}
    chat.faux.summaries.append(summary())
    chat.faux.agent.extend(
        [failure("prompt is too long"), failure("overloaded"), failure("overloaded"), answer("finally")]
    )
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    assert (await input.wait(context))["status"] == "done"
    await chat.harness.close(context)


async def test_inbox_loses_an_application_edit_placed_while_a_compaction_summarizes():
    chat = await openCompaction()
    await history(chat)
    u1 = (await allEntries(chat.root))[0]
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary(), reached))
    id = await chat.root.compact(None, context)
    await reached.promise
    edit = {"target": u1["id"], "action": "replace", "messages": [user("REDACTED")]}
    await chat.root.submit({"type": "write", "entry": {"kind": "app.redact", "edits": [edit]}}, context)
    gate.resolve()
    await result(chat, id)
    # The summary was made from the unredacted entry, and the edit's target left the range.
    assert "[User]: u1 " in userText(chat.faux.summaryRequests[0].messages[1])
    assert "REDACTED" not in [userText(message) for message in (await chat.root.context(context))["messages"]]
    await chat.harness.close(context)


async def test_inbox_keeps_the_kept_entries_mounted_in_the_conversation_view():
    chat = await openCompaction()
    await history(chat)
    state = await chat.root.viewState(context)
    chat.faux.summaries.append(summary())
    await result(chat, await chat.root.compact(None, context))
    await waitFor(lambda: state.value["entries"][0]["kind"] == "pi.compaction")
    assert state.value["entries"] == (await chat.root.context(context))["entries"]
    state.dispose()
    await chat.harness.close(context)


# ─── Edge cases ───────────────────────────────────────────────────────────


async def test_edge_keeps_the_one_compaction_limit_through_a_retry_backoff():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    holder = {"asked": 0}

    def beforeCompact(*_: Any) -> dict[str, Any]:
        holder["asked"] += 1
        return {"decline": True}

    addHooks(chat.setup.registry, CompactionTask, {"beforeCompact": beforeCompact})
    chat.faux.agent.append(failure("overloaded"))
    await turn(chat, text("u4", 200), "a4")
    # One blocking compaction, declined; the retried preparation, still above the threshold, started no other.
    assert holder["asked"] == 1
    assert len(chat.faux.agentRequests) == 5
    await chat.harness.close(context)


async def test_edge_does_not_start_a_background_compaction_when_a_manual_one_was_admitted_during_preparation():
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    chat.setup.settings["compaction"] = BACKGROUND
    rendering = deferred()
    release = deferred()
    holder = {"hold": True}

    async def slowSection(*_: Any) -> str:
        if holder["hold"]:
            holder["hold"] = False
            rendering.resolve()
            await release.promise
        return "slow"

    addSection(chat.setup.registry, "slow", slowSection)
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    chat.faux.agent.append(answer("a4"))
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    await rendering.promise
    manual = await chat.root.compact(None, context)
    await reached.promise
    release.resolve()
    assert (await input.wait(context))["status"] == "done"
    assert [task["id"] for task in await compactionTasks(chat)] == [manual]
    # A background compaction would have sent its own summarization request.
    assert len(chat.faux.summaryRequests) == 1
    await chat.harness.abortTask(manual, context)
    await chat.harness.close(context)


async def test_edge_starts_no_background_compaction_after_a_blocking_one_in_the_same_generation():
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    # Blocking at 1500, background at 200: the kept part stays above the background threshold.
    chat.setup.settings["compaction"] = {**BACKGROUND, "backgroundTokens": 1300, "keepRecentTokens": 400}
    chat.faux.summaries.append(summary())
    await turn(chat, text("u4", 1000), "a4")
    assert len(chat.faux.summaryRequests) == 1
    assert await compactionTasks(chat) == []
    await chat.harness.close(context)


async def test_edge_fails_the_run_when_its_blocking_compaction_faults_and_records_the_fault():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    original = chat.setup.models.completeSimple

    def broken(*_: Any) -> Any:
        raise RuntimeError("no credentials")

    chat.setup.models.completeSimple = broken
    input = await chat.root.submit({"type": "input", "content": text("u4", 200)}, context)
    toMatchObject(
        await input.wait(context),
        {"status": "unanswered", "reason": "model_error", "detail": "Compaction failed: no credentials"},
    )
    chat.setup.models.completeSimple = original
    assert "pi.compaction" not in await kinds(chat.root)
    state = await live(chat)
    assert state.get("compactions") is None
    toMatchObject(state.get("compactionFailure"), {"reason": "threshold", "error": "no credentials"})
    await chat.harness.close(context)


@pytest.mark.parametrize("how", ["abort", "overflow"])
async def test_edge_fails_an_overflow_run_with_its_text_when_its_compaction_is_aborted_directly_or_itself_overflows(how):
    chat = await openCompaction()
    await history(chat)
    chat.setup.settings["compaction"] = {**MANUAL, "enabled": True}
    reached = deferred()
    if how == "abort":
        chat.faux.summaries.append(gated(deferred(), summary(), reached))
    else:
        chat.faux.summaries.append(failure("prompt is too long for the summary"))
    chat.faux.agent.append(failure(OVERFLOW))
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    if how == "abort":
        await reached.promise
        [child] = await compactionTasks(chat)
        await chat.harness.abortTask(child["id"], context)
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "model_error", "detail": OVERFLOW})
    assert (await live(chat)).get("compactions") is None
    await chat.harness.close(context)


async def test_edge_treats_a_length_stop_as_an_ordinary_answer_not_an_overflow():
    chat = await openCompaction()
    await history(chat)
    chat.setup.settings["compaction"] = {**MANUAL, "enabled": True}
    chat.faux.agent.append(fauxAssistantMessage("cut short", {"stopReason": "length", "errorMessage": OVERFLOW}))
    input = await chat.root.submit({"type": "input", "content": "go"}, context)
    assert (await input.wait(context))["status"] == "done"
    assert len(chat.faux.summaryRequests) == 0
    await chat.harness.close(context)


async def test_edge_orders_the_events_of_a_summary_placed_at_once():
    chat = await openCompaction()
    await history(chat)
    events: list[dict[str, Any]] = []
    stream = await watchEvents(chat.harness, chat.root.id, context)

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    chat.faux.summaries.append(summary())
    await result(chat, await chat.root.compact(None, context))
    await waitFor(lambda: any(event["type"] == "compaction_end" for event in events))
    end = next(index for index, event in enumerate(events) if event["type"] == "compaction_end")
    batch = [event["type"] for event in events[max(0, end - 2) : end + 3]]
    assert batch == ["message_start", "message_end", "compaction_end", "submission", "usage_changed"]
    await stream.stop()
    await chat.harness.close(context)


async def test_edge_puts_compaction_start_last_in_the_batch_of_the_preparation_commit():
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    chat.setup.settings["compaction"] = BACKGROUND
    batches: list[list[dict[str, Any]]] = []
    stream = await watchEvents(chat.harness, chat.root.id, context)

    async def onBatch(batch: Any, *_: Any) -> None:
        batches.append(list(batch))

    stream.start(onBatch)
    # A new section makes the preparation commit append a system entry next to the compaction's status.
    addSection(chat.setup.registry, "extra", lambda *_: "extra")
    chat.faux.summaries.append(gated(deferred(), summary()))
    await turn(chat, text("u4", 100), "a4")
    await waitFor(lambda: any(any(event["type"] == "compaction_start" for event in batch) for batch in batches))
    batch = next(candidate for candidate in batches if any(event["type"] == "compaction_start" for event in candidate))
    assert [event["type"] for event in batch] == ["message_start", "message_end", "compaction_start"]
    await stream.stop()
    await chat.harness.abortTask((await compactionTasks(chat))[0]["id"], context)
    await chat.harness.close(context)


async def test_edge_keeps_a_background_summary_queued_through_a_retry_backoff_while_a_blocking_compaction_wins():
    chat = await openCompaction({"contextWindow": 2000})
    await history(chat)
    chat.setup.settings["compaction"] = BACKGROUND
    chat.setup.settings["retry"] = {"enabled": True, "maxRetries": 2, "baseDelayMs": 300}
    summaryGate = deferred()
    summaryReached = deferred()
    chat.faux.summaries.append(gated(summaryGate, summary("BACKGROUND"), summaryReached))
    chat.faux.summaries.append(summary("BLOCKING"))
    failed = deferred()

    async def overloaded(request: Any) -> Any:
        failed.resolve()
        return failure("overloaded")

    chat.faux.agent.append(overloaded)
    chat.faux.agent.append(answer("a4"))
    input = await chat.root.submit({"type": "input", "content": text("u4", 100)}, context)
    await asyncio.gather(failed.promise, summaryReached.promise)

    # During the backoff: the background summary queues, and the thresholds drop so the retry blocks.
    async def retrying() -> bool:
        return ((await live(chat)).get("generation") or {}).get("retry") is not None

    await waitFor(retrying)
    [background] = await compactionTasks(chat)
    summaryGate.resolve()
    queued = await result(chat, background["id"])
    submission = await chat.harness.submission(submissionIdOf(queued), context)
    assert (await submission.status(context))["status"] == "queued"
    chat.setup.settings["compaction"] = {**BACKGROUND, "reserveTokens": 1500, "keepRecentTokens": 50}
    assert (await input.wait(context))["status"] == "done"
    assert "BLOCKING" in firstUserText(chat.faux.agentRequests[-1].messages)
    toMatchObject(await submission.wait(context), {"status": "unanswered", "reason": "stale"})
    await chat.harness.close(context)


async def test_edge_summarizes_the_previous_summary_in_a_second_compaction():
    chat = await openCompaction()
    await history(chat)
    chat.faux.summaries.append(summary("FIRST"))
    await result(chat, await chat.root.compact(None, context))
    await turn(chat, text("u4", 100), text("a4", 100))
    await turn(chat, text("u5", 100), text("a5", 100))
    chat.faux.summaries.append(summary("SECOND"))
    await result(chat, await chat.root.compact(None, context))
    prompt = userText(chat.faux.summaryRequests[1].messages[1])
    assert re.match(r"^<conversation>\n\[User\]: The conversation history before this point was compacted", prompt)
    assert "FIRST" in prompt
    assert "SECOND" in firstUserText((await chat.root.context(context))["messages"])
    await chat.harness.close(context)


async def test_edge_leaves_no_usage_submission_or_summary_when_the_classifying_commit_fails_which_fails_the_harness():
    storage = ControlledStorage()
    chat = await openCompaction({"storage": storage})
    await history(chat)

    async def usage() -> Any:
        return (await chat.harness.snapshot(UsageDoc, chat.root.id, context))["models"]["faux/faux-1"]

    before = await usage()
    failure = RuntimeError("disk gone")

    async def failing(request: Any) -> Any:
        storage.failNextCommit(failure)
        return summary()

    chat.faux.summaries.append(failing)
    id = await chat.root.compact(None, context)
    assert await chat.harness.closed == {"reason": "failed", "error": failure}
    await chat.harness.close(context)
    # Nothing of the failed commit landed: the task is still live, without usage, entry, or submission.
    storage.reopen()
    assert await storage.submissionByRequest(chat.root.id, f"compaction:{id}", context) is None
    assert ((await storage.task(id, context)) or {}).get("state", {}).get("status") != "terminal"
    reopened = await openChat(storage, chat.setup)
    usageAfter = await reopened.harness.snapshot(UsageDoc, reopened.root.id, context)
    assert usageAfter["models"]["faux/faux-1"] == before
    assert "pi.compaction" not in await kinds(reopened.root)
    await reopened.harness.close(context)


# ─── Blocking and manual compaction together ──────────────────────────────


async def blockingRun(chat: Any) -> SimpleNamespace:
    """A run whose generation waits on a blocking compaction whose summary is held; returns the input and gate."""
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    gate = deferred()
    reached = deferred()
    chat.faux.summaries.append(gated(gate, summary("BLOCKING"), reached))
    input = await chat.root.submit({"type": "input", "content": text("u4", 200)}, context)
    await reached.promise
    [blocking] = await compactionTasks(chat)
    return SimpleNamespace(input=input, gate=gate, blocking=blocking["id"])


async def test_together_places_a_manual_summary_selected_before_the_blocking_one_landed_its_equal_cut_replaces_it():
    chat = await openCompaction({"contextWindow": 1000})
    run = await blockingRun(chat)
    # Selected from the same context as the blocking compaction, so it cuts at the same entry.
    chat.faux.summaries.append(summary("MANUAL"))
    manual = await result(chat, await chat.root.compact(None, context))
    submission = await chat.harness.submission(submissionIdOf(manual), context)
    assert (await submission.status(context))["status"] == "queued"
    chat.faux.agent.append(answer("a4"))
    run.gate.resolve()
    assert (await run.input.wait(context))["status"] == "done"
    # The request after the blocking compaction used its summary; the manual one landed at the final boundary.
    assert "BLOCKING" in firstUserText(chat.faux.agentRequests[-1].messages)
    assert (await submission.wait(context))["status"] == "done"
    markers = [record for record in await allEntries(chat.root) if record["kind"] == "pi.compaction"]
    assert len(markers) == 2
    assert markers[1]["head"] == markers[0]["head"]
    messages = (await chat.root.context(context))["messages"]
    assert "MANUAL" in firstUserText(messages)
    assert not any("BLOCKING" in userText(message) for message in messages)
    await chat.harness.close(context)


async def test_together_finds_nothing_to_compact_for_a_manual_compaction_selected_after_the_blocking_summary_landed():
    chat = await openCompaction({"contextWindow": 1000})
    run = await blockingRun(chat)
    answerGate = deferred()
    answerReached = deferred()
    chat.faux.agent.append(gated(answerGate, answer("a4"), answerReached))
    run.gate.resolve()
    await answerReached.promise
    assert await result(chat, await chat.root.compact(None, context)) == {"status": "completed", "result": {}}
    assert len(chat.faux.summaryRequests) == 1
    answerGate.resolve()
    assert (await run.input.wait(context))["status"] == "done"
    await chat.harness.close(context)


async def test_together_aborts_the_run_and_both_compactions_on_esc_and_appends_nothing():
    chat = await openCompaction({"contextWindow": 1000})
    run = await blockingRun(chat)
    manualReached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary("MANUAL"), manualReached))
    manual = await chat.root.compact(None, context)
    await manualReached.promise
    assert [status["blocking"] for status in (await live(chat))["compactions"]] == [True, False]
    await chat.root.abort(context)
    toMatchObject(await run.input.wait(context), {"status": "unanswered", "reason": "aborted"})
    assert (await result(chat, run.blocking))["status"] == "aborted"
    assert (await result(chat, manual))["status"] == "aborted"
    assert (await live(chat)).get("compactions") is None
    assert (await live(chat)).get("run") is None
    assert "pi.compaction" not in await kinds(chat.root)
    assert (await chat.harness.inspect(context))["submissions"] == []
    await chat.harness.close(context)


async def test_together_places_a_summary_that_survived_esc_at_the_next_submission_before_its_input():
    chat = await openCompaction()
    await history(chat)
    reached = deferred()
    chat.faux.agent.append(gated(deferred(), answer("never"), reached))
    busy = await chat.root.submit({"type": "input", "content": "busy"}, context)
    await reached.promise
    chat.faux.summaries.append(summary())
    outcome = await result(chat, await chat.root.compact(None, context))
    submission = await chat.harness.submission(submissionIdOf(outcome), context)
    await chat.root.abort(context)
    toMatchObject(await busy.wait(context), {"status": "unanswered", "reason": "aborted"})
    assert (await submission.status(context))["status"] == "queued"
    await turn(chat, "u5", "a5")
    assert (await submission.status(context))["status"] == "done"
    request = chat.faux.agentRequests[-1].messages
    assert "SUMMARY" in firstUserText(request)
    assert "u5" in [userText(message) for message in request]
    tail = [record["kind"] for record in (await allEntries(chat.root))[-4:]]
    assert tail == ["pi.compaction", "pi.user", "pi.system", "pi.assistant"]
    await chat.harness.close(context)


# ─── Pinning, silent overflow, and late policy changes ────────────────────


async def test_pinning_keeps_the_pinned_model_through_a_retry_and_advances_the_live_attempt():
    setup = chatSetup(
        {
            "models": [
                {"id": "faux-1", "contextWindow": 100_000, "maxTokens": 900},
                {"id": "faux-2", "contextWindow": 100_000, "maxTokens": 900},
            ]
        }
    )
    chat = await openCompaction({}, setup)
    await history(chat)
    holder: dict[str, Any] = {}

    async def switched(request: Any) -> Any:
        # Switched during the attempt: the retry still uses the pinned model.
        await chat.root.configure({"model": {"provider": "faux", "modelId": "faux-2"}}, context)
        return failure("overloaded")

    async def second(request: Any) -> Any:
        holder["attempt"] = ((await live(chat)).get("compactions") or [{}])[0].get("attempt")
        return summary()

    chat.faux.summaries.append(switched)
    chat.faux.summaries.append(second)
    assert (await result(chat, await chat.root.compact(None, context)))["status"] == "completed"
    assert [request.model for request in chat.faux.summaryRequests] == ["faux-1", "faux-1"]
    assert holder["attempt"] == 2
    usage = (await chat.harness.snapshot(UsageDoc, chat.root.id, context))["models"]
    assert list(usage.keys()) == ["faux/faux-1"]
    await chat.harness.close(context)


async def test_pinning_shows_a_late_joiner_a_compaction_that_is_summarizing():
    chat = await openCompaction()
    await history(chat)
    reached = deferred()
    chat.faux.summaries.append(gated(deferred(), summary(), reached))
    id = await chat.root.compact(None, context)
    await reached.promise
    stream = await watchEvents(chat.harness, chat.root.id, context)
    assert stream.snapshot["compactions"] == [{"taskId": id, "reason": "manual", "blocking": False, "attempt": 1}]
    await stream.stop()
    await chat.harness.abortTask(id, context)
    await chat.harness.close(context)


@pytest.mark.parametrize(
    ("name", "response"),
    [
        ("a stop whose input exceeds the window", answer("fine")),
        ("a length stop that fills the window without output", fauxAssistantMessage("", {"stopReason": "length"})),
    ],
)
async def test_pinning_treats_silent_overflow_as_an_ordinary_answer(name, response):
    chat = await openCompaction({"contextWindow": 300})
    await history(chat)
    # Thresholds out of reach, so only overflow classification could compact.
    chat.setup.settings["compaction"] = {
        "enabled": True,
        "reserveTokens": -100_000,
        "keepRecentTokens": 150,
        "backgroundTokens": 0,
    }
    chat.faux.agent.append(response)
    input = await chat.root.submit({"type": "input", "content": "go"}, context)
    assert (await input.wait(context))["status"] == "done"
    last = (await allEntries(chat.root))[-1]
    message = last["model"][0]
    assert message["usage"]["input"] >= 300
    assert len(chat.faux.summaryRequests) == 0
    await chat.harness.close(context)


async def test_pinning_sends_the_request_when_a_blocking_compaction_finds_nothing_under_a_policy_changed_after_preparation():
    chat = await openCompaction({"contextWindow": 1000})
    await history(chat)
    chat.setup.settings["compaction"] = BLOCKING
    holder = {"changed": False}

    async def policy(*_: Any) -> str:
        # Rendering runs after preparation read the policy; the compaction reads this one.
        if not holder["changed"]:
            holder["changed"] = True
            chat.setup.settings["compaction"] = {**BLOCKING, "keepRecentTokens": 100_000}
        return "p"

    addSection(chat.setup.registry, "policy", policy)
    await turn(chat, text("u4", 200), "a4")
    compactions = [
        task for task in (await chat.harness.inspect(context))["tasks"] if task["record"]["kind"] == "pi.compaction"
    ]
    assert compactions == []
    assert len(chat.faux.summaryRequests) == 0
    assert "pi.compaction" not in await kinds(chat.root)
    await chat.harness.close(context)


# ─── Blocked compaction ───────────────────────────────────────────────────


async def test_blocked_survives_reopen_blocked_and_is_orphaned_on_abort_with_its_status_removed(sqlitePath):
    setup = chatSetup()
    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    harness, root = opened.harness, opened.root
    # A compaction stored by a newer version than this process registers, for example after a downgrade.
    newer = {"definition": {**CompactionTask["definition"], "version": 2}}

    async def create(tx: Any) -> int:
        taskId = await tx.createTask(newer, {"reason": "manual"}, {"ownership": {"kind": "conversation"}})
        liveDoc = await tx.doc(LiveDoc, root.id)
        liveDoc["compactions"] = [{"taskId": taskId, "reason": "manual", "blocking": False, "attempt": 1}]
        return taskId  # type: ignore[no-any-return]

    id = await root.commit(create, context)
    await harness.close(context)
    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    chat = SimpleNamespace(harness=harness, root=root, setup=setup, faux=script(setup))
    events: list[dict[str, Any]] = []
    stream = await watchEvents(chat.harness, chat.root.id, context)

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    inspection = await chat.harness.inspect(context)
    inspected = next(task for task in inspection["tasks"] if task["record"]["id"] == id)
    assert inspected["state"] == {"kind": "blocked", "reason": "task_too_old"}
    await chat.harness.abortTask(id, context)
    assert (await chat.harness.waitForTask(id, context))["state"]["outcome"] == {
        "status": "orphaned",
        "reason": "task_too_old",
    }
    assert (await live(chat)).get("compactions") is None
    await waitFor(lambda: any(event["type"] == "compaction_end" for event in events))
    await stream.stop()
    await chat.harness.close(context)


# ─── Context contributions ────────────────────────────────────────────────


async def test_contributions_apply_edits_carried_by_an_older_head_marker_in_the_range():
    chat = await openCompaction()

    async def build(tx: Any) -> dict[str, int]:
        a = await tx.appendEntry(chat.root.id, {"kind": "app.note", "model": [user("a")]})
        b = await tx.appendEntry(chat.root.id, {"kind": "app.note", "model": [user("b")]})
        # An older marker that omits b, then a newer one whose range still contains the older marker.
        await tx.appendEntry(
            chat.root.id, {"kind": "app.head", "head": a["id"], "edits": [{"target": b["id"], "action": "omit"}]}
        )
        c = await tx.appendEntry(chat.root.id, {"kind": "app.note", "model": [user("c")]})
        await tx.appendEntry(chat.root.id, {"kind": "app.head", "head": a["id"], "model": [user("H")]})
        return {"a": a["id"], "b": b["id"], "c": c["id"]}

    ids = await chat.root.commit(build, context)
    contextView = await chat.root.context(context)
    assert [record["id"] for record in contextView["entries"]][1:] == [ids["a"], ids["b"], ids["c"]]
    assert [[userText(message) for message in messages] for messages in contextView["contributions"]] == [
        ["H"],
        ["a"],
        [],
        ["c"],
    ]
    assert [userText(message) for message in contextView["messages"]] == ["H", "a", "c"]
    # The summarizer sees the same contributions: the omitted entry stays out.
    chat.setup.settings["compaction"] = {**MANUAL, "keepRecentTokens": 1}
    chat.faux.summaries.append(summary())
    await result(chat, await chat.root.compact(None, context))
    prompt = userText(chat.faux.summaryRequests[0].messages[1])
    assert "<conversation>\n[User]: H\n\n[User]: a\n</conversation>" in prompt
    await chat.harness.close(context)
