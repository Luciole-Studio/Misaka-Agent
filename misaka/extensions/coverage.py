"""coverage_scan: where does a question actually live in the literature? OpenAlex, grouped by
subfield and topic, so a plan is checked against the field's real distribution rather than
the planner's memory. Every role, including the bare one-shot sessions research planning runs in."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

SESSION_KINDS = {"foreground", "dm", "card", "bare"}
API = "https://api.openalex.org"
LIMIT = 12


def _get(path, **params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "misaka/0.5 (research coverage scan)"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def scan(query, limit=LIMIT):
    """Counts of matching works per subfield and per topic, plus topics whose own description matches."""
    by_subfield = _get("/works", search=query, group_by="primary_topic.subfield.id", per_page=limit)
    by_topic = _get("/works", search=query, group_by="primary_topic.id", per_page=limit)
    named = _get("/topics", search=query, per_page=6)
    lines = [f"OpenAlex: {by_subfield['meta']['count']:,} works match \"{query}\".", "", "Subfields (works):"]
    lines += [f"  {g['key_display_name']} ({g['count']})" for g in by_subfield["group_by"][:limit]] or ["  none"]
    lines += ["", "Topics (works):"]
    lines += [f"  {g['key_display_name']} ({g['count']})" for g in by_topic["group_by"][:limit]] or ["  none"]
    if named["results"]:
        lines += ["", "Topics whose description matches the query:"]
        lines += [f"  {t['display_name']} — {t['subfield']['display_name']} / {t['field']['display_name']} / {t['domain']['display_name']}"
                  for t in named["results"]]
    lines += ["", "Counts are a radar, not a verdict: a field with few works may be the one this question needs."]
    return "\n".join(lines)


def register(harn):
    from pydantic import BaseModel, Field

    from misaka.core.extensions.types import ToolDefinition

    class ScanParams(BaseModel):
        model_config = {"extra": "forbid"}
        query: str = Field(description="The question or a phrase from it, in English (OpenAlex indexes titles and "
                                       "abstracts; try a second phrasing when the first returns little).")

    async def execute(tool_call_id, raw, signal, on_update, ctx):
        params = raw if isinstance(raw, ScanParams) else ScanParams(**(raw or {}))
        try:
            text = scan(params.query)
        except Exception as error:  # noqa: BLE001 - the radar is optional; planning goes on without it
            text = f"OpenAlex is unreachable ({type(error).__name__}: {error}); plan from the coverage maps alone."
        return {"content": [{"type": "text", "text": text}], "details": {}}

    harn.registerTool(ToolDefinition(
        name="coverage_scan", label="Scan literature coverage",
        description="Show which subfields and topics of the literature actually discuss a question (OpenAlex counts), "
                    "so a research design can be checked for fields it forgot.",
        parameters=ScanParams.model_json_schema(), execute=execute,
        promptSnippet="See which fields of the literature discuss a question",
        promptGuidelines=["Scan a question in two or three phrasings before dividing it into tasks; a neighbouring "
                          "field with many works is a dimension the plan may be missing."]))


def activate(spec):
    return register


if __name__ == "__main__":      # self-check: one live scan (skipped without network)
    try:
        out = scan("gentry tax resistance Ming Qing", limit=5)
    except OSError as error:
        print(f"coverage self-check skipped: {error}")
    else:
        assert "Subfields (works):" in out and "Topics (works):" in out, out
        print(out)
        print("coverage self-check OK")
