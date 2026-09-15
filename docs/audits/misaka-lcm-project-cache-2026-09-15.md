# MISAKA LCM: project-runtime cache acceptance

## Boundary

- The fork is `misaka.extensions.misaka_lcm`; no legacy import alias or global DB fallback.
- A session's project owns `.misaka/lcm/`. Native children inherit that project, including workers running in worktrees. A native `lcm-project` metadata entry preserves this identity on resume.
- Different projects never share this cache. Multiple live project owners share it; the final owner closes engines and background SQLite workers before removing the cache and content sidecars. Abrupt termination is cleaned on the next admission, not immediately at SIGKILL.
- The adjacent gate and activity files retain only synchronization state, not task content. Unknown directories and symlinks are not deleted.
- Native session JSONL, compaction checkpoints, Board state and project outputs are not disposable. Resume rebuilds from native entries. Explicit carry receipts use source session/entry identity and digest, not the retired DB's row IDs. Missing/changed source archives raise an error rather than guessing.
- Numeric tool handles are cache-generation-local. Search again after restart; native checkpoints rebind their sources automatically. Retired database-row-only carry receipts have no compatibility fallback.
- Import/backfill CLI content paths and generated manifests/ledgers are project-owned. Arbitrary global path environment overrides no longer apply. Explicit user-requested exports/backups are separate operator actions, not automatic durable memory.

## Core preservation and naming

`CORE_INTEGRITY.json` records the pre-change `vendor/` and `native/` bytes. The executable gate in `tests/test_project_lcm.py` verifies every entry after normalizing only `misaka.extensions.hermes_lcm` / `misaka/extensions/hermes_lcm` to the relocated namespace. Compression, summary, retrieval algorithms and tool argument schemas are unchanged by this work.

Host package, runtime identity, registered skill and product docs use MISAKA LCM. Original repository URLs, author/license notices, upstream manifests/test vocabulary, algorithm settings and host API parameter names remain as provenance/protocol identifiers. They were not blindly replaced inside the core.

## Executable coverage

`tests/test_project_lcm.py` covers path override rejection, role sharing and project isolation, worktree identity, source-backed checkpoint restoration, chained carry after reordered DB IDs, source tampering/missing archives, unknown directory protection, cross-process final-owner cleanup, SIGKILL orphan recovery, interrupted initialization, failed initialization retry, process atexit, real compression/checkpoint/fork with only the model response stubbed, reload versus quit, operator path confinement, actual JSONL import/externalized payload recovery, product status and byte/schema invariants.

Adjacent tests cover replay/no-op identity, Sisters/Research context governance and real native child startup/resume. Test providers use local fixtures; no paid provider requests or live tasks were run.

## Validation evidence

Logs are under `/tmp/misaka-lcm-work/`.

- Final shared-worktree main suite: **2048 passed, 1 skipped, 144 subtests** (`published-suite.log`). The isolated snapshot had **1714 passed** (`publish-suite.log`); the shared tree also includes the concurrent Office/Web test additions preserved during publication.
- Final focused/adjacent acceptance: **60 passed, 12 subtests** with warnings treated as errors (`published-acceptance.log`); the operator abbreviation-path regression also passes.
- The main suite with warnings promoted to errors, excluding only the pre-existing leaking test file: **2043 passed, 1 skipped, 130 subtests** (`published-strict.log`).
- The original dirty-tree baseline itself fails strict warnings because `tests/test_research_method_handoff.py:49` uses a SQLite connection context manager without closing the connection. `main-trace.log` traces its allocation; `base-tests.log` reproduces it before this change. That unrelated file was not edited.
- The complete vendored suite was run against both snapshots with the same isolated-host alias harness: baseline **3010 passed / 18 failed**, fork **3008 passed / 20 failed**, both **2 skipped / 12 xfailed**. The 18 failures are shared baseline assumptions about Hermes packaging/log names, macOS paths, or pre-existing vendored adaptations. The two additional assertions demand arbitrary externalization environment paths, intentionally removed by the new project-owned path contract. The host import test separately proves full payload preservation at the new owned location. This is not a claim of a fully green upstream suite.
- Scoped Ruff and `git diff --check` pass. Offline source/wheel build succeeds. Cold import and CLI status/help from the unpacked wheel find only the new package, include the new skill/integrity manifest, ignore an old-global-path sentinel, and leave no content cache after exit.

Publication uses original-file hash guards plus `git apply --check`. Concurrent Office/Web changes are preserved; the overlapping notice/build files receive only the scoped LCM hunks. No commit or staging is performed in the shared worktree.

## Completed cutover

- Guarded publication and byte comparison succeeded. The main index was not staged. The relocated package contains only the tested source; 369 obsolete old-namespace bytecode files were removed.
- Rechecked with `lsof` before deleting the unused global `~/.misaka/lcm.db` and its 115-file `lcm-large-outputs` directory (92,930,735 bytes total). No replacement backup or global fallback was created. The native history JSONL inventory remained byte-identical before and after deletion.
- Built the final shared-worktree wheel offline and compared the packaged storage/operator modules, skill and integrity manifest to the published files byte-for-byte. Cold status identifies `misaka-lcm` and `project-runtime`; normal exit removes its content cache.
