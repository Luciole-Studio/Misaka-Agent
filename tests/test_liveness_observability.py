"""Liveness verdicts and refused runner claims explain themselves; warnings go to a file.

2026-09-18 (B20/B33/B6): a long-lived daemon judged every session dead and a same-age
driver's node processes found their claims "superseded", with no trace of what had been
compared; and every ``logger.warning`` was painted into the pane under the cursor because
no process had a log handler."""
import logging
import os

import pytest

from misaka.config import home
from misaka.core.platform import processes, tasks
from misaka.core.research import runs, workflow


def test_explain_liveness_names_the_verdict():
    me = os.getpid()
    mine = processes.identity(me)
    assert processes.explain_liveness(me, mine) == (True, "alive")
    alive, reason = processes.explain_liveness(me, mine.replace("Mac", "Other", 1) + "x")
    assert not alive and reason.startswith("identity mismatch")
    alive, reason = processes.explain_liveness(2**22 - 7, "host:4194297:1.0")
    assert not alive and reason == "pid gone"
    assert processes.explain_liveness(None, None) == (False, "no pid or no recorded identity")


def test_only_anomalous_verdicts_are_logged_and_only_once(caplog):
    processes._REPORTED.clear()
    me = os.getpid()
    with caplog.at_level(logging.WARNING, logger="misaka.core.platform.processes"):
        assert not processes.identity_is_alive(2**22 - 7, "host:4194297:1.0")   # gone: silent
        assert not processes.identity_is_alive(me, "host:0:0.0")                 # mismatch: one line
        assert not processes.identity_is_alive(me, "host:0:0.0")                 # same anomaly: no repeat
    lines = [r.getMessage() for r in caplog.records if r.name == "misaka.core.platform.processes"]
    assert len(lines) == 1
    assert "identity mismatch" in lines[0] and str(me) in lines[0]


def test_self_drift_is_reported_once(caplog, monkeypatch):
    processes._REPORTED.clear()
    processes._SELF_IDENTITY = "host:1:1.000000"
    monkeypatch.setattr(processes, "identity", lambda pid: "host:1:2.000000")
    with caplog.at_level(logging.WARNING, logger="misaka.core.platform.processes"):
        processes._self_check()
        processes._self_check()
    drift = [r.getMessage() for r in caplog.records if "drifted" in r.getMessage()]
    assert len(drift) == 1
    processes._SELF_IDENTITY = None
    processes._REPORTED.clear()


def _branch(tmp_path):
    con = tasks.connect(str(tmp_path / "board.db"))
    runs.init(con)
    run = runs.create(con, workspace=str(tmp_path), question="why does it drift?",
                      limits={"max_depth": 1, "parallel": 1}, token_start=0, origin_session=None)
    node = runs.create_node(con, run["id"], trigger="why does it drift?", parent_id=None, depth=0)
    return con, node["id"]


def test_a_refused_claim_explains_the_key_mismatch_and_leaves_it_on_the_row(tmp_path):
    con, node_id = _branch(tmp_path)
    key = runs.prepare_runner(con, "research_branches", node_id)
    assert not runs.claim_runner(con, "research_branches", node_id, "0" * 32)
    text = runs.note_claim_failure(con, "research_branches", node_id, "0" * 32)
    assert text.startswith("claim refused: runner key mismatch")
    assert key[:8] in text and "00000000" in text
    assert runs.node(con, node_id)["last_error"] == text
    # A later, different explanation does not overwrite the first cause.
    runs.note_claim_failure(con, "research_branches", node_id, key)
    assert runs.node(con, node_id)["last_error"] == text


def test_a_refused_claim_explains_the_identity_mismatch(tmp_path):
    con, node_id = _branch(tmp_path)
    key = runs.prepare_runner(con, "research_branches", node_id)
    con.execute("UPDATE research_branches SET runner_pid=?, runner_identity=? WHERE id=?",
                (os.getpid(), "host:0:0.000000", node_id))
    assert not runs.claim_runner(con, "research_branches", node_id, key)
    text = runs.note_claim_failure(con, "research_branches", node_id, key)
    assert "runner identity mismatch" in text
    assert "host:0:0.000000" in text and str(os.getpid()) in text


def test_a_successful_claim_has_nothing_to_explain_but_still_answers(tmp_path):
    con, node_id = _branch(tmp_path)
    key = runs.prepare_runner(con, "research_branches", node_id)
    assert runs.claim_runner(con, "research_branches", node_id, key)
    assert "no visible reason" in runs.note_claim_failure(con, "research_branches", node_id, key)


def test_runner_error_carries_the_pane_tail():
    row = {"status": "queued", "last_error": None}
    plain = workflow._runner_error("Node b1's process", row)
    assert "no exception was recorded" in str(plain) and "last output" not in str(plain)

    class Spawner:
        def tail(self, handle):
            return "node b1: superseded execution: claim refused: runner key mismatch"

    with_tail = workflow._runner_error("Node b1's process", row, tail=workflow._pane_tail(Spawner(), object()))
    assert "its last output: node b1: superseded execution: claim refused" in str(with_tail)
    assert workflow._pane_tail(object(), object()) is None


def test_configure_logging_sends_warnings_to_a_file(tmp_path, monkeypatch):
    from misaka.config import engine
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = engine.configure_logging()
        assert path == str(home.path("log"))
        assert engine.configure_logging() == path                      # idempotent
        assert len(root.handlers) == len(before) + 1
        logging.getLogger("misaka.test.b6").warning("painted nowhere")
        for handler in root.handlers:
            handler.flush()
        assert "painted nowhere" in home.path("log").read_text(encoding="utf-8")
    finally:
        for handler in root.handlers[len(before):]:
            root.removeHandler(handler)
            handler.close()


@pytest.mark.parametrize("pid", [0, None])
def test_liveness_rejects_missing_pids_quietly(pid, caplog):
    with caplog.at_level(logging.WARNING):
        assert not processes.identity_is_alive(pid, "x")
    assert not [r for r in caplog.records if r.name == "misaka.core.platform.processes"]
