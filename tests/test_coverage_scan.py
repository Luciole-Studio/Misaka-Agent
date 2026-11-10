"""coverage_scan cuts a question into facets and scans each alone, all together and in pairs.

2026-09-27: the scan sent the planner's whole sentence as one search, which OpenAlex reads as
every word required; "German Confederation Prussia Austria rivalry nineteenth century state
formation" found no work at all, and Last Order wrote into her plan that the field was thinly
indexed."""
import urllib.parse

import pytest

from misaka.extensions import coverage


@pytest.fixture(autouse=True)
def _no_real_pacing(monkeypatch):
    """Heavy searches wait a real second each without a key; the scans here only need the order."""
    monkeypatch.setattr(coverage, "_heavy_interval", lambda: 0.0)


class _OpenAlex:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, path, **params):
        self.calls.append((path, params))
        if self.fail and self.fail(params):
            raise RuntimeError("HTTP 503: Anonymous search is paused")
        query = params["filter"].split(":", 1)[1]
        count = 1000 if " AND " not in query else 7
        groups = [{"key_display_name": f"{params.get('group_by')}-a", "count": count // 2}] if params.get("group_by") else []
        return {"meta": {"count": count}, "group_by": groups}


FACETS = [("naval balance", ["Royal Navy", "French navy", "marine impériale"]),
          ("economic war", ["Continental System", "blocus continental", "Kontinentalsperre"]),
          ("period", ["Napoleonic Wars"])]


def test_terms_are_alternatives_within_a_facet_and_facets_are_required_together(monkeypatch):
    api = _OpenAlex()
    monkeypatch.setattr(coverage, "_get", api)
    text = coverage.scan(FACETS)
    queries = {params["filter"].split(":", 1)[1] for _path, params in api.calls}
    naval = '("Royal Navy" OR "French navy" OR "marine impériale")'
    war = '("Continental System" OR "blocus continental" OR "Kontinentalsperre")'
    assert naval in queries and war in queries and '("Napoleonic Wars")' in queries
    assert f'{naval} AND {war} AND ("Napoleonic Wars")' in queries
    assert f"{naval} AND {war}" in queries                     # a pair
    groups = {params.get("group_by") for _path, params in api.calls}
    assert {"primary_topic.subfield.id", "primary_topic.id", "language", "type", "keywords.id"} <= groups
    assert "## Facet 1 · naval balance — 1,000 works" in text and "## All facets together — 7 works" in text
    assert "naval balance × economic war: 7" in text and "Languages (1): language-a 500" in text
    assert "a small count is not a gap" in text


def test_a_term_is_a_phrase_and_cannot_break_the_query():
    assert coverage._term("War AND Peace: \"x\", (y)") == '"War and Peace x y"'
    assert coverage._term("大陆封锁") == '"大陆封锁"'
    assert coverage._term("  ") == ""
    assert coverage._facet_query(["Royal Navy", "Royal Navy", ""]) == '("Royal Navy")'


def test_one_failed_request_is_reported_and_the_rest_still_read(monkeypatch):
    monkeypatch.setattr(coverage, "_get", _OpenAlex(fail=lambda params: params.get("group_by") == "language"))
    text = coverage.scan(FACETS[:1])
    assert "Languages: (request failed: unknown, not zero)" in text and "Subfields (1): primary_topic.subfield.id-a 500" in text
    assert "Failed requests: RuntimeError: HTTP 503" in text


def test_a_scan_that_reaches_nothing_says_why_and_how_to_get_through(monkeypatch):
    monkeypatch.delenv(coverage.KEY_ENV, raising=False)
    monkeypatch.setattr(coverage, "_get", _OpenAlex(fail=lambda params: True))
    with pytest.raises(RuntimeError, match="Anonymous search is paused.*OPENALEX_API_KEY"):
        coverage.scan(FACETS)


def test_the_api_key_goes_with_every_request(monkeypatch):
    sent = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return b'{"meta": {"count": 1}, "group_by": []}'

    def urlopen(request, timeout):
        sent.append(urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query))
        return _Response()

    monkeypatch.setenv(coverage.KEY_ENV, "k-123")
    monkeypatch.setattr(coverage.urllib.request, "urlopen", urlopen)
    coverage.scan(FACETS[:1])
    assert sent and all(query["api_key"] == ["k-123"] for query in sent)


async def test_the_tool_takes_facets_and_turns_an_outage_into_a_note(monkeypatch):
    tools = []
    harness = type("H", (), {"registerTool": lambda self, tool: tools.append(tool), "on": lambda self, *a: None})()
    coverage.register(harness)
    [tool] = tools
    schema = tool.parameters
    assert "facets" in schema["properties"] and "query" not in schema["properties"]
    monkeypatch.setattr(coverage, "_get", _OpenAlex(fail=lambda params: True))
    result = await tool.execute("c", {"facets": [{"name": "navy", "terms": ["Royal Navy"]}]}, None, None, None)
    assert "OpenAlex is unreachable" in result["content"][0]["text"]


def test_an_empty_facet_says_to_split_its_terms(monkeypatch):
    class _Empty(_OpenAlex):
        def __call__(self, path, **params):
            data = super().__call__(path, **params)
            return {"meta": {"count": 0}, "group_by": []} if "Royal Navy French Navy" in params["filter"] else data
    monkeypatch.setattr(coverage, "_get", _Empty())
    text = coverage.scan([("naval gap", ["Royal Navy French Navy"])])
    assert "## Facet 1 · naval gap — 0 works" in text and "split it into one term per concept" in text


class _Grouped(_OpenAlex):
    """Answers a grouped request the way OpenAlex does: at most ``per_page`` groups, most works first,
    and a ``meta`` naming the page it filled (``'per_page': 200, 'groups_count': 169``, 2026-11-10).
    ``most`` is the largest page this server fills, whatever is asked for."""

    def __init__(self, groups, most=None):
        super().__init__()
        self.groups, self.most = groups, most

    def __call__(self, path, **params):
        data = super().__call__(path, **params)
        found = self.groups.get(params.get("group_by"))
        if found is None:
            return data
        page = min(params["per_page"], self.most or params["per_page"])
        return {"meta": {**data["meta"], "per_page": page, "groups_count": len(found[:page])}, "group_by": found[:page]}


def test_every_group_openalex_returns_is_listed(monkeypatch):
    """2026-11-10: a scan asked for eight groups and listed eight without a word. A question about
    Meiji Japan has works in 55 languages; the list stopped at the eighth."""
    languages = [{"key": f"https://openalex.org/languages/l{n}", "key_display_name": f"Lang{n}", "count": 60 - n}
                 for n in range(55)]
    api = _Grouped({"language": languages})
    monkeypatch.setattr(coverage, "_get", api)
    text = coverage.scan(FACETS[:1])
    assert {params["per_page"] for _path, params in api.calls if params.get("group_by")} == {coverage.GROUPS}
    line = next(line for line in text.splitlines() if line.startswith("Languages"))
    assert line.startswith("Languages (55): Lang0 60 · Lang1 59 · ") and line.endswith(" · Lang54 6")
    assert line.count(" · ") == 54 and "200+" not in text


def test_a_full_answer_is_marked_as_where_openalex_stopped(monkeypatch):
    topics = [{"key": f"https://openalex.org/T{n}", "key_display_name": f"Topic{n}", "count": 900 - n}
              for n in range(350)]
    monkeypatch.setattr(coverage, "_get", _Grouped({"primary_topic.id": topics}))
    text = coverage.scan(FACETS[:1])
    line = next(line for line in text.splitlines() if line.startswith("Topics"))
    assert line.startswith("Topics (200+, the 200 with most works): Topic0 900 · ")
    assert line.endswith(" · Topic199 701") and "Topic200 " not in text
    assert "Subfields (1): " in text                 # a short answer carries its own count instead


def test_a_smaller_page_than_asked_for_is_marked_as_full_too(monkeypatch):
    """OpenAlex's paging reference calls a ``per_page`` above 100 deprecated. A hundred groups held
    against the two hundred asked for would be listed as all there were."""
    topics = [{"key": f"https://openalex.org/T{n}", "key_display_name": f"Topic{n}", "count": 900 - n}
              for n in range(350)]
    languages = [{"key": f"https://openalex.org/languages/l{n}", "key_display_name": f"Lang{n}", "count": 60 - n}
                 for n in range(55)]
    monkeypatch.setattr(coverage, "_get", _Grouped({"primary_topic.id": topics, "language": languages}, most=100))
    text = coverage.scan(FACETS[:1])
    line = next(line for line in text.splitlines() if line.startswith("Topics"))
    assert line.startswith("Topics (100+, the 100 with most works): Topic0 900 · ")
    assert line.endswith(" · Topic99 801") and "Topic100 " not in text
    assert "Languages (55): Lang0 60 · " in text     # under the smaller page, so all of them


def test_a_group_openalex_has_no_name_for_keeps_its_key(monkeypatch):
    """OpenAlex names no language for some keys; the list read "None 12"."""
    languages = [{"key": "https://openalex.org/languages/ja", "key_display_name": "Japanese", "count": 60691},
                 {"key": "https://openalex.org/languages/jp", "key_display_name": None, "count": 12},
                 {"key": "https://openalex.org/languages/中文", "key_display_name": None, "count": 4},
                 {"key": None, "key_display_name": None, "count": 1}]
    monkeypatch.setattr(coverage, "_get", _Grouped({"language": languages}))
    text = coverage.scan(FACETS[:1])
    assert "Languages (4): Japanese 60,691 · jp 12 · 中文 4 · unknown 1" in text and "None" not in text


def test_a_group_with_no_works_reads_none(monkeypatch):
    monkeypatch.setattr(coverage, "_get", _Grouped({"type": []}))
    assert "\nTypes: none\n" in coverage.scan(FACETS[:1])


def test_heavy_searches_are_spaced_and_a_rate_limit_is_asked_again(monkeypatch):
    """2026-09-27: without an API key OpenAlex answers a search of more than five boolean operators
    once a second; eight parallel workers lost nearly every pair to HTTP 429 and Last Order read the
    gaps as a literature that does not exist."""
    import itertools
    import threading
    import time

    monkeypatch.setattr(coverage, "_heavy_interval", lambda: 0.05)
    lock, heavy_times, refused = threading.Lock(), [], set()

    def api(path, **params):
        query = params["filter"].split(":", 1)[1]
        if query.count(" OR ") + query.count(" AND ") > coverage.HEAVY_OPERATORS:
            with lock:
                heavy_times.append(time.monotonic())
                if query not in refused:              # the first ask of every heavy search is refused once
                    refused.add(query)
                    raise coverage.RateLimited("HTTP 429: more than 5 operators")
        groups = [{"key_display_name": "g", "count": 3}] if params.get("group_by") else []
        return {"meta": {"count": 3}, "group_by": groups}

    monkeypatch.setattr(coverage, "_get", api)
    facets = [(f"f{i}", [f"a{i}", f"b{i}", f"c{i}"]) for i in range(4)]
    text = coverage.scan(facets)
    assert "request failed" not in text and "Failed requests" not in text
    gaps = [b - a for a, b in itertools.pairwise(heavy_times)]
    assert gaps and min(gaps) >= 0.03              # unpaced they would come back to back


def test_the_scans_rules_promise_no_work_and_no_verdict():
    """The scan's rules travel with the tool (moved here from the core's prompt governance tests:
    the core names no extension's tool)."""
    from misaka.core.wiring import ToolCollector

    collector = ToolCollector()
    collector.on = lambda name, handler: None     # the maps come through resources_discover
    coverage.register(collector)
    scan = " ".join(next(d for d in collector.tools if d.name == "coverage_scan").promptGuidelines)
    assert "two or three" not in scan
    assert "not measures of relevance, quality, or completeness" in scan
    assert "failed scan does not establish a research gap" in scan
