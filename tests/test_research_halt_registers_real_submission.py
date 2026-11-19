"""The halt path settles for real: a card accepted with a genuine submission (digests recorded by
``dispatch.accept_state``) is registered as a research artifact before the partial report is
written when the driver is interrupted, and the report lists it. The lifecycle fixture mocks
settle away; this test puts the real one back and checks the database, not a recorder."""
import hashlib
from pathlib import Path

from test_research_root_lifecycle import (
    root_run,  # noqa: F401 - the fixture is used by name
)

from misaka.core.network import dispatch
from misaka.core.platform import cards, tasks
from misaka.core.research import runs, workflow

REAL_SETTLE = workflow.settle_done_tasks


async def test_an_interrupted_run_registers_what_the_sisters_delivered(root_run, monkeypatch):  # noqa: F811
    con, run, root, _owner, _events = root_run
    tid = cards.create(con, run["workspace"], "fixture output", "fixture body", "fixture")
    runs.link_task(con, run["id"], tid, kind="research", node=root, local_id="source")
    task = tasks.get(con, tid)
    evidence = Path(task["output_dir"]) / "evidence.md"
    evidence.write_text("accepted bytes\n", encoding="utf-8")
    rel = str(evidence.relative_to(run["workspace"]))
    con.execute("UPDATE tasks SET status='running',claim_lock='fixture',claim_expires=? WHERE id=?", (10**12, tid))
    assert dispatch.accept_state(con, tasks.get(con, tid), {"summary": "fixture", "artifacts": [rel]},
                                 generation=task["generation"], claim_lock="fixture")
    assert tasks.get(con, tid)["status"] == "done"
    assert runs.artifacts(con, run["id"], task_id=tid) == []

    monkeypatch.setattr(workflow, "settle_done_tasks", REAL_SETTLE)

    async def expand(*args, **kwargs):
        raise InterruptedError("The user requested a stop.")

    monkeypatch.setattr(workflow, "_expand", expand)
    result = await workflow.run(con, {}, object(), run_id=run["id"])
    assert result["reason"] == "stopped"

    registered = runs.artifacts(con, run["id"], task_id=tid)
    assert [row["kind"] for row in registered] == ["task_output"]
    assert registered[0]["sha256"] == hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert tasks.latest_payload(con, tid, "research_v2_settled", generation=task["generation"])
    assert "evidence.md" in result["final"]["content"]
