"""Interactive card session run inside a Misaka Network pane.

The daemon owns the pane, claim, heartbeat, and exit reconciliation.  This process
only runs or resumes the Sister session; the card lifecycle hook settles its result.
"""
import asyncio
import contextlib
import os
import sys

from misaka.config import CFG, current_config
from misaka.core.network import worker
from misaka.core.platform import tasks as db

ACTIVE_STATUSES = ("running", "review")


def contract_flags(text):
    """Return engine flags that deliver ``text`` as the session's first message.

    A card body is whatever its author wrote, so it may open with "-" or "---"; passed as a
    bare positional it was read as an option instead -- the session died at parse time with
    no turn at all, and the daemon redispatched the identical card until the reclaim cap
    failed it. The end-of-options terminator is what keeps the contract prose."""
    return ["--", text]


def launch(task_id, resume_only=False, say=None, consult=False):
    """``resume_only`` reopens the saved session without resending the contract. ``say`` (with
    it) is delivered as the first turn of a new attempt: the daemon claimed the card
    (``pane.continue_card``) and this process settles it like a first run. With ``consult`` it is
    a finished card's answer to a message instead (``pane.consult_card``): no claim, the card
    stays as it is, and the consultation the daemon opened is what this process settles. Without
    a claim or a consultation a reopened session is only for looking."""
    # Read the card and let go: the session below runs for as long as a person keeps it open.
    with contextlib.closing(db.connect(CFG["db"])) as con:
        row = db.get(con, task_id)
        handoffs = worker.card_handoffs(con, row) if row is not None else []
        extras = (worker.card_extras(con, row)
                  if row is not None else {})
    if row is None:
        sys.exit(f"Card not found: {task_id}")
    task = dict(row)
    task["_handoffs"] = handoffs
    task.update(extras)
    lock = os.environ.get("MISAKA_USAGE_CLAIM_LOCK")
    generation = os.environ.get("MISAKA_USAGE_GENERATION")
    if consult:
        consulted, run_id, _consult_lock = worker.consult_env(task_id)
        with contextlib.closing(db.connect(CFG["db"])) as con:
            run = db.get_run(con, run_id) if run_id else None
        if (not say or run is None or run["status"] != "running" or consulted != int(task["generation"])
                or task["status"] not in db.CONSULTABLE):
            sys.exit(f"Card {task_id}: no consultation of this finished card is open for this pane.")
        resume_only = True
    elif lock and generation:
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
    from misaka.core.platform import cards as card_files
    task["_attachments"] = card_files.attachment_list(run_dir, task_id, workspace=workspace)
    profile_dir = os.path.join(os.path.expanduser(CFG["profiles_root"]), task["assignee"])
    if not os.path.isdir(profile_dir):
        sys.exit(f"Sister {task['assignee']} is not in the roster.")

    cfg = current_config()
    flags, assembly, prompt, role = worker.card_session_setup(
        task, workspace, profile_dir, cfg["provider"], cfg["default_model"]
    )
    from misaka.config import sessions as session_roots

    session_dir = session_roots.card_session_dir(task)
    cont = worker.continue_flags(task["session_file"], session_dir)
    if resume_only:
        # Open the existing session only; never resend the contract, which would rerun the card.
        if not cont:
            sys.exit(f"Card {task_id} has no session to resume.")
        flags += cont
        if say:
            flags += contract_flags(say)
    else:
        if cont:
            flags += cont
        flags += contract_flags(prompt)

    os.environ.update({
        "MISAKA_PROFILE_DIR": profile_dir,
        "MISAKA_WHO": role,
        "MISAKA_MCP_ROLE": role,
        "MISAKA_WORKSPACE": workspace,
        "MISAKA_TASK_OUTPUT_DIR": str(task.get("output_dir") or workspace),
        "MISAKA_APP_TITLE": f"MISAKA · {task['assignee']} · {task_id}",
        "MISAKA_TAGLINE": f"Card {task_id}: {task['title']}",
    })
    os.chdir(run_dir)

    from misaka.cli.engine import main as engine_main
    from misaka.utils import loop_watchdog
    # A claimed card is the daemon's to reclaim when its process ends; a session reopened
    # without a claim is only for a person to read, and stays open.
    loop_watchdog.configure(f"card-{task_id}", exit_on_stall=bool(lock and generation) or consult)
    code = asyncio.run(loop_watchdog.watched(engine_main(flags, assembly.engine_options())))
    sys.exit(code)
