"""Read-only Research inspection tools available to agents."""
import os
from typing import Literal

from pydantic import BaseModel, Field

from misaka.core.extensions.types import ToolDefinition
from misaka.research import ledger, runs


def _text(value):
    return {"content": [{"type": "text", "text": value}], "details": {}}


def register(harn, con_factory):
    class Params(BaseModel):
        view: Literal["run", "issues", "findings"] = Field(
            "run", description="Information to return."
        )
        run_id: str | None = Field(
            None, description="Optional research-run ID; omit to inspect the latest run of the current workspace."
        )

    async def execute(_tool_call_id, raw, _signal, _on_update, ctx):
        params = raw if isinstance(raw, Params) else Params(**(raw or {}))
        workspace = os.path.realpath(getattr(ctx, "cwd", None) or os.getcwd())
        # con_factory is memoized at the registration site (last_order/research.py) and the
        # same connection backs the /research commands — it must not be closed here.
        con = con_factory()
        run = (runs.get(con, params.run_id) if params.run_id
               else runs.latest(con, workspace=workspace))
        if not run:
            return _text("No research run exists.")
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
        rows = ledger.findings(con, run["id"], limit=50)
        return _text("\n".join(
            f"{r['id']} [{r['claim_type']}] {r['text']}" for r in rows) or "(empty)")

    harn.registerTool(ToolDefinition(
        name="misaka_research_view", label="View research run",
        description=(
            "Inspect a research run, its open issues, and findings. "
            "This tool is read-only."
        ),
        parameters=Params.model_json_schema(), execute=execute,
        promptSnippet="Inspect the current research run and its evidence ledger.",
    ))
