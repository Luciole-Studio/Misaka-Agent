# The panel

When you type `misaka`, the **panel** opens: one terminal screen where Last Order and every
Sister she starts each run in a pane of their own, so you can watch several agents work at once
and step into any of them. If you have used tmux, it will feel familiar. On Windows, open it in
Windows Terminal.

This page explains what is on the screen, the keys and the mouse, and how to find your way
between agents, conversations and folders.

## What is on the screen

```text
┌──────────────────┬─ Last Order │ node-2 │ + ───────────────────────────┐
│ spaces           │                            │                        │
│ ● my-research    │  Last Order's window       │  Sister 10032          │
│   main ✓         │                            │                        │
│ new       prefix │                            │                        │
│──────────────────│                            │                        │
│ sessions    here │                            │────────────────────────│
│ ▾ Last Order   3 │                            │                        │
│ ▾ Sisters      5 │                            │  Sister 10033          │
│──────────────────│                            │                        │
│ agents   grouped │                            │                        │
│ ● my-research    │                            │                        │
│   10032          │                            │                        │
│ new  sisters   « │                            │                        │
└──────────────────┴────────────────────────────┴────────────────────────┘
```

- **Panes** fill the main area. Each pane runs one program: an agent's chat, a shell, or an ally
  such as Claude Code. The pane with the highlighted border has the keyboard.
- **Tabs** run along the top. A tab is one arrangement of panes. Last Order's tab holds her window
  and the Sisters working her cards; each research node gets a tab of its own. `+` opens a new
  tab with a shell.
- The **sidebar** on the left has three lists:
  - **spaces**: the folders you are working in (below).
  - **sessions**: your saved conversations.
  - **agents**: every pane that runs an agent, and what it is doing.

Click `«` at the bottom of the sidebar to fold it to a narrow strip, and `»` to open it again.

### What the dots mean

Each agent, conversation and space has a dot that tells you whether it needs you.

| Dot | Means |
|---|---|
| red `●` | **blocked**: someone must act. The agent is asking you something, or a card has failed or waits for review. |
| yellow `●` | **working** |
| teal `●` | **done**: finished, and you have not looked yet |
| green `○` | **idle**: finished, and you have seen it |
| `·` | not running (a saved conversation) |

A space's dot is the most urgent dot among its panes. To see everything that needs you at once,
use the [navigator](#finding-anything-the-navigator).

## The prefix key

The panel's commands all start with the **prefix key**, `ctrl+b`: press `ctrl+b`, let go, then
press the command key. A bar at the bottom shows that the panel is waiting for one. Every other
key goes to the pane you are in.

| After `ctrl+b` | Does |
|---|---|
| `?` | show these keys |
| `1` … `9` | go to tab 1 … 9 |
| `n` / `p` | next / previous tab |
| `h` `j` `k` `l` | move to the pane on the left / below / above / on the right |
| `c` | new tab, with a shell |
| `v` | split the pane side by side, with a shell in the new half |
| `-` | split the pane top and bottom |
| `z` | zoom: the pane fills the tab; again to go back |
| `g` | the [navigator](#finding-anything-the-navigator): search every space and pane |
| `[` | [copy mode](#copying-text): move through the pane's history, search and copy |
| `r` | [resize mode](#resizing-panes) |
| `T` / `W` | rename this tab / this space |
| `x` | close the pane you are in, and stop what runs in it |
| `d` | quit the panel: every agent stops |
| `ctrl+b` | send `ctrl+b` itself to the pane |
| `esc` | never mind |

With the mouse, the `prefix` button at the bottom of the spaces list does the same as pressing
`ctrl+b`.

**Using tmux?** tmux takes `ctrl+b` for itself. Choose another prefix in
`~/.misaka/settings.json`, such as:

```json
"panel": {"prefix": "ctrl+g"}
```

The chat box inside an agent's pane has keys of its own, for sending, stopping a reply and so on.
Type `/hotkeys` there to list them, or see the [command reference](../reference/commands.md#keys-in-the-chat-box).

## The mouse

- **Click** a tab, a pane, or an entry in the sidebar to go there.
- **Drag** across text to copy it; **double-click** a word to copy the word. A note confirms
  "copied to clipboard".
- **Drag a border** between panes to resize them.
- **Scroll** over a pane to move through its history, and over a sidebar list to scroll the list.
- Click the header toggles: `here` / `all` above the sessions, `grouped` / `priority` above the
  agents.

## Working with the agents

**Last Order** is in the first tab. When she starts cards, each Sister working one appears in a
pane beside her, and Last Order closes the finished ones once she has read the results. Click a
Sister's pane (or move to it with `ctrl+b` and `h` `j` `k` `l`) to watch her or type to her.

**To talk to a Sister yourself**, click `sisters` at the bottom of the agents list and pick one:
she opens in a pane of the tab you are in. From Last Order's chat, `/sister 10032` does the same.

**To start a fresh conversation with Last Order**, click `new` at the bottom of the agents list.
It opens in a space of its own, next to the one you were in.

**Branching a conversation.** `/fork` (branch from an earlier message of yours) and `/clone`
open the new branch in a tab of its own, so both conversations stay open side by side. In the
sessions list the branch sits under the one it came from.

**Research runs** give each node below your question a tab of its own, with that node's Last
Order and her Sisters. See [Research runs](research.md#following-a-run).

**The agents list** shows every pane that runs an agent, with its space, tab and name. Click one
to jump to it. `grouped` lists them by space; click it to switch to `priority`, which puts
whatever needs you first.

## Spaces

A **space** is a folder you work in, with its own tabs. The first space is the folder where you
typed `misaka`; the list shows its git branch and status when the folder is a git repository.
Click a space to switch to it.

`new` at the bottom of the spaces list opens another space in the same folder, starting with a
shell. From that shell you can `cd` elsewhere and start `misaka chat` to work in another folder.

Spaces are remembered: when a saved conversation belongs to a space you have closed, reopening the
conversation brings the space back.

## Sessions: your saved conversations

The sessions list holds every conversation you have had, newest first, with its age. `here` shows
this space's conversations, Last Order's above the Sisters'; click it to switch to `all`, which
groups every conversation by the folder it was in. A conversation branched from another sits
under it, marked `↳`.

Click a conversation to go back to it:

- if it is open in a pane, you go to that pane;
- a saved conversation with Last Order or a Sister opens again where you left off, ready for your
  next message;
- a conversation that belongs to a card or a research node opens as a window onto it: you can read
  it, and talk to the agent while she is still running. Her work carries on as it was.

## Finding anything: the navigator

`ctrl+b` then `g` opens the navigator, a list of every pane in every space with what it is doing.

| Key | Does |
|---|---|
| `j` / `k`, arrows | move |
| `enter` | go to the pane |
| `/` | search by name, space, branch or folder |
| `b` `w` `i` `d` | show only blocked, working, idle or done panes |
| `a` | show all again |
| left / right | jump to the previous / next space |
| `esc` | close |

## Copying text

The quickest way is to drag across text with the mouse. For longer passages, or text that has
scrolled off the screen, use **copy mode**: `ctrl+b` then `[`.

| Key | Does |
|---|---|
| `h` `j` `k` `l`, arrows | move the cursor |
| `w` `b` `e`, `{` `}` | move by word, by paragraph |
| `ctrl+u` / `ctrl+d`, `ctrl+b` / `ctrl+f` | half a page, a page up / down |
| `g` / `G` | the top / the bottom of the history |
| `0` `^` `$` | start of line, first character, end of line |
| `/` `?` | search down / up; `n` / `N` for the next match |
| `v` or space | start selecting; `V` selects whole lines |
| `y` or `enter` | copy the selection and leave |
| `esc` | clear the selection or search; again to leave |
| `q` | leave |

## Resizing panes

Drag the border between two panes, or press `ctrl+b` then `r` and use `h` / `l` for width and
`j` / `k` for height. `esc` when done. `ctrl+b` then `z` zooms one pane to fill the tab.

## Closing and quitting

- `ctrl+b` then `x` closes the pane you are in and stops what runs in it. Closing the last pane
  quits the panel.
- `ctrl+b` then `d` quits the panel.

**Quitting stops everything.** Closing the panel, or the terminal window it is in, shuts down Last
Order and every Sister. Your conversations are saved and wait in the sessions list, and a
research run is saved as it goes, so `/research resume` picks it up later.

One panel runs at a time. In a shell inside one of its panes, start a conversation with
`misaka chat`.

## Without the panel

Where the panel cannot run, `misaka` opens a plain chat with Last Order. You can also choose
plain chat yourself: `misaka chat` for Last Order, `misaka chat --as 10032` for a Sister. Research
works there too, with its other nodes running in the background
([Runs started from the shell](research.md#runs-started-from-the-shell) shows how to reach them). [Troubleshooting](troubleshooting.md#the-panel) covers
the panel's error messages.
