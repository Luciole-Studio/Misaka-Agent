"""coverage_scan: where does a question actually live in the literature? OpenAlex, grouped by
subfield and topic, so a plan is checked against the field's real distribution rather than
the planner's memory. Every role, including the bare one-shot sessions research planning runs in."""
from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request

from misaka.ai.utils.user_agent import get_misaka_user_agent

SESSION_KINDS = {"foreground", "dm", "card", "bare"}
API = "https://api.openalex.org"
LIMIT = 12


def _get(path, **params):
    # The one client string this install sends, rather than a version literal that stopped
    # tracking the package three releases ago: OpenAlex reads the UA to tell clients apart,
    # and every other outbound request in the repo already identifies itself this way.
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": f"{get_misaka_user_agent()} coverage-scan"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def _works_filter(query):
    """Match titles and abstracts, not full text: a full-text ``search`` matches every paper that
    happens to contain the query's ordinary words, and the subfield counts then say nothing about
    who discusses the question. Commas and colons are OpenAlex filter syntax, so they become spaces."""
    return "title_and_abstract.search:" + " ".join(str(query).replace(",", " ").replace(":", " ").split())


def scan(query, limit=LIMIT):
    """Counts of matching works per subfield and per topic, plus topics whose own description matches."""
    by_subfield = _get("/works", filter=_works_filter(query), group_by="primary_topic.subfield.id", per_page=limit)
    by_topic = _get("/works", filter=_works_filter(query), group_by="primary_topic.id", per_page=limit)
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
        if not any("a" <= ch.lower() <= "z" for ch in params.query):
            # A CJK or other non-Latin phrase matches a handful of unrelated works at random,
            # and "0 works" then reads as a confirmed gap in the literature; the index is of
            # English titles and abstracts.
            return {"content": [{"type": "text", "text": (
                "OpenAlex indexes English titles and abstracts: a query without Latin letters "
                "cannot be matched. Rephrase the question in English and scan again.")}], "details": {}}
        try:
            # Three sequential urllib calls at up to 25s each: never on the event loop
            # (the same rule documents.py states for its corpus calls).
            text = await asyncio.to_thread(scan, params.query)
        except Exception as error:  # noqa: BLE001 - the radar is optional; planning goes on without it
            text = f"OpenAlex is unreachable ({type(error).__name__}: {error}); plan from the coverage maps alone."
        return {"content": [{"type": "text", "text": text}], "details": {}}

    harn.registerTool(ToolDefinition(
        name="coverage_scan", label="Scan literature coverage",
        description="Show OpenAlex counts of title/abstract matches grouped by subfield and topic, "
                    "plus matching topic descriptions, to help check a research design for overlooked fields.",
        parameters=ScanParams.model_json_schema(), execute=execute,
        promptSnippet="See which fields of the literature discuss a question",
        promptGuidelines=[("Use coverage_scan when checking a research design for overlooked fields. Rephrase weak "
                          "queries when useful. Counts are discovery signals, not measures of relevance, quality, "
                          "or completeness; sparse coverage or a failed scan does not establish a research gap.")]))


def activate(spec):
    return register
