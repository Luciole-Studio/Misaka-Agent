# Troubleshooting

Find what you see, then do what the entry says. If nothing here fits, see
[Reporting a problem](#reporting-a-problem) at the end.

## Installing and signing in

**`misaka` opened without the setup wizard.**
MISAKA found a sign-in for your default provider, such as `ANTHROPIC_API_KEY` in your shell, and
took you as set up. Run `misaka setup` yourself: research needs at least one Sister, which the
wizard creates.

**`No API key found for ...`**
The model you picked has no sign-in. Type `/login` in chat, or run `misaka setup model`.
`misaka auth check` shows which providers are ready.

**`Authentication failed for "..." ... Run '/login ...'`**
The sign-in has expired, or the network is down. If the network is fine, run `/login` for that
provider again.

**`The ... package is not installed`**
The SDK for that provider is missing. Reinstall with the `providers` extra:

```sh
uv tool install --force "misaka[providers] @ git+https://github.com/Luciole-Studio/Misaka-Agent.git"
```

**The wizard says the test request failed.**
The key was saved, but the provider refused it: a typo, an expired key, or no credit left. Fix
the key and run `misaka setup model` again.

**`... is required for this but was not found on PATH`**
ripgrep or fd is missing. Install it ([step 1 of Getting started](../getting-started.md#1-install-what-misaka-needs))
and try again.

## The panel

**`The panel's terminal library (libghostty-vt) is not available ...`, and plain chat opens.**
The line below the message names the file MISAKA tried and why it would not load; usually there
is no panel build for this system. Everything works in plain chat, one agent at a time. If you
build libghostty-vt yourself, point `MISAKA_GHOSTTY_VT` at it.

**`The panel could not start: ...`**
The rest of the message says why. Meanwhile `misaka chat` opens plain chat with Last Order.

**`Another MISAKA panel is already open.`**
One panel runs at a time. Switch to the terminal window where it is open, or quit it there
(`ctrl+b`, then `d`).

**`already inside a pane; panels cannot be nested.`**
You typed `misaka` in a shell inside the panel. Use `misaka chat` there, or
`misaka chat --as 10032` for a Sister.

**`The daemon is an older version and still has a running task card in a pane.`**
You updated MISAKA while a card was running under the old version. Let the card finish, then open
the panel again; the old background process is replaced. If the card can be dropped,
`misaka net stop` ends it at once.

**All the panes closed, and the panel exited.**
The programs in them ended. Their last lines are in `~/.misaka/logs/panel-crash.log`. Run
`misaka` to open the panel again; your conversations are saved and listed in the sidebar.

**I closed the panel and everything stopped.**
Closing the panel, or its terminal window, stops every agent. Research runs are saved as they go
and can be resumed; see below.

## Research runs

**`The Sister roster is empty; research tasks cannot be assigned.`**
Create at least one Sister, then start or resume the run:

```sh
misaka create 10032 --desc "History and social research"
```

**`... is your home directory; a project has to be a folder of its own.`**
Make a folder for the research and run MISAKA there:

```sh
mkdir ~/research
cd ~/research
misaka
```

**`Research run ... needs clarification`**
Last Order needs an answer from you before she can plan. Reply with the command shown:
`/research resume RUN_ID YOUR ANSWER`.

**`Research run ... paused: ...`**
Something outside the research went wrong, and the rest of the message says what. Fix it, then
`/research resume RUN_ID`.

**A node failed.**
Last Order reports it in your window. If the cause was passing, such as a provider outage, she
can run the node again straight away, up to three times. Otherwise the run ends with a partial
report once the level finishes; fix the cause and `/research resume` continues.

**Some of a Sister's cards failed.**
A card gets up to three attempts; a sign-in, quota or configuration problem fails it at once.
The node's Last Order then decides: send the cards back to their Sisters (up to twice per node),
carry on with the research cards that finished, or give up on the node. Her decision appears in
the node's tab, or in your own window for the root node.

**`Research run ... belongs to the Last Order conversation ...`**
Resume a run in the conversation that started it. Open that conversation (the message names it,
and the sidebar lists it under sessions), or add `--here` to resume in the current window, whose
Last Order starts without the run's earlier conversation.

**`Research run ... is already being driven by another process`**
The run is still going in another window or terminal. Use that one, or stop it there first.

**The run stopped with `The run reached research.token_cap`.**
The run has spent its `research.token_cap` from `settings.json`, or its next request would not
fit in what is left. Raise the cap, or set it to `0` for no cap, and resume the run.

**A window or a card froze and did not come back.**
Every MISAKA process writes the stack of an event loop stuck for over five minutes to
`~/.misaka/logs/stalls/`. A card, node or sub-agent nobody is watching then exits, and is
retried; its failure reason names the line it was stuck at. Attach that log to a bug report.
Logs are kept for 14 days. A process stopped from outside for longer than five minutes (SIGSTOP, a
debugger) is taken for stuck when it continues; the TUI's own Ctrl+Z is not.

**A resumed run can't find a Sister.**
A card was given to a Sister you have since removed. Create her again with the same number
(`misaka create ID`), then resume.

**A file named `...-draft.edited-<number>.md` (or `...-survey.edited-...`) appeared in `final/`.**
The run's saved draft was changed outside the phase that writes it, so MISAKA put the saved
version back and kept the changed one under this name. Nothing is lost: what the change meant to
fix is weighed in the final adjudication. While a run is unfinished, the agents' file tools leave
its saved conclusions, survey and draft alone.

**The report is called `...-partial.md`.**
The run ended before its final report: you stopped it, the budget ran out, a node failed, or the
final review failed. The partial report holds every node's latest conclusion.
`/research resume` carries on from where it stopped.

## Models and long conversations

**`Retrying (1/3) in 2s...`**
The provider is busy or rate-limiting. MISAKA waits and tries again. Esc cancels.

**`Retry failed after 3 attempts`**
The provider kept refusing. Wait a little, or switch model with `/model`.

**`You have hit your ChatGPT usage limit`**, or another quota message.
Your subscription or credit is used up for now. Wait, or switch provider.

**`Context overflow recovery failed ...`**
The conversation no longer fits the model even after compaction. Start a new one with
`/new --carry` (it keeps a summary), or switch to a model with a larger context window.

## Messages between agents

**A message to a Sister seems lost.**
Messages wait until their recipient reads them. One sent to a Sister by name, outside a research
run and with no session of hers open in the sender's space, waits for her contact session; to
deliver it now:

```sh
misaka dm 10032
```

Her answer goes back to the window that wrote; so does a failure, as
`Delivery to 10032 failed [reason: …]: …` (a rate limit or a server error is retried once first).
Her turn has no time limit of its own: it is stopped only after 30 minutes without any activity
(`misaka dm --timeout`). One sent to a card that has not started waits for
it to start; one sent to a running card or a live session arrives at its next tool boundary, which
can take a while if it is in the middle of a long step. A finished card is woken by the message,
so its answer comes after a new turn of hers.

**`Unknown recipient ...`, or `... is N live sessions in this space`**
Address a Sister by her number (`10032`). If several of her sessions are open, name one by its
card ID or session ID; the message lists them.

## Allies (Claude Code, Codex)

Run `misaka allies`. It checks each ally without spending any quota:

| Status | What to do |
|---|---|
| `ready` | nothing |
| `not installed` | install Node.js, which provides `npx` |
| `needs login` | sign in to Claude Code or Codex the usual way, in a normal terminal |
| `cannot start` | the detail says why |

`No ally is enabled` means `settings.json` has no usable `allies` entry yet; see
[the team guide](team.md#allies).

## Updating and your data

**`... written by a newer MISAKA ... Update MISAKA`**
You opened data with an older version than the one that last wrote it. Run
`misaka update --apply`. To go back to an older version, restore the copy the update saved in
`~/.misaka/state/backups/` as well.

**`misaka update --apply` refuses: `Work is running that an update would cut off`**
Let the research run or card finish, or stop it (`/research stop`), then update.

**`settings.json has values MISAKA cannot use`**
A setting has the wrong type, such as text where a number belongs. The message names each one;
fix or remove it. `misaka setup`, `update` and `uninstall` still run meanwhile.

**`Warning: Invalid settings file ...`**
The file is not valid JSON, often a missing comma or quote. The message gives the position.

## Windows

**Text shows as boxes or question marks.**
Use Windows Terminal. The fonts of the old console window have no letters for most scripts;
MISAKA itself reads and writes UTF-8.

**`winget install` finished, but the wizard still reports the tool missing.**
A terminal keeps the PATH it was opened with. Open a new terminal and run `misaka setup` again.

**Shift+Enter sends the message instead of starting a new line.**
The terminal did not report Shift with Enter. `ctrl+j` starts a new line everywhere.

**`misaka update --apply` stops with `os error 32`.**
Versions 0.18.1 and 0.18.2 cannot replace the `misaka.exe` they run from. The new version may
already be installed; either way, close every MISAKA window and run `uv tool upgrade misaka`
once. From 0.18.3 on, `misaka update --apply` works on Windows.

**`misaka uninstall` says MISAKA is still running in another window.**
Windows cannot remove a program or files that are in use. Close the windows it lists and run it
again. The program goes once the command has exited, and what `uv` reported is written to the
log file it names.

## Reporting a problem

1. In the chat window where it happened, type `/debug`. It saves the screen and the whole
   conversation to `~/.misaka/logs/misaka-debug.log` and prints the path. The file holds your
   conversation, so read it before you share it.
2. Look in `~/.misaka/logs/` for the rest:

   | File | Holds |
   |---|---|
   | `misaka.log` | warnings and errors from MISAKA and the panel |
   | `panel-crash.log` | the last lines of panes that closed, and panel crashes |
   | `misaka-crash.log` | crashes of the chat window itself |
   | `mcp/mcp.log` | the messages MCP servers log |

3. Note what you typed, what you expected and what happened, and include `misaka --version`.
