"""Interactive card session run inside a Misaka Network pane.

The daemon launches this in a pane and owns the claim; everything after that is
this process's job (phase 2, the card drives itself): the ``Supervisor`` thread
watches its own submission (commit; submission is acceptance), heartbeat, blocked
report, and timeout, and settles the card when the session exits. The daemon only hosts
the pane, and
an existing session is resumed when there is one.
"""
import asyncio
import os
import sys
import threading
import time

from misaka.config import CFG
from misaka.network import worker
from misaka.platform import tasks as db

ACTIVE_STATUSES = ("running", "review")


class Supervisor:
    """The card's own state-machine driver. Polls while the session runs: a valid report
    commits the worktree and submits (the session stays open so a person can continue);
    a blocked report parks the card; the deadline fails it and ends the process. ``stop``
    runs the exit reconciliation with the claim still held."""

    def __init__(self, db_path, task, run_dir, claim_lock, generation, *,
                 poll_seconds=5.0, exit_fn=None):
        self.db_path, self.task, self.run_dir = db_path, dict(task), run_dir
        self.lock, self.generation = claim_lock, int(generation)
        self.poll_seconds = poll_seconds
        self.exit_fn = exit_fn or (lambda code: os._exit(code))
        self.deadline = time.time() + int(self.task["timeout_seconds"])
        self.submitted = False
        self._stop = threading.Event()
        self._beat = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _submit(self, con, report):
        from misaka.network import dispatch
        dispatch.accept(con, self.task, report, generation=self.generation, claim_lock=self.lock,
                        workspace=self.run_dir)
        self.submitted = True

    def _step(self, con):
        tid = self.task["id"]
        row = db.get(con, tid)
        if row is None or row["claim_lock"] != self.lock                 or int(row["generation"]) != self.generation:
            return False                       # ownership changed: observe only, never touch state
        if time.time() - self._beat >= 60 and db.heartbeat(
                con, tid, self.lock, generation=self.generation,
                ttl_seconds=max(1800, int(self.task["timeout_seconds"]) + 60)):
            self._beat = time.time()
        ok, report = worker.check_report(self.run_dir, con=con, task_id=tid, generation=self.generation)
        if ok:
            self._submit(con, report)
        elif str(report).startswith("blocked:"):
            db.block_task(con, tid, "needs_input", str(report)[len("blocked:"):].strip(),
                          generation=self.generation, claim_lock=self.lock)
            self.submitted = True              # parked: stop driving, keep the session open
        elif time.time() > self.deadline:
            if db.add_event(con, tid, "failed", {"reason": "Sister timeout"},
                            generation=self.generation, claim_lock=self.lock):
                db.mark_failed(con, tid, generation=self.generation, claim_lock=self.lock)
            self.exit_fn(124)
            return False
        return True

    def _run(self):
        con = db.connect(self.db_path)
        try:
            while not self._stop.wait(self.poll_seconds):
                if not self.submitted and not self._step(con):
                    return
        finally:
            con.close()

    def stop(self):
        """The session ended: settle the card while the claim is still ours -- submit a
        report left at the last moment, park a blocked one, send anything else back."""
        self._stop.set()
        self._thread.join(timeout=10)
        if self.submitted:
            return
        con = db.connect(self.db_path)
        tid = self.task["id"]
        try:
            row = db.get(con, tid)
            if row is None or row["claim_lock"] != self.lock                     or int(row["generation"]) != self.generation:
                return
            ok, report = worker.check_report(self.run_dir, con=con, task_id=tid, generation=self.generation)
            if ok:
                self._submit(con, report)
            elif str(report).startswith("blocked:"):
                db.block_task(con, tid, "needs_input", str(report)[len("blocked:"):].strip(),
                              generation=self.generation, claim_lock=self.lock)
            elif db.back_to_ready(con, tid, generation=self.generation, claim_lock=self.lock):
                db.add_event(con, tid, "reclaimed",
                             {"reason": f"session exited without a report ({report})"},
                             generation=self.generation)
        finally:
            con.close()


def continue_flags(session_file, session_dir):
    """Return engine flags that resume the card's session, or None if there is none.

    The newest file in ``session_dir`` wins: after an in-pane fork the path recorded on
    the card is the pre-fork line (still on disk) and the branched file is newer in the
    same dir. The recorded path is the fallback for cards whose dir is empty."""
    try:
        files = [os.path.join(session_dir, n) for n in os.listdir(session_dir)
                 if n.endswith(".jsonl")]
    except OSError:
        files = []
    try:
        newest = max(files, key=os.path.getmtime) if files else None
    except OSError:
        newest = None
    if newest:
        return ["--session", newest]
    if session_file and os.path.isfile(session_file):
        return ["--session", session_file]
    return None


def launch(task_id, resume_only=False, say=None):
    """``resume_only`` reopens the saved session without resending the contract. ``say`` (with
    it) is delivered as the first turn of a new attempt: the daemon claimed the card
    (``pane.continue_card``) and this process settles it like a first run. Without a claim a
    reopened session is only for looking."""
    con = db.connect(CFG["db"])
    row = db.get(con, task_id)
    if row is None:
        sys.exit(f"Card not found: {task_id}")
    task = dict(row)
    lock = os.environ.get("MISAKA_USAGE_CLAIM_LOCK")
    generation = os.environ.get("MISAKA_USAGE_GENERATION")
    if lock and generation:
        if task["claim_lock"] != lock or int(task["generation"]) != int(generation):
            sys.exit(f"Card {task_id} is not claimed by this pane; the daemon owns the claim.")
    elif say:
        # A model turn changes the card, so it only happens under a claim (pane.continue_card).
        sys.exit(f"Card {task_id}: a message needs the daemon's claim; reopening without one is read-only.")
    elif resume_only and task["status"] in ACTIVE_STATUSES:
        # The live session is being written by another process; do not open it twice.
        sys.exit(f"Card {task_id} is still running in another pane ({task['status']}); open that pane instead.")
    workspace = db.workspace_for(task)
    if not os.path.isdir(workspace):
        # The card's folder is the project; a deleted or moved one is never recreated in silence.
        sys.exit(f"Card {task_id}: its folder {workspace} no longer exists.")
    run_dir = workspace
    from misaka.platform import cards as card_files
    task["_attachments"] = card_files.attachment_list(run_dir, task_id, workspace=workspace)
    profile_dir = os.path.join(os.path.expanduser(CFG["profiles_root"]), task["assignee"])
    if not os.path.isdir(profile_dir):
        sys.exit(f"Sister {task['assignee']} is not in the roster.")

    flags, factories, prompt, ro_root, role = worker.card_session_setup(
        task, workspace, profile_dir, CFG["provider"], CFG["default_model"]
    )
    session_dir = os.path.join(db.task_state_dir(task_id), "session")
    cont = continue_flags(task["session_file"], session_dir)
    if resume_only:
        # Open the existing session only; never resend the contract, which would rerun the card.
        if not cont:
            sys.exit(f"Card {task_id} has no session to resume.")
        flags += cont
        if say:
            worker.set_aside_report(task_id)              # the new attempt submits fresh proof
            flags.append(say + worker.report_instructions(generation))
    else:
        if cont:
            flags += cont
        flags.append(prompt)

    os.environ.update({
        "MISAKA_PROFILE_DIR": profile_dir,
        "MISAKA_WHO": role,
        "MISAKA_MCP_ROLE": role,
        "MISAKA_WORKSPACE": workspace,
        "MISAKA_TASK_DIR": db.task_state_dir(task_id),
        "MISAKA_TASK_OUTPUT_DIR": str(task.get("output_dir") or workspace),
        "MISAKA_APP_TITLE": f"MISAKA · {task['assignee']} · {task_id}",
        "MISAKA_TAGLINE": f"Card {task_id}: {task['title']}",
    })
    os.chdir(run_dir)

    supervisor = None
    if lock and generation:            # claimed, first run or continuation: the card drives itself
        supervisor = Supervisor(CFG["db"], task, run_dir, lock, generation).start()

    from misaka.cli.engine import main as engine_main
    try:
        code = asyncio.run(engine_main(flags, {"extensionFactories": factories or []}))
    finally:
        if supervisor is not None:
            supervisor.stop()
        from misaka.skills import sandbox as skill_sandbox
        skill_sandbox.cleanup(ro_root)
    sys.exit(code)
