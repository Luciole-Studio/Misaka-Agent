"""A card Last Order cancelled while the cards ran stays cancelled when the node plans again
(a resume, a withdrawn follow-up): the plan was resubmitted as first accepted (0.18.10 sweep)."""
import asyncio

from test_research_plan_change import change, executing, task  # noqa: F401

from misaka.core.research import planner, runs, workflow


def test_a_cancelled_card_stays_cancelled_when_the_node_plans_again(executing, monkeypatch):  # noqa: F811
    con, run, root, tool, ctx = executing
    change(tool, ctx, cancel=[{"card": "c", "reason": "covered elsewhere"}])
    asyncio.run(workflow._apply_plan_changes(con, run, root, 1, [0], None, (run, root)))
    assert sorted(workflow._round_cards(con, run, root, 1)) == ["a", "b"]
    # The node fails later and is resumed: runs._resume puts a failed node back to `planning`.
    con.execute("UPDATE research_execution SET status='failed' WHERE id=?", (root["id"],))
    runs.resume(con, run["id"], driver_lock="driver")
    monkeypatch.setattr(planner, "plan_needs_user", lambda *a, **k: False)
    monkeypatch.setattr(planner, "publish_project_brief", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "_views", lambda *a, **k: asyncio.sleep(0))
    seen = {}
    async def drive(con_, cfg, runner, run_id, *, scope, **kw):
        seen["scope"] = {r["local_id"]: r["status"] for r in runs.tasks(con, run_id) if r["id"] in scope}
        return "stopped"
    monkeypatch.setattr(workflow, "_drive_tasks", drive)
    out = asyncio.run(workflow._expand(con, {}, None, None, runs.get(con, run["id"]), runs.node(con, root["id"]),
                                       context=None, tool_call_id="t", poll_seconds=0, progress=None))
    assert out == "stopped" and "c" not in seen["scope"]
    plan_md = con.execute("SELECT path FROM research_artifacts WHERE kind='plan'").fetchone()[0]
    with open(plan_md, encoding="utf-8") as f:
        assert "Changed while the cards ran" in f.read()
