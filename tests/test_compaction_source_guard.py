"""Compaction tolerates appended bookkeeping, never a changed conversation/owner."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.agent_session import AgentSession
from misaka.core.compaction.compaction import CompactionResult
from misaka.core.session_manager import SessionManager
from misaka.core.system_prompt import normalize_build_system_prompt_options


def _user(text):
    return {"role": "user", "content": text, "timestamp": 1}


def _session(manager=None):
    session = AgentSession.__new__(AgentSession)
    session.sessionManager = manager or SessionManager.inMemory(cwd="/tmp")
    session.sessionManager.appendMessage(_user("first question"))
    session.sessionManager.appendMessage(_user("second question"))
    session.agent = SimpleNamespace(
        state=SimpleNamespace(messages=session.sessionManager.buildSessionContext().messages,
                              model={"provider": "fixture", "id": "fixture"},
                              tools=[], systemPrompt="fixture", thinkingLevel="high"),
        hasQueuedMessages=lambda: False,
    )
    settings = {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 1}
    session.settingsManager = SimpleNamespace(getCompactionSettings=lambda model=None: settings)
    session._baseSystemPromptOptions = normalize_build_system_prompt_options({"cwd": "/tmp", "customPrompt": "fixture"})
    session._runSystemPromptOptions = None
    session._extensionRunner = SimpleNamespace(has_handlers=lambda _: False, emit=AsyncMock())
    session.moments = SimpleNamespace(session_context_prepare=AsyncMock(),
                                      session_compact=AsyncMock(),
                                      session_compact_failed=AsyncMock())
    session.abort = AsyncMock()
    session.events = []
    session._emit = session.events.append
    return session


def _result(session, engine=False):
    return CompactionResult("summary", session.sessionManager.getLeafId(), 100,
                            contextMessages=[_user("complete engine replay")] if engine else None)


@pytest.mark.parametrize("engine", [False, True])
def test_appended_bookkeeping_is_preserved_in_archive_and_branch(tmp_path, engine):
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    session = _session(manager)
    manager.appendCustomMessageEntry("fixture", "persisted context", False)
    session.agent.state.messages = manager.buildSessionContext().messages
    result, source = _result(session, engine), session._compaction_source()
    previous_messages = copy.deepcopy(manager.buildSessionContext().messages)
    appended = [manager.appendCustomEntry("progress", {"state": "waiting"}),
                manager.appendSessionInfo("renamed during compaction"),
                manager.appendLabelChange(manager.getLeafId(), "checkpoint")]
    assert manager.buildSessionContext().messages == previous_messages

    saved = session._publish_compaction(result, engine, source)

    assert saved["parentId"] == appended[-1]
    assert [entry["id"] for entry in manager.getBranch()][-4:] == [*appended, saved["id"]]
    entries = [json.loads(line) for line in Path(manager.getSessionFile()).read_text().splitlines()]
    assert [entry["id"] for entry in entries][-4:] == [*appended, saved["id"]]
    assert session.messages == manager.buildSessionContext().messages
    assert manager.getSessionName() == "renamed during compaction"
    assert result.estimatedTokensAfter > 0
    reopened = SessionManager.open(manager.getSessionFile())
    assert [entry["id"] for entry in reopened.getBranch()] == [entry["id"] for entry in manager.getBranch()]
    assert reopened.getEntry(appended[0])["data"] == {"state": "waiting"}
    assert reopened.getLabel(appended[1]) == "checkpoint"
    assert reopened.getSessionName() == manager.getSessionName()


def _change(session, kind):
    manager = session.sessionManager
    if kind == "message":
        manager.appendMessage(_user("arrived during compression"))
    elif kind == "custom message":
        manager.appendCustomMessageEntry("notice", "new context", False)
    elif kind == "thinking":
        manager.appendThinkingLevelChange("low")
    elif kind == "model entry":
        manager.appendModelChange("fixture", "other")
    elif kind == "model":
        session.agent.state.model = {"provider": "fixture", "id": "other"}
    elif kind == "model in place":
        session.agent.state.model["id"] = "other"
    elif kind == "settings":
        session.settingsManager.getCompactionSettings()["enabled"] = False
    elif kind == "branch":
        manager.branch(manager.getBranch()[0]["id"])
    elif kind == "branch metadata":
        manager.branch(manager.getBranch()[0]["id"])
        manager.appendCustomEntry("progress")
    elif kind == "branch away and back":
        leaf = manager.getLeafId()
        manager.branch(manager.getBranch()[0]["id"])
        manager.appendCustomEntry("progress")
        manager.branch(leaf)
    elif kind == "new session":
        manager.newSession()
    elif kind == "fork":
        manager.createBranchedSession(manager.getLeafId())
        manager.appendCustomEntry("progress")
    elif kind == "session file":
        manager.sessionFile = "/tmp/different-fixture-session.jsonl"
    elif kind == "owner":
        session.sessionManager = copy.deepcopy(manager)
    elif kind == "compaction":
        manager.appendCompaction("another summary", manager.getLeafId(), 50)
    elif kind == "reset leaf":
        manager.resetLeaf()
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", [
    "message", "custom message", "thinking", "model entry", "model", "model in place", "settings",
    "branch", "branch metadata", "branch away and back", "new session", "fork", "session file",
    "owner", "compaction", "reset leaf",
])
def test_real_changes_never_publish_a_stale_summary(kind):
    session = _session()
    result, source = _result(session), session._compaction_source()
    _change(session, kind)
    entries = copy.deepcopy(session.sessionManager.getEntries())
    messages = copy.deepcopy(session.messages)
    with pytest.raises(RuntimeError, match="Session, branch, model or settings changed during compaction"):
        session._publish_compaction(result, False, source)
    assert session.sessionManager.getEntries() == entries
    assert session.messages == messages


def test_error_identifies_changed_fields():
    session = _session()
    result, source = _result(session), session._compaction_source()
    _change(session, "settings")
    with pytest.raises(RuntimeError, match=r"changed during compaction \(settings\)"):
        session._publish_compaction(result, False, source)


@pytest.mark.parametrize("mode", ["manual", "threshold", "overflow"])
@pytest.mark.parametrize("stage", ["prepare", "execute"])
@pytest.mark.parametrize("metadata", [False, True])
async def test_both_await_boundaries_use_the_same_guard(mode, stage, metadata):
    session = _session()
    result = _result(session, engine=True)

    def mutate():
        if metadata:
            session.sessionManager.appendCustomEntry("progress", {"phase": stage})
        else:
            session.sessionManager.appendMessage(_user("must not be lost"))

    async def execute():
        if stage == "execute":
            mutate()
        return result

    async def prepare(_event):
        if stage == "prepare":
            mutate()
        return {"execute": execute}

    session.moments.session_context_prepare.side_effect = prepare
    if mode == "manual":
        if metadata:
            assert await session.compact() is result
        else:
            with pytest.raises(RuntimeError, match="changed during compaction"):
                await session.compact()
    else:
        await session._run_auto_compaction(mode, mode == "overflow")

    entries = session.sessionManager.getEntries()
    compacted = [entry for entry in entries if entry["type"] == "compaction"]
    assert bool(compacted) is metadata
    if metadata:
        session.moments.session_compact.assert_awaited_once()
        session.moments.session_compact_failed.assert_not_awaited()
        assert session.messages == result.contextMessages
    else:
        session.moments.session_compact.assert_not_awaited()
        session.moments.session_compact_failed.assert_awaited_once()
        assert entries[-1]["message"]["content"] == "must not be lost"


@pytest.mark.parametrize("mode", ["manual", "threshold"])
@pytest.mark.parametrize("from_hook", [False, True])
async def test_native_preparation_and_summary_hooks_tolerate_bookkeeping(monkeypatch, mode, from_hook):
    session = _session()
    result = _result(session)
    session.moments.session_context_prepare.return_value = None
    session._get_compaction_request_auth = AsyncMock(return_value={})
    session.agent.streamFn = None
    session._summarization_retry_policy = lambda: None
    session._summarization_retry_callbacks = lambda _: None

    async def hook(_event):
        session.sessionManager.appendCustomEntry("hook-progress", {"phase": "summary"})
        return {"compaction": result} if from_hook else None

    async def summarize(*_args, **_kwargs):
        session.sessionManager.appendCustomEntry("native-progress", {"phase": "summary"})
        return result

    session.moments.session_before_compact = AsyncMock(side_effect=hook)
    summary = AsyncMock(side_effect=summarize)
    monkeypatch.setattr("misaka.core.agent_session.run_compaction", summary)
    if mode == "manual":
        await session.compact()
    else:
        await session._run_auto_compaction(mode, False)

    entry = session.sessionManager.getLeafEntry()
    assert entry["type"] == "compaction"
    assert entry["fromHook"] is from_hook
    assert summary.await_count == (0 if from_hook else 1)
    session.moments.session_compact_failed.assert_not_awaited()


def test_failed_write_keeps_the_metadata_and_old_live_view(monkeypatch):
    session = _session()
    manager = session.sessionManager
    result, source = _result(session), session._compaction_source()
    manager.appendCustomEntry("progress", {"phase": "execute"})
    entries, messages = copy.deepcopy(manager.getEntries()), copy.deepcopy(session.messages)

    def fail(_entry):
        raise OSError("fixture disk full")

    monkeypatch.setattr(manager, "_persist", fail)
    with pytest.raises(OSError, match="fixture disk full"):
        session._publish_compaction(result, False, source)
    assert manager.getEntries() == entries
    assert session.messages == messages
