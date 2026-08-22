"""MISAKA's in-process PageIndex adapter."""

from __future__ import annotations


def build_tree(pdf, *, workers=None) -> list[dict]:
    """Return the deterministic PageIndex outline for a PDF."""
    try:
        from .flash import page_index_flash
    except ModuleNotFoundError as exc:
        if exc.name in {"PyPDF2", "pypdfium2", "regex", "sortedcontainers"}:
            raise RuntimeError("PageIndex requires: uv sync --extra pageindex") from exc
        raise
    return page_index_flash(pdf, workers=workers).get("structure") or []


__all__ = ["build_tree"]
