"""A compaction checkpoint is written only when the engine changed the context.

2026-09-23: two deterministic re-emissions made `compact()` write a full-context checkpoint
on every round of every session that had ever compacted -- cards t_998052 and t_9f10b6 reached
62 and 70 checkpoints of ~14 MB each (page images embedded in every one). The engine re-emits
its summary scaffold without a source ordinal and before the host's fence, and spells an empty
assistant row ``""`` where the replay spells ``[]``; neither is a change worth a checkpoint."""
import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from misaka.core.platform.prompt_guard import untrusted
from misaka.extensions.misaka_lcm.host import context_engine, ingest

SUMMARY = "[Session Arc Summary (d1, node 7)]\nwhat was found"
USAGE = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(context_engine, "_summary_scope", lambda *_, **__: nullcontext())
    monkeypatch.setattr(context_engine, "_archive_map", lambda *_: ({}, {}))
    monkeypatch.setattr(context_engine.fence, "is_tainted", lambda *a, **k: True)
    from misaka.extensions.misaka_lcm.host import carry
    monkeypatch.setattr(carry, "sources", lambda *_: {})
    monkeypatch.setattr(context_engine.rollups, "nudge", lambda *_: None)

    def build(native, engine_output):
        replay = ingest.Replay(native)
        built = SimpleNamespace(_summary_frontier_nodes=list, current_session_id="s1",
                                last_compression_status="sanitized",
                                compress=lambda *a, **k: copy.deepcopy(engine_output))
        return context_engine.Prepared(built, context_engine.Transcript([], []), replay,
                                       copy.deepcopy(replay.messages), None, 10, "threshold", None, True, False)
    return build


def test_a_re_emitted_scaffold_behind_the_fence_is_no_checkpoint(harness):
    fenced = untrusted("lcm:compaction:s1", SUMMARY)
    native = [{"role": "user", "content": fenced, "timestamp": 1790000000000},
              {"role": "user", "content": "next question", "timestamp": 1790000000001}]
    engine_output = [{"role": "user", "content": SUMMARY},
                     {"role": "user", "content": "next question", ingest.SOURCE: 1}]
    assert context_engine.compact(harness(native, engine_output)) is None


def test_an_empty_assistant_row_spelled_differently_is_no_checkpoint(harness):
    native = [{"role": "user", "content": "q", "timestamp": 1790000000000},
              {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm"}],
               "api": "a", "provider": "p", "model": "m", "usage": USAGE, "stopReason": "stop",
               "timestamp": 1790000000001}]
    replay = ingest.Replay(native)
    assert replay.messages[1]["content"] in ("", [])
    engine_output = [{"role": "user", "content": "q", ingest.SOURCE: 0},
                     {"role": "assistant", "content": "", ingest.SOURCE: 1}]
    prepared = harness(native, engine_output)
    assert context_engine.compact(prepared) is None
    restored = prepared.replay.restore(engine_output)
    assert restored[1] == native[1] or getattr(restored[1], "content", None) == native[1]["content"]


def test_a_real_change_still_writes(harness):
    native = [{"role": "user", "content": "q", "timestamp": 1790000000000},
              {"role": "user", "content": "old", "timestamp": 1790000000001}]
    engine_output = [{"role": "user", "content": "q", ingest.SOURCE: 0},
                     {"role": "user", "content": SUMMARY}]
    result = context_engine.compact(harness(native, engine_output))
    assert result is not None
    assert result.details["lcm"]["scaffolds"] == [1]
