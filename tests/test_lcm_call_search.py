"""A search reads tool calls too: what an agent did is in them, not in ``content``.

The real searches that found nothing were for a word in the agent's own earlier calls, such as
`dl.ndl.go.jp` or a script name, and most asked for ``role: assistant``, which held only its prose.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import calls, storage, tools
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.vendor import store as vendor_store

T0 = 1789460410000
USAGE = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    ce.close_all()
    yield
    ce.close_all()


def context(tmp_path, name="project"):
    project = tmp_path / name
    project.mkdir(exist_ok=True)
    manager = SessionManager.create(str(project), str(tmp_path / "sessions"))
    manager.appendCustomEntry("lcm-project", {"workspace": str(project.resolve())})
    return storage.context(SimpleNamespace(cwd=str(project), sessionManager=manager, model=None), str(project), "test")


def say(ctx, text, at=0):
    ctx.sessionManager.appendMessage({"role": "user", "content": text, "timestamp": T0 + at})


def did(ctx, *tool_calls, text="", at=1):
    content = [{"type": "text", "text": text}] if text else []
    for index, (name, arguments) in enumerate(tool_calls):
        content.append({"type": "toolCall", "id": f"call{at}_{index}", "name": name, "arguments": arguments})
    ctx.sessionManager.appendMessage({"role": "assistant", "content": content, "api": "anthropic-messages",
                                      "provider": "x", "model": "x", "usage": USAGE, "stopReason": "stop",
                                      "timestamp": T0 + at})
    for index, (name, _) in enumerate(tool_calls):
        ctx.sessionManager.appendMessage({"role": "toolResult", "toolCallId": f"call{at}_{index}", "toolName": name,
                                          "content": [{"type": "text", "text": "done"}], "isError": False,
                                          "timestamp": T0 + at + 1})


def grep(ctx, query, **args):
    ce.sync(ctx)
    answer = tools._answer("lcm_grep", {"query": query, **args}, ce.snapshot(ctx), ctx)
    return json.loads(answer[answer.index("{"):answer.rindex("}") + 1])["results"]


@pytest.fixture
def ctx(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "find the catalogue")
    did(ctx, ("bash", {"command": "python3 annasfetch.py --host dl.ndl.go.jp"}), text="I will fetch it.")
    return ctx


def test_a_command_is_found_with_or_without_the_role(ctx):
    assert grep(ctx, "annasfetch")
    assert grep(ctx, "annasfetch", role="assistant")


def test_a_hit_names_the_tool_and_shows_the_match(ctx):
    hit = grep(ctx, "annasfetch")[0]
    assert hit["role"] == "assistant"
    assert hit["snippet"].startswith("bash: ") and "annasfetch.py" in hit["snippet"]


def test_a_term_is_matched_as_written(ctx):
    assert grep(ctx, "dl.ndl.go.jp")
    assert grep(ctx, "annasfetch.py")
    assert not grep(ctx, "dl.ndl.go.co")


def test_case_does_not_matter(ctx):
    assert grep(ctx, "ANNASFETCH")


def test_the_tool_s_own_name_is_found_but_a_field_name_is_not(ctx):
    assert grep(ctx, "bash", role="assistant")
    assert not grep(ctx, "command", role="assistant")


def test_cjk_arguments_are_found(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "start")
    did(ctx, ("write", {"path": "notes.md", "content": "沈家本調查日本監獄情形清單"}))
    assert grep(ctx, "監獄情形", role="assistant")
    assert grep(ctx, '"沈家本調查日本監獄情形清單"')


def test_every_term_must_be_in_one_call(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "start")
    did(ctx, ("bash", {"command": "alpha.py"}), ("bash", {"command": "omega.py"}))
    assert grep(ctx, "alpha.py")
    assert not grep(ctx, "alpha.py omega.py")


def test_a_row_found_both_ways_comes_back_once(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "start")
    did(ctx, ("bash", {"command": "zebrafetch"}), text="running zebrafetch now")
    assert len(grep(ctx, "zebrafetch", role="assistant")) == 1


def test_another_role_does_not_get_calls(ctx):
    assert not grep(ctx, "annasfetch", role="tool")
    assert not grep(ctx, "annasfetch", role="user")


def test_the_session_filter_holds(tmp_path):
    first = context(tmp_path)
    say(first, "start")
    did(first, ("bash", {"command": "firstonly.py"}))
    ce.sync(first)
    second = storage.context(SimpleNamespace(
        cwd=first.cwd, sessionManager=SessionManager.create(first.cwd, str(tmp_path / "other")), model=None),
        first.cwd, "test")
    second.sessionManager.appendCustomEntry("lcm-project", {"workspace": first.cwd})
    say(second, "another session")
    assert not grep(second, "firstonly.py", session_scope="current")
    assert grep(second, "firstonly.py", session_scope="all")


def test_the_source_filter_holds(ctx):
    assert not grep(ctx, "annasfetch", source="nobody")


def test_a_newer_call_outranks_an_older_message(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "lookforme in an old message", at=0)
    say(ctx, "another", at=5)
    did(ctx, ("bash", {"command": "lookforme.sh"}), at=10)
    hits = grep(ctx, "lookforme", limit=1)
    assert len(hits) == 1 and hits[0]["role"] == "assistant"


@pytest.mark.parametrize("sort", ["relevance", "hybrid", "recency"])
def test_every_sort_finds_a_call(ctx, sort):
    assert grep(ctx, "annasfetch", sort=sort)


def opened(ctx, store_id):
    answer = tools._answer("lcm_expand", {"store_id": store_id}, ce.snapshot(ctx), ctx)
    return json.loads(answer[answer.index("{"):answer.rindex("}") + 1])


def test_the_whole_call_opens_from_a_hit(ctx):
    row = opened(ctx, grep(ctx, "annasfetch")[0]["store_id"])
    assert "annasfetch.py" in row["tool_calls"][0]["function"]["arguments"]
    assert "I will fetch it." in row["content"]


def test_a_row_with_no_call_opens_as_it_did(ctx):
    row = opened(ctx, grep(ctx, "catalogue", role="user")[0]["store_id"])
    assert "tool_calls" not in row and "tool_calls_omitted" not in row


def test_a_call_too_large_for_one_result_leaves_the_row_readable(ctx, monkeypatch):
    store_id = grep(ctx, "annasfetch")[0]["store_id"]
    monkeypatch.setattr(tools, "_tokens", lambda name, payload: 10 ** 9 if "tool_calls" in payload else 0)
    row = opened(ctx, store_id)
    assert row["tool_calls_omitted"] is True and "tool_calls" not in row and "I will fetch it." in row["content"]


def test_the_newest_calls_come_first_and_the_scan_stops_at_the_limit(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "start")
    for at in range(2, 12, 2):
        did(ctx, ("bash", {"command": f"step.py {at}"}), at=at)
    hits = grep(ctx, "step.py", limit=2)
    assert [hit["snippet"].rsplit(" ", 1)[-1] for hit in hits] == ["10", "8"]


def test_the_store_merges_by_time_when_the_limit_is_tight(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "lookforme in an old message", at=0)
    say(ctx, "another", at=5)
    did(ctx, ("bash", {"command": "lookforme.sh"}), at=10)
    ce.sync(ctx)
    hits = ce.bound_engine(ctx)._store.search("lookforme", limit=1)
    assert [hit["role"] for hit in hits] == ["assistant"]


def test_a_scan_takes_the_newest_calls_and_stops_at_the_limit(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "start")
    for at in range(2, 12, 2):
        did(ctx, ("bash", {"command": f"step.py {at}"}), at=at)
    ce.sync(ctx)
    hits = calls._hits(ce.bound_engine(ctx)._store, "step.py", session_id=None, limit=2, source=None,
                       conversation_id=None, role=None, time_from=None, time_to=None)
    assert [hit["snippet"].rsplit(" ", 1)[-1] for hit in hits] == ["10", "8"]


def test_parallel_searches_each_find_every_call(tmp_path):
    ctx = context(tmp_path)
    say(ctx, "start")
    for at in range(2, 42, 2):
        did(ctx, ("bash", {"command": f"step.py {at}"}), at=at)
    ce.sync(ctx)
    store = ce.bound_engine(ctx)._store
    session = store._conn.execute("SELECT session_id FROM messages LIMIT 1").fetchone()[0]

    def search(_):
        return len(calls._hits(store, "step.py", session_id=session, limit=50, source=None, conversation_id=None,
                               role=None, time_from=None, time_to=None))

    with ThreadPoolExecutor(8) as pool:
        assert set(pool.map(search, range(64))) == {20}


def test_a_malformed_call_is_skipped():
    assert calls._snippet([{"function": {"name": "bash", "arguments": "{not json"}}], ["bash"]) is not None
    assert calls._snippet(["nonsense", {"function": "x"}], ["bash"]) is None
    assert calls._snippet("nonsense", ["bash"]) is None


def test_a_query_composed_as_fts5_syntax_stays_with_the_store(ctx):
    ce.sync(ctx)
    store = ce.bound_engine(ctx)._store
    assert not store.search("annasfetch", allow_operators=True)
    assert store.search("annasfetch")


def test_operators_are_not_terms():
    assert calls._terms('alpha OR "two words" NOT beta') == ["alpha", "two words", "beta"]


def test_installing_twice_wraps_the_search_once():
    calls.install()
    first = vendor_store.MessageStore.search
    calls.install()
    assert vendor_store.MessageStore.search is first


def test_a_failed_call_search_keeps_what_the_store_found(ctx, monkeypatch):
    def broken(*args, **kwargs):
        raise calls.sqlite3.OperationalError("locked")

    say(ctx, "plainword in a message", at=20)
    monkeypatch.setattr(calls, "_hits", broken)
    assert grep(ctx, "plainword")


def test_no_table_is_added(ctx):
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    names = {row[0] for row in built._store._conn.execute("SELECT name FROM sqlite_master")}
    assert not {name for name in names if "misaka" in name or "call" in name}
