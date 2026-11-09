"""Translated from pi's ``packages/coding-agent/test/session-context-edit.test.ts`` (v1.1.0)."""

from __future__ import annotations

import dataclasses
import time
from typing import Any

from misaka.core.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    estimateProjectedContextTokens,
    prepareCompaction,
)
from misaka.core.session_manager import SessionManager
from misaka.modes.json_event import to_jsonable
from misaka.utils.js import jsonStringify
from misaka.utils.values import read_field


def nowMs() -> int:
    return int(time.time() * 1000)


def assistant(text: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "faux",
        "provider": "faux",
        "model": "faux",
        "usage": {
            "input": 10,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 11,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": nowMs(),
    }


def text(message: Any) -> str:
    content = read_field(message, "content")
    if isinstance(content, str):
        return content
    return "".join(read_field(part, "text") or "" for part in content if read_field(part, "type") == "text")


def roles(messages: list[Any]) -> list[str]:
    return [read_field(message, "role") for message in messages]


def summaryOrText(message: Any) -> str:
    summary = read_field(message, "summary")
    return summary if summary is not None else text(message)


def stringify(value: Any) -> str:
    return jsonStringify(to_jsonable(value))


def keepOne() -> Any:
    return dataclasses.replace(DEFAULT_COMPACTION_SETTINGS, keepRecentTokens=1)


def contentBlocks(message: Any) -> list[tuple[Any, Any]]:
    return [(read_field(block, "type"), read_field(block, "text")) for block in read_field(message, "content")]


def test_omits_a_target_only_from_model_projection():
    session = SessionManager.inMemory()
    session.appendMessage({"role": "user", "content": "request", "timestamp": nowMs()})
    assistantId = session.appendMessage(assistant("partial"))
    result = {
        "role": "toolResult",
        "toolCallId": "call-1",
        "toolName": "read",
        "content": [{"type": "text", "text": "raw output"}],
        "details": {"path": "large.txt"},
        "isError": True,
        "timestamp": nowMs(),
    }
    resultId = session.appendMessage(result)
    session.appendContextEdit(assistantId, None)
    session.appendContextEdit(resultId, None)

    assert len([entry for entry in session.getBranch() if entry["type"] == "message"]) == 3
    assert roles(session.buildSessionProjection().messages) == ["user"]
    assert read_field(session.getEntry(resultId), "message") == result


def test_replaces_only_content_and_lets_the_latest_edit_win():
    session = SessionManager.inMemory()
    targetId = session.appendMessage(assistant("original"))
    session.appendContextEdit(targetId, {"content": [{"type": "text", "text": "first"}]})
    session.appendContextEdit(targetId, None)
    session.appendContextEdit(targetId, {"content": [{"type": "text", "text": "restored"}]})

    projected = session.buildSessionProjection().messages[0]
    assert read_field(projected, "role") == "assistant"
    assert text(projected) == "restored"
    assert read_field(read_field(projected, "usage"), "totalTokens") == 11
    assert text(read_field(session.getEntry(targetId), "message")) == "original"


def test_normalizes_string_replacements_for_array_only_assistant_and_tool_result_roles():
    session = SessionManager.inMemory()
    assistantId = session.appendMessage(assistant("original"))
    resultId = session.appendMessage({
        "role": "toolResult",
        "toolCallId": "call-1",
        "toolName": "read",
        "content": [{"type": "text", "text": "original result"}],
        "isError": False,
        "timestamp": nowMs(),
    })
    assistantEditId = session.appendContextEdit(assistantId, {"content": "assistant replacement"})
    resultEditId = session.appendContextEdit(resultId, {"content": "result replacement"})

    assert contentBlocks(read_field(session.getEntry(assistantEditId), "replacement")) == [
        ("text", "assistant replacement")
    ]
    assert contentBlocks(read_field(session.getEntry(resultEditId), "replacement")) == [("text", "result replacement")]
    projected = session.buildSessionProjection().messages
    assert read_field(projected[0], "role") == "assistant"
    assert contentBlocks(projected[0]) == [("text", "assistant replacement")]
    assert read_field(projected[1], "role") == "toolResult"
    assert contentBlocks(projected[1]) == [("text", "result replacement")]


def test_normalizes_imported_string_replacements_while_projecting_array_only_roles():
    session = SessionManager.inMemory()
    assistantId = session.appendMessage(assistant("original"))
    editId = session.appendContextEdit(assistantId, None)
    edit = session.getEntry(editId)
    assert read_field(edit, "type") == "context_edit", "expected context edit"
    edit["replacement"] = {"content": "imported replacement"}

    projected = session.buildSessionProjection().messages[0]
    assert read_field(projected, "role") == "assistant"
    assert contentBlocks(projected) == [("text", "imported replacement")]


def test_keeps_edits_branch_relative():
    session = SessionManager.inMemory()
    targetId = session.appendMessage({"role": "user", "content": "original", "timestamp": nowMs()})
    session.appendContextEdit(targetId, {"content": "edited"})
    assert text(session.buildSessionProjection().messages[0]) == "edited"

    session.branch(targetId)
    assert text(session.buildSessionProjection().messages[0]) == "original"


def test_uses_a_self_referencing_compaction_to_retain_no_preceding_entries():
    session = SessionManager.inMemory()
    session.appendMessage({"role": "user", "content": "discarded", "timestamp": nowMs()})
    compactionId = session.appendCompaction("exact handoff", None, 100)
    session.appendMessage({"role": "user", "content": "after", "timestamp": nowMs()})

    compaction = session.getEntry(compactionId)
    assert read_field(compaction, "type") == "compaction"
    assert read_field(compaction, "firstKeptEntryId") == compactionId
    assert roles(session.buildSessionProjection().messages) == ["compactionSummary", "user"]
    assert [summaryOrText(message) for message in session.buildSessionProjection().messages] == [
        "exact handoff",
        "after",
    ]


def test_applies_post_compaction_edits_to_retained_pre_compaction_entries():
    session = SessionManager.inMemory()
    session.appendMessage({"role": "user", "content": "summarized", "timestamp": nowMs()})
    retainedId = session.appendMessage({"role": "user", "content": "original retained", "timestamp": nowMs()})
    session.appendCompaction("summary", retainedId, 100)
    session.appendContextEdit(retainedId, {"content": "edited retained"})

    assert [summaryOrText(message) for message in session.buildSessionProjection().messages] == [
        "summary",
        "edited retained",
    ]


def test_uses_only_the_newest_summary_when_a_repeated_compaction_retains_entries_before_the_older_compaction():
    session = SessionManager.inMemory()
    session.appendMessage({"role": "user", "content": "summarized first", "timestamp": nowMs()})
    retainedId = session.appendMessage({"role": "user", "content": "retained", "timestamp": nowMs()})
    session.appendCompaction("first summary", retainedId, 100)
    session.appendMessage(assistant("after first compaction"))
    session.appendCompaction("second summary", retainedId, 80)
    session.appendMessage({"role": "user", "content": "new tail " * 100, "timestamp": nowMs()})

    summaries = [
        read_field(message, "summary")
        for message in session.buildSessionProjection().messages
        if read_field(message, "summary") is not None
    ]
    assert summaries == ["second summary"]
    preparation = prepareCompaction(session.getBranch(), keepOne())
    assert read_field(preparation, "previousSummary") == "second summary"


def test_supports_repeated_retain_none_compactions():
    session = SessionManager.inMemory()
    session.appendMessage({"role": "user", "content": "discarded", "timestamp": nowMs()})
    session.appendCompaction("first handoff", None, 100)
    session.appendMessage({"role": "user", "content": "also discarded", "timestamp": nowMs()})
    secondId = session.appendCompaction("second handoff", None, 50)

    assert read_field(session.getEntry(secondId), "firstKeptEntryId") == secondId
    assert [read_field(message, "summary") or "" for message in session.buildSessionProjection().messages] == [
        "second handoff"
    ]


def test_does_not_trust_pre_edit_assistant_usage_for_projected_context_estimates():
    session = SessionManager.inMemory()
    largeUserId = session.appendMessage({"role": "user", "content": "discarded input " * 2_000, "timestamp": nowMs()})
    response = assistant("small answer")
    response["usage"] = {**response["usage"], "input": 10_000, "totalTokens": 10_001}
    assistantId = session.appendMessage(response)
    session.appendContextEdit(largeUserId, None)

    editedEstimate = estimateProjectedContextTokens(session.buildSessionProjection(), session.getBranch())
    assert editedEstimate.usageTokens == 0
    assert editedEstimate.tokens < 100

    session.appendContextEdit(assistantId, None)
    assert estimateProjectedContextTokens(session.buildSessionProjection(), session.getBranch()).tokens == 0


def test_uses_assistant_usage_captured_after_the_latest_context_edit():
    session = SessionManager.inMemory()
    userId = session.appendMessage({"role": "user", "content": "original", "timestamp": nowMs()})
    session.appendContextEdit(userId, {"content": "edited"})
    response = assistant("answer")
    response["usage"] = {**response["usage"], "input": 4_000, "output": 100, "totalTokens": 4_100}
    session.appendMessage(response)
    session.appendMessage({"role": "user", "content": "next", "timestamp": nowMs()})

    estimate = estimateProjectedContextTokens(session.buildSessionProjection(), session.getBranch())
    assert estimate.usageTokens == 4_100
    assert estimate.trailingTokens == 1
    assert estimate.tokens == 4_101


def test_does_not_reuse_post_edit_assistant_usage_after_a_later_compaction():
    session = SessionManager.inMemory()
    userId = session.appendMessage({"role": "user", "content": "small input", "timestamp": nowMs()})
    session.appendContextEdit(userId, {"content": "edited input"})
    response = assistant("answer")
    response["usage"] = {**response["usage"], "input": 50_000, "output": 1, "totalTokens": 50_001}
    session.appendMessage(response)
    session.appendCompaction("small summary", userId, 50_001)

    estimate = estimateProjectedContextTokens(session.buildSessionProjection(), session.getBranch())
    assert estimate.usageTokens == 0
    assert estimate.tokens < 100


def test_includes_effective_system_and_tool_context_in_edited_estimates():
    session = SessionManager.inMemory()
    session.appendMessage({
        "role": "system",
        "content": "system prompt " * 3_000,
        "toolsAdded": [
            {
                "name": "example",
                "description": "tool declaration " * 100,
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "timestamp": nowMs(),
    })
    userId = session.appendMessage({"role": "user", "content": "ask", "timestamp": nowMs()})
    session.appendMessage(assistant("done"))
    session.appendContextEdit(userId, {"content": "ask"})

    assert estimateProjectedContextTokens(session.buildSessionProjection(), session.getBranch()).tokens > 10_000


def test_does_not_advance_past_a_boundary_replacement_of_the_candidate_input():
    session = SessionManager.inMemory()
    session.appendMessage({"role": "user", "content": "old request", "timestamp": nowMs()})
    session.appendMessage(assistant("old answer"))
    replacedUserId = session.appendMessage({"role": "user", "content": "original input", "timestamp": nowMs()})
    assistantId = session.appendMessage(assistant("answered original input"))
    session.appendContextEdit(replacedUserId, {"content": "NEW-INSTRUCTION " * 100})
    session.appendContextEdit(assistantId, None)
    session.appendCustomEntry("bookkeeping", {"source": "test"})

    preparation = prepareCompaction(session.getBranch(), keepOne())
    assert read_field(preparation, "firstKeptEntryId") == replacedUserId
    assert "NEW-INSTRUCTION" not in stringify(read_field(preparation, "messagesToSummarize"))
    assert "NEW-INSTRUCTION" not in stringify(read_field(preparation, "turnPrefixMessages"))


def test_does_not_let_metadata_move_the_cut_past_unsent_boundary_input():
    session = SessionManager.inMemory()
    session.appendMessage({"role": "user", "content": "old request", "timestamp": nowMs()})
    session.appendMessage(assistant("old answer"))
    instructionId = session.appendCustomMessageEntry("next-work", "UNSENT-INSTRUCTION " * 100, False)
    session.appendCustomEntry("bookkeeping", {"source": "test"})

    preparation = prepareCompaction(session.getBranch(), keepOne())
    assert read_field(preparation, "firstKeptEntryId") == instructionId
    assert "UNSENT-INSTRUCTION" not in stringify(read_field(preparation, "messagesToSummarize"))
    assert "UNSENT-INSTRUCTION" not in stringify(read_field(preparation, "turnPrefixMessages"))


def test_does_not_treat_an_omitted_custom_message_as_a_recovery_attempt():
    session = SessionManager.inMemory()
    session.appendMessage({"role": "user", "content": "unanswered input " * 100, "timestamp": nowMs()})
    customId = session.appendCustomMessageEntry("temporary", "temporary context", False)
    session.appendContextEdit(customId, None)

    preparation = prepareCompaction(session.getBranch(), keepOne())
    assert preparation is None


def test_advances_past_input_for_an_omitted_assistant_recovery_suffix_with_metadata():
    session = SessionManager.inMemory()
    userId = session.appendMessage({"role": "user", "content": "recovery input " * 100, "timestamp": nowMs()})
    attemptId = session.appendMessage(assistant("failed attempt"))
    session.appendContextEdit(attemptId, None)
    session.appendCustomEntry("bookkeeping", {"source": "test"})

    preparation = prepareCompaction(session.getBranch(), keepOne())
    assert read_field(preparation, "firstKeptEntryId") == attemptId
    turnPrefixMessages = read_field(preparation, "turnPrefixMessages")
    assert len(turnPrefixMessages) == 1
    assert read_field(turnPrefixMessages[0], "role") == "user"
    assert "recovery input" in text(turnPrefixMessages[0])
    assert not any(
        read_field(message, "role") == "user" and "recovery input" in text(message)
        for message in read_field(preparation, "messagesToSummarize")
    )
    assert userId != attemptId


def test_prepares_compaction_from_edited_model_content():
    session = SessionManager.inMemory()
    omittedId = session.appendMessage({"role": "user", "content": "OMIT-ME " * 100, "timestamp": nowMs()})
    session.appendMessage(assistant("old answer " * 100))
    session.appendContextEdit(omittedId, None)
    session.appendMessage({"role": "user", "content": "keep", "timestamp": nowMs()})
    session.appendMessage(assistant("suffix"))

    preparation = prepareCompaction(session.getBranch(), keepOne())
    assert preparation is not None
    assert "OMIT-ME" not in stringify(read_field(preparation, "messagesToSummarize"))
    assert "OMIT-ME" not in stringify(read_field(preparation, "turnPrefixMessages"))
