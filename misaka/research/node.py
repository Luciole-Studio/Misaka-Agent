"""Research processes: a node (``misaka research --node RUN NODE``), Last Order's fork on
one issue (``misaka research --probe RUN ISSUE``), and one card of theirs
(``python -m misaka.research.node --run-card TASK_ID``, this module's own child).

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
import sys
import time

from misaka.config import CFG, current_config
from misaka.platform import tasks as task_store
from misaka.research import runs


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

    def spawn(self, argv, *, cwd, title, place="split", new_session=False):
        # new_session makes the child the leader of a process group of its own, which is what
        # lets its claim name that group (see HeadlessRunner). setsid is POSIX-only.
        return subprocess.Popen(argv, cwd=cwd,
                                start_new_session=new_session and os.name == "posix")

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


CARD_ARGV = [sys.executable, "-m", "misaka.research.node", "--run-card"]   # + the card's id

# A settled card's child is not finished: acceptance commits first, and only then does the child
# commit the card's line and index its artifacts, which budgets up to 300s for a single PDF.
# Nothing re-runs that tail, so close() waits this long for it before it starts signalling.
SETTLED_TAIL_SECONDS = 300


class HeadlessRunner:
    """No panel: every ready card becomes a child process of its own, so a node's batch runs at
    the width the run asked for and the drive loop keeps watching stop and budget while the
    cards run. Running them inline made ``research_parallel`` a number with no effect: a batch
    was one thread running one card to the end of its turn before starting the next.

    Threads are not the alternative -- ``platform.session`` serialises sessions inside one
    process on purpose -- so the parallelism is processes. Each child settles its own card
    through ``dispatch.run_task`` (claim, admission, budget, settle); this side only starts
    one, reaps it, and can kill it.

    A child leads a process group of its own, so the claim it takes can name that group: a node
    that dies without running its ``close`` leaves children holding claims, and the reconciler
    fences the group -- the model session included -- before the card goes to a new owner."""

    def __init__(self, con, cfg):
        self.con, self.cfg = con, cfg
        self.flying = {}                     # task_id -> Popen: the cards this node started
        self._processes = ProcessSpawner()   # one place decides how a child of ours starts and dies

    async def launch_ready(self, *, task_ids, **_kwargs):
        from misaka.network import dispatch
        from misaka.platform import admission
        self._reap()                         # before reconcile: an unwaited-for child still answers to its pid
        # A card whose child died mid-turn comes back through the reconciler, exactly as it did
        # when a node ran its cards inline in one dispatch pass. finish_abandoned can reach git,
        # so it stays off the loop thread.
        await asyncio.to_thread(dispatch.reconcile, self.con, self.cfg)
        rows = {task_id: task_store.get(self.con, task_id) for task_id in task_ids}
        candidates = [row for task_id, row in rows.items()
                      if row is not None and task_id not in self.flying and row["status"] == "ready"]
        # The same pass also carried the project's validity gate: a card whose file is missing or
        # unreadable, whose needs list is malformed, or that blocks itself is not dispatchable, and
        # spawning it is a fresh interpreter every poll for a card no claim will ever take.
        # fair_ready keeps exactly those cards. It reads every card file of the project to do it,
        # so it goes off the loop thread like the reconcile above, and only when there is a card
        # to start -- most polls of a running batch have none. Its round-robin cursor belongs to
        # the schedulers that use its order; we use only its set.
        dispatchable = set()
        for space in {task_store.workspace_for(row) for row in candidates}:
            dispatchable.update(row["id"] for row in await asyncio.to_thread(
                task_store.fair_ready, self.con, lane="workers", advance=False, workspace=space))
        host_cap, assignee_cap = admission.limits()
        taken = self._slots_taken(rows)
        for task_id, row in rows.items():
            if task_id in self.flying or row is None or row["status"] != "ready":
                continue
            if task_id not in dispatchable:
                continue                     # the card is not dispatchable: no process, this poll or any
            if row["next_attempt_at"] is not None and int(row["next_attempt_at"]) > time.time():
                continue                     # a rate-limit cooldown: the claim would refuse it anyway
            if sum(taken.values()) >= host_cap or taken.get(row["assignee"], 0) >= assignee_cap:
                continue                     # no admission slot: the card stays ready for a later poll
            # No cwd of our own: the child inherits the run's, and run_task is what decides
            # what a card whose project folder is gone means. The parallelism ceiling is the
            # drive loop's free-slot count, already applied to task_ids -- not ours to re-decide.
            self.flying[task_id] = self._processes.spawn(
                [*CARD_ARGV, task_id], cwd=None, title=f"card {task_id}", new_session=True)
            taken[row["assignee"]] = taken.get(row["assignee"], 0) + 1

    async def stop(self, task_id, **_kwargs):
        """A halt: this card's process and everything it started go now. The claim it dies
        holding is left to ``dispatch.reconcile`` -- the one path that knows how to settle or
        return a card whose worker is gone."""
        proc = self.flying.pop(task_id, None)
        if proc is not None:
            await asyncio.to_thread(self._processes.stop, proc)

    def close(self):
        """The node's routine is over: no card of this node may outlive the node. Called from
        the process shell below, so a halt, a failure and a clean finish all end the same way.

        A child whose card has already left the board's active states settled it and is running
        the tail that follows acceptance -- the commit on the card's line, then the artifacts
        joining the corpus. Nothing re-runs that tail, so it is waited for rather than signalled.
        Every other child is killed outright: still ``running`` means the turn is in flight (and
        the card comes back through the reconciler), still ``ready`` or ``todo`` means the child
        has not claimed it yet and has a whole turn's spending ahead of it -- waiting there would
        let a halted node run the very card the halt was meant to stop."""
        self._reap()                         # an exited child needs neither wait nor signal
        for task_id, proc in list(self.flying.items()):
            row = task_store.get(self.con, task_id)
            if row is not None and row["status"] not in ("ready", "todo", "running"):
                try:
                    proc.wait(SETTLED_TAIL_SECONDS)
                except subprocess.TimeoutExpired:
                    pass                     # the tail is not finishing: stop the tree below
            self.flying.pop(task_id, None)
            self._processes.stop(proc)

    def _slots_taken(self, rows):
        """The admission slots a new child would have to fit into: every claimed card on the
        board (what ``db.claim`` counts) plus the children of ours that have not claimed theirs
        yet. The claim stays the authority -- counting here only keeps this node from starting a
        process per poll for a card the host has no room for. Inline, a refused claim cost
        nothing; a refused claim now costs a process, and the drive loop offers the same card
        again two seconds later."""
        taken = {}
        for row in self.con.execute(
                "SELECT assignee FROM tasks WHERE status='running' AND claim_lock IS NOT NULL"):
            taken[row["assignee"]] = taken.get(row["assignee"], 0) + 1
        for task_id in self.flying:
            row = rows[task_id] if task_id in rows else task_store.get(self.con, task_id)
            if row is not None and row["status"] == "ready":     # started, about to claim
                taken[row["assignee"]] = taken.get(row["assignee"], 0) + 1
        return taken

    def _reap(self):
        for task_id, proc in list(self.flying.items()):
            if proc.poll() is not None:      # poll() reaps: the pid is free before reconcile looks
                self.flying.pop(task_id)


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
    finally:
        # However this process ends, the card processes it started end with it. A pane
        # runner has nothing of its own to close: the daemon owns those panes.
        close = getattr(runner, "close", None)
        if close is not None:
            close()
    if isinstance(result, dict):
        questions = "; ".join(result.get("questions") or [])
        print(f"{label} needs input: {questions}", flush=True)
        report("blocked", questions)
        return 2
    print(f"{label}: {result}", flush=True)
    report("idle", f"{label}: {result}")
    return 3 if result in ("stopped", "budget") else 0


def main_card(task_id):
    """Run one ready card in this process: the entry point ``HeadlessRunner`` spawns. Claim,
    admission, budget and settle are ``dispatch.run_task``'s and are not repeated here, so a
    card started this way is the same card the daemon or a pane would have run. Exit 0 when
    this process ran the card, 1 when it did not (no such card, someone else holds the claim,
    the budget stopped, the assignee has no profile)."""
    from misaka.network import dispatch
    con = task_store.connect(os.path.expanduser(CFG["db"]))
    try:
        row = task_store.get(con, task_id)
        if row is None:
            print(f"card {task_id}: not on the board", flush=True)
            return 1
        return 0 if dispatch.run_task(con, row, current_config()) else 1
    finally:
        con.close()


def main(run_id, node_id):
    # Imported here, not at the top: a card child runs this module too, and the research
    # workflow is a second of imports it has no use for.
    from misaka.research import workflow
    return _run(f"node {node_id}", lambda con, cfg, runner, worker, progress: workflow.expand_node(
        con, cfg, runner, worker, run_id=run_id, node_id=node_id, spawner=spawner(), progress=progress))


def main_probe(run_id, issue_id):
    from misaka.research import workflow
    return _run(f"fork {issue_id}", lambda con, cfg, runner, worker, progress: workflow.probe(
        con, cfg, runner, worker, run_id=run_id, issue_id=issue_id, progress=progress))


if __name__ == "__main__":
    # ``python -m misaka.research.node --run-card TASK_ID``. A node and a fork are user-facing
    # and keep their CLI sub-command; a card child is this module's own and needs no CLI surface.
    if sys.argv[1:2] != ["--run-card"] or len(sys.argv) != 3:
        sys.exit("usage: python -m misaka.research.node --run-card TASK_ID")
    sys.exit(main_card(sys.argv[2]))
