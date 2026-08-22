"""Interactive card session run inside a Misaka Network pane.

The daemon launches this in a pane and owns claiming, timeouts and status
transitions; this module only wires the card setup (same as the background
worker) into the interactive engine, folding in any red-team feedback from
the previous round and resuming the card's existing session when there is one.
"""
import asyncio
import json
import os
import sys

from misaka.config import CFG
from misaka.platform import tasks as db
from misaka.network import worker

ACTIVE_STATUSES = ("running", "review", "verifying", "finalizing")


def continue_flags(session_file, session_dir):
    """Return engine flags that resume the card's session, or None if there is none.

    The exact path recorded on the card wins; otherwise fall back to the most
    recent session in ``session_dir`` (``-c``)."""
    if session_file and os.path.isfile(session_file):
        return ["--session", session_file]
    try:
        names = os.listdir(session_dir)
    except OSError:
        return None
    return ["-c"] if any(n.endswith(".jsonl") for n in names) else None


def launch(task_id, resume_only=False):
    con = db.connect(CFG["db"])
    row = db.get(con, task_id)
    if row is None:
        sys.exit(f"Card not found: {task_id}")
    task = dict(row)
    if resume_only and task["status"] in ACTIVE_STATUSES:
        # The live session is being written by another process; do not open it twice.
        sys.exit(f"Card {task_id} is still running in another pane ({task['status']}); open that pane instead.")
    feedback = db.latest_payload(con, task_id, "verify_fail", generation=task["generation"])
    if feedback:
        fixes = json.loads(feedback).get("must_fix", [])
        if fixes:
            task["feedback"] = ("⚠️ The previous review failed. Fix the following before resubmitting (rewrite deliverables to the latest requirements):\n"
                                + "\n".join(f"- {x}" for x in fixes))
    workspace = db.workspace_for(task)
    os.makedirs(workspace, exist_ok=True)
    task["_attachments"] = db.stage_attachments(con, task_id, workspace)
    profile_dir = os.path.join(os.path.expanduser(CFG["profiles_root"]), task["assignee"])
    if not os.path.isdir(profile_dir):
        sys.exit(f"Sister {task['assignee']} is not in the roster.")

    flags, factories, prompt, _ro_root, role = worker.card_session_setup(
        task, workspace, profile_dir, CFG["provider"], CFG["default_model"]
    )
    session_dir = os.path.join(db.task_state_dir(task_id), "session")
    cont = continue_flags(task["session_file"], session_dir)
    if resume_only:
        # Open the existing session only; never resend the contract, which would rerun the card.
        if not cont:
            sys.exit(f"Card {task_id} has no session to resume.")
        flags += cont
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
    os.chdir(workspace)

    from misaka.cli.engine import main as engine_main
    sys.exit(asyncio.run(engine_main(flags, {"extensionFactories": factories or []})))
