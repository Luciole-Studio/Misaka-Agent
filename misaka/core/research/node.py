"""Research node and Sister card processes.

Every fork LO is a formal child node (``misaka research --node RUN NODE``), with its
own session, depth and Sisters. In the panel the node process is a pane of its own: a
fork Last Order runs as an interactive window in a new tab beside her parent, the
routine driven from inside that window (``wiring.node.NodePart``), her Sisters gridded
into her tab. Without a panel (a command-line run) nodes run headless in the background
and Sessions opens their conversations read-only or attached. Exit codes of a headless
node: 0 done, 2 waiting for input, 3 stopped or budget exhausted, 1 error (the run
stays resumable); an interactive node returns its window's exit code.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import traceback

from misaka.config import CFG, current_config
from misaka.core.platform import processes
from misaka.core.platform import tasks as task_store
from misaka.core.research import runs


class ProcessSpawner:
    """Managed research children, independent of visible panes and their terminals."""

    def spawn(self, argv, *, cwd, new_session=False):
        env = os.environ.copy()
        # A child of a pane is not the pane: neither it nor its model sessions may report as
        # the foreground LO, and its output must not land on the pane's screen.
        quiet = env.pop("MISAKA_NET_PANE", None) is not None
        return subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
            # Nodes stay in the foreground owner's group so closing its panel also
            # stops them. Headless cards need their own group for claim reconciliation.
            start_new_session=new_session and os.name == "posix")

    def alive(self, proc):
        return proc.poll() is None

    def stop(self, proc):
        if proc.poll() is not None:
            return
        processes.terminate(proc.pid)      # the whole tree: a node's cards and LLM children must not outlive it
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def _node_title(argv):
    """The tab's name: the node named after ``--node RUN NODE`` in a node's own argv."""
    argv = list(argv)
    if "--node" in argv and argv.index("--node") + 2 < len(argv):
        return f"Node {argv[argv.index('--node') + 2]}"
    return " ".join(argv[-2:])


class PaneHandle:
    """A research child the panel runs in a pane of its own: the pane to close, the process to
    watch. Identity is captured at spawn so a recycled pid is never mistaken for the node."""
    __slots__ = ("identity", "pane_id", "pid")

    def __init__(self, pane_id, pid, identity):
        self.pane_id, self.pid, self.identity = pane_id, pid, identity


class PaneSpawner:
    """Research children as panes of the panel. The node process runs in a new tab beside its
    parent's pane, with a terminal of its own, so a fork Last Order is the window the user
    sees -- not a background process behind a viewer. The daemon hands the child its own pane
    id (``MISAKA_NET_PANE``), which is how the node knows to run interactively and where to
    grid its Sisters; the parent's ``MISAKA_*`` settings travel along, its pane id does not."""

    def __init__(self, parent_pane=None):
        self.parent = parent_pane or os.environ.get("MISAKA_NET_PANE")

    def spawn(self, argv, *, cwd, new_session=False):
        from misaka.ui.panel import client as net
        env = {key: value for key, value in os.environ.items()
               if key.startswith("MISAKA_") and key != "MISAKA_NET_PANE"}
        out = net.request("pane.create", {
            "argv": list(argv), "cwd": cwd, "title": _node_title(argv),
            "env": env, "place": {"tab": self.parent}})
        pid = int(out["pid"])
        return PaneHandle(out["pane_id"], pid, processes.identity(pid))

    def alive(self, handle):
        return processes.identity_is_alive(handle.pid, handle.identity)

    def stop(self, handle):
        """Close the node's pane, which ends its process tree; if the panel cannot be reached,
        end the process directly. Either way wait for it to be gone."""
        from misaka.ui.panel import client as net
        if not self.alive(handle):
            return
        try:
            net.request("pane.close", {"id": handle.pane_id})
        except (RuntimeError, OSError):
            processes.terminate(handle.pid)
        deadline = time.monotonic() + 10
        while self.alive(handle) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.alive(handle):
            processes.terminate(handle.pid)


class PaneRunner:
    """Ready cards become Sister panes gridded into their Last Order's own tab: a tab holds one
    Last Order and the Sisters she summoned. The Last Order's pane is the home -- the root's
    foreground window, or a fork node's own window (``run_interactive``); nothing is opened
    on the node's behalf and nothing is gridded into a parent's tab."""

    def __init__(self, con, cfg, label, pane):
        self.con, self.cfg, self.label = con, cfg, label
        self.home = pane
        self._said = {}       # card id / "status" -> the last refusal printed; a 2-second poll must not repeat it

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
                        "place": {"grid": self.home}})
                    self._said.pop(tid, None)
                except (RuntimeError, OSError) as error:
                    if self._said.get(tid) != str(error):
                        self._said[tid] = str(error)
                        print(f"card {tid}: {error}", flush=True)

    async def stop(self, task_id, **_kwargs):
        from misaka.ui.panel import client as net
        try:
            await asyncio.to_thread(net.request, "card.stop", {"task_id": task_id})
        except (RuntimeError, OSError):
            pass

    async def pending(self, task_ids):
        """A card's TUI stays open after completion; wait for its settled idle report."""
        from misaka.ui.panel import client as net
        try:
            panes = (await asyncio.to_thread(net.request, "panes.status"))["panes"]
            self._said.pop("status", None)
        except OSError as error:
            if self._said.get("status") != str(error):
                self._said["status"] = str(error)
                print(f"Panel status unavailable; waiting to retry: {error}", flush=True)
            # Unknown is not settled/dead. The drive loop still checks stop and budget.
            return set(task_ids)
        return {pane["card"] for pane in panes
                if pane.get("card") in task_ids and pane.get("alive")
                and (pane.get("reported") or {}).get("state") != "idle"}


CARD_ARGV = [sys.executable, "-m", "misaka.cli.research_node", "--run-card"]   # + the card's id

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
        from misaka.core.network import dispatch
        from misaka.core.platform import admission
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
                [*CARD_ARGV, task_id], cwd=None, new_session=True)
            taken[row["assignee"]] = taken.get(row["assignee"], 0) + 1

    async def stop(self, task_id, **_kwargs):
        """A halt: this card's process and everything it started go now. The claim it dies
        holding is left to ``dispatch.reconcile`` -- the one path that knows how to settle or
        return a card whose worker is gone."""
        proc = self.flying.pop(task_id, None)
        if proc is not None:
            await asyncio.to_thread(self._processes.stop, proc)

    async def pending(self, task_ids):
        """Database acceptance precedes Git/PageIndex; retain and reap the whole worker."""
        self._reap()
        return set(task_ids).intersection(self.flying)

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
                proc.wait()                  # active work has no elapsed-time kill switch
            self.flying.pop(task_id, None)
            self._processes.stop(proc)

    def _slots_taken(self, rows):
        """The admission slots a new child would have to fit into: every claimed card on the
        board (what ``db.claim`` counts) plus our unclaimed children and accepted-card tails.
        The claim stays the authority -- counting here only keeps this node from starting a
        process per poll for a card the host has no room for. Inline, a refused claim cost
        nothing; a refused claim now costs a process, and the drive loop offers the same card
        again two seconds later."""
        from misaka.core.platform import admission
        occupied = admission.occupied(self.con)
        for task_id in self.flying:
            row = rows[task_id] if task_id in rows else task_store.get(self.con, task_id)
            if row is not None:
                occupied[task_id] = row["assignee"]
        taken = {}
        for assignee in occupied.values():
            taken[assignee] = taken.get(assignee, 0) + 1
        return taken

    def _reap(self):
        for task_id, proc in list(self.flying.items()):
            if proc.poll() is not None:      # poll() reaps: the pid is free before reconcile looks
                self.flying.pop(task_id)


def _run(label, routine, *, row_id, runner_key):
    """Node process shell: connect, report, run the coroutine, map its result to an exit code."""
    from misaka.core.network import worker
    con = task_store.connect(os.path.expanduser(CFG["db"]))
    runner, failure = None, None

    def progress(event):
        from misaka.core.platform import notifications
        branch = runs.node(con, row_id)
        payload = {**event, "node_id": branch["id"], "depth": branch["depth"],
                   "issue_id": None}
        notifications.publish(con, "research", branch["run_id"], "progress", payload)
        print(event["message"], flush=True)
        for item in event.get("tasks") or []:
            print(f"  - {item['title']} → Sister {item['assignee']}", flush=True)

    def failed(error):
        traceback.print_exc()
        text = f"{type(error).__name__}: {error}"
        print(f"{label} failed: {text}", flush=True)
        try:
            con.execute('UPDATE research_branches SET last_error=COALESCE(last_error,?) WHERE id=? AND runner_key=?',
                        (text, row_id, runner_key))
        except Exception:  # noqa: BLE001 - keep the original traceback even when recording it also fails
            traceback.print_exc()

    try:
        runs.init(con)
        if not runs.claim_runner(con, "research_branches", row_id, runner_key):
            print(f"{label}: superseded execution", flush=True)
            return 1
        cfg = current_config()
        runner = HeadlessRunner(con, cfg)
        result = asyncio.run(routine(con, cfg, runner, worker, progress))
    except Exception as error:  # noqa: BLE001 - the persisted attempt names the actual cause
        failed(error)
        failure = error
    finally:
        close = getattr(runner, "close", None)
        try:
            if close is not None:
                close()
        except Exception as error:  # noqa: BLE001 - cleanup must not overwrite the original failure
            failed(error)
            failure = failure or error
        finally:
            con.close()
    if failure is not None:
        return 1
    if isinstance(result, dict):
        questions = "; ".join(result.get("questions") or [])
        print(f"{label} needs input: {questions}", flush=True)
        return 2
    print(f"{label}: {result}", flush=True)
    return 3 if result in ("stopped", "budget") else 0


def main_card(task_id):
    """Run one ready card in this process: the entry point ``HeadlessRunner`` spawns. Claim,
    admission, budget and settle are ``dispatch.run_task``'s and are not repeated here, so a
    card started this way is the same card the daemon or a pane would have run. Exit 0 when
    this process ran the card, 1 when it did not (no such card, someone else holds the claim,
    the budget stopped, the assignee has no profile)."""
    from misaka.core.network import dispatch
    con = task_store.connect(os.path.expanduser(CFG["db"]))
    try:
        row = task_store.get(con, task_id)
        if row is None:
            print(f"card {task_id}: not on the board", flush=True)
            return 1
        return 0 if dispatch.run_task(con, row, current_config()) else 1
    finally:
        con.close()


def main(run_id, node_id, *, runner_key):
    """``misaka research --node RUN NODE``. In a pane of the panel (the daemon hands the process
    its pane id and a terminal of its own) the node is an interactive window; anywhere else --
    a command-line run, a test, a redirected stdin -- it runs headless in the background."""
    if os.environ.get("MISAKA_NET_PANE") and sys.stdin.isatty():
        return run_interactive(run_id, node_id, runner_key=runner_key)
    return run_headless(run_id, node_id, runner_key=runner_key)


def run_interactive(run_id, node_id, *, runner_key):
    """The node's process is a pane of the panel: open its Last Order's conversation as the same
    interactive chat the root has (``misaka chat``'s Last Order assembly, this node's own session
    folder, its saved session when the run is resumed) and let ``wiring.node.NodePart`` run the
    routine inside it. What the user types there is a turn of that very Last Order -- the plan
    that waits for a go-ahead, a word mid-run, a question about her conclusion once the routine
    is over and the window stays open. The node row is claimed here and released to the parent
    driver by the part when the routine ends; a window closed before that ends the node."""
    from misaka.config import identity, profiles
    from misaka.core.research import planner
    con = task_store.connect(os.path.expanduser(CFG["db"]))
    try:
        runs.init(con)
        if not runs.claim_runner(con, "research_branches", node_id, runner_key):
            print(f"node {node_id}: superseded execution", flush=True)
            return 1
        run, node = runs.get(con, run_id), runs.node(con, node_id)
    finally:
        con.close()
    cfg = current_config()
    profile = os.path.join(cfg["roles_root"], "last_order")
    role = profiles.role_of(profile)
    flags = ["--provider", cfg["provider"], "--model", cfg["lo_model"], "--thinking", "high",
             "--append-system-prompt", profiles.shared_soul()]
    for section in identity.prompt_sections(profile, role):
        flags += ["--append-system-prompt", section]
    flags += ["--session-dir", planner._lo_session(run, node)]
    if node["session_file"] and os.path.exists(node["session_file"]):
        flags += ["--session", node["session_file"]]    # a resumed node goes on in its own conversation
    os.chdir(run["workspace"])
    os.environ.update({
        "MISAKA_RESEARCH_NODE": f"{run_id} {node_id} {runner_key}",
        "MISAKA_APP_TITLE": f"MISAKA · Last Order · node {node_id}",
        "MISAKA_TAGLINE": (f"Last Order of research node {node_id} (depth {node['depth']}, run {run_id}). "
                           "Her plan for this node waits for your go-ahead here: talk it over with her and she "
                           "starts it once you agree. Her Sisters open beside this window."),
        "MISAKA_WHO": "last-order",
        "MISAKA_MCP_ROLE": "last-order",
        "MISAKA_PROFILE_DIR": profile,
        "MISAKA_WORKSPACE": run["workspace"],
        "MISAKA_INPUT_HISTORY": os.path.expanduser("~/.misaka/input-history/last-order.json"),
        "MISAKA_CODING_AGENT": "true",
        # Her tools' spending counts against the run, as a card's counts against its card.
        "MISAKA_USAGE_DB": str(cfg["db"]), "MISAKA_USAGE_TASK_ID": run_id,
        "MISAKA_USAGE_GENERATION": "1", "MISAKA_USAGE_TOKEN_CAP": str(cfg.get("token_cap") or 0)})
    from misaka.core.wiring import SessionSpec, assemble
    # The root window's assembly, less the Last Order mailbox: what is addressed to Last Order
    # is the root's to read, and this node's cards report through the run itself.
    assembly = assemble(SessionSpec(
        profile_dir=profile, role=role, workspace=run["workspace"], kind="foreground",
        sender="last-order", mcp_role="last-order", receive_messages=False, research_context=True))
    from misaka.cli.engine import main as engine_main
    try:
        return asyncio.run(engine_main(flags, assembly.engine_options()))
    except Exception as error:  # noqa: BLE001 - a window that never opened is the node's failure to report
        traceback.print_exc()
        con = task_store.connect(os.path.expanduser(CFG["db"]))
        try:
            con.execute('UPDATE research_branches SET last_error=COALESCE(last_error,?) WHERE id=? AND runner_key=?',
                        (f"{type(error).__name__}: {error}", node_id, runner_key))
        finally:
            con.close()
        return 1


def run_headless(run_id, node_id, *, runner_key):
    # Imported here, not at the top: a card child runs this module too, and the research
    # workflow is a second of imports it has no use for.
    from misaka.core.research import workflow
    from misaka.core.research.window import node_session

    async def routine(con, cfg, runner, _worker, progress):
        async with node_session(con, cfg, runs.get(con, run_id), runs.node(con, node_id)) as owner:
            return await workflow.expand_node(
                con, cfg, runner, owner, run_id=run_id, node_id=node_id, progress=progress, session=owner.session)

    return _run(f"node {node_id}", routine, row_id=node_id, runner_key=runner_key)
