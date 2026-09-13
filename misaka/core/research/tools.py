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
from misaka.config import CFG
from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.research import ledger, runs


def _text(value):
    return {"content": [{"type": "text", "text": value}], "details": {}}


def register(harn):
    class Params(BaseModel):
        view: Literal["run", "workspace", "issues", "findings"] = Field(
            "run", description="Information to return; workspace gives current states and exact card/artifact paths."
        )
        run_id: str | None = Field(
            None, description="Optional research-run ID; omit to inspect the latest run of the current workspace."
        )
        offset: int = Field(0, ge=0, description="Findings page offset.")
        limit: int = Field(50, ge=1, le=200, description="Findings per page; a next offset is returned when more exist.")

    def inspect(params, workspace, db):
        path = Path(db).expanduser().resolve()
        if not path.is_file():
            return _text("No research run exists.")
        # Own the reader in this thread. Inspection neither creates/migrates a board nor
        # shares the driver's writer; closing also releases the short WAL read snapshot.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
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
            return _text(f"Research workspace snapshot at {stamp}; query again for current state.\n"
                         + untrusted("research-workspace", workspace_index.render(tree)))
        if params.view == "run":
            value = runs.summary(con, run["id"])
            return _text("\n".join(f"{k}: {v}" for k, v in value.items()))
        if params.view == "issues":
            rows = con.execute(
                "SELECT * FROM research_issues WHERE run_id=? ORDER BY status,priority DESC",
                (run["id"],),
            ).fetchall()
            return _text("\n".join(
                f"{r['id']} [{r['status']}/{r['kind']}] {r['question']} — {r['rationale']}"
                for r in rows) or "(empty)")
        rows = ledger.findings(con, run["id"], limit=params.limit + 1, offset=params.offset)
        result = _text("\n".join(
            f"{r['id']} [{r['claim_type']}] {r['text']}" for r in rows[:params.limit]) or "(empty)")
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
            "Inspect live research state, the workspace index with exact file paths, issues, and findings. "
            "Each call reads current records; saved workspace-index files are historical snapshots. This tool is read-only."
        ),
        parameters=Params.model_json_schema(), execute=execute,
        promptSnippet="Inspect live research state, material paths, and declarations",
        promptGuidelines=[
            ("For research state or file locations, use misaka_research_view(view='workspace', run_id=...) instead of a saved index. "
             "Use returned paths verbatim; node IDs and artifact titles are not filenames. "
             "Saved workspace indexes and context packets are snapshots, not live state; query again for newer artifacts.")
        ],
    ))


SESSION_KINDS = {"foreground", "dm", "card", "child", "bare", "beast"}


def activate(spec):
    return register
