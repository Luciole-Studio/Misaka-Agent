# Office regression suite

Historical MISAKA Office tests restored from the 2026-09-05 baseline snapshot, then adapted to
current by-node artifact registration, declaration-only ledger semantics, workspace-owned
intent archives, and current (non-exclusive) tool prompt guidance. Old expectations were not
used to revert newer Research or prompt governance.

`conftest.py` isolates HOME, corpus, runs, caches and intent archives. Required Python packages
are imported normally (missing dependencies fail collection). LibreOffice is stubbed for unit
coverage; these tests alone do not certify real rendering or formula execution.

`test_office_operation_coverage.py` exercises all 56 named format operations plus the
`format_cells` alias, saving and reopening real files after each step. The upstream comparison
harness reuses these programs against a separately supplied checkout at the exact recorded pin:

```sh
PYTHONPATH=. .venv/bin/python scripts/audit/office_upstream.py /path/to/FrontierAgent /new/audit/output
PYTHONPATH=. .venv/bin/python -m pytest tests/office -q
```

The harness does not fetch code or install dependencies. It executes the explicitly supplied
pinned source locally, without its sandbox main or macro installer. It reports OOXML part
hash differences; differences are evidence to review, not a blanket parity verdict.
