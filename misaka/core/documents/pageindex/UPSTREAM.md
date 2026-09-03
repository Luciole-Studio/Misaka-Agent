# PageIndex source slice

- Upstream: <https://github.com/VectifyAI/PageIndex>
- Commit: `416e304f5150f7930fee8d2fe4d36e26caf57f35`
- License: MIT (`LICENSE.PageIndex`)

`flash/` is the deterministic PageIndex Flash PDF-outline pipeline. MISAKA keeps
the layout parser and bundled lookup data, replaces the public adapter, and omits
the upstream CLI, LLM summaries/optimization, examples, and generated results.

## Local delta versus upstream `pageindex/flash/`

This tree is **not** a verbatim copy, and the earlier claim that it was is what made the
2026-09-02 audit read 72 files as unregistered drift. Three kinds of difference, and
nothing else:

1. `api.py` — MISAKA's own adapter (no LLM summary/optimize passes, `workers` forwarded
   to `extract_toc`). Upstream's `README.md`, `assets/` and the 16 top-level
   `pageindex/*.py` modules are not vendored at all.
2. One mechanical lint normalisation pass (import order, `F401`, `UP`, `C4`, `SIM`,
   `RUF`, …), which is why nearly every file differs from upstream byte for byte while
   meaning the same thing. Upstream identifiers were mechanically renamed
   (`primary_slot` etc.) in the same spirit.
3. Six registered semantic edits, all of the same shape (a function-body import hoisted
   to module scope, plus one deleted `pass`).

The register, the resync contract, and the check that enforces it are in
[`PORT_NOTES.md`](PORT_NOTES.md); the checker is
`scripts/pageindex_sync_check.py --upstream <checkout>`. Do not expect a clean three-way
merge against raw upstream files — run the checker instead, and read the upstream commit
history when changing anything.
