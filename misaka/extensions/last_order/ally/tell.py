"""`misaka tell`: lets an ally send a mailbox message to Last Order from inside its card workspace."""
import os

from misaka.config import CFG


def _task_from_cwd(cwd=None):
    """Find the ally card whose workspace or output_dir contains cwd; None if ambiguous."""
    here = os.path.realpath(cwd or os.getcwd())
    try:
        from misaka.core.platform import tasks as db
        con = db.connect(os.path.expanduser(CFG["db"]))
        rows = con.execute(
            "SELECT id,workspace,output_dir FROM tasks WHERE executor IS NOT NULL"
        ).fetchall()
        con.close()
    except Exception:  # noqa: BLE001 - if the lookup fails, do not guess an identity
        return None
    matches = []
    for row in rows:
        for value in (row["output_dir"], row["workspace"]):
            root = os.path.realpath(value) if value else ""
            if root and (here == root or here.startswith(root + os.sep)):
                matches.append((len(root), row["id"]))
    if not matches:
        return None
    depth = max(item[0] for item in matches)
    ids = {task_id for length, task_id in matches if length == depth}
    return ids.pop() if len(ids) == 1 else None


def whoami(cwd=None):
    """Return (sender, task_id) for the current process; sender is None when it cannot be identified."""
    sender = os.environ.get("MISAKA_ALLY") or None
    task_id = os.environ.get("MISAKA_USAGE_TASK_ID") or _task_from_cwd(cwd)
    if not sender and task_id:          # A sandbox may have cleared the env var: look the card up instead.
        try:
            from misaka.core.platform import tasks as db
            con = db.connect(os.path.expanduser(CFG["db"]))
            row = db.get(con, task_id)
            con.close()
            if row:
                sender = row["assignee"]
        except Exception:  # noqa: BLE001, S110 - unknown sender; tell() refuses to send below
            pass
    return sender, task_id


def tell(body, *, to_addr="last-order", summary=None, cwd=None):
    """Send a message. Returns (ok, message). Refuses when the sender cannot be identified (no impersonation)."""
    if not (body or "").strip():
        return False, "Message cannot be empty."
    sender, task_id = whoami(cwd)
    if not sender:
        return False, (
            "Cannot tell which ally you are: MISAKA_ALLY is not set and the working directory "
            "is not inside any card's workspace. Run this from the card's workspace."
        )
    from misaka.config import sisters
    from misaka.network import messages
    con = messages.connect()
    try:
        known = {"last-order"} | set(sisters())    # Same roster messages.register uses.
        if to_addr not in known:
            return False, (
                f"Unknown recipient {to_addr!r}. Available recipients: "
                f"{', '.join(sorted(known))}"
            )
        messages.send(con, to_addr, body.strip(),
                      summary=summary or f"Message from ally {sender}",
                      sender=sender, task_id=task_id)
    finally:
        con.close()
    suffix = f", card {task_id}" if task_id else ""
    return True, f"Sent to {to_addr} from {sender}{suffix}."
