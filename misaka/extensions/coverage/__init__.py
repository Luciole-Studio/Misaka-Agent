"""The coverage check for research designs: the ``coverage-maps`` skill (maps of fields,
facets, kinds of question and traditions, read on demand) and ``coverage_scan`` (where a
question actually lives in the literature: OpenAlex, facet by facet), so a plan is checked
against more than the planner's memory. Every role, including the resident headless sessions
used by Research.

The maps ship here rather than as a skill to install: the scan is step 0 of the maps'
procedure, and a separately installed copy could go missing while the scan stayed loaded,
leaving half a check. A field's own maps are a skill of their own in the project's
``skills/``; a ``coverage-maps`` copy left in a skill layer by an older install is listed
beside this one and answers to the unprefixed name."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.typebox_helpers import typeboxSchema

# `builtin:coverage`, like every bundled extension: hidden from the startup list, and disabled with
# `-builtin:coverage` in the `extensions` setting.
BUILTIN = True
SESSION_KINDS = {"foreground", "dm", "card", "bare"}
API = "https://api.openalex.org"
LIMIT = 8
MAX_FACETS = 6
MAX_TERMS = 16
SKILLS = os.path.join(os.path.dirname(__file__), "skills")
# What each scan is grouped by: where a literature lives (subfield, topic), which literatures
# the index holds (language), and what kind of publication carries it (type).
FACET_GROUPS = (("Subfields", "primary_topic.subfield.id"), ("Topics", "primary_topic.id"),
                ("Languages", "language"), ("Types", "type"))
ALL_GROUPS = (*FACET_GROUPS, ("Keywords", "keywords.keyword"))
KEY_ENV = "OPENALEX_API_KEY"
# OpenAlex holds a search with more than five boolean operators to one request a second per client,
# five with an API key. A scan's pairs and its "all together" are such searches (2026-09-27: eight
# workers sent them at once and lost nearly every pair to HTTP 429).
HEAVY_OPERATORS = 5
RETRIES = 2


class RateLimited(RuntimeError):
    """OpenAlex answered HTTP 429: ask again after a pause."""


def _heavy_interval():
    return 0.2 if os.environ.get(KEY_ENV) else 1.0


class _Pacer:
    """Spaces requests to one per ``interval`` seconds across the scan's threads."""

    def __init__(self, interval):
        self.interval, self._lock, self._next = interval, threading.Lock(), 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


def _get(path, **params):
    # The one client string this install sends, rather than a version literal that stopped
    # tracking the package three releases ago: OpenAlex reads the UA to tell clients apart,
    # and every other outbound request in the repo already identifies itself this way.
    if os.environ.get(KEY_ENV):
        params["api_key"] = os.environ[KEY_ENV]
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": f"{getPiUserAgent()} coverage-scan"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # OpenAlex says why in the body ("Anonymous search is paused ... use a free API key");
        # the status line alone read as an outage.
        try:
            detail = json.loads(error.read() or b"{}").get("message") or ""
        except (ValueError, AttributeError):
            detail = ""
        raise (RateLimited if error.code == 429 else RuntimeError)(
            f"HTTP {error.code}" + (f": {detail}" if detail else "")) from error


def _term(text):
    """One alternative, matched as a phrase. Quotes, brackets and the filter syntax's commas and
    colons are dropped; an upper-case AND/OR/NOT inside a term would become an operator. Every
    term is quoted: an unquoted "Continental System" is two words anywhere in the record, and a
    CJK term without quotes is its characters in any order."""
    words = re.sub(r'[\"()\[\]{}:;,*?~\\]', " ", str(text)).split()
    words = [word.lower() if word in {"AND", "OR", "NOT"} else word for word in words]
    return f'"{" ".join(words)}"' if words else ""


def _facet_query(terms):
    quoted = list(dict.fromkeys(term for term in map(_term, terms) if term))
    return "(" + " OR ".join(quoted) + ")" if quoted else ""


def _works(query, group=None):
    params = {"filter": f"title_and_abstract.search:{query}"}
    if group:
        params.update(group_by=group, per_page=LIMIT)
    else:
        params.update(per_page=1, select="id")
    return _get("/works", **params)


def scan(facets):
    """``facets``: ``[(name, terms)]``. Each facet alone (any of its terms), all of them together
    (one term of every facet), and every pair of them, grouped by where the literature lives, the
    languages it is in and the kinds of publication that carry it."""
    facets = [(name, _facet_query(terms)) for name, terms in facets]
    facets = [(name, query) for name, query in facets if query]
    if not facets:
        return "No usable term: give each facet at least one word or phrase."
    together = " AND ".join(query for _, query in facets)
    jobs = {("facet", i, group): (query, group) for i, (_, query) in enumerate(facets) for _, group in FACET_GROUPS}
    if len(facets) > 1:
        jobs |= {("all", 0, group): (together, group) for _, group in ALL_GROUPS}
    if len(facets) > 2:
        jobs |= {("pair", (i, j), None): (f"{facets[i][1]} AND {facets[j][1]}", None)
                 for i, j in combinations(range(len(facets)), 2)}

    pacer = _Pacer(_heavy_interval())

    def fetch(job):
        query, group = job
        heavy = len(re.findall(r"\b(?:AND|OR|NOT)\b", query)) > HEAVY_OPERATORS
        for attempt in range(RETRIES + 1):
            if heavy:
                pacer.wait()
            try:
                return _works(query, group), None
            except RateLimited as error:
                if attempt == RETRIES:
                    return None, f"{type(error).__name__}: {error}"
                time.sleep(pacer.interval)
            except Exception as error:  # noqa: BLE001 - one failed group is reported, not fatal
                return None, f"{type(error).__name__}: {error}"
        return None, "unreachable"

    # Up to 44 list requests. Light ones go in parallel; heavy ones wait their turn on the pacer.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(jobs, pool.map(fetch, jobs.values()), strict=True))
    failures = sorted({error for _, error in results.values() if error})
    if all(error for _, error in results.values()):
        hint = "" if os.environ.get(KEY_ENV) else f" A free OpenAlex API key in {KEY_ENV} (the home's .env) avoids anonymous limits."
        raise RuntimeError("; ".join(failures) + hint)

    def section(title, kind, index, groups, query):
        found = [results[(kind, index, group)][0] for _, group in groups]
        count = next((data["meta"]["count"] for data in found if data), None)
        lines = [f"## {title} — " + (f"{count:,} works" if count is not None else "not scanned"), f"Query: {query}"]
        if count == 0 and kind == "facet":
            # Each term is an exact phrase: "Royal Navy French Navy" is one rare phrase, not two
            # navies, and a zero read as "the literature is thin" (2026-09-27).
            lines.append("No work has any of these exact phrases: a term that names two things matches only as "
                         "that whole phrase -- split it into one term per concept and scan again before reading "
                         "this as a gap.")
        for (label, _), data in zip(groups, found, strict=True):
            if data is None:
                lines.append(f"{label}: (request failed: unknown, not zero)")
                continue
            items = [f"{g['key_display_name']} {g['count']:,}" for g in data["group_by"][:LIMIT]]
            lines.append(f"{label}: " + (" · ".join(items) if items else "none"))
        return lines

    lines = [("OpenAlex, titles and abstracts. A facet counts works matching any of its terms; "
              "\"all facets\" needs one term of every facet."), ""]
    for i, (name, query) in enumerate(facets):
        lines += section(f"Facet {i + 1} · {name}", "facet", i, FACET_GROUPS, query) + [""]
    if len(facets) > 1:
        lines += section("All facets together", "all", 0, ALL_GROUPS, together) + [""]
    if len(facets) > 2:
        lines.append("## Pairs")
        for (kind, pair, group), (data, _error) in results.items():
            if kind == "pair":
                i, j = pair
                lines.append(f"{facets[i][0]} × {facets[j][0]}: "
                             + (f"{data['meta']['count']:,}" if data else "(request failed: unknown, not zero)"))
        lines.append("")
    if failures:
        lines += ["Failed requests: " + "; ".join(failures), ""]
    lines.append("Counts are a radar, not a verdict. A facet with a literature of its own but little overlap with the "
                 "others is a dimension the combination hides. Languages and types show which literatures and kinds "
                 "of publication the index holds: books without abstracts match on their titles only, and archives "
                 "and much non-English scholarship are thin here, so a small count is not a gap.")
    return "\n".join(lines)


def register(harn):
    from pydantic import BaseModel, Field

    from misaka.core.extensions.types import ToolDefinition

    class Facet(BaseModel):
        model_config = {"extra": "forbid"}
        name: str = Field(min_length=1, description="What this facet stands for, in a few words.")
        terms: list[str] = Field(min_length=1, max_length=MAX_TERMS, description=(
            "Alternatives, any of which counts: synonyms, narrower terms, variant spellings and names, and the "
            "terms other literatures use in their own languages (a literature in another language names things its "
            "own way). One concept per term, a word or a short name, matched as that exact phrase: two concepts "
            "are two terms, and one term naming both matches almost nothing."))

    class ScanParams(BaseModel):
        model_config = {"extra": "forbid"}
        facets: list[Facet] = Field(min_length=1, max_length=MAX_FACETS, description=(
            f"The question cut into its concepts -- its object, actors, places, period, processes, sources, "
            f"method -- one facet each, at most {MAX_FACETS} per scan (more concepts take a second scan). Each is "
            f"scanned alone, all together, and in pairs, so a dimension with a literature of its own shows even "
            f"where the combination has none."))

    async def execute(tool_call_id, raw, signal, on_update, ctx):
        params = raw if isinstance(raw, ScanParams) else ScanParams(**(raw or {}))
        try:
            # Dozens of urllib calls at up to 25s each: never on the event loop
            # (the same rule documents.py states for its corpus calls).
            text = await asyncio.to_thread(scan, [(facet.name, facet.terms) for facet in params.facets])
        except Exception as error:  # noqa: BLE001 - the radar is optional; planning goes on without it
            text = f"OpenAlex is unreachable ({type(error).__name__}: {error}); plan from the coverage maps alone."
        return {"content": [{"type": "text", "text": text}], "details": {}}

    harn.registerTool(ToolDefinition(
        name="coverage_scan", label="Scan literature coverage",
        description="Show where a question lives in the literature (OpenAlex titles and abstracts): each facet of "
                    "the question alone, all together and in pairs, grouped by subfield, topic, language and "
                    "publication type, to help check a research design for overlooked fields.",
        parameters=typeboxSchema(ScanParams), execute=execute,
        promptSnippet="See which fields of the literature discuss a question",
        promptGuidelines=[("Use coverage_scan when checking a research design for overlooked fields: cut the "
                          "question into facets and give each its alternative terms, in the languages its "
                          "literatures use. Counts are discovery signals, not measures of relevance, quality, "
                          "or completeness; sparse coverage or a failed scan does not establish a research gap.")]))

    async def discover_resources(event, ctx):
        return {"skillPaths": [SKILLS]}

    harn.on("resources_discover", discover_resources)


def activate(spec):
    return register
