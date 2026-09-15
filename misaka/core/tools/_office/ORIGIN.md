# Office source and native adaptations

Comparison baseline: [ApodexAI/FrontierAgent](https://github.com/ApodexAI/FrontierAgent/tree/9e533db6f6c34d16037ee5ec964c479d0eb51cde), Apache-2.0.
`PROVENANCE.json` pins source hashes; `LICENSE.frontier-agent` contains the upstream license.
This is a capability port, **not** a claim that these Python files are unmodified upstream files.
Upstream credits Mercor-Intelligence/archipelago (Apache-2.0) as feature-set inspiration;
FrontierAgent's addressing and parameter design is its own.

| Upstream | MISAKA |
|---|---|
| `_writer_core.py` | `_office/__init__.py`, `_receipt.py`, `_runs.py`, `_intent.py` |
| `_writer_docx.py`, `_writer_xlsx.py`, `_writer_pptx.py`, `_writer_text.py` | `_office/docx.py`, `xlsx.py`, `pptx.py`, `text.py` |
| `create_file.py` | `tools/office.py`, `_office/paths.py`, `_office/schema.py`, subagent path policy |
| `_reader_xlsx.py`, `_reader_pptx.py` | `documents/office/xlsx.py`, `pptx.py` |
| `_reader_docx.py` | `documents/office/docx.py`: native XML renderer, not the pandoc subprocess |
| `_reader_core.py`, `read_file.py` | `documents/office/{cache,paging,soffice}.py`, `tools/read.py`, `documents/index.py` |

Native adaptations: workspace ownership, error envelopes, worker threads and mutation queues;
staged writes/exports and rollback on ordinary errors (not a multi-file crash transaction);
quote-preserving text rendering; read-only recalculation copies; private LibreOffice profiles;
mandatory Python dependencies instead of runtime pip installs; no global pivot-parser patch.
VBA variants are read-only. LibreOffice remains optional; full formula calculation, legacy
conversion and PDF export require a working installation, not merely a PATH wrapper.

The public `read` API retains MISAKA's line offsets and byte/line limits, and has no `save_to`.
Office formatting extraction is not visual-layout verification. Upstream PDF OCR/VLM and image
batch/gateway/sandbox facilities belong to separate MISAKA stacks and are not claimed here.

Audit and executable boundaries: `docs/audits/office-frontieragent-2026-09-15.md`;
regressions: `tests/office`; fixed-source differential harness: `scripts/audit/office_upstream.py`.
