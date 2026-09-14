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


def contract_flags(text):
    """Return engine flags that deliver ``text`` as the session's first message.

    A card body is whatever its author wrote, so it may open with "-" or "---"; passed as a
    bare positional it was read as an option instead -- the session died at parse time with
    no turn at all, and the daemon redispatched the identical card until the reclaim cap
    failed it. The end-of-options terminator is what keeps the contract prose."""
    return ["--", text]


def launch(task_id, resume_only=False, say=None):
    """``resume_only`` reopens the saved session without resending the contract. ``say`` (with
    it) is delivered as the first turn of a new attempt: the daemon claimed the card
    (``pane.continue_card``) and this process settles it like a first run. Without a claim a
    reopened session is only for looking."""
    # Read the card and let go: the session below runs for as long as a person keeps it open.
    with contextlib.closing(db.connect(CFG["db"])) as con:
        row = db.get(con, task_id)
        handoffs = worker.card_handoffs(con, row) if row is not None else []
        extras = (worker.card_extras(con, row, include_colleagues=False)
                  if row is not None else {})
    if row is None:
        sys.exit(f"Card not found: {task_id}")
    task = dict(row)
    task["_handoffs"] = handoffs
    task.update(extras)
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
    from misaka.core.platform import cards as card_files
    task["_attachments"] = card_files.attachment_list(run_dir, task_id, workspace=workspace)
    profile_dir = os.path.join(os.path.expanduser(CFG["profiles_root"]), task["assignee"])
    if not os.path.isdir(profile_dir):
        sys.exit(f"Sister {task['assignee']} is not in the roster.")

    cfg = current_config()
    flags, assembly, prompt, ro_root, role = worker.card_session_setup(
        task, workspace, profile_dir, cfg["provider"], cfg["default_model"]
    )
    from misaka.config import sessions as session_roots

    session_dir = session_roots.card_session_dir(task)
    cont = continue_flags(task["session_file"], session_dir)
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
    if os.path.isdir(ro_root):
        os.environ["MISAKA_SKILL_SANDBOX"] = ro_root
    else:
        os.environ.pop("MISAKA_SKILL_SANDBOX", None)
    os.chdir(run_dir)

    from misaka.cli.engine import main as engine_main
    from misaka.core.network.todo import TodoPart

    for part in assembly.parts:
        if isinstance(part, TodoPart):
            part.usage = {"usage_db": CFG["db"], "task_id": task_id,
                          "generation": int(task["generation"])}
    try:
        code = asyncio.run(engine_main(flags, assembly.engine_options()))
    finally:
        from misaka.core.skills import sandbox as skill_sandbox
        skill_sandbox.cleanup(ro_root)
    sys.exit(code)
