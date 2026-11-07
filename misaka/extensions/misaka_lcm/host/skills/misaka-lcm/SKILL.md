---
name: misaka-lcm
description: Use MISAKA LCM to recover compacted conversation details, inspect context storage, and verify recalled evidence within the current project.
---

# MISAKA LCM

User-controlled settings: `/lcm-settings` opens the plugin's native dialog;
`/lcm-settings on|off|status|check` also works without the dialog. Automatic recall
uses the same loaded-project boundary below. The switch and effective embedding
route are plugin preferences, not saved conversation memory.

LCM is this project's context cache, not a permanent user-memory service.
All roles belonging to the project share its cache; another project's data is not
searched. Native MISAKA session files are the durable archive. The cache stays between
runs, so a session loaded once can still be found after every process has exited.
Reopening a saved session reconstructs its context and source-backed summaries, and a
session whose rows no longer match its file is rebuilt from it, so a numeric handle
that is not found belongs to rows that were rebuilt: search again. A deleted session's
rows go when the project is next opened. `misaka lcm clear` removes the cache and its
payload sidecars; original session files are never touched. A session never opened
here is not in the cache: starting one does not import earlier session archives.

When asked about memory, separate capability from evidence. Available LCM tools
do not prove that earlier conversations are loaded. In `lcm_status`, native
`state_sessions_total` / `state_only_sessions` describe catalog identifiers, not
LCM conversation content. Check actual LCM session/content counts and retrieval
results before claiming access to a previous conversation. Status is not a project
file listing and does not prove that project files were inspected.

Use existing context first. For exact old wording use `lcm_grep` then `lcm_expand`;
for already-loaded cross-conversation history use `lcm_recall`. Summaries are
navigation aids, not exact evidence. Do not claim a memory was permanently saved.

Every stored row says who said it: its `source` is that session's address --
`last-order`, a Sister's name, `name@<card id>` when she worked a card
(`momo@t_3fa9c1`), `name/<agent id>` for a sub-agent. `lcm_grep(query,
session_scope="all", source=...)` searches one of them (the value is exact: take
it from a hit or build it from a card); a hit's `session_id` opens that
conversation with `lcm_load_session`. One LCM result is at most 25k tokens: a
larger transcript page comes back cut short with `has_more` and `next_cursor`,
and any other oversized result asks for a smaller request.

For tool recipes, read the relevant upstream reference under
`../../../vendor/skills/hermes-lcm/references/`. Those references describe the
inherited tool algorithms; MISAKA owns storage paths and lifecycle as described
above. Where they name Hermes's `session_search`, its configuration files or its
chat platforms, MISAKA has none of them: use `lcm_recall` or
`lcm_grep(session_scope="all")`, the `LCM_*` environment settings and
`/lcm-settings`. Tool names, argument schemas, `LCM_*` algorithm options, and
upstream copyright notices are retained. `misaka lcm status` addresses the
current project.
