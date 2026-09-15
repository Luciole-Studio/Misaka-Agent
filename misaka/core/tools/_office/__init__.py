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
from contextlib import ExitStack

from misaka.core.documents.office import soffice
from misaka.core.tools._office import docx as _docx
from misaka.core.tools._office import pptx as _pptx
from misaka.core.tools._office import text as _text
from misaka.core.tools._office import xlsx as _xlsx
from misaka.core.tools._office._receipt import format_receipt, normalise
from misaka.core.tools._office._runs import md_hint
from misaka.core.tools._office.paths import pdf_path

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
    out = pdf_path(source, args)
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


def _cleanup(action, path, warnings):
    """Cleanup faults must not turn a committed write into a retryable failure."""
    try:
        action()
    except OSError as error:
        warnings.append(f"cleanup failed; check retained path {path}: {type(error).__name__}: {error}")


def _publish(staged, cleanup_warnings):
    """Replace each destination, restoring earlier ones on ordinary publish failures.

    Rename is atomic per file, not across files or power loss. Recovery copies survive
    a failed rollback and their paths are reported rather than silently deleted.
    """
    backups, published, recovery = {}, [], set()
    try:
        for target in staged:
            if os.path.exists(target):
                handle, backup = tempfile.mkstemp(prefix=".office-backup-", dir=os.path.dirname(target))
                os.close(handle)
                backups[target] = backup
                shutil.copy2(target, backup)
        for target, source in staged.items():
            os.replace(source, target)
            published.append(target)
    except Exception as error:
        for target in reversed(published):
            try:
                if target in backups:
                    os.replace(backups[target], target)
                else:
                    os.unlink(target)
            except OSError:
                recovery.add(target)
        if recovery:
            locations = {target: backups.get(target, "new file; remove manually") for target in recovery}
            raise OSError(f"{error}; rollback incomplete; recovery copies: {locations}") from error
        raise
    finally:
        for target, backup in backups.items():
            if target not in recovery and os.path.exists(backup):
                _cleanup(lambda backup=backup: os.unlink(backup), backup, cleanup_warnings)


def run_ops(path, ops, *, overwrite=False):
    """Stage ordered operations and exports; publish only after the batch succeeds."""
    writer = format_of(path)
    if writer is None:
        suffix = os.path.splitext(str(path))[1].lower() or "a file with no suffix"
        return (f"[error] cannot write {suffix}: office writes "
                f"{' '.join(sorted(SUFFIXES))}.")
    problem = validate_ops(ops)
    if problem:
        return f"[error] {problem}"

    target = os.path.realpath(str(path))
    results, stopped_at, meta, tail = [], None, {}, ""
    cleanup_warnings = []
    with ExitStack() as stack:
        staged = {}

        def stage(destination):
            destination = os.path.realpath(destination)
            if destination not in staged:
                directory = os.path.dirname(destination)
                os.makedirs(directory, exist_ok=True)
                temporary = tempfile.TemporaryDirectory(prefix=".office-", dir=directory)
                stack.callback(_cleanup, temporary.cleanup, temporary.name, cleanup_warnings)
                staged[destination] = os.path.join(temporary.name, os.path.basename(destination))
            return staged[destination]

        index, op = 1, next(iter(ops[0]))
        try:
            source = stage(target)
            if os.path.exists(target):
                shutil.copy2(target, source)
                rebuilding = "create" in ops[0] and (overwrite or ops[0]["create"].get("overwrite"))
                if writer in (_docx, _xlsx, _pptx) and not rebuilding:
                    from misaka.core.documents.office import precheck
                    precheck(source)
            for index, item in enumerate(ops, 1):
                op, args = next(iter(item.items()))
                if op == "export_pdf":
                    out = os.path.realpath(pdf_path(target, args))
                    if out == target or not out.lower().endswith(".pdf"):
                        raise ValueError("export_pdf needs a distinct .pdf output path")
                    if writer not in (_docx, _xlsx, _pptx):
                        raise ValueError("export_pdf reads .docx/.xlsx/.pptx; build an Office file first")
                    raw = _export_pdf(source, {"out": stage(out)}, meta)
                else:
                    raw = writer.write(source, op, args, overwrite=overwrite)
                outcome = normalise(op, raw)
                outcome["idx"] = index
                for destination, temporary in staged.items():
                    outcome["summary"] = str(outcome["summary"]).replace(temporary, destination)
                    if outcome.get("warn"):
                        outcome["warn"] = str(outcome["warn"]).replace(temporary, destination)
                results.append(outcome)
                if not outcome["ok"]:
                    stopped_at = index
                    break
            if stopped_at is None:
                # Recalculation is part of staging, never a post-commit mutation.
                edited_workbook = writer is _xlsx and any(r["op"] != "export_pdf" for r in results)
                if edited_workbook and _xlsx.cache_empty(source):
                    if _recalculate(source, meta):
                        errors = _xlsx.formula_errors(source)
                        tail = ("formulas recalculated with LibreOffice; "
                                f"{len(errors)} error(s)"
                                + (": " + "  ".join(errors[:10]) + (" …" if len(errors) > 10 else "")
                                   if errors else "")
                                + " (workbook re-saved; chart/validation fidelity best-effort)")
                        if _xlsx.cache_empty(source):
                            tail += "; some formula results are still uncached; recalculation is incomplete"
                    else:
                        failure = meta.get("soffice_error")
                        tail = ("formulas written; their cached values are empty until the file is "
                                "opened in Excel or LibreOffice, so read shows them as `uncached`"
                                + (f" ({failure})" if failure else f" — {soffice.INSTALL_HINT}"))
                _publish(staged, cleanup_warnings)
        except Exception as error:  # noqa: BLE001 - faults become structured batch failures
            stopped_at = index
            failure = normalise(op, f"[error] {type(error).__name__}: {error}")
            failure["idx"] = index
            results.append(failure)
            if "rollback incomplete" in str(error):
                # A filesystem that also rejects restoration breaks the usual rollback promise.
                return f"[error] {error}"
    hint = md_hint(ops) if os.path.splitext(target)[1].lower() in _HINT_FORMATS else ""
    if cleanup_warnings:
        tail = "\n  ".join(filter(None, [tail, *(f"⚠ {warning}" for warning in cleanup_warnings)]))
    receipt = format_receipt(os.path.basename(target), results, len(ops), stopped_at, tail)
    return receipt + ("\n  " + hint if hint else "")
