"""Translated from pi's ``packages/coding-agent/test/session-manager/`` ``custom-session-id.test.ts``,
``labels.test.ts``, ``load-entries.test.ts`` and ``save-entry.test.ts`` (v1.1.0).

MISAKA fork: ``migration.test.ts`` is not translated, and neither is the load-entries case that migrates an older
header; MISAKA reads only version-3 sessions (``require_current_version``) and carries no migrations.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from misaka.core.session_manager import (
    InvalidSessionFileError,
    NewSessionOptions,
    SessionManager,
)
from misaka.utils.values import read_field

UUID_V7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
def sessionFileRe(id: str) -> str:
    return r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_" + re.escape(id) + r"\.jsonl$"


def nowMs() -> int:
    return int(time.time() * 1000)


def assistantMessage(text: str, timestamp: int = 2) -> dict[str, Any]:
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
        "timestamp": timestamp,
    }


# --- SessionManager.newSession with custom id ---------------------------------------------------


def test_uses_the_provided_id_instead_of_generating_one():
    session = SessionManager.inMemory()
    session.newSession(NewSessionOptions(id="my-custom-id"))
    assert session.getSessionId() == "my-custom-id"


def test_uses_the_provided_id_when_creating_an_in_memory_session():
    session = SessionManager.inMemory(os.getcwd(), NewSessionOptions(id="memory-session-id"))
    assert session.getSessionId() == "memory-session-id"
    assert session.getHeader()["id"] == "memory-session-id"
    assert session.getSessionFile() is None


def test_allows_alphanumeric_session_ids_with_interior_punctuation():
    session = SessionManager.inMemory()
    session.newSession(NewSessionOptions(id="abc-123_def.456"))
    assert session.getSessionId() == "abc-123_def.456"


@pytest.mark.parametrize("id", ["", "-abc", "abc-", "_abc", "abc_", ".abc", "abc.", "abc/def", "abc\\def", "abc def"])
def test_rejects_invalid_custom_session_ids(id: str):
    session = SessionManager.inMemory()
    with pytest.raises(Exception, match="Session id must be non-empty, contain only alphanumeric characters"):
        session.newSession(NewSessionOptions(id=id))


def test_generates_a_uuidv7_id_when_no_id_is_provided():
    session = SessionManager.inMemory()
    session.newSession()
    id = session.getSessionId()
    assert id
    assert UUID_V7_RE.match(id)


def test_generates_a_uuidv7_id_when_options_is_provided_without_id():
    session = SessionManager.inMemory()
    session.newSession(NewSessionOptions(parentSession="parent.jsonl"))
    id = session.getSessionId()
    assert id
    assert UUID_V7_RE.match(id)


def test_includes_the_custom_id_in_the_session_header():
    session = SessionManager.inMemory()
    session.newSession(NewSessionOptions(id="header-test-id"))

    header = session.getHeader()
    assert header is not None
    assert header["id"] == "header-test-id"


def test_generates_a_uuidv7_id_when_constructed_without_an_explicit_id():
    session = SessionManager.inMemory()
    assert UUID_V7_RE.match(session.getSessionId())
    assert session.getHeader()["id"] == session.getSessionId()


def test_uses_the_provided_id_when_creating_a_persisted_session(tmp_path: Path):
    session = SessionManager.create(str(tmp_path), str(tmp_path), NewSessionOptions(id="created-session-id"))

    assert session.getSessionId() == "created-session-id"
    assert session.getHeader()["id"] == "created-session-id"
    sessionFile = session.getSessionFile()
    assert sessionFile is not None
    assert "created-session-id" in sessionFile
    assert re.match(sessionFileRe("created-session-id"), os.path.basename(sessionFile))
    assert not os.path.exists(sessionFile)


def test_generates_a_uuidv7_id_when_creating_a_branched_session():
    session = SessionManager.inMemory()
    firstId = session.appendMessage({"role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": nowMs()})

    session.createBranchedSession(firstId)

    assert UUID_V7_RE.match(session.getSessionId())
    assert session.getHeader()["id"] == session.getSessionId()


def _writeSource(tmp_path: Path, id: str, *entries: dict[str, Any]) -> str:
    sourcePath = tmp_path / "source.jsonl"
    header = {"type": "session", "version": 3, "id": id, "timestamp": "2026-01-01T00:00:00.000Z", "cwd": str(tmp_path)}
    sourcePath.write_text("\n".join(json.dumps(entry) for entry in (header, *entries)) + "\n", encoding="utf-8")
    return str(sourcePath)


def test_generates_a_uuidv7_id_when_forking_from_another_session_file(tmp_path: Path):
    sourcePath = _writeSource(
        tmp_path,
        "legacy-session-id",
        {
            "type": "message",
            "id": "entry-1",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {**assistantMessage("hello", nowMs()), "api": "openai-responses", "provider": "openai", "model": "gpt-5.4"},
        },
    )

    forked = SessionManager.forkFrom(sourcePath, str(tmp_path), str(tmp_path))
    header = forked.getHeader()
    assert header is not None
    assert UUID_V7_RE.match(header["id"])
    assert header["parentSession"] == sourcePath


def test_uses_the_provided_id_when_forking_from_another_session_file(tmp_path: Path):
    sourcePath = _writeSource(tmp_path, "source-session-id")

    forked = SessionManager.forkFrom(sourcePath, str(tmp_path), str(tmp_path), NewSessionOptions(id="forked-session-id"))
    header = forked.getHeader()
    assert header is not None
    assert header["id"] == "forked-session-id"
    assert header["parentSession"] == sourcePath
    sessionFile = forked.getSessionFile()
    assert sessionFile is not None
    assert "forked-session-id" in sessionFile
    assert re.match(sessionFileRe("forked-session-id"), os.path.basename(sessionFile))


# --- SessionManager labels ----------------------------------------------------------------------


def _labelEntries(session: SessionManager) -> list[Any]:
    return [entry for entry in session.getEntries() if read_field(entry, "type") == "label"]


def _node(nodes: list[Any], entryId: str) -> Any:
    return next((node for node in nodes if read_field(read_field(node, "entry"), "id") == entryId), None)


def test_sets_and_gets_labels():
    session = SessionManager.inMemory()

    msgId = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})

    # No label initially
    assert session.getLabel(msgId) is None

    # Set a label
    labelId = session.appendLabelChange(msgId, "checkpoint")
    assert session.getLabel(msgId) == "checkpoint"

    # Label entry should be in entries
    labelEntry = _labelEntries(session)[0]
    assert labelEntry["id"] == labelId
    assert labelEntry["targetId"] == msgId
    assert labelEntry["label"] == "checkpoint"


def test_clears_labels_with_undefined():
    session = SessionManager.inMemory()

    msgId = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})

    session.appendLabelChange(msgId, "checkpoint")
    assert session.getLabel(msgId) == "checkpoint"

    # Clear the label
    session.appendLabelChange(msgId, None)
    assert session.getLabel(msgId) is None


def test_last_label_wins():
    session = SessionManager.inMemory()

    msgId = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})

    session.appendLabelChange(msgId, "first")
    session.appendLabelChange(msgId, "second")
    lastLabelId = session.appendLabelChange(msgId, "third")

    assert session.getLabel(msgId) == "third"

    lastLabelEntry = next(entry for entry in session.getEntries() if entry["id"] == lastLabelId)
    msgNode = _node(session.getTree(), msgId)
    assert read_field(msgNode, "labelTimestamp") == lastLabelEntry["timestamp"]


def test_labels_are_included_in_tree_nodes():
    session = SessionManager.inMemory()

    msg1Id = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})
    msg2Id = session.appendMessage(assistantMessage("hi"))

    msg1LabelId = session.appendLabelChange(msg1Id, "start")
    msg2LabelId = session.appendLabelChange(msg2Id, "response")

    entries = session.getEntries()
    msg1LabelEntry = next(entry for entry in entries if entry["id"] == msg1LabelId)
    msg2LabelEntry = next(entry for entry in entries if entry["id"] == msg2LabelId)
    tree = session.getTree()

    # Find the message nodes (skip label entries)
    msg1Node = _node(tree, msg1Id)
    assert read_field(msg1Node, "label") == "start"
    assert read_field(msg1Node, "labelTimestamp") == msg1LabelEntry["timestamp"]

    # msg2 is a child of msg1
    msg2Node = _node(read_field(msg1Node, "children"), msg2Id)
    assert read_field(msg2Node, "label") == "response"
    assert read_field(msg2Node, "labelTimestamp") == msg2LabelEntry["timestamp"]


def test_labels_are_preserved_in_create_branched_session():
    session = SessionManager.inMemory()

    msg1Id = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})
    msg2Id = session.appendMessage(assistantMessage("hi"))

    msg1LabelId = session.appendLabelChange(msg1Id, "important")
    msg2LabelId = session.appendLabelChange(msg2Id, "also-important")
    originalEntries = session.getEntries()
    msg1LabelEntry = next(entry for entry in originalEntries if entry["id"] == msg1LabelId)
    msg2LabelEntry = next(entry for entry in originalEntries if entry["id"] == msg2LabelId)

    # Branch from msg2 (in-memory mode returns None, but updates internal state)
    session.createBranchedSession(msg2Id)

    # Labels should be preserved
    assert session.getLabel(msg1Id) == "important"
    assert session.getLabel(msg2Id) == "also-important"

    # New label entries should exist
    assert len(_labelEntries(session)) == 2

    tree = session.getTree()
    msg1Node = _node(tree, msg1Id)
    msg2Node = _node(read_field(msg1Node, "children"), msg2Id)
    assert read_field(msg1Node, "labelTimestamp") == msg1LabelEntry["timestamp"]
    assert read_field(msg2Node, "labelTimestamp") == msg2LabelEntry["timestamp"]


def test_rewires_children_of_removed_labels_when_forking():
    session = SessionManager.inMemory()

    msg1Id = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})
    session.appendLabelChange(msg1Id, "checkpoint")
    modelChangeId = session.appendModelChange("anthropic", "claude-test")
    msg2Id = session.appendMessage({"role": "user", "content": "followup", "timestamp": 2})

    session.createBranchedSession(msg2Id)

    assert read_field(session.getEntry(modelChangeId), "parentId") == msg1Id


def test_labels_not_on_path_are_not_preserved_in_create_branched_session():
    session = SessionManager.inMemory()

    msg1Id = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})
    msg2Id = session.appendMessage(assistantMessage("hi"))
    msg3Id = session.appendMessage({"role": "user", "content": "followup", "timestamp": 3})

    # Label all messages
    session.appendLabelChange(msg1Id, "first")
    session.appendLabelChange(msg2Id, "second")
    session.appendLabelChange(msg3Id, "third")

    # Branch from msg2 (excludes msg3)
    session.createBranchedSession(msg2Id)

    # Only labels for msg1 and msg2 should be preserved
    assert session.getLabel(msg1Id) == "first"
    assert session.getLabel(msg2Id) == "second"
    assert session.getLabel(msg3Id) is None


def test_labels_are_not_included_in_build_session_context():
    session = SessionManager.inMemory()

    msgId = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})
    session.appendLabelChange(msgId, "checkpoint")

    ctx = session.buildSessionContext()
    assert [read_field(message, "role") for message in ctx.messages] == ["user"]


def test_throws_when_labeling_non_existent_entry():
    session = SessionManager.inMemory()

    with pytest.raises(Exception, match="Entry non-existent not found"):
        session.appendLabelChange("non-existent", "label")


# --- SessionManager.inMemory with preloaded entries ---------------------------------------------


def userMessage(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": nowMs()}


def storedEntries(build: Any) -> list[Any]:
    source = SessionManager.inMemory("/project")
    build(source)
    return source.getEntries()


def test_adopts_entries_verbatim():
    def build(source: SessionManager) -> None:
        source.appendMessage(userMessage("hello"))
        source.appendModelChange("anthropic", "claude-opus-4-5")
        source.appendMessage(userMessage("again"))

    entries = storedEntries(build)

    session = SessionManager.inMemory("/project", None, entries)

    assert session.getEntries() == entries


def test_keeps_the_loaded_leaf_so_appends_continue_the_conversation():
    def build(source: SessionManager) -> None:
        source.appendMessage(userMessage("hello"))
        source.appendMessage(userMessage("again"))

    entries = storedEntries(build)
    lastId = entries[-1]["id"]

    session = SessionManager.inMemory("/project", None, entries)
    appendedId = session.appendMessage(userMessage("continued"))

    assert session.getLeafId() == appendedId
    assert read_field(session.getEntry(appendedId), "parentId") == lastId


def test_never_mints_an_id_that_collides_with_a_loaded_entry():
    def build(source: SessionManager) -> None:
        for i in range(50):
            source.appendMessage(userMessage(f"message {i}"))

    entries = storedEntries(build)

    session = SessionManager.inMemory("/project", None, entries)
    appendedId = session.appendMessage(userMessage("continued"))

    assert not any(entry["id"] == appendedId for entry in entries)


def test_rebuilds_the_branch_structure_rather_than_a_flat_chain():
    def build(source: SessionManager) -> None:
        firstId = source.appendMessage(userMessage("hello"))
        source.appendMessage(userMessage("abandoned"))
        source.branch(firstId)
        source.appendMessage(userMessage("kept"))

    entries = storedEntries(build)

    session = SessionManager.inMemory("/project", None, entries)
    roots = session.getTree()

    assert len(roots) == 1
    assert len(read_field(roots[0], "children")) == 2


def test_rebuilds_labels():
    labelled: dict[str, str] = {}

    def build(source: SessionManager) -> None:
        labelled["id"] = source.appendMessage(userMessage("hello"))
        source.appendLabelChange(labelled["id"], "checkpoint")

    entries = storedEntries(build)

    session = SessionManager.inMemory("/project", None, entries)

    assert session.getLabel(labelled["id"]) == "checkpoint"


def test_resolves_a_compaction_against_the_entry_it_was_written_against():
    kept: dict[str, str] = {}

    def build(source: SessionManager) -> None:
        source.appendMessage(userMessage("dropped"))
        kept["id"] = source.appendMessage(userMessage("kept"))
        source.appendCompaction("summary so far", kept["id"], 1000)

    entries = storedEntries(build)

    session = SessionManager.inMemory("/project", None, entries)
    context = session.buildContextEntries()

    assert any(entry["id"] == kept["id"] for entry in context)


def test_creates_a_header_from_the_options_when_the_entries_carry_none():
    entries = storedEntries(lambda source: source.appendMessage(userMessage("hello")))

    session = SessionManager.inMemory("/project", NewSessionOptions(id="restored-session"), entries)

    assert session.getSessionId() == "restored-session"
    assert session.getHeader()["id"] == "restored-session"
    # The header cwd is the resolved cwd ("/project" on POSIX, the current drive's root on Windows).
    assert session.getHeader()["cwd"] == session.getCwd()


def test_generates_a_session_id_when_the_options_carry_none():
    entries = storedEntries(lambda source: source.appendMessage(userMessage("hello")))

    session = SessionManager.inMemory("/project", None, entries)

    assert UUID_V7_RE.match(session.getSessionId())
    assert session.getHeader()["id"] == session.getSessionId()


def test_stays_off_the_filesystem():
    entries = storedEntries(lambda source: source.appendMessage(userMessage("hello")))

    session = SessionManager.inMemory("/project", None, entries)
    session.appendMessage(userMessage("continued"))

    assert session.getSessionFile() is None
    assert session.isPersisted() is False


def test_starts_an_empty_session_when_the_entries_are_empty():
    session = SessionManager.inMemory("/project", NewSessionOptions(id="empty-session"), [])

    assert session.getSessionId() == "empty-session"
    assert session.getEntries() == []
    assert session.getLeafId() is None


def test_takes_the_session_identity_from_a_header_among_the_entries():
    body = storedEntries(lambda source: source.appendMessage(userMessage("hello")))
    entries = [
        {"type": "session", "version": 3, "id": "stored-session", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/stored"},
        *body,
    ]

    session = SessionManager.inMemory("/project", NewSessionOptions(id="ignored"), entries)

    assert session.getSessionId() == "stored-session"
    assert session.getHeader()["cwd"] == "/stored"


def test_refuses_entries_restored_with_an_older_header():
    # MISAKA fork: pi migrates a version-2 header in place; MISAKA reads only version-3 sessions.
    entries = [
        {"type": "session", "version": 2, "id": "v2-session", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/project"},
        {
            "type": "message",
            "id": "abc12345",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "hookMessage", "content": "from a hook", "timestamp": 1},
        },
    ]

    with pytest.raises(InvalidSessionFileError):
        SessionManager.inMemory("/project", None, entries)


def test_adopts_headerless_entries_as_current_version_without_migrating_them():
    entries = [
        {
            "type": "message",
            "id": "abc12345",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "hookMessage", "content": "from a hook", "timestamp": 1},
        }
    ]

    session = SessionManager.inMemory("/project", None, entries)
    restored = session.getEntries()[0]

    assert restored["message"]["role"] == "hookMessage"


# --- SessionManager.saveCustomEntry -------------------------------------------------------------


def test_saves_custom_entries_and_includes_them_in_tree_traversal():
    session = SessionManager.inMemory()

    # Save a message
    msgId = session.appendMessage({"role": "user", "content": "hello", "timestamp": 1})

    # Save a custom entry
    customId = session.appendCustomEntry("my_data", {"foo": "bar"})

    # Save another message
    msg2Id = session.appendMessage(assistantMessage("hi"))

    # Custom entry should be in entries
    entries = session.getEntries()
    assert len(entries) == 3

    customEntry = next(entry for entry in entries if entry["type"] == "custom")
    assert customEntry["customType"] == "my_data"
    assert customEntry["data"] == {"foo": "bar"}
    assert customEntry["id"] == customId
    assert customEntry["parentId"] == msgId

    # Tree structure should be correct
    path = session.getBranch()
    assert [entry["id"] for entry in path] == [msgId, customId, msg2Id]

    # buildSessionContext should work (custom entries skipped in messages)
    ctx = session.buildSessionContext()
    assert len(ctx.messages) == 2  # only message entries
