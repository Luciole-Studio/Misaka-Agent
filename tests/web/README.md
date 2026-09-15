# Web regression fixtures

Run from the repository root:

```sh
PYTHONPATH="$PWD" .venv/bin/python -m pytest tests/web -q -W error
```

The suite redirects MISAKA state to disposable directories. Provider calls are
mocked; no account credentials or paid services are required. `test_web_repairs.py`
covers the September audit repairs and native extract-dispatch timeout behavior.
`test_vault_native.py` covers host lifecycle, masked input, redaction and permissions.

`fixtures/hermes_990473a/` contains verbatim source from Hermes Agent commit
`990473a79c6b0396b0a648fdd85ee8f7a5c267d3` (MIT, see its `LICENSE` and
`PROVENANCE.json`). Schema and selected-symbol comparisons execute this fixed
fixture, not an installed Hermes checkout. The upstream Firecrawl error-envelope
characterization intentionally keeps the upstream behavior visible.

`test_vault_upstream.py` adapts selected tests from
`tests/tools/test_browser_vault.py` at Hermes commit
`62e5f466565ee56351e4483ead8e62f9e782f8b3` (MIT). Imports and host callbacks are
mapped to MISAKA; this is selected coverage, not the complete upstream suite.
Production source lineage is in `misaka/core/web/browser/vault/PROVENANCE.json`.

The real-browser regression is opt-in and creates only fresh browser profiles
and loopback fixture pages. Supply already-installed executables explicitly:

```sh
MISAKA_TEST_BROWSER_COMMAND=/absolute/path/to/agent-browser \
MISAKA_TEST_CHROME=/absolute/path/to/chromium \
PYTHONPATH="$PWD" .venv/bin/python -m pytest tests/web/test_vault_browser_live.py -q -W error
```

It exercises password and OTP fills, masked input, readback/dialog redaction,
origin changes, DOM reordering, cross-site iframe retirement, owned/shared page
focus rules, cancellation and profile cleanup. It does not test real password
manager accounts, cloud-browser billing, personal browser profiles or Windows.

## Deliberate host differences

- `WebRuntime` owns CDP, subprocesses, cancellation and temporary resources;
  synchronous vault handlers use a worker bridge and awaited cleanup.
- Vault secrets are kept in a scope-owned redaction set, separate from the
  bounded API-key rotation cache; unlock tokens are session-owned.
- Secret input uses the existing TUI custom-dialog interface. Headless contexts
  do not collect credentials, and payment fills require a UI confirmation.
- Secret-bearing evidence retains a private original under the profile's
  `web-evidence/originals/`; the workspace receives a redacted copy. Ordinary
  readers/indexers reject originals and vault storage. This is not an OS sandbox.
  Historical evidence is not migrated by this change.
