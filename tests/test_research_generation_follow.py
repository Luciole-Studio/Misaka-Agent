"""The research driver follows a card into a new attempt instead of failing the run.

2026-09-18 (B13): stopping and continuing one finished card (a Sister tool operation)
changed its generation; the driver raised, the run went to ``failed`` and its cleanup stopped
every other card in the scope."""
from misaka.core.research import workflow


async def test_drive_tasks_follows_a_card_into_its_next_attempt(monkeypatch):
    rows = [{"id": "t1", "status": "done", "generation": 2},
            {"id": "t2", "status": "done", "generation": 1}]
    monkeypatch.setattr(workflow.runs, "get", lambda con, run_id: {"id": run_id, "workspace": "/tmp", "limits_json": "{}"})
    monkeypatch.setattr(workflow.runs, "tasks", lambda con, run_id: rows)
    monkeypatch.setattr(workflow.runs, "stop_requested", lambda con, run_id: False)
    monkeypatch.setattr(workflow.budget, "exhausted", lambda con, cap: False)
    monkeypatch.setattr(workflow, "_release_dependencies", lambda con, run_id, owner=None: None)
    events = []

    async def progress(payload):
        events.append(payload)

    class Runner:
        async def pending(self, scope):
            return []

        async def launch_ready(self, **kwargs):
            return None

    captured = {"t1": {"id": "t1", "status": "running", "generation": 1},
                "t2": {"id": "t2", "status": "running", "generation": 1}}
    result = await workflow._drive_tasks_inner(
        None, {}, Runner(), "r1", scope={"t1", "t2"}, captured=captured, progress=progress, poll_seconds=0)
    assert result == "done"
    moves = [e["message"] for e in events if "moved to attempt" in e["message"]]
    assert moves == ["Card t1 moved to attempt 2 (was 1); the run follows it."]
    assert captured["t1"]["generation"] == 2
