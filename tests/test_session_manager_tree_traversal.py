"""Ported from pi's ``packages/coding-agent/test/session-manager/tree-traversal.test.ts`` (v1.1.0)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from misaka.core.session_manager import SessionManager


def userMsg(text: str) -> dict:
    return {"role": "user", "content": text, "timestamp": int(time.time() * 1000)}


def assistantMsg(text: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "anthropic-messages",
        "provider": "anthropic",
        "model": "test",
        "usage": {
            "input": 1,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 2,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": int(time.time() * 1000),
    }


def readSessionFileRoles(file: str) -> list[str]:
    """One label per record: the message role for message entries, otherwise the entry type."""
    records = [json.loads(line) for line in Path(file).read_text(encoding="utf-8").strip().split("\n")]
    return [(record.get("message") or {}).get("role") or record["type"] for record in records]


def _plain(value):
    """``toEqual``: compare models by their JSON shape."""
    dump = getattr(value, "model_dump", None)
    return dump(exclude_none=True) if callable(dump) else value


def _content(message):
    return message["content"] if isinstance(message, dict) else message.content


def _text(message) -> str:
    block = _content(message)[0]
    return block["text"] if isinstance(block, dict) else block.text


USAGE = {
    "input": 10,
    "output": 20,
    "cacheRead": 30,
    "cacheWrite": 40,
    "totalTokens": 100,
    "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0.3, "cacheWrite": 0.4, "total": 1},
}


# SessionManager append and tree traversal -- append operations


def test_append_message_creates_entry_with_correct_parent_id_chain():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("first"))
    id2 = session.appendMessage(assistantMsg("second"))
    id3 = session.appendMessage(userMsg("third"))

    entries = session.getEntries()
    assert len(entries) == 3

    assert entries[0]["id"] == id1
    assert entries[0]["parentId"] is None
    assert entries[0]["type"] == "message"

    assert entries[1]["id"] == id2
    assert entries[1]["parentId"] == id1

    assert entries[2]["id"] == id3
    assert entries[2]["parentId"] == id2


def test_append_thinking_level_change_integrates_into_tree():
    session = SessionManager.inMemory()

    msgId = session.appendMessage(userMsg("hello"))
    thinkingId = session.appendThinkingLevelChange("high")
    session.appendMessage(assistantMsg("response"))

    entries = session.getEntries()
    assert len(entries) == 3

    thinkingEntry = next((e for e in entries if e["type"] == "thinking_level_change"), None)
    assert thinkingEntry is not None
    assert thinkingEntry["id"] == thinkingId
    assert thinkingEntry["parentId"] == msgId

    assert entries[2]["parentId"] == thinkingId


def test_append_model_change_integrates_into_tree():
    session = SessionManager.inMemory()

    msgId = session.appendMessage(userMsg("hello"))
    modelId = session.appendModelChange("openai", "gpt-4")
    session.appendMessage(assistantMsg("response"))

    entries = session.getEntries()
    modelEntry = next((e for e in entries if e["type"] == "model_change"), None)
    assert modelEntry is not None
    assert modelEntry["id"] == modelId
    assert modelEntry["parentId"] == msgId
    assert modelEntry["provider"] == "openai"
    assert modelEntry["modelId"] == "gpt-4"

    assert entries[2]["parentId"] == modelId


def test_append_compaction_integrates_into_tree():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))
    compactionId = session.appendCompaction("summary", id1, 1000, None, False, USAGE)
    session.appendMessage(userMsg("3"))

    entries = session.getEntries()
    compactionEntry = next((e for e in entries if e["type"] == "compaction"), None)
    assert compactionEntry is not None
    assert compactionEntry["id"] == compactionId
    assert compactionEntry["parentId"] == id2
    assert compactionEntry["summary"] == "summary"
    assert compactionEntry["firstKeptEntryId"] == id1
    assert compactionEntry["tokensBefore"] == 1000
    assert _plain(compactionEntry["usage"]) == USAGE

    assert entries[3]["parentId"] == compactionId


def test_append_custom_entry_integrates_into_tree():
    session = SessionManager.inMemory()

    msgId = session.appendMessage(userMsg("hello"))
    customId = session.appendCustomEntry("my_data", {"key": "value"})
    session.appendMessage(assistantMsg("response"))

    entries = session.getEntries()
    customEntry = next(e for e in entries if e["type"] == "custom")
    assert customEntry["id"] == customId
    assert customEntry["parentId"] == msgId
    assert customEntry["customType"] == "my_data"
    assert customEntry["data"] == {"key": "value"}

    assert entries[2]["parentId"] == customId


def test_leaf_pointer_advances_after_each_append():
    session = SessionManager.inMemory()

    assert session.getLeafId() is None

    id1 = session.appendMessage(userMsg("1"))
    assert session.getLeafId() == id1

    id2 = session.appendMessage(assistantMsg("2"))
    assert session.getLeafId() == id2

    id3 = session.appendThinkingLevelChange("high")
    assert session.getLeafId() == id3


# getPath


def test_get_path_returns_empty_array_for_empty_session():
    session = SessionManager.inMemory()
    assert session.getBranch() == []


def test_get_path_returns_single_entry_path():
    session = SessionManager.inMemory()
    id = session.appendMessage(userMsg("hello"))

    path = session.getBranch()
    assert len(path) == 1
    assert path[0]["id"] == id


def test_get_path_returns_full_path_from_root_to_leaf():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))
    id3 = session.appendThinkingLevelChange("high")
    id4 = session.appendMessage(userMsg("3"))

    path = session.getBranch()
    assert len(path) == 4
    assert [e["id"] for e in path] == [id1, id2, id3, id4]


def test_get_path_returns_path_from_specified_entry_to_root():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))
    session.appendMessage(userMsg("3"))
    session.appendMessage(assistantMsg("4"))

    path = session.getBranch(id2)
    assert len(path) == 2
    assert [e["id"] for e in path] == [id1, id2]


# getTree


def test_get_tree_returns_empty_array_for_empty_session():
    session = SessionManager.inMemory()
    assert session.getTree() == []


def test_get_tree_returns_single_root_for_linear_session():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))
    id3 = session.appendMessage(userMsg("3"))

    tree = session.getTree()
    assert len(tree) == 1

    root = tree[0]
    assert root.entry["id"] == id1
    assert len(root.children) == 1
    assert root.children[0].entry["id"] == id2
    assert len(root.children[0].children) == 1
    assert root.children[0].children[0].entry["id"] == id3
    assert len(root.children[0].children[0].children) == 0


def test_get_tree_returns_tree_with_branches_after_branch():
    session = SessionManager.inMemory()

    # Build: 1 -> 2 -> 3
    id1 = session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))
    id3 = session.appendMessage(userMsg("3"))

    # Branch from id2, add new path: 2 -> 4
    session.branch(id2)
    id4 = session.appendMessage(userMsg("4-branch"))

    tree = session.getTree()
    assert len(tree) == 1

    root = tree[0]
    assert root.entry["id"] == id1
    assert len(root.children) == 1

    node2 = root.children[0]
    assert node2.entry["id"] == id2
    assert len(node2.children) == 2  # id3 and id4 are siblings

    childIds = sorted(c.entry["id"] for c in node2.children)
    assert childIds == sorted([id3, id4])


def test_get_tree_handles_multiple_branches_at_same_point():
    session = SessionManager.inMemory()

    session.appendMessage(userMsg("root"))
    id2 = session.appendMessage(assistantMsg("response"))

    # Branch A
    session.branch(id2)
    idA = session.appendMessage(userMsg("branch-A"))

    # Branch B
    session.branch(id2)
    idB = session.appendMessage(userMsg("branch-B"))

    # Branch C
    session.branch(id2)
    idC = session.appendMessage(userMsg("branch-C"))

    tree = session.getTree()
    node2 = tree[0].children[0]
    assert node2.entry["id"] == id2
    assert len(node2.children) == 3

    branchIds = sorted(c.entry["id"] for c in node2.children)
    assert branchIds == sorted([idA, idB, idC])


def test_get_tree_handles_deep_branching():
    session = SessionManager.inMemory()

    # Main path: 1 -> 2 -> 3 -> 4
    session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))
    id3 = session.appendMessage(userMsg("3"))
    session.appendMessage(assistantMsg("4"))

    # Branch from 2: 2 -> 5 -> 6
    session.branch(id2)
    id5 = session.appendMessage(userMsg("5"))
    session.appendMessage(assistantMsg("6"))

    # Branch from 5: 5 -> 7
    session.branch(id5)
    session.appendMessage(userMsg("7"))

    tree = session.getTree()

    # Verify structure
    node2 = tree[0].children[0]
    assert len(node2.children) == 2  # id3 and id5

    node5 = next(c for c in node2.children if c.entry["id"] == id5)
    assert len(node5.children) == 2  # id6 and id7

    node3 = next(c for c in node2.children if c.entry["id"] == id3)
    assert len(node3.children) == 1  # id4


# branch


def test_branch_moves_leaf_pointer_to_specified_entry():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("1"))
    session.appendMessage(assistantMsg("2"))
    id3 = session.appendMessage(userMsg("3"))

    assert session.getLeafId() == id3

    session.branch(id1)
    assert session.getLeafId() == id1


def test_branch_throws_for_non_existent_entry():
    session = SessionManager.inMemory()
    session.appendMessage(userMsg("hello"))

    with pytest.raises(Exception, match="Entry nonexistent not found"):
        session.branch("nonexistent")


def test_branch_new_appends_become_children_of_branch_point():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("1"))
    session.appendMessage(assistantMsg("2"))

    session.branch(id1)
    id3 = session.appendMessage(userMsg("branched"))

    entries = session.getEntries()
    branchedEntry = next(e for e in entries if e["id"] == id3)
    assert branchedEntry["parentId"] == id1  # sibling of id2


# branchWithSummary


def test_branch_with_summary_inserts_branch_summary_with_the_source_and_destination_and_advances_leaf():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("1"))
    session.appendMessage(assistantMsg("2"))
    id3 = session.appendMessage(userMsg("3"))

    summaryId = session.branchWithSummary(id1, "Summary of abandoned work", None, False, USAGE)

    assert session.getLeafId() == summaryId

    entries = session.getEntries()
    summaryEntry = next((e for e in entries if e["type"] == "branch_summary"), None)
    assert summaryEntry is not None
    assert summaryEntry["parentId"] == id1
    assert summaryEntry["fromId"] == id3
    assert summaryEntry["summary"] == "Summary of abandoned work"
    assert _plain(summaryEntry["usage"]) == USAGE


def test_branch_with_summary_throws_for_non_existent_entry():
    session = SessionManager.inMemory()
    session.appendMessage(userMsg("hello"))

    with pytest.raises(Exception, match="Entry nonexistent not found"):
        session.branchWithSummary("nonexistent", "summary")


# getLeafEntry


def test_get_leaf_entry_returns_undefined_for_empty_session():
    session = SessionManager.inMemory()
    assert session.getLeafEntry() is None


def test_get_leaf_entry_returns_current_leaf_entry():
    session = SessionManager.inMemory()

    session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))

    leaf = session.getLeafEntry()
    assert leaf is not None
    assert leaf["id"] == id2


# getEntry


def test_get_entry_returns_undefined_for_non_existent_id():
    session = SessionManager.inMemory()
    assert session.getEntry("nonexistent") is None


def test_get_entry_returns_entry_by_id():
    session = SessionManager.inMemory()

    id1 = session.appendMessage(userMsg("first"))
    id2 = session.appendMessage(assistantMsg("second"))

    entry1 = session.getEntry(id1)
    assert entry1 is not None
    assert entry1["type"] == "message"
    assert _content(entry1["message"]) == "first"

    entry2 = session.getEntry(id2)
    assert entry2 is not None
    assert _text(entry2["message"]) == "second"


# buildSessionContext with branches


def test_build_session_context_returns_messages_from_current_branch_only():
    session = SessionManager.inMemory()

    # Main: 1 -> 2 -> 3
    session.appendMessage(userMsg("msg1"))
    id2 = session.appendMessage(assistantMsg("msg2"))
    session.appendMessage(userMsg("msg3"))

    # Branch from 2: 2 -> 4
    session.branch(id2)
    session.appendMessage(assistantMsg("msg4-branch"))

    ctx = session.buildSessionContext()
    assert len(ctx.messages) == 3  # msg1, msg2, msg4-branch (not msg3)

    assert _content(ctx.messages[0]) == "msg1"
    assert _text(ctx.messages[1]) == "msg2"
    assert _text(ctx.messages[2]) == "msg4-branch"


# createBranchedSession


def test_create_branched_session_throws_for_non_existent_entry():
    session = SessionManager.inMemory()
    session.appendMessage(userMsg("hello"))

    with pytest.raises(Exception, match="Entry nonexistent not found"):
        session.createBranchedSession("nonexistent")


def test_creates_new_session_with_path_to_specified_leaf_in_memory():
    session = SessionManager.inMemory()

    # Build: 1 -> 2 -> 3 -> 4
    id1 = session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))
    id3 = session.appendMessage(userMsg("3"))
    session.appendMessage(assistantMsg("4"))

    # Branch from 3: 3 -> 5
    session.branch(id3)
    session.appendMessage(userMsg("5"))

    # Create branched session from id2 (should only have 1 -> 2)
    result = session.createBranchedSession(id2)
    assert result is None  # in-memory returns null

    # Session should now only have entries 1 and 2
    entries = session.getEntries()
    assert len(entries) == 2
    assert entries[0]["id"] == id1
    assert entries[1]["id"] == id2


def test_extracts_correct_path_from_branched_tree():
    session = SessionManager.inMemory()

    # Build: 1 -> 2 -> 3
    id1 = session.appendMessage(userMsg("1"))
    id2 = session.appendMessage(assistantMsg("2"))
    session.appendMessage(userMsg("3"))

    # Branch from 2: 2 -> 4 -> 5
    session.branch(id2)
    id4 = session.appendMessage(userMsg("4"))
    id5 = session.appendMessage(assistantMsg("5"))

    # Create branched session from id5 (should have 1 -> 2 -> 4 -> 5)
    session.createBranchedSession(id5)

    entries = session.getEntries()
    assert len(entries) == 4
    assert [e["id"] for e in entries] == [id1, id2, id4, id5]


def test_does_not_duplicate_entries_when_forking_from_before_the_first_user_message(tmp_path):
    tempDir = str(tmp_path)
    # Create a persisted session with a couple of turns
    session = SessionManager.create(tempDir, tempDir)
    modelChangeId = session.appendModelChange("anthropic", "claude-sonnet-4-5")
    session.appendMessage(userMsg("first question"))
    session.appendMessage(assistantMsg("first answer"))

    # Fork from a setup entry (no user or assistant message in the branched path)
    newFile = session.createBranchedSession(modelChangeId)
    assert newFile is not None

    # Nothing to save yet, so the file is created later by the first user message
    assert not os.path.exists(newFile)

    session.appendMessage(userMsg("new question"))
    assert os.path.exists(newFile)

    # Simulate extension adding entry before assistant (like preset on turn_start)
    session.appendCustomEntry("preset-state", {"name": "plan"})
    session.appendMessage(assistantMsg("new answer"))

    # Exactly one header and each entry written once
    assert readSessionFileRoles(newFile) == ["session", "model_change", "user", "custom", "assistant"]


def test_preserves_tool_and_summary_usage_across_a_file_backed_reload(tmp_path):
    tempDir = str(tmp_path)
    session = SessionManager.create(tempDir, tempDir)
    rootId = session.appendMessage(userMsg("question"))
    session.appendMessage(assistantMsg("answer"))
    session.appendMessage({
        "role": "toolResult",
        "toolCallId": "call-1",
        "toolName": "nested-model",
        "content": [{"type": "text", "text": "result"}],
        "isError": False,
        "usage": USAGE,
        "timestamp": int(time.time() * 1000),
    })
    session.appendCompaction("summary", rootId, 100, None, False, USAGE)
    session.branchWithSummary(rootId, "branch summary", None, False, USAGE)

    file = session.getSessionFile()
    assert file is not None
    reopened = SessionManager.open(file, tempDir)
    entries = reopened.getEntries()
    assert any(e["type"] == "compaction" and _plain(e["usage"]) == USAGE for e in entries)
    assert any(e["type"] == "branch_summary" and _plain(e["usage"]) == USAGE for e in entries)

    def toolResultUsage(entry):
        message = entry.get("message")
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        usage = message.get("usage") if isinstance(message, dict) else getattr(message, "usage", None)
        return role == "toolResult" and _plain(usage) == USAGE

    assert any(e["type"] == "message" and toolResultUsage(e) for e in entries)


def test_writes_file_immediately_when_forking_at_a_user_message(tmp_path):
    tempDir = str(tmp_path)
    session = SessionManager.create(tempDir, tempDir)
    id1 = session.appendMessage(userMsg("first question"))
    session.appendMessage(assistantMsg("first answer"))

    newFile = session.createBranchedSession(id1)
    assert os.path.exists(newFile)

    session.appendMessage(assistantMsg("new answer"))
    assert readSessionFileRoles(newFile) == ["session", "user", "assistant"]
