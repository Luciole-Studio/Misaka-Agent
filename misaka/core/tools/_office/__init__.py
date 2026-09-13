"""Applying a batch of ops to one file, atomically.

Ported from FrontierAgent's ``plugins/tools/_writer_core.py`` dispatch (audit D149-D152),
with one change: **the batch is all or nothing**. FrontierAgent applies each op to the file
in place, so a batch that fails on op 7 of 12 leaves a half-built document on disk and a
receipt saying which ops "survived". A model that then retries the batch applies ops 1-6 a
second time. Here the ops run against a temp copy and the result is moved into place only
when every one of them succeeded, so a failed batch leaves the original exactly as it was.

Ops are single-key objects -- ``{"set_cell": {...}}`` -- applied in order. One key per
object, because a two-key object has no defined order and the model that wrote it meant a
sequence.
"""
from __future__ import annotations

import os
import shutil
import tempfile

from misaka.core.documents.office import soffice
from misaka.core.tools._office import docx as _docx
from misaka.core.tools._office import pptx as _pptx
from misaka.core.tools._office import text as _text
from misaka.core.tools._office import xlsx as _xlsx
from misaka.core.tools._office._receipt import format_receipt, normalise
from misaka.core.tools._office._runs import md_hint

# Suffix to writer. Each module owns its op set and its own refusal for an unknown op.
_WRITERS = {}
for _module in (_docx, _xlsx, _pptx, _text):
    for _suffix in _module.SUFFIXES:
        _WRITERS[_suffix] = _module

SUFFIXES = frozenset(_WRITERS)

# The Markdown hint is for the formats where formatting is structure: writing ``**bold**``
# into a .docx paragraph produces four literal asterisks. In a .md or .html file the markup
# IS the content, and in a .csv or .json there are no runs to point the model at, so the
# hint there would be advice to stop doing the right thing.
_HINT_FORMATS = frozenset(_docx.SUFFIXES | _pptx.SUFFIXES | _xlsx.SUFFIXES)

__all__ = ["SUFFIXES", "format_of", "run_ops", "validate_ops"]


def format_of(path):
    """The writer module for a path, or ``None``."""
    return _WRITERS.get(os.path.splitext(str(path))[1].lower())


def _export_pdf(source, args, meta):
    """The ``export_pdf`` op, which every Office format shares.

    Handled here rather than in three writers because it is one LibreOffice call and has
    nothing to do with the document's own model.
    """
    if soffice.binary() is None:
        return f"[error] export_pdf needs LibreOffice: {soffice.INSTALL_HINT}."
    out = args.get("out") or args.get("path") or os.path.splitext(source)[0] + ".pdf"
    if not soffice.export_pdf(source, out, meta=meta):
        return f"[error] export_pdf failed: {meta.get('soffice_error', 'no pdf produced')}"
    return f"exported pdf: {out}"


def _recalculate(target, meta):
    """Fill in the formula results of a workbook this batch just wrote.

    openpyxl stores a formula string and cannot store its value, so without this every
    formula the batch wrote reads back as ``uncached`` -- and a model that asked for a
    total gets a blank cell. The recalculated copy replaces the target in one move.
    """
    if soffice.binary() is None:
        return False
    with tempfile.TemporaryDirectory(prefix=".recalc-", dir=os.path.dirname(os.path.abspath(target))) as staging:
        fresh = soffice.recalc(target, into=staging, meta=meta)
        if fresh is None:
            return False
        os.replace(fresh, target)
    return True


def validate_ops(ops):
    """The error string for a malformed op list, or ``""``.

    The shape is load-bearing, so the refusal has to name it: a model handed only "invalid"
    sends the same thing back.
    """
    if not isinstance(ops, list) or not ops:
        return "ops must be a non-empty JSON array of single-key objects"
    for index, item in enumerate(ops):
        if not isinstance(item, dict) or len(item) != 1:
            return (f"ops[{index}]: each ops item must be a single-key object "
                    "{op_name: {params}}")
        name = next(iter(item))
        if not isinstance(item[name], dict):
            return f"ops[{index}]: the parameters of {name!r} must be an object"
    return ""


def run_ops(path, ops, *, overwrite=False):
    """Apply every op in order to one file and return the receipt.

    The whole batch runs against a temp copy: on success it replaces the target in one
    move, and on failure the target is untouched.
    """
    writer = format_of(path)
    if writer is None:
        suffix = os.path.splitext(str(path))[1].lower() or "a file with no suffix"
        return (f"[error] cannot write {suffix}: office writes "
                f"{' '.join(sorted(SUFFIXES))}.")
    problem = validate_ops(ops)
    if problem:
        return f"[error] {problem}"

    target = os.path.abspath(str(path))
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    handle, staging = tempfile.mkstemp(dir=directory, prefix=".office-",
                                       suffix=os.path.splitext(target)[1])
    os.close(handle)
    os.remove(staging)                     # the writers decide whether the file exists yet
    if os.path.exists(target):
        shutil.copy2(target, staging)      # so ``create`` sees the file it must refuse

    results, stopped_at, meta = [], None, {}
    try:
        for index, item in enumerate(ops, 1):
            op = next(iter(item))
            raw = (_export_pdf(staging, item[op], meta) if op == "export_pdf"
                   else writer.write(staging, op, item[op], overwrite=overwrite))
            outcome = normalise(op, raw)
            outcome["idx"] = index
            # The writers work on the staging copy and name it in their summaries; the
            # model asked about the file it can actually open.
            outcome["summary"] = str(outcome["summary"]).replace(staging, target)
            if outcome.get("warn"):
                outcome["warn"] = str(outcome["warn"]).replace(staging, target)
            results.append(outcome)
            if not outcome["ok"]:
                stopped_at = index
                break
        if stopped_at is None:
            os.replace(staging, target)
    except Exception as error:             # noqa: BLE001 - any writer fault is one op's
        stopped_at = len(results) + 1
        results.append({"op": next(iter(ops[stopped_at - 1])), "idx": stopped_at,
                        "ok": False, "warn": None, "wrote_formula": False, "counts": {},
                        "summary": f"[error] {type(error).__name__}: {error}"})
    finally:
        if os.path.exists(staging):
            os.remove(staging)

    # openpyxl writes the formula but not its result, so the numbers are not in the file
    # until something computes them. LibreOffice fills them in when it is installed; when
    # it is not, saying so beats a model quoting an empty cell. The package scan runs only
    # when an op reported a formula: it reads every sheet.
    tail = ""
    wrote_formula = stopped_at is None and any(
        outcome.get("wrote_formula") for outcome in results)
    if wrote_formula and _xlsx.cache_empty(target):
        if _recalculate(target, meta):
            tail = "formulas recalculated with LibreOffice"
        else:
            failure = meta.get("soffice_error")
            tail = ("formulas written; their cached values are empty until the file is "
                    "opened in Excel or LibreOffice, so read shows them as `uncached`"
                    + (f" ({failure})" if failure else f" — {soffice.INSTALL_HINT}"))
    hint = md_hint(ops) if os.path.splitext(target)[1].lower() in _HINT_FORMATS else ""
    receipt = format_receipt(os.path.basename(target), results, len(ops), stopped_at, tail)
    return receipt + ("\n  " + hint if hint else "")
