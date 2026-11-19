"""The outline of a PDF, read by PageIndex Flash in a child process.

PageIndex is upstream's code, kept unchanged in ``pageindex/``; this module is MISAKA's side of
it. It calls ``flash.main.extract_toc``, the LLM-free reading of a PDF's bookmarks and layout.
``flash/api.py``, upstream's own entry, adds model-written summaries and is not called.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from misaka.utils.shell import killProcessTree

# The third-party packages the vendored tree imports, all of them MISAKA's own dependencies.
# One missing is a broken install.
REQUIREMENTS = ("PyPDF2", "pypdfium2", "regex", "sortedcontainers")

INSTALL_HINT = "reinstall MISAKA (PyPDF2 and sortedcontainers are its dependencies; in a checkout, uv sync)"

HEADER = b"%PDF-"
HEADER_WITHIN = 1024

# The slowest of 258 real PDFs took 57 s (585 pages; 2026-11-19). Ten minutes is room for a book
# several times that; past it the outline is given up, not the document.
TIMEOUT = 600

# ``-P`` keeps the working directory off the child's path: it is the user's workspace, and a
# file there named like a module must not be what the child imports.
_CHILD = ("import sys; sys.path.append(sys.argv[1]); "
          "from misaka.core.documents.pdf_outline import _child; _child(sys.argv[2], int(sys.argv[3]))")


class PageIndexUnavailable(RuntimeError):
    """PageIndex's packages are missing from this install.

    A distinct type so callers can tell "this install cannot read PDF outlines at all" --
    actionable, and the same for every document -- from a per-document parse failure.
    """


class PageIndexFailed(RuntimeError):
    """PageIndex could not read this PDF. The message says how: the exception it raised, a
    timeout, or the child's exit."""


def available() -> bool:
    """Whether PageIndex can run in this install."""
    for name in REQUIREMENTS:
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def read(pdf, *, workers=None) -> dict:
    """PageIndex's deterministic reading of the PDF at ``pdf``: ``structure``, the outline as
    nested nodes (empty where it found none), and ``toc_source``, one of ``bookmarks``, ``hybrid``
    (bookmarks with detected sections hung under them) and ``detected`` (the page layout alone).

    Made in a child process, as every other use of PDFium here is: a page PDFium cannot load
    leaves handles open in the process that asked, and a hang there has no way out.
    """
    argv = [sys.executable, "-P", "-c", _CHILD, str(Path(__file__).resolve().parents[3]),
            os.path.abspath(pdf), str(workers or 0)]
    own_group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                 else {"start_new_session": True})
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, **own_group)
    except OSError as error:
        raise PageIndexFailed(f"{type(error).__name__}: {error}") from error
    try:
        out, err = child.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        killProcessTree(child.pid)
        try:
            child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise PageIndexFailed(f"timeout: no outline within {TIMEOUT} s") from None
    try:
        answer = json.loads(out)
    except ValueError:
        said = " ".join(err.decode("utf-8", errors="replace").split())[-160:]
        raise PageIndexFailed(f"exit {child.returncode}: {said}") from None
    if "unavailable" in answer:
        raise PageIndexUnavailable(answer["unavailable"])
    if "error" in answer:
        raise PageIndexFailed(answer["error"])
    return answer


def _child(path, workers):
    """The child's whole run: one JSON object on its stdout. Anything else written there, by a
    library or a pool worker, goes to stderr instead."""
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    try:
        from misaka.core.documents.pageindex.flash.main import extract_toc
        result = extract_toc(_from_header(path), workers=workers or None)
        nodes = result.get("structure") or []
        _drop_internal_keys(nodes)
        answer = {"structure": nodes, "toc_source": result.get("toc_source")}
    except Exception as error:  # noqa: BLE001 - whatever it was, the parent records it as the reason
        if isinstance(error, ModuleNotFoundError) and error.name in REQUIREMENTS:
            answer = {"unavailable": f"PageIndex cannot run in this install: {INSTALL_HINT}"}
        else:
            answer = {"error": f"{type(error).__name__}: {error}"}
    json.dump(answer, out)
    out.flush()


def _from_header(path):
    """The path, or the file's bytes from ``%PDF-`` on when something stands before it.

    PDFium reads such a file as it is. PyPDF2, which reads the bookmarks, counts every offset from
    byte 0 and fails on ``/Root``: a thesis lost all 68 of its bookmarks to that (2026-11-19).
    """
    with open(path, "rb") as source:
        at = source.read(HEADER_WITHIN).find(HEADER)
        if at <= 0:
            return path
        source.seek(at)
        return io.BytesIO(source.read())


def _drop_internal_keys(nodes):
    for node in nodes:
        node.pop("_same_page", None)
        _drop_internal_keys(node.get("nodes") or [])


__all__ = ["INSTALL_HINT", "REQUIREMENTS", "PageIndexFailed", "PageIndexUnavailable", "available", "read"]
