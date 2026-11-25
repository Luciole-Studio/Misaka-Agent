# Runtime traces

Use `misaka trace last` to inspect the latest chat session or Research run. Use `misaka trace show ID --json` to retrieve the machine-readable timeline, tool operations, request counts, known usage and Research budget data. In a chat the trace ID is the session ID. In Research it is the existing run ID. Each prompt has its own execution ID within that trace. These commands read existing records even when a run is interrupted.

By default, Misaka keeps run and model-request outcomes, retries and budget refusals. It summarizes successful built-in `read`, `ls`, `find` and `grep` calls per turn. It records other tool attempts individually before entering the executor. For a more detailed trace, start with `misaka chat --diagnostics`, `misaka panel --diagnostics` or `misaka research --diagnostics "question"`. From the panel, `/research --diagnostics question` enables it for a new Research run. Research continuations and native workers inherit the saved mode. In a reopened chat, detailed recording starts with the new execution.

## Read an interruption

Here is a shortened timeline for a file write:

```text
model_started
model_finished toolUse
tool_entered write call-7
```

The `unknown` operation means Misaka recorded the attempt before calling the tool, with no later outcome in the trace. Check the destination before deciding whether to repeat the operation. `returned` means the executor reported a result. `receipt_saved` means a corresponding entry was observed in the persisted session. `not_executed` means the runtime recorded a rejection before invoking the tool. A nested tool can return without a separate session entry. The outer tool owns its transcript receipt.

`transcript_persist_error` means the session could not save a message. The tool's recorded execution result remains available, and the run stops automatic continuation. `model_finished` describes the provider request. `run_finished` records how the whole run ended, including failures and cancellation after a successful model response.

In Research, `board_events` shows confirmed submissions and settlements with Board event IDs, card generations and artifact digests. A submission can be confirmed even when the worker stops before returning. Its settlement appears when the coordinator records it. This evidence is available from older v18 Board records before migration. When some Board evidence cannot be read, the report sets `board_incomplete` and shows the records it could read. The Board is read in a single read-only SQLite transaction. SQLite may manage its WAL and shared-memory files during the read. When part of the trace inventory cannot be read, `trace last` reports the gap and shows the latest readable trace it found.

For a card using local files and a restricted tool set, a successful completion tool can end its own turn. After the turn settles, the worker records a preparation event on the Board before accepting the submission. When the worker dies between those writes, reconciliation verifies the same claim and the frozen submission before accepting it. A file written before that preparation still needs to be checked by the next attempt.

## Read usage and retries

The OpenAI-compatible adapter and Codex SSE transport record HTTP client attempts. An attempt can fail before reaching the service. An observed count of zero is distinct from an unavailable transport count, such as a request completed over the Codex WebSocket. The usage summary counts complete reports, partial reports, missing reports, conflicting fields and refused requests separately. `reported_tokens` includes validated entries. Inspect the individual usage event for the provider fields, normalized numbers and validation issues. Reasoning tokens are a component of output.

Research's budget summary is a snapshot of usage charged against the cap and current reservations. The expired-reservation figure is a portion of the charged total. `misaka usage --run RUN_ID` totals normalized session records. Use `misaka trace show RUN_ID` for validated Provider usage and counts of missing, partial or conflicting reports. New usage entries record their run when saved, including in a panel shared with ordinary chat. Older panel entries can be matched by request ID in the trace. The report labels any time-based estimate included in the total and shows entries with unresolved ownership separately. Native forks identify copied entries by their recorded seed boundary or legacy fork evidence. A separate unverified amount is shown when older fork records lack sufficient evidence.

When `/research resume RUN_ID --here` adopts a different panel, the Board records the binding change with the current root pointer. The usage breakdown includes confirmed entries from both panels, even after switching back. Older runs can recover earlier windows from the root command journal or from saved trace receipts matched to session headers in the catalog. When a transcript is empty, unreadable or damaged, the report flags incomplete history and keeps the valid subtotal. With several windows, legacy panel entries without ownership evidence stay outside the total. Board charges and reservations appear separately.

For a single shared panel, time-based estimates use the Board's binding periods when available. Entries in an overlap or the second of an uncertain binding stay outside the estimate. A legacy panel with no binding events can still use the recorded run order.

`budget_admitted`, `budget_wait` and `budget_refused` explain the decision before a model request. `budget_need` is the input bound plus minimum output and any separate thinking allowance. `budget_desired` uses the configured output limit. With a ledger snapshot, `budget_remaining` is the cap minus charged usage, `budget_held` is the amount held by other requests, and `budget_granted` is this request's reservation. Provider usage is reported separately when a response arrives. Budget events in older traces may have only the cap, used amount and required reservation.

Provider and session retries randomize locally calculated waits within their existing limits. A valid server `Retry-After` takes priority. A delay above the configured limit stops automatic retries and is recorded as `retry_delay_limit`.

Five consecutive transient logical failures within two minutes open the circuit on that session's model route for 30 seconds. A transient HTTP failure still counts when the provider asks the client to skip automatic retries. Invalid local URLs, protocols and request headers stop without retrying or counting against the route. The next foreground request after cooldown tests the route. Concurrent requests on the same route fail immediately while that trial runs. A successful trial restores normal requests. A transient failure starts a new cooldown. The trace records the opening, refusals and trial outcome. Research cards honor a Provider decision to stop automatic retries, including a deterministic HTTP error or explicit quota exhaustion. A card also stops after five consecutive rate-limit outcomes in the same generation. Use `misaka research --resume RUN_ID` after correcting the problem or when the service recovers.

Trace segments live under `state/runtime-traces/` in `MISAKA_HOME`. They store execution metadata, while the existing session files hold conversation and tool content. A malformed row marks the trace incomplete. The valid rows remain readable. If a trace file cannot be written, Misaka logs a warning and continues the underlying operation.
