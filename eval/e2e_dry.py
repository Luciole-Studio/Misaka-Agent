"""End-to-end dry run: no LLM calls. A fake worker walks the full chain
(create card -> claim -> submit -> red-team review -> reject -> pass -> hooks -> budget),
then the Research Workflow runs one complete cycle (plan -> preflight -> tasks -> synthesis ->
red team -> branch -> audit -> final report).

Every seam of the real pipeline is exercised; only the LLM slot is replaced with a scripted stub.
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from misaka.platform import tasks as db  # noqa: E402
from misaka.network import dispatch  # noqa: E402


class FakeWorker:
    """Scripted worker: writes artifacts and returns verdicts per the script, replacing every LLM call."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.judge_calls = 0
        self.card_runs = 0

    @property
    def MAX_REPORT_BYTES(self):
        from misaka.network import worker as real
        return real.MAX_REPORT_BYTES

    def check_report(self, *args, **kwargs):
        from misaka.network import worker as real
        return real.check_report(*args, **kwargs)

    def run_card(self, task, workspace, profile_dir, provider, default_model, on_event,
                 **_kwargs):
        self.card_runs += 1
        os.makedirs(workspace, exist_ok=True)
        on_event(json.dumps({"type": "agent_end", "messages": [
            {"role": "assistant", "usage": {"totalTokens": 1000}}]}))
        # Round one deliberately omits the footer line (forcing a rejection); round two adds it.
        body = "Conclusion 1\nConclusion 2\n" + ("## Verified\n" if self.card_runs > 1 else "")
        with open(os.path.join(workspace, "out.md"), "w", encoding="utf-8") as f:
            f.write(body)
        report = {"schema_version": 1, "status": "done", "summary": "Dry-run artifact",
                  "artifacts": ["out.md"], "uncertain": ["This is a dry run; there is no real evidence."], "notes": ""}
        state = db.task_state_dir(task["id"])
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)
        from misaka.network import worker as real
        ok, result = real.check_report(workspace, task_id=task["id"])
        return ({"ok": True, "report": result, "exit_code": 0, "timed_out": False} if ok
                else {"ok": False, "reason": result, "exit_code": 1, "timed_out": False})

    def run_llm_json(self, profile_dir, prompt, provider, default_model,
                     cwd=None, tools=None, timeout=600, model=None, **_kwargs):
        role = os.path.basename(profile_dir)
        if role == "redteam":
            self.judge_calls += 1
            has_footer = "## Verified" in open(os.path.join(cwd, "out.md"), encoding="utf-8").read()
            if has_footer:
                return {"pass": True, "reasons": ["All criteria met"], "must_fix": []}, "", None
            return {"pass": False, "reasons": ["Missing footer line"], "must_fix": ["Append a final line '## Verified'"]}, "", None
        return None, "", "unexpected role " + role


def main():
    from misaka.config import CFG
    tmp = tempfile.mkdtemp(prefix="misaka-e2e-")
    old_tasks_root = CFG["tasks_root"]
    CFG["tasks_root"] = os.path.join(tmp, "task-state")
    db_path = os.path.join(tmp, "board.db")
    con = db.connect(db_path)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    profiles = os.path.join(tmp, "profiles")   # the dry run brings its own role directories; never touches ~/.misaka
    for d in ("sisters/10032", "redteam", "last_order"):
        os.makedirs(os.path.join(profiles, d))
    cfg = {"db": db_path,
           "profiles_root": os.path.join(profiles, "sisters"),
           "roles_root": profiles,
           "hooks_dir": os.path.join(repo, "hooks"),
           "workspaces_root": os.path.join(tmp, "ws"),
           "provider": "x", "default_model": "y",
           "judge_timeout": 5, "token_cap": 0}

    fake = FakeWorker(tmp)
    real_worker = dispatch.worker
    dispatch.worker = fake
    try:
        body = "## goal\nDry run\n## boundaries\nnone\n## acceptance criteria\n- out.md exists\n- its last line is '## Verified'"
        tid = db.create_task(con, "Dry-run card", body=body, assignee="10032",
                             workspace=os.path.join(tmp, "workspace"))

        n1 = dispatch.dispatch_once(con, cfg)          # run + review: should be rejected
        assert n1 >= 1, n1
        t = db.get(con, tid)
        assert t["status"] == "ready" and t["verify_rounds"] == 1, dict(t)
        assert db.latest_payload(con, tid, "verify_fail"), "a rejection must record must_fix"

        dispatch.dispatch_once(con, cfg)                # round two fixes it: should pass
        t = db.get(con, tid)
        assert t["status"] == "done", dict(t)
        assert fake.card_runs == 2 and fake.judge_calls == 2, (fake.card_runs, fake.judge_calls)

        # Hook gate is wired into the chain: an empty artifact must be rejected
        open(os.path.join(t["workspace"], "out.md"), "w").close()
        assert dispatch.run_hooks(cfg, t, t["workspace"]), "the hook should reject an empty artifact"

        # Budget accounting: usage reported by the fake worker is read from the event stream
        from misaka.platform import budget
        assert budget.spent(con) == 2000, budget.spent(con)
        assert budget.status(con, 1000)["mode"] == "stop"

        # blocked path: an honest "stuck" is not a failure; a blocked event is recorded, no failed event
        tid2 = db.create_task(con, "blocked path", body="## goal\nx\n## boundaries\ny\n## acceptance criteria\n- out.md",
                              assignee="10032", workspace=os.path.join(tmp, "blocked-workspace"))
        real_run_card = fake.run_card

        def blocked_run_card(task, workspace, *a, **k):
            os.makedirs(workspace, exist_ok=True)
            state = db.task_state_dir(task["id"])
            os.makedirs(state, exist_ok=True)
            with open(os.path.join(state, "report.json"), "w", encoding="utf-8") as f:
                json.dump({"schema_version": 1, "status": "blocked", "summary": "Waiting for input",
                           "artifacts": [], "uncertain": ["-"], "notes": "The original archive is missing and cannot be obtained."}, f)
            from misaka.network import worker as real
            ok, why = real.check_report(workspace, task_id=task["id"])
            return {"ok": ok, "reason": why, "exit_code": 0, "timed_out": False}

        fake.run_card = blocked_run_card
        dispatch.dispatch_once(con, cfg)
        fake.run_card = real_run_card
        t2 = db.get(con, tid2)
        assert t2["status"] == "blocked", dict(t2)
        assert db.latest_payload(con, tid2, "blocked"), "expected a blocked event"
        assert not db.latest_payload(con, tid2, "failed"), "there must be no failed event"
    finally:
        dispatch.worker = real_worker
        CFG["tasks_root"] = old_tasks_root
    print(f"e2e ok — create -> run -> reject -> fix -> pass -> hooks -> budget, full chain "
          f"({fake.card_runs} runs / {fake.judge_calls} verdicts)")


class ResearchFake:
    """Scripted LLM for the Research Workflow: plan -> method review -> preflight -> one red-team
    issue (spawning one branch) -> no further issues -> audit -> final report."""

    def __init__(self):
        self.global_critiques = 0

    def run_llm_json(self, _profile, prompt, _provider, _model, **kwargs):
        # Prompts are recognized by the role named in their opening sentence (case-insensitive).
        lowered = prompt.lower()
        if kwargs.get("raw"):
            return None, ("# Final report\n\nThe report answers the original question directly and separates facts, causal interpretations, and normative judgements. "
                          "# Competing conclusions\nThe report preserves competing interpretations, their premises, evidence limits, and possible counterevidence rather than forcing a majority decision. "
                          "The independence of existing sources, the scope of application and missing material have been clearly identified and subsequent evidence may alter judgement."), None
        if "methodology reviewer" in lowered:
            return {"approved": True, "review_markdown": "# Methodology review\n\nPass.",
                    "material_omissions": [], "required_revisions": [], "method_probes": []}, "", None
        if "you are a sister" in lowered and "research assignment" in lowered:
            return {"preflight_markdown": "# Preflight\n\n- Check the primary records first, then look for independent sources and counterevidence.",
                    "sources": ["primary records"], "queries": ["primary records of the object"], "tools": ["read"],
                    "failure_modes": ["single-source evidence"], "falsifiers": ["primary records saying the opposite"],
                    "stop_condition": "no further material can be found"}, "", None
        if "the planner for research mode" in lowered:
            branch = "targeted extension branch" in lowered
            return {"status": "ready", "clarifying_questions": [],
                    "plan_markdown": "# Research plan\n\nBreaks the question into premises, objects, processes, interactions, methods, sources, consequences, competing interpretations, and falsifiers.",
                    "methods": [{"name": "source criticism", "why": "primary records", "blind_spots": "coverage gaps"}],
                    "tasks": [{"local_id": "branch-source" if branch else "root-source",
                               "title": "Verify a second source" if branch else "Verify the primary records",
                               "question": "What do the primary records actually say?", "rationale": "Establish the facts.",
                               "method": "source criticism", "source_strategy": "primary and independent records",
                               "falsifiers": "primary records saying the opposite", "deliverable": "result.md",
                               "dependencies": [], "capabilities": ["archive"], "assignee": "10032",
                               "assignee_reason": "archive skills", "priority": 9}],
                    "synthesis_team": [{"assignee": "10032", "lens": "facts"},
                                       {"assignee": "10033", "lens": "causes and consequences"}],
                    "extensions": {}}, "", None
        if "red-team critic" in lowered:
            self.global_critiques += 1
            issues = ([{"kind": "fact", "question": "Does a second independent source exist?",
                        "rationale": "Possibly single-source evidence.", "priority": 10, "material": True,
                        "branch_id": None}] if self.global_critiques == 1 else [])
            return {"review_markdown": "# Red-team review\n\nReviewed facts, logic, method, and scope.",
                    "issues": issues, "disagreements": [],
                    "evidence_assessments": [{"finding_id": "", "claim": "key fact",
                        "source_independence": "to be verified", "proximity": "primary records",
                        "method_fit": "fits", "counterevidence": "none seen",
                        "scope": "this case", "judgement": "conditionally accepted"}],
                    "stop_recommendation": {"stop": not issues, "reason": "no new gaps"}}, "", None
        if "final-report auditor" in lowered:
            return {"approved": True, "audit_markdown": "# Audit\n\nFaithful to the evidence.",
                    "material_errors": [], "unresolved_ok": []}, "", None
        return None, "", "unexpected research prompt: " + prompt[:80]


class FakeRunner:
    def __init__(self, con, root):
        self.con, self.root = con, root

    async def launch_ready(self, *, task_ids=None, **_kwargs):
        from misaka.research.report import SYNTHESIS_PREFIX
        for task_id in task_ids or []:
            task = db.get(self.con, task_id)
            if not task or task["status"] not in {"ready", "running", "verifying", "finalizing"}:
                continue
            workspace = task["workspace"]
            output = task["output_dir"] or workspace
            os.makedirs(output, exist_ok=True)
            name = "synthesis.md" if task["title"].startswith(SYNTHESIS_PREFIX) else "result.md"
            artifact = os.path.join(output, name)
            with open(artifact, "w", encoding="utf-8") as f:
                f.write("# Artifact\n\nA verifiable fact.\n\n## Competing interpretations\nAn alternative explanation remains open.\n")
            state = db.task_state_dir(task_id)
            os.makedirs(state, exist_ok=True)
            with open(os.path.join(state, "report.json"), "w", encoding="utf-8") as f:
                findings = ([] if name == "synthesis.md" else [{
                    "text": "The primary record contains a verifiable fact.",
                    "claim_type": "fact", "source_file": os.path.relpath(artifact, workspace),
                    "quote": "A verifiable fact"}])
                json.dump({"schema_version": 1, "status": "done", "summary": "Done",
                           "artifacts": [os.path.relpath(artifact, workspace)],
                           "uncertain": [], "findings": findings,
                           "notes": ""}, f,
                          ensure_ascii=False)
            self.con.execute("UPDATE tasks SET status='done',completed_at=1 WHERE id=?", (task_id,))
        return []


def research_dry():
    from misaka.config import CFG
    from misaka.platform import projects
    from misaka.research import runs, workflow

    tmp = tempfile.mkdtemp(prefix="misaka-research-v2-")
    old_tasks_root = CFG["tasks_root"]
    CFG["tasks_root"] = os.path.join(tmp, "task-state")
    db_path = os.path.join(tmp, "state.db")
    con = db.connect(db_path)
    profiles = os.path.join(tmp, "profiles")
    for name in ("sisters/10032", "sisters/10033", "redteam", "last_order"):
        os.makedirs(os.path.join(profiles, name), exist_ok=True)
    for sid in ("10032", "10033"):
        with open(os.path.join(profiles, "sisters", sid, "DESCRIBE.md"), "w", encoding="utf-8") as f:
            f.write(f"---\ndescription: Sister {sid}, researcher\n---\n")
    cfg = {"db": db_path, "profiles_root": os.path.join(profiles, "sisters"),
           "roles_root": profiles, "hooks_dir": os.path.join(tmp, "hooks"),
           "workspaces_root": os.path.join(tmp, "ws"), "provider": "x",
           "default_model": "y", "judge_timeout": 5, "token_cap": 0}
    try:
        project = projects.create(con, tmp, "Research Workflow demo")
        run = runs.create(con, project_id=project["id"], question="Why did this happen?",
                          limits={"max_depth": 2})
        fake = ResearchFake()
        out = asyncio.run(workflow.run(
            con, cfg, FakeRunner(con, cfg["workspaces_root"]), fake,
            run_id=run["id"], poll_seconds=0))
        assert out["reason"] == "done", out
        assert len(runs.branches(con, run["id"])) == 1
        assert len(runs.assessments(con, run["id"])) == 2
        assert con.execute("SELECT COUNT(*) FROM research_findings WHERE run_id=?",
                           (run["id"],)).fetchone()[0] == 2
        assert os.path.isfile(out["final"]["path"])
        assert all(row["preflight_artifact"] for row in runs.tasks(con, run["id"]))
        print("research dry ok — project / Last Order plan / preflight / synthesis / red team / evidence assessment / final audit, full chain")
    finally:
        CFG["tasks_root"] = old_tasks_root


if __name__ == "__main__":
    main()
    research_dry()
