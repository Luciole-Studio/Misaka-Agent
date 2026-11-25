"""Resume a Research driver after it dies with live claims or unregistered submissions."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path

import psutil
import pytest

from misaka.config import home
from misaka.core.network import dispatch
from misaka.core.platform import cards, tasks
from misaka.core.research import runs
from tests.http_capture import Reply, captureServer
from tests.test_runtime_trace_interruption import _interpreter


def _json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


SOURCES = {"north": b"N1\t1200\nN2\t800\n", "south": b"S1\t1500\nS2\t500\n"}


def _prepare(project, server, bounded):
    _json(home.path("models"), {"providers": {"test-recovery": {
        "baseUrl": server.baseUrl + "/v1", "api": "openai-completions", "apiKey": "local-fixture",
        "models": [{"id": "card-fixture", "contextWindow": 64000, "maxTokens": 2048}],
    }}})
    _json(home.path("settings"), {
        "defaultProvider": "test-recovery", "defaultModel": "card-fixture", "defaultThinkingLevel": "off",
        "extensions": ["-builtin:mcp", "-builtin:misaka_lcm"],
        "research": {"plan_approval": False, "token_cap": 500000},
    })
    for sister in ("10032", "10033"):
        directory = home.path("profiles_root") / sister
        directory.mkdir(parents=True)
        (directory / "SOUL.md").write_text("Fixture Sister\n", encoding="utf-8")
    cards.init_project(str(project), draft_brief=False, git=False)
    (project / "sources").mkdir(exist_ok=True)
    for side, data in SOURCES.items():
        (project / "sources" / f"{side}.txt").write_bytes(data)
    with closing(tasks.connect(str(home.path("db")))) as con:
        run = runs.create(con, workspace=str(project), question="Recover both fixture deliverables",
                          limits={"diagnostics": True, "sister_parallel": 2, "max_depth": 0, "max_nodes": 1})
        root = runs.root(con, run["id"])
        for side, sister in (("north", "10032"), ("south", "10033")):
            options = {"local_work": {"input_paths": [f"sources/{side}.txt"], "tools": ["read", "write"]}} if bounded else {}
            cid = cards.create(con, str(project), side, f"Fixture-{side}\n\n## deliverable\n`evidence.md`", sister,
                               **options)
            runs.link_task(con, run["id"], cid, kind="research", node=root, local_id=side)
        runs.set_node(con, root["id"], status="executing")
        return run["id"], {r["local_id"]: dict(r) for r in runs.tasks(con, run["id"])}


def _tool_call(name, call_id, **kwargs):
    return {"index": 0, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(kwargs)}}


def _reply(delta, finish):
    return Reply(body=("data: " + json.dumps({
        "id": "card-response", "object": "chat.completion.chunk", "model": "card-fixture",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }) + "\n\ndata: [DONE]\n\n").encode())


def _user_text(messages):
    for message in messages:
        if message["role"] != "user":
            continue
        content = message.get("content") or ""
        if isinstance(content, str):
            yield content
        else:
            yield from (part.get("text", "") for part in content)


@contextmanager
def _launch(python, env, project, log, run_id, gate=None, *, prepared=False):
    args = [python, "-m", "tests.test_research_process_recovery", run_id]
    if gate is not None:
        args.append(str(gate))
    if prepared:
        args.append("--prepared")
    with log.open("w", encoding="utf-8") as output:
        proc = subprocess.Popen(args, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT)
        identity = psutil.Process(proc.pid)
        try:
            yield proc, identity
        finally:
            children = identity.children(recursive=True) if identity.is_running() else []
            if proc.poll() is None:
                assert identity.is_running(), "The fixture driver PID was reused"
                proc.kill()
                proc.wait(timeout=10)
            for child in reversed(children):
                # A psutil.Process returned by children() carries the PID and creation time.
                if child.is_running():
                    child.kill()
            _, alive = psutil.wait_procs(children, timeout=10)
            assert not alive, "Fixture worker survived its driver"


def _until(proc, log, condition):
    deadline = time.monotonic() + 60
    while proc.poll() is None and time.monotonic() < deadline:
        if result := condition():
            return result
        time.sleep(0.05)
    pytest.fail(f"Research fixture did not reach its boundary (exit {proc.poll()})\n{log.read_text(encoding='utf-8')}")


def _trace(python, env, project, run_id):
    result = subprocess.run([python, "-m", "misaka", "trace", "show", run_id, "--json"], cwd=project, env=env,
                            capture_output=True, encoding="utf-8", check=True, timeout=30)
    return json.loads(result.stdout)


@pytest.mark.parametrize(("boundary", "bounded"), [("claim", False), ("submitted", False),
                                                    ("claim", True), ("submitted", True), ("written", True),
                                                    ("prepared", True)])
def test_research_cli_reuses_accepted_cards_and_fences_dead_claims(tmp_path, monkeypatch, boundary, bounded):
    project, isolated_home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    isolated_home.mkdir()
    monkeypatch.setenv("MISAKA_HOME", str(isolated_home))
    python, env = _interpreter()
    env = {k: v for k, v in env.items() if not k.endswith(("_API_KEY", "_BASE_URL")) and k != "MISAKA_NET_PANE"}
    env.update(PYTHONUTF8="1", NO_PROXY="127.0.0.1", MISAKA_OFFLINE="1", GIT_CEILING_DIRECTORIES=str(tmp_path))
    resume, release_initial, release_retry = threading.Event(), threading.Event(), threading.Event()
    waiting = {stage: threading.Event() for stage in ("initial", "retry")}
    writes = Counter()
    requests = Counter()

    def respond(request):
        messages = request.body.get("messages", [])
        side = next((name for name in ("north", "south") if f"Fixture-{name}".encode() in request.raw), None)
        if side is None:
            return Reply(status=400)
        tools = {entry["function"]["name"] for entry in request.body["tools"]}
        if bounded:
            assert tools == {"read", "write", "misaka_card_note", "misaka_card_complete"}
        else:
            assert "find" in tools and "read" in tools and "misaka_card_complete" in tools
        results = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
        if f"call_complete_{side}" in results:
            requests["closing_reply"] += 1
            return _reply({"content": f"{side} delivered"}, "stop")
        if f"call_write_{side}" in results and boundary == "written" and side == "south" and not resume.is_set():
            waiting["initial"].set()
            release_initial.wait(70)
            return Reply(status=503)
        output = Path(by_side[side]["output_dir"], "evidence.md")
        if f"call_write_{side}" in results or f"call_existing_{side}" in results:
            if f"call_existing_{side}" in results:
                assert results[f"call_existing_{side}"].strip() == "rows: 2\ntotal cents: 2000"
            requests["completion_tool"] += 1
            return _reply({"tool_calls": [_tool_call("misaka_card_complete", f"call_complete_{side}",
                                                     summary=f"{side} delivered")]}, "tool_calls")
        if f"call_read_{side}" in results:
            rows = [int(line.split("\t")[1]) for line in results[f"call_read_{side}"].strip().splitlines()]
            if boundary == "written" and side == "south" and resume.is_set():
                assert sum(rows) == 2000
                requests["read_existing"] += 1
                return _reply({"tool_calls": [_tool_call("read", f"call_existing_{side}", path=str(output))]},
                              "tool_calls")
            requests["write"] += 1
            writes[side] += 1
            assert writes[side] == 1, "A recovered card requested another write"
            return _reply({"tool_calls": [_tool_call("write", f"call_write_{side}", path=str(output),
                                                     content=f"rows: {len(rows)}\ntotal cents: {sum(rows)}\n")]},
                          "tool_calls")
        if side == "south" and resume.is_set() and boundary in {"claim", "written"}:
            waiting["retry"].set()
            assert release_retry.wait(70), "Parent never released the new claim"
        if side == "south" and not resume.is_set() and boundary == "claim":
            waiting["initial"].set()
            release_initial.wait(70)
            return Reply(status=503)
        source = project / "sources" / f"{side}.txt"
        if bounded:
            text = "\n".join(_user_text(messages))
            assert str(source) in text or source.as_posix() in text
            if boundary == "written" and side == "south" and resume.is_set():
                assert "deliverable already exists" in text
        requests["read"] += 1
        return _reply({"tool_calls": [_tool_call("read", f"call_read_{side}", path=str(source))]}, "tool_calls")

    with captureServer(respond) as server, closing(tasks.connect(str(home.path("db")))) as con:
        run_id, by_side = _prepare(project, server, bounded)
        north, south = (by_side[side]["id"] for side in ("north", "south"))
        gate = tmp_path / "settle.json"
        initial_log, resume_log = tmp_path / "initial.log", tmp_path / "resume.log"

        def claimed():
            return waiting["initial"].is_set() and tasks.get(con, north)["status"] == "done"

        try:
            with _launch(python, env, project, initial_log, run_id,
                         gate if boundary in {"submitted", "prepared"} else None,
                         prepared=boundary == "prepared") as (proc, identity):
                if boundary != "submitted":
                    check_gate = claimed if boundary != "prepared" else (
                        lambda: gate.is_file() and tasks.get(con, north)["status"] == "done")
                    _until(proc, initial_log, check_gate)
                    abandoned = dict(tasks.get(con, south))
                    assert abandoned["status"] == "running" and abandoned["claim_lock"]
                    assert abandoned["worker_pid"] in {p.pid for p in identity.children(recursive=True)}
                    if boundary == "written":
                        assert tasks.latest_payload(con, south, "artifact_written")
                        assert not tasks.latest_payload(con, south, "submitted")
                    if boundary == "prepared":
                        parked = json.loads(gate.read_text(encoding="utf-8"))
                        assert parked == {"card_id": south, "pid": abandoned["worker_pid"],
                                          "created": psutil.Process(abandoned["worker_pid"]).create_time()}
                        assert tasks.latest_payload(con, south, "terminal_submission_prepared")
                        assert not tasks.latest_payload(con, south, "submitted")
                else:
                    _until(proc, initial_log, gate.is_file)
                    assert json.loads(gate.read_text(encoding="utf-8")) == {
                        "run_id": run_id, "pid": identity.pid, "created": identity.create_time(),
                    }
                accepted = {r["id"]: r["generation"] for r in runs.tasks(con, run_id) if r["status"] == "done"}
                first_hash = hashlib.sha256(Path(by_side["north"]["output_dir"], "evidence.md").read_bytes()).hexdigest()
                assert north in accepted
                assert not runs.artifacts(con, run_id, task_id=north)
            release_initial.set()
            before = _trace(python, env, project, run_id)
            _json(tmp_path / "before.json", before)

            resume.set()
            with _launch(python, env, project, resume_log, run_id) as (proc, _):
                if boundary in {"claim", "written"}:
                    _until(proc, resume_log, waiting["retry"].is_set)
                    current = tasks.get(con, south)
                    assert current["status"] == "running" and current["claim_lock"] != abandoned["claim_lock"]
                    assert not dispatch.accept_state(con, abandoned, {"summary": "stale", "artifacts": []},
                                                     generation=abandoned["generation"], claim_lock=abandoned["claim_lock"])
                    release_retry.set()
                try:
                    code = proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    pytest.fail(f"Research fixture did not finish recovery\n{resume_log.read_text(encoding='utf-8')}")
                assert code == 0, resume_log.read_text(encoding="utf-8")
        finally:
            release_initial.set()
            release_retry.set()

        assert writes == {"north": 1, "south": 1}
        assert requests == {"read": 3 if boundary == "written" else 2, "write": 2, "completion_tool": 2,
                            **({"closing_reply": 2} if not bounded else {}),
                            **({"read_existing": 1} if boundary == "written" else {})}
        assert [r["id"] for r in runs.nodes(con, run_id)] == [runs.root(con, run_id)["id"]]
        assert {r["id"] for r in runs.tasks(con, run_id)} == {north, south}
        if boundary in {"claim", "written"}:
            assert tasks.latest_payload(con, south, "reclaimed", generation=abandoned["generation"])
        after = _trace(python, env, project, run_id)
        _json(tmp_path / "after.json", after)
        assert after["run_id"] == run_id
        expected_reports = 6 if bounded and boundary != "written" else 8
        assert after["usage"]["reported"] == expected_reports
        assert after["usage"]["reported_tokens"]["total"] == expected_reports * 15
        if boundary in {"prepared", "submitted"}:
            assert before["usage"] == after["usage"]
        admitted = [row for row in after["events"] if row["event"] == "budget_admitted"]
        assert admitted and {row["budget_cap"] for row in admitted} == {500000}
        assert not [row for row in after["events"] if row["event"] == "budget_refused"]
        restarts = [(r["mode"], r["status"]) for r in after["events"] if r["event"] == "research_started"]
        assert restarts == [("diagnostic", "resumed")] * 2
        for side, cid in (("north", north), ("south", south)):
            generation = tasks.get(con, cid)["generation"]
            assert generation == accepted.get(cid, by_side[side]["generation"])
            evidence = Path(by_side[side]["output_dir"], "evidence.md")
            assert evidence.read_bytes() == b"rows: 2\ntotal cents: 2000\n"
            assert (project / "sources" / f"{side}.txt").read_bytes() == SOURCES[side]
            digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
            assert [r["sha256"] for r in runs.artifacts(con, run_id, task_id=cid)] == [digest]
            for event in ("artifact_written", "submitted", "research_v2_settled"):
                assert con.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind=?", (cid, event)).fetchone()[0] == 1
            trace_events = [r for r in after["events"] if r.get("card_id") == cid]
            assert any(r["event"] == "tool_receipt_saved" and r["mode"] == "diagnostic"
                       and r["generation"] == generation for r in trace_events)
            submitted, settled = [r for r in after["board_events"] if r["card_id"] == cid]
            assert submitted["event"] == "submitted" and submitted["artifact_hashes"] == [digest]
            assert settled["event"] == "research_v2_settled" and settled["submission_event_id"] == submitted["board_event_id"]
        assert [(r["card_id"], r["event"]) for r in before["board_events"]] == [
            (r["card_id"], "submitted") for r in after["board_events"]
            if r["event"] == "submitted" and r["card_id"] in accepted
        ]
        assert first_hash in next(r for r in before["board_events"] if r["card_id"] == north)["artifact_hashes"]


def _drive(run_id, gate=None, *, prepared=False):
    from misaka.__main__ import main
    from misaka.core.research import planner, workflow

    def finished_recovery(*_args, **_kwargs):
        # A later synthesis and its review are independent workloads. Stop at the real
        # boundary after both original cards have been accepted and settled.
        raise InterruptedError("Fixture reached synthesis")

    planner.synthesize = finished_recovery
    if prepared:
        from misaka.core.research import node

        node.CARD_ARGV = [sys.executable, "-m", "tests.test_research_process_recovery", "--park-card", str(gate)]
    elif gate is not None:
        settle = workflow.settle_done_tasks

        def stop_before_settle(con, *, run_id, **kwargs):
            linked = runs.tasks(con, run_id, kind="research")
            if len(linked) == 2 and all(card["status"] == "done" for card in linked):
                marker = gate.with_suffix(".pending")
                _json(marker, {"run_id": run_id, "pid": os.getpid(), "created": psutil.Process().create_time()})
                marker.replace(gate)
                threading.Event().wait()
            return settle(con, run_id=run_id, **kwargs)

        workflow.settle_done_tasks = stop_before_settle
    return main(["research", "--resume", run_id])


def _park_card(gate, args):
    from misaka.cli.research_node import main

    record = dispatch.record_terminal_submission

    def stop_after_preparing(con, row, submission):
        recorded = record(con, row, submission)
        if recorded and row["title"] == "south":
            marker = gate.with_suffix(".pending")
            _json(marker, {"card_id": row["id"], "pid": os.getpid(), "created": psutil.Process().create_time()})
            marker.replace(gate)
            threading.Event().wait()
        return recorded

    dispatch.record_terminal_submission = stop_after_preparing
    return main(["--run-card", *args])


if __name__ == "__main__":
    if sys.argv[1] == "--park-card":
        raise SystemExit(_park_card(Path(sys.argv[2]), sys.argv[3:]))
    raise SystemExit(_drive(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) >= 3 else None,
                            prepared="--prepared" in sys.argv[3:]))
