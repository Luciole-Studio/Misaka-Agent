"""Research processes: a node (``misaka research --node RUN NODE``) and Last Order's fork on
one issue (``misaka research --probe RUN ISSUE``).

Inside the panel a node is a pane split beside the Last Order that started the run, a fork is
a pane split beside its node (the fork rule: one line of context, one tab), and every Sister
card either opens is a card pane in a tab of its own. Headless they are plain subprocesses and
the cards run through dispatch. Exit codes: 0 done, 2 waiting for the user, 3 the run halted
(stop or budget), 1 an error (the run stays resumable).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

from misaka.config import CFG, current_config
from misaka.platform import tasks as task_store
from misaka.research import runs, workflow


class PaneSpawner:
    """The panel: ``place`` is "split" (beside this pane) or "tab" (a tab in this pane's space)."""

    def __init__(self, pane):
        self.pane = pane

    def spawn(self, argv, *, cwd, title, place="split"):
        from misaka.ui.panel import client as net
        return net.request("pane.create", {
            "argv": argv, "cwd": cwd, "title": title, "place": {place: self.pane},
            "env": {"MISAKA_THEME": os.environ.get("MISAKA_THEME", "dark")}})["pane_id"]

    def alive(self, pane_id):
        from misaka.ui.panel import client as net
        row = next((p for p in net.request("panes.list")["panes"] if p["id"] == pane_id), None)
        return bool(row and row["alive"])

    def stop(self, pane_id):
        from misaka.ui.panel import client as net
        net.request("pane.close", {"id": pane_id})
        for _ in range(25):                          # the daemon escalates SIGTERM -> SIGKILL itself; wait for it
            if not self.alive(pane_id):
                return
            time.sleep(0.2)


class ProcessSpawner:
    """No panel: a child process whose output goes to the terminal."""

    def spawn(self, argv, *, cwd, title, place="split"):
        return subprocess.Popen(argv, cwd=cwd)

    def alive(self, proc):
        return proc.poll() is None

    def stop(self, proc):
        if proc.poll() is not None:
            return
        from misaka.platform import processes
        processes.terminate(proc.pid)      # the whole tree: a node's cards and LLM children must not outlive it
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def spawner():
    pane = os.environ.get("MISAKA_NET_PANE")
    return PaneSpawner(pane) if pane else ProcessSpawner()


class PaneRunner:
    """Inside a node or fork pane: ready cards become card panes (a tab each, named after the
    owner); a stop closes the card's pane."""

    def __init__(self, con, cfg, label, pane):
        self.con, self.cfg, self.label, self.pane = con, cfg, label, pane

    async def launch_ready(self, *, task_ids, **_kwargs):
        from misaka.ui.panel import client as net
        for tid in task_ids:
            row = task_store.get(self.con, tid)
            if row is None:
                continue
            if row["status"] == "ready":
                try:
                    await asyncio.to_thread(net.request, "pane.run_card", {
                        "task_id": tid,
                        "place": {"tab": self.pane, "name": f"{self.label}·{row['assignee']}·{tid}"}})
                except (RuntimeError, ConnectionError) as error:
                    print(f"card {tid}: {error}", flush=True)

    async def stop(self, task_id, **_kwargs):
        from misaka.ui.panel import client as net
        try:
            await asyncio.to_thread(net.request, "card.stop", {"task_id": task_id})
        except (RuntimeError, ConnectionError):
            pass


class HeadlessRunner:
    """No panel: dispatch runs a ready card inline; its submission is its acceptance."""

    def __init__(self, con, cfg):
        self.con, self.cfg = con, cfg

    async def launch_ready(self, *, task_ids, **_kwargs):
        from misaka.network import dispatch
        await asyncio.to_thread(dispatch.dispatch_once, self.con, self.cfg, task_ids=task_ids)

    async def stop(self, task_id, **_kwargs):
        """Inline dispatch runs a card to the end of its turn in this very process; it cannot be
        killed from here. The drive loop already holds back ready/todo cards on a halt -- this
        exists so a halt is an explicit no-op instead of a silently missing method."""


class Reporter:
    """The process's own word on its pane's dot (pane.report_state): working / blocked / idle."""

    def __init__(self):
        self.pane, self.seq = os.environ.get("MISAKA_NET_PANE"), 0

    def __call__(self, state, message=""):
        if not self.pane:
            return
        from misaka.ui.panel import client as net
        self.seq += 1
        try:
            net.request("pane.report_state", {"id": self.pane, "state": state, "message": message[:240],
                                              "seq": self.seq})
        except (RuntimeError, ConnectionError):
            pass


def _run(label, routine):
    """Common shell of both processes: connect, report, run the coroutine, map its result to an exit code."""
    from misaka.network import worker
    con = task_store.connect(os.path.expanduser(CFG["db"]))
    runs.init(con)
    cfg = current_config()
    report = Reporter()
    runner = PaneRunner(con, cfg, label, report.pane) if report.pane else HeadlessRunner(con, cfg)

    def progress(event):
        print(event["message"], flush=True)
        for item in event.get("tasks") or []:
            print(f"  - {item['title']} → Sister {item['assignee']}", flush=True)
        report("working", event["message"])

    report("working", f"{label} starting")
    try:
        result = asyncio.run(routine(con, cfg, runner, worker, progress))
    except Exception as error:  # noqa: BLE001 - the pane shows why; the run stays resumable
        print(f"{label} failed: {type(error).__name__}: {error}", flush=True)
        report("blocked", f"{type(error).__name__}: {error}")
        return 1
    if isinstance(result, dict):
        questions = "; ".join(result.get("questions") or [])
        print(f"{label} needs input: {questions}", flush=True)
        report("blocked", questions)
        return 2
    print(f"{label}: {result}", flush=True)
    report("idle", f"{label}: {result}")
    return 3 if result in ("stopped", "budget") else 0


def main(run_id, node_id):
    return _run(f"node {node_id}", lambda con, cfg, runner, worker, progress: workflow.expand_node(
        con, cfg, runner, worker, run_id=run_id, node_id=node_id, spawner=spawner(), progress=progress))


def main_probe(run_id, issue_id):
    return _run(f"fork {issue_id}", lambda con, cfg, runner, worker, progress: workflow.probe(
        con, cfg, runner, worker, run_id=run_id, issue_id=issue_id, progress=progress))
