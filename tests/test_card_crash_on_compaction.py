"""Two live-run failures from 2026-09-23 (run r_5b1357a9c6, card t_c69786).

The card's Sister process died twice with ``'Usage' object has no attribute 'get'``: the footer's
render asked the session for its context usage while an auto-compaction was in flight, and
the session's own context-token count assumed a dict where an in-process message carries a ``Usage``
model. Between the two deaths the pane runner asked the daemon to start the card on every
two-second poll, and the daemon's refusal ("waiting out a retry backoff for another N s")
changed every second, so the once-per-message print printed every time."""
import time

import pytest

from misaka.ai.types import Usage
from misaka.core.compaction import calculateContextTokens
from misaka.core.research import node
from misaka.ui.panel import client


def test_context_tokens_read_a_usage_model_as_well_as_a_dict():
    usage = Usage(input=10, output=5, cacheRead=100, cacheWrite=20,
                  totalTokens=135, cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0})
    assert calculateContextTokens(usage) == 135
    assert calculateContextTokens(usage.model_dump()) == 135
    assert calculateContextTokens({"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4}) == 10
    assert calculateContextTokens({}) == 0


@pytest.mark.parametrize("next_attempt_offset, requested", [(60, False), (-1, True), (None, True)])
async def test_pane_runner_leaves_a_card_in_backoff_alone(monkeypatch, capsys, next_attempt_offset, requested):
    row = {"id": "t_x", "status": "ready", "generation": 1,
           "next_attempt_at": None if next_attempt_offset is None else int(time.time()) + next_attempt_offset}
    monkeypatch.setattr(node.task_store, "get", lambda con, tid: row)
    calls = []

    def request(method, params=None, **_kwargs):
        calls.append((method, params))
        return {"pane_id": "p9", "pid": 1}

    monkeypatch.setattr(client, "request", request)
    runner = node.PaneRunner(con=None, cfg={}, label="fixture", pane="p1")
    await runner.launch_ready(task_ids=["t_x"])
    assert bool(calls) is requested
    assert "backoff" not in capsys.readouterr().out
