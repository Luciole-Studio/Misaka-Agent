---
name: misaka-lcm
description: Use MISAKA LCM to recover compacted conversation details, inspect context storage, and verify recalled evidence within the current project.
---

# MISAKA LCM

User-controlled settings: `/lcm-settings` opens the plugin's native dialog;
`/lcm-settings on|off|status|check` also works without the dialog. Automatic recall
uses the same loaded-project boundary below. The switch and effective embedding
route are plugin preferences, not saved conversation memory.

LCM is this project's disposable context cache, not a permanent user-memory service.
All roles belonging to the project share its cache; another project's data is not
searched. Native MISAKA session files are the durable archive. Reopening a saved
session reconstructs its context and source-backed summaries. The cache and its
payload sidecars are removed after the final project owner exits; abandoned caches
are cleared on the next start. Original session files remain unchanged by cleanup.
Starting a new session does not automatically import earlier session archives.

When asked about memory, separate capability from evidence. Available LCM tools
do not prove that earlier conversations are loaded. In `lcm_status`, native
`state_sessions_total` / `state_only_sessions` describe catalog identifiers, not
LCM conversation content. Check actual LCM session/content counts and retrieval
results before claiming access to a previous conversation. Status is not a project
file listing and does not prove that project files were inspected.

Use existing context first. For exact old wording use `lcm_grep` then `lcm_expand`;
for already-loaded cross-conversation history use `lcm_recall`. Summaries are
navigation aids, not exact evidence. Do not claim a memory was permanently saved.

For tool recipes, read the relevant upstream reference under
`../../../vendor/skills/hermes-lcm/references/`. Those references describe the
inherited tool algorithms; MISAKA owns storage paths and lifecycle as described
above. Tool names, argument schemas, `LCM_*` algorithm options, and upstream
copyright notices are retained. `misaka lcm status` addresses the current project.
