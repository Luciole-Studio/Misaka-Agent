#!/bin/sh
# Run one complete reading-action chain over the two public texts in examples/reading.
#
#   scripts/run_reading_demo.sh [WORKSPACE]
#
# WORKSPACE is a scratch folder (default: a new temporary directory). The materials are
# downloaded there, the run is written to WORKSPACE/reading/<run-id>/, and nothing is
# copied back into the repository. The script prints every command it runs, the run ID,
# and the node files the engine actually wrote.
#
# Prerequisites: Node.js >= 22.19.0, the reading engine built once
#   npm --prefix packages/reading install && npm --prefix packages/reading run build
# and a text model reachable through MISAKA's settings (or --model below). If MISAKA is
# not on PATH, set MISAKA_READING_BIN to the command that starts it (for example
# "uv run --project /path/to/Misaka-Agent misaka"); the default tries uv, then misaka.
#
# MISAKA_READING_RUNTIME points at an already-built dist/cli.js; the source checkout
# finds its own. MISAKA_READING_NODE points at a node binary that is not on PATH.

set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WORKSPACE=${1:-}
if [ -z "$WORKSPACE" ]; then
  WORKSPACE=$(mktemp -d "${TMPDIR:-/tmp}/misaka-reading-demo.XXXXXX")
fi
mkdir -p "$WORKSPACE"
WORKSPACE=$(CDPATH= cd -- "$WORKSPACE" && pwd)

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python 3 is needed to read the --json output." >&2
  exit 1
fi

if [ -n "${MISAKA_READING_BIN:-}" ]; then
  # shellcheck disable=SC2086 # the override is a command line, by design
  run_misaka() { $MISAKA_READING_BIN "$@"; }
elif [ -x "$REPO_ROOT/.venv/Scripts/misaka.exe" ]; then
  run_misaka() { "$REPO_ROOT/.venv/Scripts/misaka.exe" "$@"; }
elif [ -x "$REPO_ROOT/.venv/bin/misaka" ]; then
  run_misaka() { "$REPO_ROOT/.venv/bin/misaka" "$@"; }
elif command -v uv >/dev/null 2>&1; then
  run_misaka() { uv run --project "$REPO_ROOT" misaka "$@"; }
else
  run_misaka() { misaka "$@"; }
fi

read_json() {
  # read_json FIELD : print one top-level field of the JSON on stdin. `-X utf8` is not
  # decoration: on Windows a piped stdin is otherwise decoded with the locale codec, and a
  # run id or path with Chinese characters in it comes apart before json can parse it.
  "$PYTHON" -I -X utf8 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

LUXUN='https://zh.wikisource.org/wiki/%E5%A8%9C%E6%8B%89%E8%B5%B0%E5%BE%8C%E6%80%8E%E6%A8%A3'
IBSEN='https://www.gutenberg.org/files/2542/2542-0.txt'
QUESTION='个人出走与经济条件如何共同构成自由问题'

say() { printf '\n== %s\n' "$1"; }
show() { printf '   $ %s\n' "$*"; }

say "workspace: $WORKSPACE"

# 1. Open: index/download both texts and write the run. No model is called here; N0 is
#    the reader's own question.
show "misaka read open <luxun> <ibsen> --question '...' --intent '...' --workspace '$WORKSPACE' --json"
OPEN=$(run_misaka read open "$LUXUN" "$IBSEN" \
  --question "$QUESTION" \
  --intent "把两篇文本放在一起读，最后回到最初的问题。" \
  --workspace "$WORKSPACE" --json)
RUN=$(printf '%s' "$OPEN" | read_json runId)
ROOT_NODE=$(printf '%s' "$OPEN" | read_json currentNodeId)
RUN_DIR=$(printf '%s' "$OPEN" | read_json runDirectory)
printf '   run: %s\n   entry node: %s\n   directory: %s\n' "$RUN" "$ROOT_NODE" "$RUN_DIR"

# 2. Reconstruct: rebuild the lecture's question and its turns in the entry node.
show "misaka read act $RUN reconstruct --node $ROOT_NODE --focus '论证的转折' --workspace '$WORKSPACE' --json"
ACT=$(run_misaka read act "$RUN" reconstruct --node "$ROOT_NODE" \
  --focus "论证的转折" --workspace "$WORKSPACE" --json)
printf '   wrote: %s\n' "$(printf '%s' "$ACT" | read_json bodyPath)"

# 3. Fork: two lenses, run in parallel as independent reading lines.
show "misaka read fork $RUN --node $ROOT_NODE --lens '主体性与自我理解' --lens '经济条件与社会关系' --workspace '$WORKSPACE' --json"
FORK=$(run_misaka read fork "$RUN" --node "$ROOT_NODE" \
  --lens "主体性与自我理解" --lens "经济条件与社会关系" \
  --workspace "$WORKSPACE" --json)
FORK_IDS=$(printf '%s' "$FORK" | "$PYTHON" -I -X utf8 -c 'import json, sys; print(" ".join(json.load(sys.stdin)["nodeIds"]))')
printf '   lines: %s\n' "$FORK_IDS"

# 4. Compare: one node with every chosen line as a parent.
# shellcheck disable=SC2086 # FORK_IDS is a list of ids
show "misaka read compare $RUN $FORK_IDS --question '...' --workspace '$WORKSPACE' --json"
CMP=$(run_misaka read compare "$RUN" $FORK_IDS \
  --question "两条路线给出的自由是怎样的，它们在哪里分岔？" \
  --workspace "$WORKSPACE" --json)
CMP_NODE=$(printf '%s' "$CMP" | read_json nodeId)
printf '   wrote: %s\n' "$(printf '%s' "$CMP" | read_json bodyPath)"

# 5. Reread: the entry node's material, read again with the new understanding.
show "misaka read reread $RUN --node $ROOT_NODE --using $CMP_NODE --focus '结尾' --workspace '$WORKSPACE' --json"
RE=$(run_misaka read reread "$RUN" --node "$ROOT_NODE" --using "$CMP_NODE" \
  --focus "结尾" --workspace "$WORKSPACE" --json)
printf '   wrote: %s\n' "$(printf '%s' "$RE" | read_json bodyPath)"

# 6. Graph: the map and every node body path, read back from disk.
say "graph (read back from $RUN_DIR/graph.json)"
show "misaka read graph $RUN --workspace '$WORKSPACE' --json"
GRAPH=$(run_misaka read graph "$RUN" --workspace "$WORKSPACE" --json)
# The JSON goes in as an argument: with `python -` the program arrives on stdin, so the
# graph cannot also come that way. A quoted heredoc keeps the shell out of the Python.
"$PYTHON" -I -X utf8 - "$GRAPH" <<'PY'
import json, os, sys
graph = json.loads(sys.argv[1])
run_dir = graph["runDirectory"]
print("graph:", graph["graphMarkdownPath"])
print("map:  ", graph["readingPath"])
for node in graph["nodes"]:
    lens = f" [{node['lens']}]" if node.get("lens") else ""
    actions = ", ".join(node.get("actions") or []) or "unread"
    print(f"  {node['id']}{lens}  {actions}  {os.path.join(run_dir, node['bodyPath'])}")
PY

# 7. Resume: talk with the leader, then reopen the run in a new process and talk again.
#    Both turns use the leader session saved under sessions/. The message is followed by
#    end of input rather than /stop: an explicit /stop means "stop now" and would cancel the
#    turn this script is waiting for.
say "chat: one leader turn, then end of input"
show "misaka read chat $RUN   <<< '用两三句话说明这次阅读的两条线'"
printf '%s\n' "用两三句话说明这次阅读的两条线分别读到了什么。" \
  | run_misaka read chat "$RUN" --workspace "$WORKSPACE"

say "resume: a new process reopens the run and its leader session"
show "misaka read graph $RUN --workspace '$WORKSPACE' --json   (read-only)"
AGAIN=$(run_misaka read graph "$RUN" --workspace "$WORKSPACE" --json)
printf '%s\n' "$AGAIN" | "$PYTHON" -I -X utf8 -c '
import json, sys
before, after = json.loads(sys.argv[1]), json.load(sys.stdin)
old = [(n["id"], n["bodyPath"]) for n in before["nodes"]]
new = [(n["id"], n["bodyPath"]) for n in after["nodes"]]
if old != new:
    raise SystemExit(f"graph changed after reopening it: {old} != {new}")
print(f"   {len(new)} nodes, unchanged: " + ", ".join(node_id for node_id, _ in new))
' "$GRAPH"

show "session files recorded in $RUN_DIR/graph.json"
"$PYTHON" -I -X utf8 -c '
import json, os, sys
graph_path = sys.argv[1]
with open(graph_path, encoding="utf-8-sig") as handle:
    graph = json.load(handle)
if not graph["sessions"]:
    raise SystemExit("no sessions recorded: the leader conversation was not saved")
for record in graph["sessions"]:
    where = record.get("nodeId") or record.get("lineId") or "-"
    path = record["sessionFile"]
    saved = os.path.exists(path)
    print("   %-8s %-6s %s  saved=%s" % (record["role"], where, path, saved))
    if not saved:
        raise SystemExit("session file missing: %s" % path)
' "$RUN_DIR/graph.json"

show "misaka read chat $RUN   <<< '刚才两条线，哪一条更接近你的判断？'"
printf '%s\n' "刚才那两条线，哪一条更接近你的判断？" \
  | run_misaka read chat "$RUN" --workspace "$WORKSPACE"

say "done"
printf 'run:    %s\nfolder: %s\n' "$RUN" "$RUN_DIR"
printf 'Open %s and %s to read what was actually written.\n' \
  "$RUN_DIR/reading.md" "$RUN_DIR/nodes/"
