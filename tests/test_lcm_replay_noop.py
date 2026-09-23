"""Cached receipt timestamps are not native context changes."""
import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import context_engine, ingest


@pytest.fixture
def prepared(monkeypatch):
    replay = ingest.Replay([{"role": "user", "content": "question", "timestamp": 1789395211383}])
    messages = copy.deepcopy(replay.messages)
    messages[0]["timestamp"] = 1789395211.380
    built = SimpleNamespace(_summary_frontier_nodes=list, current_session_id="fixture")
    monkeypatch.setattr(context_engine, "_summary_scope", lambda *_, **__: nullcontext())
    monkeypatch.setattr(context_engine, "_archive_map", lambda *_: ({}, {}))
    from misaka.extensions.misaka_lcm.host import carry
    monkeypatch.setattr(carry, "sources", lambda *_: {})
    return context_engine.Prepared(built, context_engine.Transcript([], []), replay, messages,
                                   None, 10, "auto", None, False, False)


def test_compact_skips_timestamp_only_cache_hit(prepared):
    original = copy.deepcopy(prepared.messages)
    assert context_engine.compact(prepared) is None
    assert prepared.messages == original


@pytest.mark.parametrize("change", ["content", "metadata", "retry", "scaffold"])
def test_compact_keeps_real_context_changes(prepared, change, monkeypatch):
    if change == "content":
        prepared.messages[0]["content"] = "changed"
    elif change == "metadata":
        prepared.messages[0]["_micro_compact_marker"] = True
    elif change == "retry":
        prepared.replay_changed = True
    elif change == "scaffold":
        prepared.messages.append({"role": "user", "content": "new summary"})
        monkeypatch.setattr(context_engine, "_guarded_summary", lambda _, text: text)
    result = context_engine.compact(prepared)
    assert result is not None
    if change == "metadata":
        assert result.details["lcm"]["nativeMetadata"] == {"0": {"_micro_compact_marker": True}}
    if change == "scaffold":
        assert result.details["lcm"]["scaffolds"] == [1]


def test_prepare_skips_timestamp_only_ingest_result(monkeypatch):
    manager = SessionManager.inMemory(cwd="/tmp")
    manager.appendMessage({"role": "user", "content": "question", "timestamp": 1789395211383})
    ctx = SimpleNamespace(sessionManager=manager, model=None)

    def cached(messages):
        return [{**message, "timestamp": 1789395211.380} for message in messages]

    built = SimpleNamespace(ingest=lambda _: None, _ingest_messages=cached,
                            api_mode="", provider="", model="", base_url="")
    monkeypatch.setattr(context_engine, "bound_engine", lambda _: built)
    monkeypatch.setattr(context_engine, "_messages", lambda *_: ingest.upstream_messages(manager.buildSessionContext().messages))
    monkeypatch.setattr(context_engine, "_summary_scope", lambda *_, **__: nullcontext())
    assert context_engine.prepare({"allowCompression": False}, ctx) is None


@pytest.mark.parametrize("field,value", [
    ("content", "different"), ("role", "assistant"), ("tool_call_id", "other"),
    ("tool_name", "other"), ("tool_calls", []), (ingest.SOURCE, 1),
    *[(key, value) for key in sorted(ingest.NATIVE_METADATA) for value in (False, True)],
])
def test_noop_comparison_keeps_provenance_and_semantic_fields(prepared, field, value):
    prepared.messages[0][field] = value
    assert not prepared.replay.unchanged(prepared.messages)


def test_unchanged_replay_preserves_originals_and_metadata(prepared):
    prepared.replay.messages[0]["_micro_compact_marker"] = True
    prepared.messages[0]["_micro_compact_marker"] = True
    assert prepared.replay.unchanged(prepared.messages)
    assert prepared.replay.restore(prepared.messages) == prepared.replay.originals
    assert not prepared.replay.unchanged([])
    assert not prepared.replay.unchanged(prepared.messages * 2)


def test_real_ingest_cache_jitter_then_reopen_and_fork(tmp_path, monkeypatch):
    """Use the host's actual NativeLCMEngine, SQLite store and replay cache."""
    from misaka.extensions.misaka_lcm.vendor.config import LCMConfig

    config = LCMConfig(database_path=context_engine.config_bridge.database_path(SimpleNamespace(cwd=str(tmp_path))))
    monkeypatch.setenv("LCM_DATABASE_PATH", config.database_path)
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path))
    monkeypatch.delenv("MISAKA_SUBAGENT_PARENT_SESSION_ID", raising=False)
    monkeypatch.setattr(context_engine.config_bridge, "load_config", lambda **_: config)
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    manager.appendCustomMessageEntry("todo-reminder", "remember", True)
    ctx = SimpleNamespace(sessionManager=manager, model=None, cwd=str(tmp_path))

    def check_cache():
        built = context_engine.bound_engine(ctx)
        native = copy.deepcopy(ctx.sessionManager.buildSessionContext().messages)
        native[0].timestamp += 3
        count = len(ctx.sessionManager.getEntries())
        for _ in range(5):
            assert context_engine.prepare({"allowCompression": False, "messages": native}, ctx) is None
        cached = built._cached_active_replay_messages(ingest.upstream_messages(native))
        assert cached is not None
        assert cached[0]["timestamp"] != native[0].timestamp / 1000
        assert len(ctx.sessionManager.getEntries()) == count

    try:
        check_cache()
        context_engine.close(ctx)
        ctx.sessionManager = SessionManager.open(manager.sessionFile)
        check_cache()
        context_engine.close(ctx)
        ctx.sessionManager.createBranchedSession(ctx.sessionManager.getLeafId())
        check_cache()
    finally:
        context_engine.close(ctx)
