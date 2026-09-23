# MISAKA LCM plugin settings

The plugin owns its settings dialog and configuration adapter. No LCM-specific
option or loader was added to the generic settings page or core engine.

## Entry point

- `/lcm-settings`: native extension selector (available in the slash-command menu).
- `/lcm-settings on` / `off`: enable or disable automatic recall.
- `/lcm-settings status`: show effective preferences and cache boundary.
- `/lcm-settings check`: test the configured embedding provider with a synthetic
  readiness string, not conversation content.

The command is registered independently of `LCM_ENABLE_SLASH_COMMAND`.
Headless contexts get text status rather than attempting a terminal dialog.
The existing `/lcm` operator command remains opt-in and unchanged.

## Configuration and lifetime

Preferences are stored in the home, at `state/plugins/misaka-lcm/settings.json`
(normally `~/.misaka/state/plugins/misaka-lcm/settings.json`). This file contains
four preferences only, not task content. Writes are locked, atomic and mode 0600.
Invalid JSON/keys/types are reported rather than silently overwritten. Existing
`LCM_*` environment overrides still win, including explicit false/empty values;
conflicting overrides are reported instead of presenting a successful toggle.

Supported keys: `proactive_recall_enabled`, `embeddings_enabled`,
`embedding_provider`, `embedding_model`. Fresh installs remain default-off.
Enabling also enables embeddings; when no route exists, it uses the already-cached
local `fastembed` / `BAAI/bge-small-en-v1.5` route. The dialog saves an enabled state
only after a successful real embedding probe. It does not install dependencies or
download models. Disabling automatic recall does not disable manual semantic tools.

New-code runtimes reload these preferences at their next serialized LCM operation,
without replacing engines or changing transcript cursors. Processes started before
this implementation need one restart to load the new plugin code. No user process
is automatically restarted. Native session archives remain unchanged.

One default differs from upstream: `embedding_query_timeout_s` is 30 s here (upstream 3 s).
Upstream applies that deadline to `lcm_grep`'s full-text arm as well, interrupting the SQLite
query, and a research project's `lcm.db` grows to tens of megabytes, where 3 s dropped the
message-search arm in silence (2026-09-18, B32). `LCM_EMBEDDING_QUERY_TIMEOUT_S` still wins
when set; the vendored code is untouched.

## Actual automatic-recall path

The native `context` hook calls the original upstream recall builder once per
native user ingress, even when no compaction is needed. Retries/tool rounds reuse
the same transient result. A settings change invalidates that cached result.
Retrieved content is fenced as untrusted data and attached only to the outbound
copy of the user message. It is not written into a native compaction checkpoint
or added as a new raw LCM turn. The core assembler's automatic block is suppressed
by a host subclass so native requests do not inject or persist a duplicate.

For FastEmbed only, the plugin prepares profiles with the cached-only query API
and original VectorStore profile helpers. It invokes the original incremental
backfill for both summary and chunk corpora, up to 32 selected items per corpus
per new ingress. The foreground waits at most 2 seconds for this maintenance;
a late worker remains owned and is drained before SQLite/cache cleanup. Its
configuration is frozen. Original backfill leases, profile identities, chunking,
scoring, thresholds and the 500-token default recall budget are unchanged.

Only content already in the current project cache is indexed. This is not an
archive importer. Automatic raw-text indexing is not performed for cloud routes;
those retain the original explicit operator/consent workflow. Local model weights
are reusable assets, not conversation memory. The final project owner still
cleans `.misaka/lcm/`, including vectors and indexing bookkeeping, while preserving
plugin preferences and native session files.

No hit is a valid outcome: the original conversational policy skips turns below
40 estimated tokens, active-session/low-score hits are filtered, and an unfinished
index or failed retrieval injects nothing. The original pending-row scan is linear
in loaded cache size; no new indexing scheduler or ranking algorithm was invented.

## Verification (2026-09-15)

`tests/test_plugin_settings.py` is plugin-owned. It covers the settings dialog,
headless/cancel paths, false/env precedence, malformed settings/readiness failure,
hot updates without replay, ephemeral once-per-ingress recall, cloud-index exclusion,
real cached local embedding/recall with sockets blocked, and late-worker ownership.
The optional real-model test skips only when the dependency/model cache is absent;
it passed on the development machine with the existing 384-dimensional model.

Logs are in `/tmp/misaka-lcm-work/`: `plugin-settings-real4.log` proves the real
local model -> original backfill -> original recall -> non-compacting native request
chain, with hot disable and final-owner cleanup. The original short-message probe
correctly produced no injection because it did not qualify for the unchanged
40-token chunk policy. The automatic path does not call the operator warmup, which
permits network downloads; it uses cached-only embedding queries instead.

All production changes are inside `misaka/extensions/misaka_lcm/`. Outside the
plugin, one existing test harness gained its previously unused `registerCommand`
stub. `vendor/` and `native/` (342 files) remain byte-identical to this task's baseline.

Final isolated validation: **90 passed, 12 subtests** in focused/adjacent tests
(`plugin-settings-final-accept.log`, warnings are errors); strict main plus plugin
tests **2165 passed, 1 skipped, 130 subtests**, excluding only the already-documented
`test_research_method_handoff.py` SQLite ResourceWarning fixture
(`plugin-settings-strict.log`). Offline sdist/wheel build and cold import/status
passed; all 379 LCM wheel files matched the tested sources.


Published-worktree validation: **2170 passed, 1 skipped, 144 subtests**
(`plugin-settings-published-main.log`); **90 passed, 12 subtests** in the strict
focused/adjacent run (`plugin-settings-published-accept.log`). The unmodified
upstream proactive-recall tests separately passed **14/14**
(`plugin-settings-upstream.log`). Publication used original SHA guards and kept
the git index and 1900 unrelated existing files byte-identical.

The requested user preference was saved as automatic recall ON + embeddings ON,
using the cached local FastEmbed model, after the real readiness check succeeded.
A fresh configuration read confirms the effective values. No running user process
was restarted and no user LCM database was opened during preference activation.
