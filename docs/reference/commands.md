# Commands

There are two kinds of command. **Terminal commands** start with `misaka` and are typed in your
shell. **Chat commands** start with `/` and are typed in a chat window, where `/` on its own
lists them all.

`misaka COMMAND --help` shows every option of a terminal command.

## Terminal commands

### Starting and chatting

| Command | What it does |
|---|---|
| `misaka` | open the panel (`misaka panel` does the same). Runs the setup wizard first when your default provider has no sign-in; opens plain chat when the panel cannot run. |
| `misaka setup [SECTION]` | the setup wizard, or one section of it: `environment`, `model`, `sisters`, `skills`, `documents`, `web`, `research`, `project` |
| `misaka chat` | chat with Last Order without the panel (`--model` for another model, `-s NAME` to load a skill) |
| `misaka chat --as 10032` | chat with one Sister |
| `misaka chat --continue` | carry on the most recent conversation |
| `misaka chat --pick` | choose a past conversation to reopen |
| `misaka chat --attach --session PATH` | join a session that is already running, such as a research node |
| `misaka dm 10032 "MESSAGE"` | send one message to an agent's contact session and wait for her reply; without a message, deliver what is waiting for her |
| `misaka --version` | the installed version |

### Research

| Command | What it does |
|---|---|
| `misaka research "QUESTION"` | start a research run in this folder |
| `misaka research --resume RUN_ID` | resume a run |
| `misaka research --tell RUN_ID [--to NODE_ID] "MESSAGE"` | say something to a running node's Last Order, the root's by default |
| `misaka research --limits RUN_ID --sister-parallel 2 …` | change a running run's limits, with the options below |
| `misaka usage [--run RUN_ID]` | what recent runs spent, or one run by conversation and model |
| `--depth N` | how many levels below the question (default 3) |
| `--parallel N` | nodes running at once (default 4) |
| `--sister-parallel N` | Sister cards per node at once (default 4) |
| `--followups N` | extra rounds a node may send its Sisters out (default 2) |
| `--revisions N` | times a node may revise its conclusion in each review loop, the red team's and the divergence review's (default 2) |
| `--max-nodes N` | nodes in the whole run (default 30) |

### The team

| Command | What it does |
|---|---|
| `misaka create [ID]` | create a Sister. Options: `--desc "SPECIALTY"`, `--model provider/model`, `--thinking LEVEL`. In a terminal it asks for what you leave out. |
| `misaka remove ID` | remove a Sister's profile; her cards, conversations and workspaces are kept (`--yes` skips the question). Refused while she has unfinished cards. |
| `misaka allies` | list the enabled allies and whether each can start |
| `misaka moa list` / `configure` / `delete` | Mixture-of-Agents presets |
| `misaka board` | show the task board |
| `misaka task add TITLE --to SISTER --body-file FILE` | add a card by hand (`--body TEXT`, `--reviewer`, `--priority`, `--model`, `--needs ID`); the body needs `## acceptance criteria` |
| `misaka task start ID` | run a ready card in this terminal |
| `misaka task ID --delete` | delete a card and its history |

### Documents and the web

| Command | What it does |
|---|---|
| `misaka init` | make this folder a project: a git repository with `PROJECT.md` and `cards/` |
| `misaka doc scan [FOLDER]` | index every readable file in a folder |
| `misaka doc add FILE` | index one file (`--no-tree` skips the outline) |
| `misaka doc list` | indexed documents and their IDs |
| `misaka doc find "WORDS"` | where a phrase occurs (`--doc ID` limits it to one document) |
| `misaka doc verify "QUOTATION" --doc ID` | find the page a quotation is on |
| `misaka doc tree ID` | a document's outline |
| `misaka web` | web search settings, as a menu |
| `misaka web status` | what is configured, without any network call |
| `misaka web set KEY VALUE` / `unset KEY` | change one web setting |

### Reading

Reading actions work on materials with a question; see the [reading guide](../guide/reading.md).
The engine has to be built first (`npm --prefix packages/reading run build`), and these commands
need a text model.

| Command | What it does |
|---|---|
| `misaka read open SOURCE... --question Q` | index the materials and create a run; `--intent TEXT` records what you want from it |
| `misaka read act RUN_ID ACTION --node NODE_ID` | run one of the eight actions on a node (`--focus`, `--lens`, `--context`, `--question`) |
| `misaka read fork RUN_ID --node NODE_ID --lens "A" --lens "B"` | split a node into independent lines read in parallel |
| `misaka read compare RUN_ID NODE_ID NODE_ID ... --question Q` | compare two or more nodes in a new node |
| `misaka read reread RUN_ID --node OLD --using NEW` | reread older material with a newer understanding |
| `misaka read graph RUN_ID` | the reading map and the node files |
| `misaka read chat RUN_ID` | talk with the run's leader; Ctrl+C or `/stop` leaves |

`--workspace`, `--runs-dir`, `--json` and `--model provider/id` are accepted where they apply.

### Skills

| Command | What it does |
|---|---|
| `misaka skills` | list the skills a role sees (`--as sisters/10032` chooses the role) |
| `misaka skills pending` / `approve ID` / `reject ID` | review skills waiting for your approval |
| `misaka skills hub-search` / `hub-install NAME` | find and install skills from the catalog |
| `misaka skills mode off\|forbid\|ask\|allow` | whether agents may write skills |
| `misaka bundles list` / `show` / `save` / `delete` | named sets of skills for a role |

`misaka skills --help` lists the remaining operations.

### Signing in

| Command | What it does |
|---|---|
| `misaka auth check` | which providers have a sign-in and their SDK installed (sends no request) |
| `misaka auth check --provider NAME` | one provider, refreshing an expired browser sign-in |

### The panel's background process

The panel runs on a background process that owns every pane. You rarely need these; all but `stop` start the process if it is not running.

| Command | What it does |
|---|---|
| `misaka net status` | is it running, and how many panes |
| `misaka net panes` | list the panes |
| `misaka net read PANE` | print the last lines of a pane (`--lines N`) |
| `misaka net send PANE "TEXT"` | type into a pane |
| `misaka net close PANE` | close a pane and stop what runs in it |
| `misaka net explain [PANE]` | why a pane counts as busy or idle |
| `misaka net stop` | stop it and close every pane |

### Maintenance

| Command | What it does |
|---|---|
| `misaka update` | report whether this install is behind |
| `misaka update --apply` | update, after saving a copy of your settings and board |
| `misaka uninstall` | remove the program and keep your data |
| `misaka uninstall --full` | remove the program and your data |
| `misaka uninstall --data` | remove your data and keep the program |
| `misaka uninstall --dry-run` | list what would be removed (`--yes` skips the questions) |

On Windows, `misaka uninstall` refuses while MISAKA runs in another window, and removes the
program once it has exited.

## Chat commands

### Research

Available in Last Order's window.

| Command | What it does |
|---|---|
| `/research` | choose the run's settings; your next message is the question |
| `/research [OPTIONS] QUESTION` | start at once, with the same options as `misaka research` |
| `/research status [RUN_ID]` | the state of the latest run in this folder, or the one you name |
| `/research stop [RUN_ID]` | stop a run; it writes a partial report |
| `/research resume [RUN_ID] [ANSWER]` | resume a paused run, answering its questions if it asked any |
| `/research tell [RUN_ID] [--to NODE_ID] MESSAGE` | say something to a running node's Last Order, the root's by default |
| `/research limits [RUN_ID] --sister-parallel 2 …` | change a running run's limits |
| `/research resume RUN_ID --here` | continue the run in this window (this Last Order will not remember the run's earlier conversation) |
| `/research help` | the full usage |

### The team

| Command | What it does |
|---|---|
| `/sister` | list the roles, or pick one |
| `/sister 10032` | talk to Sister 10032. In the panel she opens in a pane of her own. |
| `/sister last-order` | a new conversation with Last Order |
| `/create [ID]` | create a Sister (in Last Order's window) |
| `/remove [ID]` | remove a Sister (in Last Order's window) |
| `/board` | the task board of this project |
| `/agents` | sub-agent types and running sub-agents (in a Sister's window) |
| `/mcp` | MCP servers and their tools, when any are configured; `/mcp auth SERVER` signs in to one |

### Models

| Command | What it does |
|---|---|
| `/login [PROVIDER]` | sign in to a provider |
| `/logout` | sign out |
| `/model [NAME]` | choose a model: Enter uses it for this conversation, Ctrl+S makes it this agent's default |
| `/scoped-models` | choose the models Ctrl+P cycles through |
| `/llama` | manage the models of a local llama.cpp server ([Models](../guide/models.md#a-model-on-your-own-machine)) |
| `/thinking [--default] [LEVEL]` | choose the thinking level; `--default` keeps it |

### This conversation

| Command | What it does |
|---|---|
| `/new` | start a new conversation (`--carry` keeps the summaries of this one) |
| `/resume` | reopen a past conversation. To resume research, use `/research resume`. |
| `/name [NAME]` | name this conversation |
| `/session` | facts and figures about this conversation |
| `/tree` | move between branches of this conversation |
| `/fork` | branch off from an earlier message of yours |
| `/clone` | duplicate this conversation as it stands |
| `/compact [INSTRUCTIONS]` | summarise the conversation so far to free up context |
| `/clear` | erase this conversation's history |
| `/copy` | copy the last reply |
| `/export [PATH]` | save the conversation as HTML, or as `.jsonl` |
| `/import PATH.jsonl` | load a saved conversation |
| `/share` | share the conversation as a secret GitHub gist (needs the `gh` tool) |

### The project

| Command | What it does |
|---|---|
| `/commit [MESSAGE]` | commit the project to git, after you see the files and confirm |
| `/trust` | remember whether this project's own settings may be used |

### Reading

Available in Last Order's window. `/read` takes the same subcommands as `misaka read`, and needs
the engine to be built ([reading guide](../guide/reading.md)).

| Command | What it does |
|---|---|
| `/read open SOURCE... --question Q` | start a reading run in this folder |
| `/read act RUN_ID ACTION --node NODE_ID` | run one action, as in the terminal |
| `/read fork`, `compare`, `reread`, `graph` | the same operations as `misaka read` |
| `/read say RUN_ID MESSAGE` | say something to that run's leader |
| `/read stop` | stop the engine this window started; the run stays on disk |
| `/read --help` | the full usage |

### Skills

| Command | What it does |
|---|---|
| `/skill` | list skills; `/skill NAME [INSTRUCTION]` uses one now |
| `/NAME` | a skill, or a saved set of skills, as a command of its own (when no other command has the name) |
| `/learn [TEXT]` | turn files, notes or the work just done into a reusable skill |
| `/refine` | look over the finished task for improvements to skills |
| `/skill-mode [off\|forbid\|ask\|allow]` | whether agents may write skills |
| `/reload-skills` | rescan the skill folders |

### Everything else

| Command | What it does |
|---|---|
| `/settings` | the settings menu |
| `/hotkeys` | every keyboard shortcut |
| `/reload` | reload keybindings, extensions, skills, prompts and themes |
| `/debug` | write the screen and the conversation to a file, for a bug report |
| `/lcm-settings [on\|off\|status\|check]` | turn automatic recall from earlier conversations on or off |
| `/quit` | leave |

A message that starts with `//` is not treated as a command; it goes to the agent as you typed it.

## Shell commands from chat

| You type | What happens |
|---|---|
| `!ls sources/` | runs the command and shows its output to you and to the agent |
| `!!ls sources/` | runs it and shows the output to you only |

## Keys in the chat box

| Key | What it does |
|---|---|
| Enter | send. While the agent is working, your message steers her current turn. |
| Shift+Enter, Ctrl+J, or `\` then Enter | new line |
| Alt+Enter | queue a message for when the current turn ends; Alt+Up takes queued messages back |
| Esc | stop the reply |
| Esc, Esc (empty box) | open the conversation tree |
| Ctrl+C | stop the reply, or clear the box. Twice quickly: quit. |
| Ctrl+D (empty box) | quit |
| Ctrl+Z | suspend to the background (`fg` brings it back) |
| Up (empty box) | your earlier messages |
| `/` | the command menu |
| `@` | suggest file names |
| Tab | complete a path, or accept the suggestion |
| Ctrl+V | paste an image from the clipboard |
| Shift+Tab | cycle the thinking level |
| Ctrl+P, Shift+Ctrl+P | next, previous model |
| Ctrl+L | the model selector |
| Ctrl+O | show or hide tool output |
| Ctrl+T | show or hide the agent's thinking |
| Ctrl+G | write the message in your own editor |
| Ctrl+X | copy the selection, or the last reply |

`/hotkeys` shows the complete list, including your own changes. To change a key, edit
`~/.misaka/keybindings.json` and run `/reload`.
