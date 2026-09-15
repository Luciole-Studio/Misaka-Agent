"""What a batch of ops reports back.

Ported from FrontierAgent's ``plugins/tools/_writer_core.py:168-263`` (audit D72-D77). The
shape is the point: a clean batch of twelve ops folds to two lines, and only the ops that
went wrong expand. A per-op transcript costs the model a screen of "✓ set_cell" it cannot
act on, and buries the one line that says a find matched nothing.

Three outcomes, not two. ``ok=False`` is fatal and stops the batch; ``warn`` is an op that
ran and changed nothing -- a find with no match, an anchor that is not in the document --
which is not an error but is almost always a mistake, and silence about it is how a model
comes to believe it edited a file it did not touch.
"""
from __future__ import annotations


def result(summary, *, ok=True, warn=None, wrote_formula=False, counts=None):
    """One op's structured outcome.

    ``summary`` is one sentence. ``wrote_formula`` says this op put a formula in a
    workbook, which is what decides whether a recalculation has to run. ``counts`` is
    ``{singular label: n}`` for an op that wrote several things at once.
    """
    return {"ok": ok, "summary": summary, "warn": warn,
            "wrote_formula": wrote_formula, "counts": counts or {}}


def normalise(op, raw):
    """An op's return -- a dict from ``result`` or a bare string -- as a full result dict."""
    base = {"ok": True, "summary": "", "warn": None, "wrote_formula": False, "counts": {}}
    if isinstance(raw, dict):
        base.update(raw)
        base["op"] = op
        return base
    text = str(raw)
    base["ok"] = not text.startswith(("[error", "[office_writer error"))
    base["summary"] = text
    base["op"] = op
    return base


def format_counts(counts):
    """``{'paragraph': 6, 'table': 2}`` -> ``6 paragraphs, 2 tables``; zeros are dropped."""
    parts = [f"{n} {label}" + ("" if n == 1 else "s") for label, n in counts.items() if n]
    return ", ".join(parts) if parts else "nothing"


def _breakdown(items):
    """The one-line tally for the ops that went cleanly.

    ``create`` expands its own write receipt because it is the op that wrote the document;
    everything else is counted by name.
    """
    tally, order, creates = {}, [], []
    for item in items:
        if item["op"] == "create" and item.get("counts"):
            creates.append("create(" + format_counts(item["counts"]) + ")")
        else:
            if item["op"] not in tally:
                order.append(item["op"])
            tally[item["op"]] = tally.get(item["op"], 0) + 1
    return ", ".join(creates + [f"{tally[op]} {op}" for op in order])


def format_receipt(path, results, total, stopped_at, extra_line=""):
    """Every op's outcome folded into a receipt the model can act on.

    ``stopped_at`` is the 1-based index of the op that failed, or ``None``. When a batch
    stops, the receipt has to say what the file now holds -- the model's next call depends
    on whether the earlier ops survived.
    """
    if stopped_at is not None:
        failure = results[-1]
        lines = [f"✗ {path} — STOPPED at op {stopped_at}/{total}",
                 f"  [{failure['idx']}] {failure['op']}: {failure['summary']}"]
        if stopped_at < total:
            lines.append(f"  ops {stopped_at + 1}-{total} not executed")
        lines.append("  file unchanged; staged edits and exports not published")
        if extra_line:
            lines.append("  " + extra_line)
        return "\n".join(lines)

    warned = [item for item in results if item.get("warn")]
    clean = [item for item in results if not item.get("warn")]

    if total == 1:
        only = results[0]
        base = only["summary"] or only["op"]
        head = f"⚠ {base} — {only['warn']}" if only.get("warn") else f"✓ {base}"
        return "\n".join([head, *(["  " + extra_line] if extra_line else [])])

    note = f", {len(warned)} wrote nothing" if warned else ""
    lines = [f"✓ {path} — {total} ops applied{note}"]
    breakdown = _breakdown(clean)
    if breakdown:
        lines.append("  " + breakdown)
    for item in warned:
        lines.append(f"  ⚠ [{item['idx']}] {item['op']}: {item['warn']}")
    if extra_line:
        lines.append("  " + extra_line)
    return "\n".join(lines)
