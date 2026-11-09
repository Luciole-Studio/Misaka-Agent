"""Translated from pi's ``packages/coding-agent/test/session-manager/build-context.test.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any

from misaka.core.session_manager import buildContextEntries, buildSessionContext
from misaka.utils.values import read_field


def msg(id: str, parentId: str | None, role: str, text: str) -> dict[str, Any]:
    base = {"type": "message", "id": id, "parentId": parentId, "timestamp": "2025-01-01T00:00:00Z"}
    if role == "user":
        return {**base, "message": {"role": role, "content": text, "timestamp": 1}}
    return {
        **base,
        "message": {
            "role": role,
            "content": [{"type": "text", "text": text}],
            "api": "anthropic-messages",
            "provider": "anthropic",
            "model": "claude-test",
            "usage": {
                "input": 1,
                "output": 1,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 2,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "stop",
            "timestamp": 1,
        },
    }


def compaction(id: str, parentId: str | None, summary: str, firstKeptEntryId: str) -> dict[str, Any]:
    return {
        "type": "compaction",
        "id": id,
        "parentId": parentId,
        "timestamp": "2025-01-01T00:00:00Z",
        "summary": summary,
        "firstKeptEntryId": firstKeptEntryId,
        "tokensBefore": 1000,
    }


def branchSummary(id: str, parentId: str | None, summary: str, fromId: str) -> dict[str, Any]:
    return {
        "type": "branch_summary",
        "id": id,
        "parentId": parentId,
        "timestamp": "2025-01-01T00:00:00Z",
        "summary": summary,
        "fromId": fromId,
    }


def custom(id: str, parentId: str | None, customType: str, data: Any = None) -> dict[str, Any]:
    return {
        "type": "custom",
        "id": id,
        "parentId": parentId,
        "timestamp": "2025-01-01T00:00:00Z",
        "customType": customType,
        "data": data,
    }


def thinkingLevel(id: str, parentId: str | None, level: str) -> dict[str, Any]:
    return {
        "type": "thinking_level_change",
        "id": id,
        "parentId": parentId,
        "timestamp": "2025-01-01T00:00:00Z",
        "thinkingLevel": level,
    }


def modelChange(id: str, parentId: str | None, provider: str, modelId: str) -> dict[str, Any]:
    return {
        "type": "model_change",
        "id": id,
        "parentId": parentId,
        "timestamp": "2025-01-01T00:00:00Z",
        "provider": provider,
        "modelId": modelId,
    }


def roles(ctx: Any) -> list[str]:
    return [read_field(message, "role") for message in ctx.messages]


def content(message: Any) -> Any:
    return read_field(message, "content")


def firstText(message: Any) -> Any:
    return read_field(content(message)[0], "text")


def summary(message: Any) -> str:
    return read_field(message, "summary")


def model(ctx: Any) -> Any:
    return None if ctx.model is None else {"provider": read_field(ctx.model, "provider"), "modelId": read_field(ctx.model, "modelId")}


# --- trivial cases ------------------------------------------------------------------------------


def test_empty_entries_returns_empty_context():
    ctx = buildSessionContext([])
    assert ctx.messages == []
    assert ctx.thinkingLevel == "off"
    assert ctx.model is None


def test_single_user_message():
    ctx = buildSessionContext([msg("1", None, "user", "hello")])
    assert len(ctx.messages) == 1
    assert roles(ctx) == ["user"]


def test_simple_conversation():
    entries = [
        msg("1", None, "user", "hello"),
        msg("2", "1", "assistant", "hi there"),
        msg("3", "2", "user", "how are you"),
        msg("4", "3", "assistant", "great"),
    ]
    ctx = buildSessionContext(entries)
    assert len(ctx.messages) == 4
    assert roles(ctx) == ["user", "assistant", "user", "assistant"]


def test_tracks_thinking_level_changes():
    entries = [msg("1", None, "user", "hello"), thinkingLevel("2", "1", "high"), msg("3", "2", "assistant", "thinking hard")]
    ctx = buildSessionContext(entries)
    assert ctx.thinkingLevel == "high"
    assert len(ctx.messages) == 2


def test_tracks_model_from_assistant_message():
    ctx = buildSessionContext([msg("1", None, "user", "hello"), msg("2", "1", "assistant", "hi")])
    assert model(ctx) == {"provider": "anthropic", "modelId": "claude-test"}


def test_tracks_model_from_model_change_entry():
    entries = [
        msg("1", None, "user", "hello"),
        modelChange("2", "1", "openai", "gpt-4"),
        msg("3", "2", "assistant", "hi"),
    ]
    ctx = buildSessionContext(entries)
    # Assistant message overwrites model change
    assert model(ctx) == {"provider": "anthropic", "modelId": "claude-test"}


# --- with compaction ----------------------------------------------------------------------------


def test_includes_summary_before_kept_messages():
    entries = [
        msg("1", None, "user", "first"),
        msg("2", "1", "assistant", "response1"),
        msg("3", "2", "user", "second"),
        msg("4", "3", "assistant", "response2"),
        compaction("5", "4", "Summary of first two turns", "3"),
        msg("6", "5", "user", "third"),
        msg("7", "6", "assistant", "response3"),
    ]
    ctx = buildSessionContext(entries)

    # Should have: summary + kept (3,4) + after (6,7) = 5 messages
    assert len(ctx.messages) == 5
    assert "Summary of first two turns" in summary(ctx.messages[0])
    assert content(ctx.messages[1]) == "second"
    assert firstText(ctx.messages[2]) == "response2"
    assert content(ctx.messages[3]) == "third"
    assert firstText(ctx.messages[4]) == "response3"


def test_handles_compaction_keeping_from_first_message():
    entries = [
        msg("1", None, "user", "first"),
        msg("2", "1", "assistant", "response"),
        compaction("3", "2", "Empty summary", "1"),
        msg("4", "3", "user", "second"),
    ]
    ctx = buildSessionContext(entries)

    # Summary + all messages (1,2,4)
    assert len(ctx.messages) == 4
    assert "Empty summary" in summary(ctx.messages[0])


def test_multiple_compactions_uses_latest():
    entries = [
        msg("1", None, "user", "a"),
        msg("2", "1", "assistant", "b"),
        compaction("3", "2", "First summary", "1"),
        msg("4", "3", "user", "c"),
        msg("5", "4", "assistant", "d"),
        compaction("6", "5", "Second summary", "4"),
        msg("7", "6", "user", "e"),
    ]
    ctx = buildSessionContext(entries)

    # Should use second summary, keep from 4
    assert len(ctx.messages) == 4
    assert "Second summary" in summary(ctx.messages[0])


def test_build_context_entries_returns_compaction_aware_entries_including_custom_entries():
    entries = [
        msg("1", None, "user", "first"),
        custom("2", "1", "old-state", {"hidden": True}),
        msg("3", "2", "assistant", "response1"),
        custom("4", "3", "kept-card", {"title": "Kept"}),
        msg("5", "4", "user", "second"),
        compaction("6", "5", "Summary", "4"),
        custom("7", "6", "after-card", {"title": "After"}),
        msg("8", "7", "assistant", "response2"),
    ]

    assert [read_field(entry, "id") for entry in buildContextEntries(entries)] == ["6", "4", "5", "7", "8"]
    ctx = buildSessionContext(entries)
    assert roles(ctx) == ["compactionSummary", "user", "assistant"]


def test_keeps_settings_from_the_full_path_after_compaction():
    entries = [
        msg("1", None, "user", "first"),
        thinkingLevel("2", "1", "high"),
        msg("3", "2", "assistant", "response1"),
        msg("4", "3", "user", "second"),
        compaction("5", "4", "Summary", "4"),
    ]

    ctx = buildSessionContext(entries)
    assert ctx.thinkingLevel == "high"
    assert roles(ctx) == ["compactionSummary", "user"]


# --- with branches ------------------------------------------------------------------------------


def test_follows_path_to_specified_leaf():
    # Tree:
    #   1 -> 2 -> 3 (branch A)
    #         \-> 4 (branch B)
    entries = [
        msg("1", None, "user", "start"),
        msg("2", "1", "assistant", "response"),
        msg("3", "2", "user", "branch A"),
        msg("4", "2", "user", "branch B"),
    ]

    ctxA = buildSessionContext(entries, "3")
    assert len(ctxA.messages) == 3
    assert content(ctxA.messages[2]) == "branch A"

    ctxB = buildSessionContext(entries, "4")
    assert len(ctxB.messages) == 3
    assert content(ctxB.messages[2]) == "branch B"


def test_includes_branch_summary_in_path():
    entries = [
        msg("1", None, "user", "start"),
        msg("2", "1", "assistant", "response"),
        msg("3", "2", "user", "abandoned path"),
        branchSummary("4", "2", "Summary of abandoned work", "3"),
        msg("5", "4", "user", "new direction"),
    ]
    ctx = buildSessionContext(entries, "5")

    assert len(ctx.messages) == 4
    assert "Summary of abandoned work" in summary(ctx.messages[2])
    assert content(ctx.messages[3]) == "new direction"


def test_complex_tree_with_multiple_branches_and_compaction():
    # Tree:
    #   1 -> 2 -> 3 -> 4 -> compaction(5) -> 6 -> 7 (main path)
    #              \-> 8 -> 9 (abandoned branch)
    #                    \-> branchSummary(10) -> 11 (resumed from 3)
    entries = [
        msg("1", None, "user", "start"),
        msg("2", "1", "assistant", "r1"),
        msg("3", "2", "user", "q2"),
        msg("4", "3", "assistant", "r2"),
        compaction("5", "4", "Compacted history", "3"),
        msg("6", "5", "user", "q3"),
        msg("7", "6", "assistant", "r3"),
        # Abandoned branch from 3
        msg("8", "3", "user", "wrong path"),
        msg("9", "8", "assistant", "wrong response"),
        # Branch summary resuming from 3
        branchSummary("10", "3", "Tried wrong approach", "9"),
        msg("11", "10", "user", "better approach"),
    ]

    # Main path to 7: summary + kept(3,4) + after(6,7)
    ctxMain = buildSessionContext(entries, "7")
    assert len(ctxMain.messages) == 5
    assert "Compacted history" in summary(ctxMain.messages[0])
    assert content(ctxMain.messages[1]) == "q2"
    assert firstText(ctxMain.messages[2]) == "r2"
    assert content(ctxMain.messages[3]) == "q3"
    assert firstText(ctxMain.messages[4]) == "r3"

    # Branch path to 11: 1,2,3 + branch_summary + 11
    ctxBranch = buildSessionContext(entries, "11")
    assert len(ctxBranch.messages) == 5
    assert content(ctxBranch.messages[0]) == "start"
    assert firstText(ctxBranch.messages[1]) == "r1"
    assert content(ctxBranch.messages[2]) == "q2"
    assert "Tried wrong approach" in summary(ctxBranch.messages[3])
    assert content(ctxBranch.messages[4]) == "better approach"


# --- edge cases ---------------------------------------------------------------------------------


def test_uses_last_entry_when_leaf_id_not_found():
    entries = [msg("1", None, "user", "hello"), msg("2", "1", "assistant", "hi")]
    ctx = buildSessionContext(entries, "nonexistent")
    assert len(ctx.messages) == 2


def test_handles_orphaned_entries_gracefully():
    entries = [
        msg("1", None, "user", "hello"),
        msg("2", "missing", "assistant", "orphan"),  # parent doesn't exist
    ]
    ctx = buildSessionContext(entries, "2")
    # Should only get the orphan since parent chain is broken
    assert len(ctx.messages) == 1
