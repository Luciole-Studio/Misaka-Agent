"""Run an ally (third-party agent CLI) non-interactively in a pane and mail its reply to Last Order.

Non-interactive modes (`codex exec` / `claude -p` / `gemini -p`) are used instead of
scraping the screen: process exit is a definite completion signal and stdout is clean
text, so there is no idle detection and no ANSI stripping.
"""
import os
import shlex

TAIL_CAP = 20000        # Mailbox reply cap; longer output keeps head + tail (the mailbox is not a log store).


def build_argv(argv, prompt):
    """Command line = the argv Last Order gave plus the prompt as the final argument."""
    if not argv:
        raise ValueError("argv must not be empty: say which CLI to run.")
    return [*argv, prompt]


def label_for(argv, label=None):
    """Sender name used in the mailbox and pane title: the given label, else the command name."""
    return label or (os.path.basename(argv[0]) if argv else "ally")


def summarize(text, cap=TAIL_CAP):
    """Trim long output to head + tail: the tail usually holds the conclusion, the head the context."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    head, tail = text[: cap // 3], text[-(cap // 3 * 2):]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n\n… ({omitted} characters omitted) …\n\n{tail}"


def notify(task_id, text, *, sender, to_addr="last-order"):
    """Send a mailbox message to Last Order on the ally's behalf.

    Sisters report via SendMessage themselves; allies cannot, so the daemon sends for
    them and Last Order receives both through the same path. Failures are reported
    too: a login or command error must reach Last Order rather than vanish.
    """
    from misaka.core.network import messages
    con = messages.connect()
    try:
        messages.send(con, to_addr, text, summary=f"ally {sender}·card {task_id}",
                      sender=sender)
    finally:
        con.close()


CONTRACT = """(The task contract follows. Write your deliverables into the current directory; the last part of your output is recorded as the submission summary.

To reach the coordinator while you work, run in the current directory:
    misaka tell "your message"
If you get stuck, find that a premise no longer holds, or need a human decision, say so right away; do not wait until the end.)

{body}
"""


def card_prompt(row):
    """Card -> prompt for the ally: the contract wrapper around the same body a Sister would get."""
    body = (row["body"] or "").strip() or row["title"]
    review_feedback = row.get("review_feedback") if hasattr(row, "get") else None
    if review_feedback:
        body = f"""Independent reviewer requested changes:
{review_feedback}

---

{body}"""
    attachments = row.get("_attachments", []) if hasattr(row, "get") else []
    if attachments:
        body += "\n\n## Input attachments\n" + "\n".join(
            f"- `{item.get('source')}`" for item in attachments
        )
    output_dir = row.get("output_dir") if hasattr(row, "get") else None
    if output_dir:
        body += f"\n\nWrite every new deliverable under `{output_dir}`."
    return CONTRACT.format(body=body).strip()


def _artifacts(root, workspace, since):
    """Files under ``root`` written during the run (mtime >= ``since``), nested ones included;
    hidden entries and the board's own folders are not deliverables."""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("cards", "research", "node_modules"))
        for name in sorted(files):
            path = os.path.join(base, name)
            if name.startswith("."):
                continue
            try:
                if since is not None and os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
            out.append(os.path.relpath(path, workspace))
    return out


def submission(workspace, exit_code, output, *, assignee, output_dir=None, since=None):
    """Build an external ally's board submission from its exit and output."""
    if exit_code != 0:
        return None, f"Ally {assignee} exited with code {exit_code}: {summarize(output, 500)}"
    tail = summarize(output, 2000).strip()
    if not tail:
        return None, f"Ally {assignee} produced no output."
    artifacts = _artifacts(output_dir or workspace, workspace, since)
    result = {
        "summary": tail[-1500:],
        "artifacts": artifacts,
        "notes": "",
        "uncertain": [
            f"Output was produced by ally {assignee} and has not been independently reviewed."
        ],
        "findings": [],
    }
    return result, result["summary"]


def finish(workspace, exit_code, output, *, assignee, task_id, output_dir=None, generation=None, since=None):
    """Build the submission after the ally process exits; the daemon owns settlement."""
    return submission(
        workspace,
        exit_code,
        output,
        assignee=assignee,
        output_dir=output_dir,
        since=since,
    )


def describe(argv, prompt):
    """One human-readable line of what was launched, for pane titles and logs."""
    shown = " ".join(shlex.quote(a) for a in argv)
    head = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return f"{shown} ⟨{head[:40]}⟩" if head else shown
