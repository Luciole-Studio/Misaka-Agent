# PageIndex source slice

- Upstream: <https://github.com/VectifyAI/PageIndex>
- Commit: `416e304f5150f7930fee8d2fe4d36e26caf57f35`
- License: MIT (`LICENSE.PageIndex`)

`flash/` is the deterministic PageIndex Flash PDF-outline pipeline. MISAKA keeps
the layout parser and bundled lookup data, replaces the public adapter, and omits
the upstream CLI, LLM summaries/optimization, examples, and generated results.

Local delta versus upstream `pageindex/flash/`: `api.py` is MISAKA's own adapter
(no LLM summary/optimize passes, ``workers`` forwarded to ``extract_toc``); the
upstream ``README.md`` and ``assets/`` are not vendored. Everything else is a
verbatim copy. Note that upstream identifiers were mechanically renamed
(``primary_slot`` etc.); read the upstream commit history when changing anything.
