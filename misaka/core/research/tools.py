"""Live, read-only research navigation for Last Order and Sisters."""
import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from misaka import workspace as workspace_index
from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.config import CFG
from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform import tasks as task_store
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.research import ledger, runs, views
from misaka.core.tools.truncate import TruncationOptions, formatSize, truncateHead

# One tool result is one prompt turn for the smallest window in play (272k); the whole index of a
# mature project renders to megabytes (2026-09-24: 1.85 MB, 25k lines, a Sister at 1.19M tokens).
WORKSPACE_VIEW_MAX_BYTES = 200 * 1024


def _text(value):
    return {"content": [{"type": "text", "text": value}], "details": {}}


def register(harn):
    class Params(BaseModel):
        view: Literal["run", "graph", "workspace", "issues", "findings"] = Field(
            "run", description=("Information to return: graph gives every node, how it was reached, the decisions "
                                "and options, and the relations; workspace gives current states and exact "
                                "card/artifact paths; issues gives every review issue and what Last Order did with it.")
        )
        run_id: str | None = Field(
            None, description="Optional research-run ID; omit to inspect the latest run of the current workspace."
        )
        node: str | None = Field(None, description="issues only: one node's issues instead of the whole run's.")
        offset: int = Field(0, ge=0, description="Findings page offset.")
        limit: int = Field(50, ge=1, le=200, description="Findings per page; a next offset is returned when more exist.")

    def inspect(params, workspace, db):
        path = Path(db).expanduser().resolve()
        if not path.is_file():
            return _text("No research run exists.")
        # Own the reader in this thread. Inspection neither creates/migrates a board nor
        # shares the driver's writer; closing also releases the short WAL read snapshot.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = task_store.Row
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='research_runs' AND type='table'").fetchone():
                return _text("No research run exists.")
            return read_view(con, params, workspace)

    def read_view(con, params, workspace):
        run = (runs.get(con, params.run_id) if params.run_id
               else runs.latest(con, workspace=workspace))
        # The board is one file for the whole machine, so a run_id handed in by the caller is not
        # by itself proof the run belongs here: without this an id seen in another project reads
        # out that project's question, issues, and findings. Same answer as "no such run", so the
        # tool does not confirm the id exists elsewhere either.
        if run and os.path.realpath(run["workspace"]) != workspace:
            run = None
        if not run:
            return _text("No research run exists.")
        if params.view == "workspace":
            tree = workspace_index.outline(con, workspace=workspace, run_id=run["id"], research_store=runs)
            stamp = datetime.now(UTC).isoformat(timespec="seconds")
            truncation = truncateHead(workspace_index.render(tree),
                                       TruncationOptions(maxLines=2**31 - 1, maxBytes=WORKSPACE_VIEW_MAX_BYTES))
            text = truncation.content
            if truncation.truncated:
                text += (f"\n\n[{formatSize(WORKSPACE_VIEW_MAX_BYTES)} limit reached: {truncation.outputLines} of "
                         f"{truncation.totalLines} index lines shown. Narrow the question instead of re-reading: "
                         "doc_list / doc_find for materials, find / grep under a card's folder for its outputs; "
                         "the run, issues and findings views stay small.]")
            result = _text(f"Research workspace snapshot at {stamp}; query again for current state.\n"
                           + untrusted("research-workspace", text))
            result["details"] = {"truncated": truncation.truncated, "total_lines": truncation.totalLines}
            return result
        if params.view == "run":
            value = runs.summary(con, run["id"])
            return _text("\n".join(f"{k}: {v}" for k, v in value.items()))
        if params.view == "graph":
            return _text(untrusted("research-graph", views.render_graph(views.snapshot(con, run))))
        if params.view == "issues":
            rows = sorted(runs.issues(con, run["id"], node_id=params.node),
                          key=lambda r: (r["branch_id"] or "", r["round"], r["origin"], -r["priority"]))
            return _text(untrusted("research-issues", "\n".join(
                f"{r['id']} node {r['branch_id']} round {r['round']} [{r['origin']}/{r['kind']}] "
                f"→ {r['disposition'] or 'undisposed'}" + (f" ({runs.destination(r)})" if runs.destination(r) else "")
                + f": {r['question']} — {r['rationale']}" + (f" | Last Order: {r['reason']}" if r["reason"] else "")
                for r in rows) or "(empty)"))
        rows = ledger.findings(con, run["id"], limit=params.limit + 1, offset=params.offset)
        # A finding is a Sister's words about her sources: data, fenced as the other views are.
        listed = "\n".join(f"{r['id']} [{r['claim_type']}] {r['text']}" for r in rows[:params.limit])
        result = _text(untrusted("research-findings", listed) if listed else "(empty)")
        next_offset = params.offset + params.limit if len(rows) > params.limit else None
        result["details"] = {"offset": params.offset, "next_offset": next_offset}
        if next_offset is not None:
            result["content"][0]["text"] += f"\nMore findings: use offset={next_offset}."
        return result

    async def execute(_tool_call_id, raw, _signal, _on_update, ctx):
        params = raw if isinstance(raw, Params) else Params(**(raw or {}))
        workspace = os.path.realpath(getattr(ctx, "cwd", None) or os.getcwd())
        return await asyncio.to_thread(inspect, params, workspace, CFG["db"])

    harn.registerTool(ToolDefinition(
        name="misaka_research_view", label="View research run",
        description=(
            "Inspect live research state: the research graph, the workspace index with exact file paths, review issues "
            "with their dispositions, and findings. Each call reads current records; saved workspace-index and graph "
            "files are snapshots. This tool is read-only."
        ),
        parameters=typeboxSchema(Params), execute=execute,
        promptSnippet="Inspect live research state, material paths, and declarations",
        promptGuidelines=[
            ("For research state or file locations, use misaka_research_view(view='workspace', run_id=...) instead of a saved index. "
             "Use returned paths verbatim; node IDs and artifact titles are not filenames. "
             "Saved workspace indexes and context packets are snapshots, not live state; query again for newer artifacts.")
        ],
    ))


    class Retry(BaseModel):
        node: str = Field(min_length=1, description="The id of the failed node to run again.")
        run_id: str | None = Field(None, description="Optional research-run ID; omit for the latest run of this workspace.")

    def retry(params, workspace, session_file, call_id):
        with closing(task_store.connect(os.path.expanduser(CFG["db"]))) as con:
            run = runs.get(con, params.run_id) if params.run_id else runs.latest(con, workspace=workspace)
            if run is None:
                return _text("No research run exists.")
            attempt = runs.retry_node(con, run, params.node, session_file=session_file, tool_call_id=call_id)
            when = ("its level's driver starts it again as soon as a slot is free" if run["status"] in runs.ACTIVE
                    else f"`/research resume {run['id']}` starts it")
            return _text(f"Node {params.node} is queued again (retry {attempt} of {runs.RETRY_LIMIT}): its failed cards went "
                         f"back to their Sisters and it picks up where it was; {when}.")

    async def execute_retry(tool_call_id, raw, _signal, _on_update, ctx):
        params = raw if isinstance(raw, Retry) else Retry(**(raw or {}))
        workspace = os.path.realpath(getattr(ctx, "cwd", None) or os.getcwd())
        session_file = getattr(getattr(ctx, "sessionManager", None), "sessionFile", None)
        return await asyncio.to_thread(retry, params, workspace, session_file, tool_call_id)

    retry_description = ("Run a failed research node again -- its failed cards go back to their Sisters and it picks up "
                         "where it was -- without waiting for the level to end and the run to be resumed")
    harn.registerTool(ToolDefinition(
        name="misaka_research_retry", label="Retry a failed research node", description=retry_description,
        parameters=typeboxSchema(Retry), execute=execute_retry, promptSnippet=retry_description,
        promptGuidelines=[("When a research node fails -- a provider refused its request, a Sister card failed, its process "
                           "died -- and the cause was passing, misaka_research_retry(node=...) runs it again at once; a "
                           "node that keeps failing the same way is left for the user.")],
    ))


SESSION_KINDS = {"foreground", "dm", "card", "child", "bare", "beast"}


def activate(spec):
    return register
