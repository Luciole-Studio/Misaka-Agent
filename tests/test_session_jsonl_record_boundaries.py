"""Unicode inside JSON strings is content, never a JSONL record boundary."""
import asyncio
import json
from pathlib import Path

import pytest

from misaka.core.session_manager import (
    InvalidSessionFileError,
    SessionManager,
    _dump_jsonl,
    _parse_jsonl_entries,
    load_entries_from_file,
    read_session_header,
)

TEXT = "中文\u0085next\u2028line\u2029paragraph\nreal newline\rreturn"


def transcript(tmp_path):
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    manager.appendMessage({"role": "user", "content": TEXT, "timestamp": 1})
    manager.appendMessage({"role": "assistant", "content": [{"type": "text", "text": TEXT}], "timestamp": 2})
    return manager, Path(manager.getSessionFile())


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("record_ending", ["\n", "\r\n"])
@pytest.mark.parametrize("strict", [False, True])
def test_jsonl_only_splits_at_lf(separator, record_ending, strict):
    entries = [{"text": f"before{separator}after"}, {"text": "second"}]
    content = _dump_jsonl(entries).replace("\n", record_ending)
    assert separator in content  # Reproduce the native unescaped serializer.
    assert _parse_jsonl_entries(content, strict=strict) == entries


def test_native_open_list_and_forks_preserve_unicode(tmp_path):
    from misaka.core.research.planner import fork_session

    original, path = transcript(tmp_path)
    before = path.read_bytes()
    assert all(character.encode() in before for character in ("\u0085", "\u2028", "\u2029"))
    assert read_session_header(str(path))["id"] == original.getSessionId()
    for manager in (SessionManager.open(str(path)), SessionManager.openInMemory(str(path))):
        assert manager.getEntries() == original.getEntries()
        assert manager.getLeafId() == original.getLeafId()
    for infos in (
        asyncio.run(SessionManager.list(str(tmp_path), str(path.parent))),
        asyncio.run(SessionManager.listAll(str(path.parent))),
    ):
        assert len(infos) == 1
        assert infos[0].messageCount == 2
        assert infos[0].firstMessage == TEXT
        assert infos[0].allMessagesText == f"{TEXT} {TEXT}"

    forked = SessionManager.forkFrom(str(path), str(tmp_path), str(tmp_path / "fork"))
    research_path = fork_session(str(path), str(tmp_path / "research-fork"))
    for manager in (forked, SessionManager.open(research_path)):
        assert manager.getSessionId() != original.getSessionId()
        assert manager.getEntries() == original.getEntries()
        assert read_session_header(manager.getSessionFile())["parentSession"] == str(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_file_record_endings_and_header_unicode(tmp_path, ending):
    header = {"type": "session", "id": "fixture", "version": 3, "cwd": str(tmp_path), "note": TEXT}
    entries = [header, {"type": "custom", "id": "entry", "data": TEXT}]
    records = [json.dumps(entry, ensure_ascii=False).encode() for entry in entries]
    path = tmp_path / "fixture.jsonl"
    path.write_bytes((ending or b"\n").join(records) + ending)
    before = path.read_bytes()
    assert read_session_header(str(path)) == header
    assert load_entries_from_file(str(path), strict=True) == entries
    assert path.read_bytes() == before
    load_entries_from_file(str(path), strict=True, repair_unterminated=True)
    assert path.read_bytes() == before + (b"\n" if not ending else b"")


@pytest.mark.parametrize("broken", ['{"content":"unfinished', '[]', '{"content":"raw\nnewline"}'])
def test_strict_rejects_malformed_records_without_modifying_file(tmp_path, broken):
    _, path = transcript(tmp_path)
    path.write_bytes(path.read_bytes() + broken.encode())
    before = path.read_bytes()
    with pytest.raises(InvalidSessionFileError):
        SessionManager.open(str(path))
    with pytest.raises(InvalidSessionFileError):
        SessionManager.openInMemory(str(path))
    with pytest.raises(RuntimeError, match="empty or invalid"):
        SessionManager.forkFrom(str(path), str(tmp_path), str(tmp_path / "fork"))
    assert path.read_bytes() == before


def test_nonstrict_skips_only_bad_physical_records():
    entries = [{"text": TEXT}, {"text": "tail"}]
    content = _dump_jsonl(entries[:1]) + '{"truncated":\n[]\n\n' + _dump_jsonl(entries[1:])
    assert _parse_jsonl_entries(content) == entries


@pytest.mark.parametrize("content", ["", " \n", "{}\n", "[]\n"])
def test_empty_or_missing_session_header_stays_invalid(tmp_path, content):
    path = tmp_path / "invalid.jsonl"
    path.write_text(content)
    with pytest.raises(InvalidSessionFileError):
        SessionManager.open(str(path))
    assert read_session_header(str(path)) == {}
    assert load_entries_from_file(str(path)) == []


def test_sister_resume_preserves_unicode(tmp_path):
    from misaka.core.subagent.runtime import read_resume_transcript

    manager, path = transcript(tmp_path)
    assert read_resume_transcript(path) == manager.fileEntries


def test_sister_tail_preserves_unicode(tmp_path):
    from misaka.core.network.sister_runtime import transcript_tail

    _, path = transcript(tmp_path)
    tail = transcript_tail(str(path))
    assert f"[user] {TEXT}" in tail
    assert f"[assistant] {TEXT}" in tail


@pytest.mark.parametrize("broken", ['{"content":"unfinished', '[]', '{}'])
def test_sister_resume_remains_strict(tmp_path, broken):
    from misaka.core.subagent.runtime import read_resume_transcript

    path = tmp_path / "invalid.jsonl"
    path.write_text(broken)
    with pytest.raises(ValueError):
        read_resume_transcript(path)
    assert path.read_text() == broken


def test_sister_resume_reports_empty_transcript(tmp_path):
    from misaka.core.subagent.runtime import read_resume_transcript

    path = tmp_path / "empty.jsonl"
    path.touch()
    with pytest.raises(ValueError, match="transcript is empty"):
        read_resume_transcript(path)


def test_research_sources_keep_unicode_tool_records(tmp_path):
    from misaka.core.research.bundle import _Index, consulted_in_session

    source = tmp_path / "source.md"
    source.write_text("# Fixture source\n")
    entries = [
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": TEXT},
            {"type": "toolCall", "name": "read", "arguments": {"path": str(source)}},
        ]}},
        {"message": {"role": "toolResult", "toolName": "web_extract", "content": TEXT,
                     "details": {"saved_path": str(source)}}},
    ]
    path = tmp_path / "sources.jsonl"
    index = _Index(str(tmp_path))
    for entry in entries:
        path.write_text(_dump_jsonl([entry]))
        assert consulted_in_session(str(path), index) == {str(source.resolve())}
