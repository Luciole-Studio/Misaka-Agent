"""PageIndex Flash, upstream's bytes: the LLM-free reader of a PDF's outline.

``flash/`` and ``naming.py`` are https://github.com/VectifyAI/PageIndex at
340b8535a219ea448c4c0f5bdd354761449b55b3 (2026-11-18): ``pageindex/flash`` without its
``README.md`` and ``assets/``, and ``pageindex/naming.py``, which ``flash/api.py`` imports. MIT,
``LICENSE.PageIndex``. This file is the one thing here that is not upstream's.

Nothing in this directory is edited: ``tests/test_pageindex_vendored.py`` fails on any byte of
difference. MISAKA's side is ``documents/pdf_outline.py``. To take a newer upstream, copy the
same paths over these and set that test's digest to the one it prints.
"""
