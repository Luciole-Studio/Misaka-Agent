"""Placebo audit for the red-team reviewer.

Copies a finished task workspace, deliberately damages one artifact, and asks the red team
to review the damaged copy. A reviewer that still passes it is leaking; the verdict is
recorded as a placebo_caught / placebo_failed event on the task.
"""
import json
import os
import random
import shutil
import tempfile

from misaka.platform import prompt_guard


def reservoir(items, k, rng=None):
    """Reservoir-sample k items from an iterable in one pass."""
    rng = rng or random
    out = []
    for i, x in enumerate(items):
        if i < k:
            out.append(x)
        else:
            j = rng.randint(0, i)
            if j < k:
                out[j] = x
    return out


def corrupt(text):
    """Apply one damage that a reviewer *should* catch: drop the last non-empty line.

    Returns (new_text, removed_line); removed_line is None if there was nothing to remove.
    """
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            removed = lines[i]
            return "\n".join(lines[:i] + lines[i + 1:]) + "\n", removed
    return text, None


def make_placebo(workspace, artifacts):
    """Copy the workspace and damage one artifact. Returns (copy_dir, removed_line) or (None, None)."""
    targets = [a for a in artifacts
               if a.lower().endswith((".md", ".txt", ".json", ".py")) and a != "report.json"]
    if not targets:
        return None, None
    tmp = tempfile.mkdtemp(prefix="misaka-placebo-")
    dst = os.path.join(tmp, "ws")
    shutil.copytree(workspace, dst, ignore=shutil.ignore_patterns("session", ".skills-ro"))
    victim = os.path.join(dst, targets[0])
    with open(victim, encoding="utf-8", errors="replace") as f:
        text = f.read()
    new, removed = corrupt(text)
    if removed is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, None
    with open(victim, "w", encoding="utf-8") as f:
        f.write(new)
    return dst, removed


def run_placebo(con, db, task, cfg, worker):
    """Review a damaged copy of the task's workspace and record whether the red team caught it.

    Returns "caught", "leaked", or "skipped:<reason>".
    """
    ws = task["workspace"] or ""
    try:
        with open(os.path.join(ws, "report.json"), encoding="utf-8") as f:
            report = json.load(f)
    except OSError:
        return "skipped:no-report"
    fake_ws, removed = make_placebo(ws, report.get("artifacts", []))
    if not fake_ws:
        return "skipped:no-text-artifact"
    try:
        prompt = f"""{task['body']}

# Submitted report.json
{prompt_guard.untrusted('report.json', json.dumps(report, ensure_ascii=False))}
The current directory is the task workspace. Build a checklist from the contract's
`## acceptance criteria` section, verify every artifact with the read tool, and output only:
{{"pass": true|false, "reasons": ["…"], "must_fix": ["…"]}}
"""
        obj, _raw, err = worker.run_llm_json(
            os.path.join(cfg["roles_root"], "redteam"), prompt,
            cfg["provider"], cfg["default_model"],
            cwd=fake_ws, tools=["read"], timeout=cfg.get("judge_timeout", 600))
    finally:
        shutil.rmtree(os.path.dirname(fake_ws), ignore_errors=True)
    if err or not isinstance(obj, dict) or not isinstance(obj.get("pass"), bool):
        return f"skipped:judge-error({err})"
    if obj["pass"]:
        db.add_event(con, task["id"], "placebo_failed",
                     {"removed": removed[:120], "reasons": obj.get("reasons", [])[:2]})
        return "leaked"
    db.add_event(con, task["id"], "placebo_caught", {"removed": removed[:120]})
    return "caught"


if __name__ == "__main__":
    rng = random.Random(0)
    sample = reservoir(range(1000), 10, rng)
    assert len(sample) == 10 and len(set(sample)) == 10, sample
    counts = [0] * 10
    for _ in range(2000):  # rough uniformity check: every element should be picked about equally often
        s = reservoir(range(10), 3, random.Random())
        for x in s:
            counts[x] += 1
    assert min(counts) > 300, counts  # expected ~600 per element, comfortably above 300
    text = "First line\nSecond line\n\n## Verified\n"
    new, removed = corrupt(text)
    assert removed == "## Verified" and "Verified" not in new, (new, removed)
    assert corrupt("   \n\n")[1] is None, "whitespace-only text must return None"
    print(f"placebo selfcheck ok — reservoir is uniform (min={min(counts)}); damage removed {removed!r}")
