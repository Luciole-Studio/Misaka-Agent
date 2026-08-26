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
    from misaka.network import messages
    con = messages.connect()
    try:
        messages.send(con, to_addr, text, summary=f"ally {sender}·card {task_id}",
                      sender=sender, task_id=task_id)
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


def write_report(workspace, exit_code, output, *, assignee, task_id=None, output_dir=None, generation=None):
    """Write report.json on the ally's behalf so the board's submit -> done
    flow works unchanged (only the verification gate marks done; allies and Sisters are treated alike).
    Returns (submitted, summary).
    """
    import json as _json
    if exit_code != 0:
        return False, f"Ally {assignee} exited with code {exit_code}: {summarize(output, 500)}"
    tail = summarize(output, 2000).strip()
    if not tail:
        return False, f"Ally {assignee} produced no output."
    artifacts = []
    artifact_root = output_dir or workspace
    try:
        artifacts = sorted(os.path.relpath(os.path.join(artifact_root, f), workspace)
                           for f in os.listdir(artifact_root)
                           if not f.startswith(".") and f != "report.json"
                           and os.path.isfile(os.path.join(artifact_root, f)))
    except OSError:
        pass
    report = {"schema_version": 1, "status": "done",
              **({"generation": int(generation)} if generation is not None else {}),
              "summary": tail[-1500:],
              "artifacts": artifacts,
              "uncertain": [f"Output was produced by ally {assignee} and has not been independently reviewed."]}
    report_dir = workspace
    if task_id:
        from misaka.platform import tasks
        report_dir = tasks.task_state_dir(task_id)
    os.makedirs(report_dir, exist_ok=True)
    with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as f:
        _json.dump(report, f, ensure_ascii=False)
    return True, report["summary"]


def finish(workspace, exit_code, output, *, assignee, task_id, output_dir=None, generation=None):
    """Wrap up after the ally process exits: write report.json, then mail Last Order. Returns (submitted, summary)."""
    ok, summary = write_report(
        workspace, exit_code, output, assignee=assignee,
        task_id=task_id, output_dir=output_dir, generation=generation)
    head = "finished and submitted" if ok else "could not submit"
    try:
        notify(task_id, f"Ally {assignee} {head} (card {task_id}):\n\n{summary}",
               sender=assignee)
    except Exception:  # noqa: BLE001 - a failed notification must not change the submission result
        pass
    return ok, summary


def describe(argv, prompt):
    """One human-readable line of what was launched, for pane titles and logs."""
    shown = " ".join(shlex.quote(a) for a in argv)
    head = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return f"{shown} ⟨{head[:40]}⟩" if head else shown
