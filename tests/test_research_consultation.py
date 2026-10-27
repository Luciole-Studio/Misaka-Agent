"""A research card answering a message stays done (R40): the run neither cascades from it nor
loses sight of what the answer changed. 2026-10-25: Ea's consultation window was closed while
it loaded; Ea went to stopped and the run's dependency release stopped X3 and C4b."""
from pathlib import Path

from test_research_node_routine import RESEARCHER, lab  # noqa: F401 - the fixture

from misaka.core.network import dispatch
from misaka.core.platform import cards, tasks
from misaka.core.research import runs, workflow

LOCK = "consult:test"


def _linked(lab, title, needs=()):  # noqa: F811
    con, run, root = lab.con, lab.run, lab.root
    tid = cards.create(con, str(lab.tmp), title, "## deliverable\nx.md\n", RESEARCHER, needs=needs)
    runs.link_task(con, run["id"], tid, kind="research", node=root, local_id=title.lower())
    con.commit()
    return tid


def _deliver(lab, tid, text="Finding one"):  # noqa: F811
    con = lab.con
    row = tasks.get(con, tid)
    out = Path(row["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "x.md").write_text("the deliverable\n", encoding="utf-8")
    rel = str((out / "x.md").relative_to(Path(row["workspace"])))
    assert tasks.claim(con, tid, "lock:1", generation=int(row["generation"]))
    row = tasks.get(con, tid)
    submission = {"summary": "done", "artifacts": [rel], "notes": "", "uncertain": [],
                  "findings": [{"text": text, "claim_type": "fact", "source_file": rel}]}
    assert dispatch.accept_state(con, row, submission, generation=row["generation"], claim_lock="lock:1")
    workflow.settle_done_tasks(con, run_id=lab.run["id"])
    return rel


def test_a_stopped_consultation_leaves_the_cards_built_on_her_waiting(lab):  # noqa: F811
    con, run = lab.con, lab.run
    ea = _linked(lab, "Ea")
    fa = _linked(lab, "Fa")                                   # still working
    x3 = _linked(lab, "X3", needs=[ea, fa])
    _deliver(lab, ea)
    consultation = tasks.start_consultation(con, ea, generation=1, lock=LOCK)
    tasks.end_consultation(con, consultation["id"], LOCK, "stopped", reason="her window was closed")

    workflow._release_dependencies(con, run["id"])

    assert tasks.get(con, x3)["status"] == "todo"
    assert tasks.latest_payload(con, x3, "dependency_failed") is None
    _deliver(lab, fa)
    workflow._release_dependencies(con, run["id"])
    assert tasks.get(con, x3)["status"] == "ready"


def test_an_answer_that_changed_nothing_settles_again_as_it_was(lab):  # noqa: F811
    con, run = lab.con, lab.run
    ea = _linked(lab, "Ea")
    _deliver(lab, ea)
    consultation = tasks.start_consultation(con, ea, generation=1, lock=LOCK)
    tasks.end_consultation(con, consultation["id"], LOCK, "done")
    assert not workflow._settled_current(con, ea, 1), "a consultation ended since: look again"

    workflow.settle_done_tasks(con, run_id=run["id"])

    assert workflow._settled_current(con, ea, 1)
    assert tasks.latest_payload(con, ea, "research_artifact_drift", generation=1) is None
    assert [r["text"] for r in con.execute("SELECT text FROM research_findings WHERE task_id=?", (ea,))] \
        == ["Finding one"]


def test_a_consultation_stopped_half_way_through_an_edit_is_caught_as_drift(lab):  # noqa: F811
    con, run = lab.con, lab.run
    ea = _linked(lab, "Ea")
    rel = _deliver(lab, ea)
    consultation = tasks.start_consultation(con, ea, generation=1, lock=LOCK)
    (Path(tasks.get(con, ea)["workspace"]) / rel).write_text("half edited\n", encoding="utf-8")
    tasks.end_consultation(con, consultation["id"], LOCK, "stopped", reason="her window was closed")

    workflow.settle_done_tasks(con, run_id=run["id"])

    assert tasks.latest_payload(con, ea, "research_artifact_drift", generation=1) is not None
    row = tasks.get(con, ea)
    assert (row["status"], row["generation"]) == ("ready", 2), "back to her to declare again"


async def test_a_halted_run_stops_what_its_finished_cards_are_answering(lab, monkeypatch):  # noqa: F811
    con = lab.con
    ea = _linked(lab, "Ea")
    quiet = _linked(lab, "Quiet")
    _deliver(lab, ea)
    _deliver(lab, quiet)
    consultation = tasks.start_consultation(con, ea, generation=1, lock=LOCK)
    from misaka.ui.panel import client as net
    calls = []
    monkeypatch.setattr(net, "request", lambda method, params=None, **kw: calls.append((method, params)))

    await workflow._stop_consultations(con, [ea, quiet])

    assert calls == [("card.stop", {"task_id": ea, "consultation": consultation["id"],
                                    "reason": "the research run was stopped"})]
