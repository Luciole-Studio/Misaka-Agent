"""MISAKA's in-process PageIndex adapter."""

from __future__ import annotations

import importlib.util

# The third-party packages the vendored tree imports. ``pypdfium2`` is a base dependency;
# the other three arrive only with the ``pageindex`` extra, which is why a plain install can
# split a PDF into pages but never build its outline.
REQUIREMENTS = ("PyPDF2", "pypdfium2", "regex", "sortedcontainers")

# A wheel user cannot run `uv sync`; name the pip command first and keep the repo one after.
INSTALL_HINT = "pip install 'misaka[pageindex]' (or, in a checkout, uv sync --extra pageindex)"


class PageIndexUnavailable(RuntimeError):
    """The optional ``pageindex`` extra is not installed.

    A distinct type so callers can tell "this install cannot do structure extraction at all"
    -- actionable, and the same for every document -- from a per-document parse failure.
    """


def available() -> bool:
    """Whether structure extraction can run in this install."""
    for name in REQUIREMENTS:
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def build_tree(pdf, *, workers=None) -> list[dict]:
    """Return the deterministic PageIndex outline for a PDF."""
    try:
        from .flash import page_index_flash
    except ModuleNotFoundError as exc:
        if exc.name in set(REQUIREMENTS):
            raise PageIndexUnavailable(
                f"PageIndex structure extraction needs the optional extra: {INSTALL_HINT}"
            ) from exc
        raise
    return page_index_flash(pdf, workers=workers).get("structure") or []


__all__ = ["INSTALL_HINT", "REQUIREMENTS", "PageIndexUnavailable", "available", "build_tree"]
